"""Bounded task overlap with one rate gate and owner-thread-only mutable state."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import queue
import threading
import time

from teambition_readonly import (BatchBudgetExceeded, Client, CollectionError,
                                validate_binding)
from report_contract import task_updated_timestamp


class RequestGate:
    """Aggregate request admissions and 429 cooldown, never per-worker quotas."""

    def __init__(self, *, interval, clock=time.monotonic, sleep=time.sleep, cancel_event=None):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.external_cancel = cancel_event
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._next = self._cooldown = 0.0
        self.last_admission = None

    def cancelled(self):
        return self._cancelled.is_set() or (self.external_cancel is not None and self.external_cancel.is_set())

    def check_cancelled(self):
        if self.cancelled():
            raise CollectionError('Collection cancelled')

    def cancel(self):
        self._cancelled.set()

    def wait(self, delay, *, check=None):
        end = self.clock() + delay
        while True:
            self.check_cancelled()
            if check is not None:
                check()
            remaining = end - self.clock()
            if remaining <= 0:
                return
            amount = min(remaining, .1)
            if self.sleep is time.sleep:
                self._cancelled.wait(amount)
            else:
                self.sleep(amount)

    def acquire(self, *, check=None):
        while True:
            self.check_cancelled()
            if check is not None:
                check()
            with self._lock:
                now = self.clock()
                delay = max(self._next, self._cooldown) - now
                if delay <= 0:
                    self._next = now + self.interval
                    self.last_admission = now
                    return
            self.wait(delay, check=check)

    @property
    def next_allowed(self):
        with self._lock:
            return max(self._next, self._cooldown)

    def defer(self, delay):
        with self._lock:
            self._cooldown = max(self._cooldown, self.clock() + delay)


class _CapacityWait(Exception):
    pass


class _OwnerCalls:
    """Workers rendezvous with the existing coordinator; no cross-thread SQLite."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.gate = coordinator.request_gate
        self.owner = threading.get_ident()
        self.queue = queue.Queue()
        self.partial = {}
        self.waiting = []
        self.active_reservations = set()
        self.sequence = 0

    def _execute(self, kind, args):
        if kind == 'budget':
            name, positional, keyword = args
            result = getattr(self.coordinator.budget, name)(*positional, **keyword)
            if name in ('before_request', 'finish_response'):
                self.coordinator._progress('collecting')
            return result
        if kind == 'reserve':
            params, detail, minimum_bytes, minimum_records, preferred_bytes = args
            try:
                if self.active_reservations:
                    budget = self.coordinator.budget
                    remaining_rows = budget.policy.max_records - budget.counters['records']
                    remaining_bytes = budget.policy.max_response_bytes - budget.counters['response_bytes']
                    desired_rows = 1 if detail else params.get('pageSize', 100)
                    if (remaining_rows < min(desired_rows, budget.policy.max_records)
                            or remaining_bytes < min(preferred_bytes, budget.policy.max_response_bytes)):
                        raise _CapacityWait
                reservation = self.coordinator.budget.plan_request(params, detail=detail,
                                                                    minimum_bytes=minimum_bytes,
                                                                    minimum_records=minimum_records,
                                                                    preferred_bytes=preferred_bytes)
                # Plan and write-ahead charge atomically on the owner thread.
                self.coordinator.budget.before_request(reservation)
            except CollectionError as exc:
                if self.active_reservations and str(exc) in (
                        'Collection session response byte budget exhausted',
                        'Collection session record budget exhausted'):
                    raise _CapacityWait from None
                raise
            self.sequence += 1
            reservation['_concurrent_reservation'] = self.sequence
            self.active_reservations.add(self.sequence)
            self.coordinator._progress('collecting')
            return reservation
        if kind == 'end_request':
            self.active_reservations.discard(args[0].get('_concurrent_reservation'))
            return None
        if kind == 'counters':
            return dict(self.coordinator.budget.counters)
        if kind == 'partial':
            task_id, used = args
            self.partial[task_id] = used
            self.coordinator.budget.retained(self.coordinator._live_bytes() + sum(self.partial.values()))
            return None
        raise CollectionError('Invalid concurrent collection operation')

    def call(self, kind, *args):
        self.gate.check_cancelled()
        if threading.get_ident() == self.owner:
            return self._execute(kind, args)
        done, result = threading.Event(), []
        self.queue.put((kind, args, done, result))
        while not done.wait(.05):
            self.gate.check_cancelled()
        succeeded, value = result[0]
        if not succeeded:
            raise value
        return value

    def service(self, timeout=.01):
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            if not self.waiting:
                return
            item = self.waiting.pop(0)
        kind, args, done, result = item
        try:
            self.gate.check_cancelled()
            result.append((True, self._execute(kind, args)))
        except _CapacityWait:
            # Existing reads will settle reservations. Do not confuse temporary
            # in-flight occupancy with final session-budget exhaustion.
            self.waiting.append(item)
            return
        except BaseException as exc:
            result.append((False, exc))
        done.set()

    def drop_partial(self, task_id):
        self.partial.pop(task_id, None)
        self.coordinator.budget.retained(self.coordinator._live_bytes() + sum(self.partial.values()))


class _BudgetProxy:
    def __init__(self, owner):
        self.owner = owner

    def plan_request(self, params, *, detail=False, minimum_bytes=0, minimum_records=0, preferred_bytes=0):
        return self.owner.call('reserve', params, detail, minimum_bytes, minimum_records, preferred_bytes)

    def before_request(self, reservation):
        # The owner already durably charged the atomic reservation.
        return None

    def end_request(self, reservation):
        return self.owner.call('end_request', reservation)

    @property
    def counters(self):
        return self.owner.call('counters')

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.owner.call('budget', name, args, kwargs)


@dataclass
class _Lane:
    client: object = None
    tasks: int = 0


def _new_client(coordinator, owner):
    coordinator.budget.new_batch()
    limits = {key: getattr(coordinator.client, key) for key in
              ('interval', 'attempts', 'max_pages', 'max_requests', 'max_tasks',
               'max_records', 'max_seconds', 'max_response_bytes')}
    return Client(coordinator.client.tenant_id, transport=coordinator.client.transport if coordinator.client.injected_transport else None,
                  sleep=coordinator.client.sleep, clock=coordinator.client.clock,
                  credentials=lambda: coordinator.credentials,
                  shared_budget=_BudgetProxy(owner), request_gate=coordinator.request_gate,
                  mcp_adapter_factory=coordinator.client.mcp_adapter_factory,
                  mcp_envelope_validator=coordinator.client.mcp_envelope_validator,
                  archive_contract_validator=coordinator.client.archive_contract_validator,
                  **limits)


def _read_record(coordinator, owner, client, task_id, candidate):
    # This function never mutates the coordinator, checkpoint or shared counters.
    from collection_batches import _VersionChanged, _size
    detail = client.get('task.details', {'taskId': task_id, 'projectId': coordinator.project_id,
                                         'includeArchived': coordinator.include_archived}).get('result')
    if isinstance(detail, list) and len(detail) == 1:
        detail = detail[0]
    if coordinator._validate_task(detail, require_scope=False) != task_id:
        raise CollectionError('Detail identity or project mismatch')
    if (task_updated_timestamp(detail['updated']) != task_updated_timestamp(candidate['updated'])
            or detail['isArchived'] is not candidate['isArchived']):
        raise _VersionChanged
    record = {'task': detail, 'activities': []}
    used = _size(record)
    owner.call('partial', task_id, used)
    for activity in client.pages('task.activities', {'taskId': task_id, 'projectId': coordinator.project_id,
                                                       'pageSize': coordinator.page_size}):
        validate_binding(activity, task_id)
        used += _size(activity) + (1 if record['activities'] else 0)
        owner.call('partial', task_id, used)
        record['activities'].append(activity)
    observed_at = datetime.now(timezone.utc)
    for activity in record['activities']:
        try:
            instant = task_updated_timestamp(activity.get('createTime'))
        except (TypeError, ValueError):
            continue
        if instant > observed_at:
            raise CollectionError('Activity timestamp exceeds collection time')
    coordinator._validate_record(task_id, candidate, record)
    return record, used


def collect_tasks(coordinator):
    """Read only independent tasks concurrently; all opaque cursor chains stay serial."""
    from collection_batches import _VersionChanged
    owner = _OwnerCalls(coordinator)
    jobs = iter((key, candidate) for key, candidate in coordinator.candidates.items()
                if key not in coordinator.completed)
    futures = {}
    pool = ThreadPoolExecutor(max_workers=coordinator.client.concurrent_tasks,
                              thread_name_prefix='upcam-read')

    def submit(lane, task_id, candidate, attempt=0):
        if lane.client is None or lane.tasks >= coordinator.client.max_tasks:
            lane.client, lane.tasks = _new_client(coordinator, owner), 0
        lane.tasks += 1
        future = pool.submit(_read_record, coordinator, owner, lane.client, task_id, candidate)
        futures[future] = (lane, task_id, candidate, attempt)

    def next_job(lane):
        value = next(jobs, None)
        if value is not None:
            submit(lane, *value)

    try:
        for _ in range(coordinator.client.concurrent_tasks):
            next_job(_Lane())
        while futures:
            coordinator.request_gate.check_cancelled()
            owner.service()
            for future in list(futures):
                if not future.done():
                    continue
                lane, task_id, candidate, attempt = futures.pop(future)
                try:
                    record, used = future.result()
                except _VersionChanged:
                    owner.drop_partial(task_id)
                    next_job(lane)
                    continue
                except BatchBudgetExceeded:
                    owner.drop_partial(task_id)
                    if attempt:
                        raise CollectionError('Task exceeds a single collection batch') from None
                    lane.client, lane.tasks = _new_client(coordinator, owner), 0
                    submit(lane, task_id, candidate, attempt=1)
                    continue
                owner.partial.pop(task_id, None)
                coordinator._store('save_task', task_id, candidate, record)
                coordinator.completed[task_id] = {'candidate': candidate, 'record': record}
                coordinator.record_bytes += used
                coordinator.budget.retained(coordinator._live_bytes() + sum(owner.partial.values()))
                coordinator._progress('collecting')
                next_job(lane)
    except BaseException:
        # Cancel queued admissions before waiting for already in-flight bounded reads.
        coordinator.request_gate.cancel()
        for future in futures:
            future.cancel()
        raise
    finally:
        # Workers never touch the checkpoint. After cancellation, their pending owner
        # calls fail without executing; uncertain responses keep write-ahead charges.
        pool.shutdown(wait=True, cancel_futures=True)
