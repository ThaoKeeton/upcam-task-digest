"""Bounded, fail-closed Teambition MCP-only collection coordinator."""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from report_contract import task_is_candidate, task_updated_timestamp, updated_selection
from collection_progress import refresh_collection_progress


class CollectionError(RuntimeError):
    """Messages deliberately exclude response bodies, URLs and credentials."""


class BatchBudgetExceeded(CollectionError):
    """Only a local batch limit; a coordinator may start one fresh batch."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise CollectionError("Invalid identifier")
    return value


def record_id(record, *, task_fallback=True):
    if not isinstance(record, dict):
        raise CollectionError("Expected object record")
    values = [record[k] for k in ("id", "_id") if record.get(k)]
    if not values and task_fallback and record.get("taskId"):
        values = [record["taskId"]]
    if not values or any(v != values[0] for v in values):
        raise CollectionError("Missing or conflicting record identity")
    return identifier(values[0])


def validate_binding(raw, task_id):
    if (raw.get('boundToObjectId') != task_id or raw.get('boundToObjectType') != 'task'
            or ('taskId' in raw and raw['taskId'] != task_id)):
        raise CollectionError('Missing or mismatched activity task binding')


def records_equal(left, right):
    """Compare JSON by type as well as value (True must not equal 1)."""
    try:
        def encode(value):
            return json.dumps(value, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False)
        return encode(left) == encode(right)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CollectionError('Invalid collection JSON') from None



class Client:
    def __init__(self, tenant_id, *, transport=None, sleep=time.sleep, clock=time.monotonic,
                 credentials=None, interval=0.25, attempts=3, max_pages=50,
                 max_requests=1000, max_tasks=300, max_records=50000, max_seconds=310,
                 max_response_bytes=128 * 1024 * 1024, batch_mode=False,
                 max_project_tasks=10000, checkpoint=None, shared_budget=None,
                 batch_policy=None, concurrent_tasks=1, request_gate=None, cancel_event=None,
                 mcp_adapter_factory=None, mcp_envelope_validator=None, archive_contract_validator=None,
                 project_discovery=False):
        if type(project_discovery) is not bool or (project_discovery and tenant_id is not None):
            raise CollectionError('Invalid project discovery scope')
        self.tenant_id = None if project_discovery else identifier(tenant_id)
        self.project_discovery = project_discovery
        if type(concurrent_tasks) is not int or not 1 <= concurrent_tasks <= 4:
            raise CollectionError("Invalid concurrent task limit")
        if cancel_event is not None and not callable(getattr(cancel_event, 'is_set', None)):
            raise CollectionError("Invalid collection cancellation signal")
        self.concurrent_tasks, self.request_gate, self.cancel_event = concurrent_tasks, request_gate, cancel_event
        self.injected_transport = transport is not None
        self.transport = transport
        self.sleep, self.clock = sleep, clock
        self.credentials = credentials or (lambda: None)
        self.mcp_adapter_factory = mcp_adapter_factory
        self.mcp_envelope_validator = mcp_envelope_validator
        self.archive_contract_validator = archive_contract_validator
        self._adapter = None
        if interval < 0.25 or not 1 <= attempts <= 5 or not 1 <= max_pages <= 100:
            raise CollectionError("Invalid request limits")
        if (type(max_requests) is not int or not 1 <= max_requests <= 2000
                or type(max_tasks) is not int or not 1 <= max_tasks <= 700
                or type(max_records) is not int or not 1 <= max_records <= 50000
                or type(max_seconds) is not int or not 1 <= max_seconds <= 600
                or type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 512 * 1024 * 1024):
            raise CollectionError("Invalid collection budgets")
        if (type(batch_mode) is not bool or type(max_project_tasks) is not int
                or not 1 <= max_project_tasks <= 10000):
            raise CollectionError("Invalid batch collection policy")
        self.batch_mode, self.max_project_tasks = batch_mode, max_project_tasks
        self.checkpoint, self.shared_budget, self.batch_policy = checkpoint, shared_budget, batch_policy
        self.interval, self.attempts, self.max_pages = interval, attempts, max_pages
        self.max_requests, self.max_tasks = max_requests, max_tasks
        self.max_records, self.max_seconds = max_records, max_seconds
        self.max_response_bytes, self.response_bytes = max_response_bytes, 0
        self.request_count, self.record_count = 0, 0
        self.started = None
        self.last_request = None

    def _remaining(self):
        if self.request_gate is not None:
            self.request_gate.check_cancelled()
        if self.shared_budget is not None:
            self.shared_budget.check_time()
        if self.started is None:
            self.started = self.clock()
        remaining = self.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise BatchBudgetExceeded("Collection time budget exhausted")
        return remaining

    def _wait(self, delay):
        if self.shared_budget is not None:
            self.shared_budget.before_wait(delay)
        if delay >= self._remaining():
            raise BatchBudgetExceeded("Wait would exceed collection time budget")
        if self.request_gate is not None:
            self.request_gate.wait(delay, check=self._remaining)
        else:
            self.sleep(delay)

    def _before_mcp(self, metadata):
        """Durably reserve request and response budgets before each HTTP attempt."""
        if self.request_count >= self.max_requests:
            raise BatchBudgetExceeded("Collection request budget exhausted")
        self._remaining()
        page_size = metadata.get('pageSize', 1) if metadata.get('method') == 'tools/call' else 1
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise CollectionError('Invalid page size')
        response_limit = metadata.get('response_limit')
        if type(response_limit) is not int or response_limit < 1:
            raise CollectionError('Invalid response byte reservation')
        remaining_bytes = self.max_response_bytes - self.response_bytes
        if remaining_bytes <= 1:
            raise BatchBudgetExceeded('Collection response byte budget exhausted')
        effective_limit = min(response_limit, remaining_bytes - 1)
        page_tool = metadata.get('method') == 'tools/call' and metadata.get('tool_name') in {
            'teambition.project.query', 'teambition.project.task-query', 'teambition.task.activity',
            'teambition.member.query'}
        original_page_size = page_size
        if metadata.get('method') == 'tools/call':
            remaining_records = self.max_records - self.record_count
            if remaining_records <= 0:
                raise BatchBudgetExceeded('Collection record budget exhausted')
            if page_tool:
                page_size = min(page_size, remaining_records)
        reservation = None
        charged = False
        try:
            if self.shared_budget is not None:
                reservation = self.shared_budget.plan_request({'pageSize': page_size}, detail=not page_tool,
                                                                preferred_bytes=effective_limit + 1)
                charged = '_concurrent_reservation' in reservation
                self.shared_budget.before_request(reservation)
                charged = True
                effective_limit = min(effective_limit, reservation['body_limit'])
            else:
                reservation = {}
            if effective_limit < 1:
                raise CollectionError('Collection session response byte budget exhausted')
            if effective_limit < response_limit:
                reservation['_mcp_response_limit'] = effective_limit
            effective_page_size = reservation.get('params', {}).get('pageSize', page_size)
            if page_tool and effective_page_size < original_page_size:
                reservation['_mcp_page_size'] = effective_page_size
            # A concurrent budget proxy performs an owner-thread RPC for counters.
            # All such bookkeeping must finish before the final send admission.
            refresh_collection_progress(requests=self.shared_budget.counters['requests']
                                        if self.shared_budget is not None else self.request_count + 1)
            remaining = self._remaining()
            if self.shared_budget is not None:
                remaining_fn = getattr(self.shared_budget, 'remaining_seconds', None)
                if remaining_fn is not None:
                    remaining = min(remaining, remaining_fn())
            if remaining <= 0:
                raise BatchBudgetExceeded('Collection time budget exhausted')
            if remaining < 30:
                reservation['_mcp_timeout'] = remaining
            return reservation
        except BaseException:
            if reservation is not None:
                if charged:
                    try:
                        self.shared_budget.cancel_unsent(reservation)
                    except Exception:
                        pass  # Retain conservative charges if rollback cannot be persisted.
                end = getattr(self.shared_budget, 'end_request', None)
                if end is not None:
                    end(reservation)
            raise

    def _dispatch_mcp(self, metadata):
        """Apply rate/time limits immediately before the actual HTTP dispatch."""
        if self.request_count >= self.max_requests:
            raise BatchBudgetExceeded('Collection request budget exhausted')
        timeout = metadata.get('timeout', 30)
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise CollectionError('Invalid MCP request timeout')
        if self.request_gate is None and self.last_request is not None:
            self._wait(max(0, self.interval - (self.clock() - self.last_request)))
        remaining = self._remaining()
        if self.shared_budget is not None:
            remaining_fn = getattr(self.shared_budget, 'remaining_seconds', None)
            if remaining_fn is not None:
                remaining = min(remaining, remaining_fn())
        started = self.clock()
        if self.request_gate is not None:
            self.request_gate.acquire(check=self._remaining)
        remaining -= max(0, self.clock() - started)
        if remaining <= 0:
            raise BatchBudgetExceeded('Collection time budget exhausted')
        self.last_request = self.clock()
        self.request_count += 1
        return min(timeout, remaining)

    def _after_mcp(self, reservation, outcome):
        try:
            used = outcome.get('response_bytes')
            if type(used) is not int or used < 0:
                raise CollectionError('Invalid MCP request accounting')
            self.response_bytes += used
            if self.response_bytes > self.max_response_bytes:
                raise BatchBudgetExceeded('Collection response byte budget exhausted')
            if outcome.get('dispatched') is False:
                if used != 0:
                    raise CollectionError('Invalid MCP request accounting')
                if self.shared_budget is not None:
                    self.shared_budget.cancel_unsent(reservation)
                return
            if outcome.get('completed') is True and outcome.get('uncertain') is False:
                count = outcome.get('result_count')
                if type(count) is not int or count < 0:
                    raise CollectionError('Invalid MCP request accounting')
                if self.shared_budget is not None:
                    self.shared_budget.finish_response(reservation, used, count)
                self.record_count += count
                if self.record_count > self.max_records:
                    raise BatchBudgetExceeded('Collection record budget exhausted')
                self._remaining()
            elif outcome.get('dispatched') is True:
                self._remaining()
        finally:
            end = getattr(self.shared_budget, 'end_request', None)
            if reservation is not None and end is not None:
                end(reservation)

    def _mcp(self):
        if self._adapter is None:
            if self.mcp_adapter_factory is not None:
                self._adapter = self.mcp_adapter_factory(self)
            else:
                from teambition_mcp import MCPTransport
                from mcp_collection_adapter import MCPCollectionAdapter
                from mcp_service_contract import VERIFIED_VALIDATORS, task_archive_is_boolean
                transport = MCPTransport(token_provider=self.credentials, transport=self.transport,
                                         before_request=self._before_mcp, before_dispatch=self._dispatch_mcp,
                                         after_request=self._after_mcp,
                                         clock=self.clock)
                self._adapter = MCPCollectionAdapter(
                    transport, validate_envelope=(self.mcp_envelope_validator
                        if self.mcp_envelope_validator is not None else VERIFIED_VALIDATORS),
                    validate_task_archive=(self.archive_contract_validator
                        if self.archive_contract_validator is not None else task_archive_is_boolean),
                    organization_id=self.tenant_id, max_pages=self.max_pages,
                    max_records=self.max_records)
        return self._adapter

    def list_projects(self):
        """List the current user's visible projects after complete MCP pagination."""
        if not self.project_discovery:
            raise CollectionError('Project discovery requires a separate unscoped client')
        rows = self._mcp().projects()
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise CollectionError('Invalid project discovery result')
            project, organization, name = identifier(row.get('id')), identifier(row.get('organizationId')), row.get('name')
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise CollectionError('Invalid project discovery result')
            choice = {'id': project, 'name': name, 'organizationId': organization}
            prefix = row.get('uniqueIdPrefix')
            if prefix is not None and prefix != '':
                from task_display import TaskDisplayError, validate_task_prefix
                try:
                    choice['uniqueIdPrefix'] = validate_task_prefix(prefix, allow_empty=False)
                except TaskDisplayError:
                    raise CollectionError('Invalid project discovery result') from None
            result.append(choice)
        return result

    def get(self, operation, params):
        """Dispatch fixed collection operations exclusively to approved MCP tools."""
        if not isinstance(params, dict):
            raise CollectionError('Invalid collection operation')
        if operation == 'project.tasks':
            if set(params) - {'projectId', 'pageSize', 'pageToken', 'includeArchived', 'q'}:
                raise CollectionError('Invalid collection operation')
            project = identifier(params.get('projectId'))
            query = params.get('q')
            match = None if query is None else re.fullmatch(r"updated >= '([^']+)'", query)
            if query is not None and match is None:
                raise CollectionError('Invalid candidate query')
            action = lambda: self._mcp().task_page(
                project, updated_since=match.group(1) if match else None,
                include_archived=params.get('includeArchived', False),
                page_size=params.get('pageSize', 100), page_token=params.get('pageToken'))
        elif operation == 'task.activities':
            if set(params) - {'taskId', 'projectId', 'pageSize', 'pageToken'}:
                raise CollectionError('Invalid collection operation')
            task = identifier(params.get('taskId'))
            project = params.get('projectId')
            if project is not None:
                project = identifier(project)
            action = lambda: self._mcp().activity_page(task, project_id=project,
                                                        page_size=params.get('pageSize', 100),
                                                        page_token=params.get('pageToken'))
        elif operation == 'task.details':
            if set(params) - {'taskId', 'projectId', 'includeArchived'}:
                raise CollectionError('Invalid collection operation')
            task, project = identifier(params.get('taskId')), identifier(params.get('projectId'))
            action = lambda: self._mcp().detail_envelope(task, project_id=project,
                                     include_archived=params.get('includeArchived', False))
        else:
            raise CollectionError('Operation outside read-only allowlist')
        from teambition_mcp import MCPHTTPError, MCPResponseLimitError
        for attempt in range(self.attempts):
            try:
                return action()
            except MCPResponseLimitError:
                raise BatchBudgetExceeded('Collection response byte budget exhausted') from None
            except MCPHTTPError as exc:
                if not exc.retryable or attempt + 1 >= self.attempts:
                    raise CollectionError('Teambition MCP request failed') from None
                delay = max(2 ** attempt, exc.retry_after or 0)
                if self.request_gate is not None and exc.status == 429:
                    self.request_gate.defer(delay)
                self._wait(delay)
        raise CollectionError('Teambition MCP request failed')

    def pages(self, path, params, *, max_items=None):
        params = dict(params)
        seen_records = {}
        for _ in range(self.max_pages):
            data = self.get(path, params)
            rows = data.get("result")
            if not isinstance(rows, list) or "nextPageToken" not in data:
                raise CollectionError("Invalid page envelope")
            for row in rows:
                key = record_id(row, task_fallback=path != "task.activities")
                if key in seen_records:
                    if not records_equal(row, seen_records[key]):
                        raise CollectionError("Conflicting duplicate; collection is not a snapshot")
                    continue
                seen_records[key] = row
                if max_items is not None and len(seen_records) > max_items:
                    raise CollectionError("Task selection exceeds bounded collection budget")
                yield row
            token = data["nextPageToken"]
            if token is None or token == "":
                return
            if not isinstance(token, str) or len(token) > 8192:
                raise CollectionError("Invalid pagination token")
            params["pageToken"] = token
        raise CollectionError("Pagination limit exceeded; incomplete collection")

    def collect(self, project_id, *, include_archived=False, day=None, page_size=100, window=None,
                batch_mode=None, candidate_mode="updated_since_start"):
        if self.project_discovery:
            raise CollectionError('Project discovery client cannot collect tasks')
        # Caller freezes the Shanghai report date and optional half-open window
        # before any request. Candidate time never changes the activity window.
        try:
            selection = updated_selection(day, *(window or (None, None)), candidate_mode=candidate_mode)
        except (ValueError, TypeError):
            raise CollectionError('Canonical report day and timezone-aware window must be fixed before collection') from None
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise CollectionError('Invalid page size')
        if batch_mode is not None and type(batch_mode) is not bool:
            raise CollectionError('Invalid batch collection policy')
        enabled = self.batch_mode if batch_mode is None else batch_mode
        if enabled:
            from collection_batches import collect_project
            return collect_project(self, project_id, include_archived, page_size, selection)
        return self._collect(project_id, include_archived, page_size, selection)

    def _collect_bounded_fixture(self, project_id, *, include_archived=False):
        """Test-only bounded traversal. Never a production selection implementation."""
        if not self.injected_transport:
            raise CollectionError("Offline fixture collection requires an injected transport")
        return self._collect(project_id, include_archived, 100,
                             {'mode': 'offline-unfiltered', 'view_equivalent': False})

    def _collect(self, project_id, include_archived, page_size, selection):
        project_id = identifier(project_id)
        if type(include_archived) is not bool:
            raise CollectionError("include_archived must be boolean")
        params = {'projectId': project_id, 'pageSize': page_size, 'includeArchived': include_archived}
        if selection.get('q') is not None:
            params['q'] = selection['q']
        activity_scope = selection['mode'] == 'v3-project-activities-half-open'
        tasks = list(self.pages("project.tasks", params, max_items=self.max_tasks))
        project_tasks = tasks
        selected_updates = {}
        if activity_scope:
            try:
                for task in tasks:
                    if task.get('projectId') != project_id or type(task.get('isArchived')) is not bool:
                        raise CollectionError('Candidate identity or archive scope mismatch')
                    if task['isArchived'] and not include_archived:
                        raise CollectionError('Candidate identity or archive scope mismatch')
                    selected_updates[record_id(task)] = task_updated_timestamp(task.get('updated'))
                    if selection.get('q') is not None and not task_is_candidate(task.get('updated'), selection):
                        raise CollectionError('Server returned task outside requested updated candidate range')
                # Validate the complete listing before skipping any histories.
                tasks = [task for task in tasks if task_is_candidate(task.get('updated'), selection)]
            except ValueError:
                raise CollectionError('Invalid task updated timestamp') from None
        output = []
        for task in tasks:
            task_id = record_id(task)
            detail = self.get("task.details", {"taskId": task_id, "projectId": project_id, "includeArchived": include_archived}).get("result")
            if isinstance(detail, list) and len(detail) == 1:
                detail = detail[0]
            if record_id(detail) != task_id or detail.get("projectId") != project_id:
                raise CollectionError("Detail identity or project mismatch")
            if activity_scope:
                try:
                    current_update = task_updated_timestamp(detail.get('updated'))
                except ValueError:
                    raise CollectionError('Invalid task updated timestamp') from None
                if (current_update != selected_updates[task_id]
                        or detail.get('isArchived') is not task.get('isArchived')):
                    raise CollectionError('Task changed during collection; retry a fresh bounded run')
            if type(detail.get("isArchived")) is not bool:
                raise CollectionError("Missing archive flag")
            if detail["isArchived"] and not include_archived:
                continue
            # Exhaust history without assuming updated/createTime ordering or
            # stopping when a page happens to contain only out-of-window events.
            activities = list(self.pages("task.activities", {"taskId": task_id, "projectId": project_id, "pageSize": page_size}))
            for activity in activities:
                validate_binding(activity, task_id)
            output.append({"task": detail, "activities": activities})
        if activity_scope:
            closing = list(self.pages("project.tasks", params,
                                      max_items=self.max_tasks))
            if not records_equal({record_id(row): row for row in project_tasks},
                                 {record_id(row): row for row in closing}):
                # The API has no snapshot token. This detects observable drift,
                # but cannot attest deleted/inaccessible tasks or an atomic read.
                raise CollectionError('Task changed during collection; retry a fresh bounded run')
        from mcp_collection_adapter import metadata_from_tasks
        metadata = metadata_from_tasks(project_tasks + [record['task'] for record in output])
        return {"tenant_id": self.tenant_id, "project_id": project_id, "include_archived": include_archived,
                "selection": selection,
                "batch_id": str(uuid.uuid4()),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "pagination_complete": True, "records": output, "metadata": metadata}
