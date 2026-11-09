"""Bounded project traversal with complete-task checkpoints and shared limits.

Candidate equality detects observable source drift. It does not make cursors a
snapshot or imply that every activity changes the task's version timestamp.
``collection_stats.records`` and ``response_bytes`` are budget counters. Unknown
interrupted responses retain conservative reservations, so these counters may
exceed confirmed received rows/bytes; business activity counts use raw records.
"""

import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import time
import uuid

from collection_progress import report_collection
from report_contract import task_is_candidate, task_updated_timestamp
from teambition_readonly import (BatchBudgetExceeded, Client, CollectionError,
                                identifier, record_id, validate_binding)


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CollectionError("Invalid collection JSON") from None


def _size(value):
    return len(_json_bytes(value))


@dataclass(frozen=True)
class BatchPolicy:
    version: int = 1
    max_project_tasks: int = 10000
    max_requests: int = 50000
    max_records: int = 1000000
    max_response_bytes: int = 512 * 1024 * 1024
    max_retained_bytes: int = 128 * 1024 * 1024
    max_seconds: int = 7200
    checkpoint_ttl_seconds: int = 7200
    max_passes: int = 3

    def __post_init__(self):
        ceilings = {"version": 1, "max_project_tasks": 10000, "max_requests": 50000,
                    "max_records": 1000000, "max_response_bytes": 512 * 1024 * 1024,
                    "max_retained_bytes": 128 * 1024 * 1024, "max_seconds": 7200,
                    "checkpoint_ttl_seconds": 7200, "max_passes": 3}
        if any(type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= ceiling
               for key, ceiling in ceilings.items()):
            raise CollectionError("Invalid batch collection policy")


_COUNTERS = ("requests", "records", "response_bytes", "retained_bytes", "batches", "passes")


def _counters(value=None):
    if value is None:
        return {key: 0 for key in _COUNTERS}
    if (not isinstance(value, dict)
            or set(value) not in (set(_COUNTERS), set(_COUNTERS) | {"elapsed_seconds"})):
        raise CollectionError("Invalid collection checkpoint counters")
    output = dict(value)
    for key in _COUNTERS:
        amount = output.setdefault(key, 0)
        if type(amount) is not int or amount < 0:
            raise CollectionError("Invalid collection checkpoint counters")
    if "elapsed_seconds" in output:
        amount = output["elapsed_seconds"]
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            raise CollectionError("Invalid collection checkpoint counters")
    return output


class MemoryCheckpoint:
    """An explicit in-memory store; there is no plaintext filesystem fallback."""

    def __init__(self):
        self.context = None
        self.created_at = None
        self.counters = _counters()
        self.tasks = {}

    def start(self, context, *, now, ttl_seconds=7200):
        encoded = _json_bytes(context)
        resumed = (self.context == encoded and self.created_at is not None
                   and 0 <= now - self.created_at < ttl_seconds)
        if not resumed:
            self.clear()
            self.context, self.created_at = encoded, float(now)
        return {"created_at": self.created_at, "counters": dict(self.counters), "resumed": resumed}

    def load_task(self, task_id):
        return copy.deepcopy(self.tasks.get(task_id))

    def save_task(self, task_id, candidate, record):
        self.tasks[task_id] = copy.deepcopy({"candidate": candidate, "record": record})

    def save_counters(self, counters):
        self.counters = _counters(counters)

    def prune(self, task_ids):
        keep = set(task_ids)
        self.tasks = {key: value for key, value in self.tasks.items() if key in keep}

    def clear(self):
        self.context, self.created_at = None, None
        self.counters, self.tasks = _counters(), {}

    def close(self):
        pass


class SessionBudget:
    """Counters charge actual attempts and returned data across every batch."""

    def __init__(self, policy, counters, *, created_at, now, clock, save):
        if (type(created_at) not in (int, float) or not math.isfinite(created_at)
                or created_at > now or created_at < 0):
            raise CollectionError("Invalid collection checkpoint age")
        if not isinstance(counters, dict):
            raise CollectionError("Invalid collection checkpoint counters")
        self.policy, self.counters = policy, _counters(counters)
        self.clock, self.started = clock, clock()
        self.age, self.save = now - created_at, save
        self.check_time()
        for key, maximum in (("requests", policy.max_requests), ("records", policy.max_records),
                             ("response_bytes", policy.max_response_bytes),
                             ("retained_bytes", policy.max_retained_bytes)):
            if self.counters[key] > maximum:
                raise CollectionError("Collection session budget exhausted")
        if self.counters["passes"] >= policy.max_passes:
            raise CollectionError("Collection reconciliation limit exhausted")

    def check_time(self):
        if self.age + self.clock() - self.started >= self.policy.max_seconds:
            raise CollectionError("Collection session time budget exhausted")

    def before_wait(self, delay):
        self.check_time()
        if self.age + self.clock() - self.started + delay >= self.policy.max_seconds:
            raise CollectionError("Wait would exceed collection session time budget")

    def _add(self, key, amount, maximum, label):
        self.counters[key] += amount
        self.save(dict(self.counters))
        if self.counters[key] > maximum:
            raise CollectionError(f"Collection session {label} budget exhausted")

    def plan_request(self, params, *, detail=False):
        """Reserve a bounded response before an attempt can leave the process.

        An unknown interrupted read retains these charges. Complete decoded
        responses atomically replace them with actual bytes and returned rows.
        """
        self.check_time()
        if self.counters["requests"] >= self.policy.max_requests:
            raise CollectionError("Collection session request budget exhausted")
        remaining_rows = self.policy.max_records - self.counters["records"]
        remaining_bytes = self.policy.max_response_bytes - self.counters["response_bytes"]
        if remaining_rows <= 0:
            raise CollectionError("Collection session record budget exhausted")
        if remaining_bytes <= 1:
            raise CollectionError("Collection session response byte budget exhausted")
        effective_params = dict(params)
        requested_rows = 1 if detail else effective_params.get("pageSize", 100)
        if type(requested_rows) is not int or not 1 <= requested_rows <= 100:
            raise CollectionError("Invalid page size")
        reserved_rows = min(requested_rows, remaining_rows)
        if not detail:
            effective_params["pageSize"] = reserved_rows
        reserved_bytes = min(8 * 1024 * 1024 + 1, remaining_bytes)
        return {"params": effective_params, "records": reserved_rows,
                "response_bytes": reserved_bytes, "body_limit": reserved_bytes - 1}

    def before_request(self, reservation):
        self.check_time()
        if self.counters["requests"] >= self.policy.max_requests:
            raise CollectionError("Collection session request budget exhausted")
        if self.counters["records"] + reservation["records"] > self.policy.max_records:
            raise CollectionError("Collection session record budget exhausted")
        if self.counters["response_bytes"] + reservation["response_bytes"] > self.policy.max_response_bytes:
            raise CollectionError("Collection session response byte budget exhausted")
        self.counters["requests"] += 1
        self.counters["records"] += reservation["records"]
        self.counters["response_bytes"] += reservation["response_bytes"]
        self.save(dict(self.counters))

    def finish_response(self, reservation, response_bytes, records):
        settled = dict(self.counters)
        settled["response_bytes"] += response_bytes - reservation["response_bytes"]
        settled["records"] += records - reservation["records"]
        self.save(settled)
        self.counters = settled
        if records > reservation["records"]:
            raise CollectionError("Response row count exceeds requested page size")
        if settled["records"] > self.policy.max_records:
            raise CollectionError("Collection session record budget exhausted")
        if settled["response_bytes"] > self.policy.max_response_bytes:
            raise CollectionError("Collection session response byte budget exhausted")

    def add_response_bytes(self, amount):
        self._add("response_bytes", amount, self.policy.max_response_bytes, "response byte")

    def add_records(self, amount):
        self._add("records", amount, self.policy.max_records, "record")

    def retained(self, amount):
        self.counters["retained_bytes"] = amount
        self.save(dict(self.counters))
        if amount > self.policy.max_retained_bytes:
            raise CollectionError("Collection session retained JSON budget exhausted")
        self.check_time()

    def new_batch(self):
        self.check_time()
        self.counters["batches"] += 1
        self.save(dict(self.counters))

    def before_closing_scan(self):
        self.check_time()
        if self.counters["passes"] >= self.policy.max_passes:
            raise CollectionError("Collection reconciliation limit exhausted")
        self.counters["passes"] += 1
        self.save(dict(self.counters))


class _VersionChanged(Exception):
    pass


class _Coordinator:
    def __init__(self, client, project_id, include_archived, page_size, selection):
        self.client, self.project_id = client, identifier(project_id)
        if type(include_archived) is not bool:
            raise CollectionError("include_archived must be boolean")
        self.include_archived, self.page_size = include_archived, page_size
        self.selection = copy.deepcopy(selection)
        self.policy = client.batch_policy or BatchPolicy()
        if not isinstance(self.policy, BatchPolicy):
            raise CollectionError("Invalid batch collection policy")
        self.max_tasks = min(client.max_project_tasks, self.policy.max_project_tasks)
        self.store = client.checkpoint if client.checkpoint is not None else MemoryCheckpoint()
        self.worker, self.worker_tasks, self.budget = None, 0, None
        self.candidates, self.completed = {}, {}
        self.candidate_bytes, self.record_bytes = 0, 0
        self.project_tasks_listed = 0
        self.resumed_ids = set()
        self.initial_resumed = False
        self.credentials = None
        self.request_gate = None
        if client.concurrent_tasks > 1 or client.cancel_event is not None:
            from collection_concurrency import RequestGate
            self.request_gate = RequestGate(interval=client.interval, clock=client.clock,
                                            sleep=client.sleep, cancel_event=client.cancel_event)
            not_before = getattr(client, '_next_collection_request', 0.0)
            if client.last_request is not None:
                not_before = max(not_before, client.last_request + client.interval)
            self.request_gate.defer(max(0.0, not_before - client.clock()))

    def _progress(self, phase, *, total=None):
        counters = self.budget.counters if self.budget is not None else {}
        report_collection(phase, completed=len(self.completed),
                          total=len(self.candidates) if total is None else total,
                          requests=counters.get("requests", 0),
                          batches=counters.get("batches", 0), passes=counters.get("passes", 0))

    def _store(self, action, *args, **kwargs):
        try:
            return getattr(self.store, action)(*args, **kwargs)
        except Exception:
            raise CollectionError("Collection checkpoint is unavailable or invalid") from None

    def _start(self):
        try:
            pair = self.client.credentials()
            if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                    or any(not isinstance(item, str) or not item for item in pair)):
                raise ValueError
            self.credentials = tuple(pair)
        except Exception:
            raise CollectionError("Missing or invalid collection credentials") from None
        batch = {key: getattr(self.client, key) for key in
                 ("max_tasks", "max_requests", "max_records", "max_response_bytes",
                  "max_seconds", "max_pages", "attempts", "interval", "concurrent_tasks")}
        policy = asdict(self.policy)
        policy.update(max_project_tasks=self.max_tasks, batch=batch)
        context = {"policy": policy, "tenant_id": self.client.tenant_id,
                   "project_id": self.project_id, "include_archived": self.include_archived,
                   "selection": self.selection,
                   "credential_identity": hashlib.sha256(_json_bytes(self.credentials)).hexdigest()}
        now = time.time()
        checkpoint_started = self.client.clock()
        state = self._store("start", context, now=now,
                            ttl_seconds=self.policy.checkpoint_ttl_seconds)
        if (not isinstance(state, dict) or type(state.get("resumed")) is not bool
                or "created_at" not in state):
            raise CollectionError("Invalid collection checkpoint state")
        self.initial_resumed = state["resumed"]
        budget_now = now + (self.client.clock() - checkpoint_started)
        self.budget = SessionBudget(self.policy, state.get("counters"),
                                    created_at=state["created_at"], now=budget_now, clock=self.client.clock,
                                    save=lambda counters: self._store("save_counters", counters))
        if self.initial_resumed:
            # A previous process's monotonic instant cannot be compared locally.
            self.budget.before_wait(self.client.interval)
            self.client.sleep(self.client.interval)

    def _new_batch(self):
        last_request = self.worker.last_request if self.worker is not None else self.client.last_request
        self.budget.new_batch()
        limits = {key: getattr(self.client, key) for key in
                  ("interval", "attempts", "max_pages", "max_requests", "max_tasks",
                   "max_records", "max_seconds", "max_response_bytes")}
        self.worker = Client(self.client.tenant_id, transport=self.client.transport,
                             sleep=self.client.sleep, clock=self.client.clock,
                             credentials=lambda: self.credentials, shared_budget=self.budget,
                             request_gate=self.request_gate,
                             **limits)
        self.worker.last_request = last_request
        self.worker_tasks = 0

    def _validate_task(self, value, *, require_scope=True):
        identity = record_id(value)
        if value.get("projectId") != self.project_id or type(value.get("isArchived")) is not bool:
            raise CollectionError("Candidate identity or archive scope mismatch")
        if require_scope and value["isArchived"] and not self.include_archived:
            raise CollectionError("Candidate identity or archive scope mismatch")
        try:
            task_updated_timestamp(value.get("updated"))
        except (ValueError, TypeError):
            raise CollectionError("Invalid task updated timestamp") from None
        return identity

    def _validate_record(self, task_id, candidate, record):
        if (not isinstance(record, dict) or not isinstance(record.get("activities"), list)
                or self._validate_task(record.get("task")) != task_id):
            raise CollectionError("Invalid complete task checkpoint")
        detail = record["task"]
        if (task_updated_timestamp(detail["updated"]) != task_updated_timestamp(candidate["updated"])
                or detail["isArchived"] is not candidate["isArchived"]):
            raise CollectionError("Invalid complete task checkpoint version")
        events = {}
        for activity in record["activities"]:
            event_id = record_id(activity, task_fallback=False)
            validate_binding(activity, task_id)
            if event_id in events and events[event_id] != activity:
                raise CollectionError("Conflicting checkpoint activity identity")
            events[event_id] = activity

    def _live_bytes(self):
        return self.candidate_bytes + self.record_bytes

    def _scan(self):
        phase = "reconciling" if self.budget is not None and self.budget.counters.get("passes", 0) else "listing"
        self._progress(phase)
        params = {"pageSize": self.page_size, "includeArchived": str(self.include_archived).lower()}
        # Restart an interrupted local scan at its first page, with bounded retry.
        for attempt in range(2):
            if self.worker is None:
                self._new_batch()
            old_pages = self.worker.max_pages
            self.worker.max_pages = 100
            candidates, used, listed, listing_bytes = {}, 2, 0, 2
            try:
                for row in self.worker.pages(f"/api/v3/project/{self.project_id}/task/query", params,
                                             max_items=self.max_tasks):
                    task_id = self._validate_task(row)
                    listed += 1
                    # pages() retains its duplicate-detection rows until the
                    # iterator ends, including rows excluded from history reads.
                    listing_bytes += _size(row) + (1 if listed > 1 else 0)
                    self.budget.retained(self._live_bytes() + listing_bytes)
                    if not task_is_candidate(row["updated"], self.selection):
                        continue
                    used += _size(row) + (1 if candidates else 0)
                    candidates[task_id] = row
                    if len(candidates) % self.page_size == 0:
                        self._progress(phase, total=len(candidates))
                self._progress(phase, total=len(candidates))
                self.project_tasks_listed = listed
                return candidates, used
            except BatchBudgetExceeded:
                if attempt:
                    raise CollectionError("Project scan exceeds a single collection batch") from None
            finally:
                self.worker.max_pages = old_pages
                self.budget.retained(self._live_bytes())
            self._new_batch()
        raise CollectionError("Incomplete project scan")

    def _accept_candidates(self, candidates, candidate_bytes):
        self.candidates, self.candidate_bytes = candidates, candidate_bytes
        self.completed = {key: {"candidate": candidates[key], "record": value["record"]}
                          for key, value in self.completed.items()
                          if key in candidates and value["candidate"] == candidates[key]}
        self.record_bytes = sum(_size(value["record"]) for value in self.completed.values())
        self.budget.retained(self._live_bytes())
        self._store("prune", list(candidates))

    def _load_completed(self):
        self._progress("loading_cache")
        for task_id, candidate in self.candidates.items():
            if task_id in self.completed:
                continue
            value = self._store("load_task", task_id)
            if value is None:
                continue
            self.budget.retained(self._live_bytes() + _size(value))
            if (not isinstance(value, dict) or "candidate" not in value or "record" not in value
                    or self._validate_task(value["candidate"]) != task_id):
                raise CollectionError("Invalid complete task checkpoint")
            self._validate_record(task_id, value["candidate"], value["record"])
            if value["candidate"] == candidate:
                self.completed[task_id] = {"candidate": candidate, "record": value["record"]}
                self.record_bytes += _size(value["record"])
                if self.initial_resumed:
                    self.resumed_ids.add(task_id)
            self.budget.retained(self._live_bytes())
            self._progress("loading_cache")

    def _read_task(self, task_id, candidate):
        detail = self.worker.get("/api/v3/task/query", {"taskId": task_id}).get("result")
        if isinstance(detail, list) and len(detail) == 1:
            detail = detail[0]
        if self._validate_task(detail, require_scope=False) != task_id:
            raise CollectionError("Detail identity or project mismatch")
        if (task_updated_timestamp(detail["updated"]) != task_updated_timestamp(candidate["updated"])
                or detail["isArchived"] is not candidate["isArchived"]):
            raise _VersionChanged
        record = {"task": detail, "activities": []}
        used = _size(record)
        self.budget.retained(self._live_bytes() + used)
        for activity in self.worker.pages(f"/api/v3/task/{task_id}/activity/list",
                                          {"pageSize": self.page_size}):
            validate_binding(activity, task_id)
            used += _size(activity) + (1 if record["activities"] else 0)
            self.budget.retained(self._live_bytes() + used)
            record["activities"].append(activity)
        observed_at = datetime.now(timezone.utc)
        for activity in record["activities"]:
            try:
                instant = task_updated_timestamp(activity.get("createTime"))
            except (TypeError, ValueError):
                continue  # Preserve unknown timestamps for the pipeline's evidence checks.
            if instant > observed_at:
                raise CollectionError("Activity timestamp exceeds collection time")
        self._validate_record(task_id, candidate, record)
        self._store("save_task", task_id, candidate, record)
        self.completed[task_id] = {"candidate": candidate, "record": record}
        self.record_bytes += used
        self._progress("collecting")

    def _collect_tasks(self):
        if self.client.concurrent_tasks > 1:
            from collection_concurrency import collect_tasks
            self._progress("collecting")
            return collect_tasks(self)
        return self._collect_tasks_serial()

    def _collect_tasks_serial(self):
        self._progress("collecting")
        for task_id, candidate in self.candidates.items():
            if task_id in self.completed:
                continue
            for attempt in range(2):
                if self.worker is None or self.worker_tasks >= self.client.max_tasks:
                    self._new_batch()
                self.worker_tasks += 1
                try:
                    self._read_task(task_id, candidate)
                    break
                except _VersionChanged:
                    break  # The closing scan will supply candidates for the next pass.
                except BatchBudgetExceeded:
                    if attempt:
                        raise CollectionError("Task exceeds a single collection batch") from None
                    self._new_batch()
                finally:
                    self.budget.retained(self._live_bytes())

    def run(self):
        try:
            self._progress("starting")
            self._start()
            candidates, used = self._scan()
            while True:
                self._accept_candidates(candidates, used)
                self._load_completed()
                self._collect_tasks()
                self.budget.before_closing_scan()
                closing, closing_bytes = self._scan()
                if self.request_gate is not None:
                    self.request_gate.check_cancelled()
                if {key: value["candidate"] for key, value in self.completed.items()} == closing:
                    output = {"tenant_id": self.client.tenant_id, "project_id": self.project_id,
                              "include_archived": self.include_archived, "selection": self.selection,
                              "batch_id": str(uuid.uuid4()),
                              "collected_at": datetime.now(timezone.utc).isoformat(),
                              "pagination_complete": True,
                              "records": [self.completed[key]["record"] for key in closing],
                              "collection_stats": dict(self.budget.counters,
                                                       tasks_scanned=len(closing),
                                                       project_tasks_listed=self.project_tasks_listed,
                                                       resumed_tasks=len(self.resumed_ids))}
                    self._store("clear")
                    return output
                if self.budget.counters["passes"] >= self.policy.max_passes:
                    raise CollectionError("Project changed during collection; reconciliation limit exhausted")
                candidates, used = closing, closing_bytes
        finally:
            if self.budget is not None:
                self.client.request_count = self.budget.counters["requests"]
                self.client.record_count = self.budget.counters["records"]
                self.client.response_bytes = self.budget.counters["response_bytes"]
            if self.worker is not None:
                self.client.last_request = self.worker.last_request
            if self.request_gate is not None:
                self.client._next_collection_request = self.request_gate.next_allowed
                if self.request_gate.last_admission is not None:
                    self.client.last_request = self.request_gate.last_admission
            self._store("close")


def collect_project(client, project_id, include_archived, page_size, selection):
    return _Coordinator(client, project_id, include_archived, page_size, selection).run()
