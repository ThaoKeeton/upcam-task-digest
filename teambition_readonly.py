"""Small, fail-closed Teambition enterprise v3 GET client (stdlib only)."""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
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


def app_jwt(app_id, secret, now=None, ttl=300):
    if not app_id or not secret or not 1 <= ttl <= 300:
        raise CollectionError("Missing credentials or invalid token lifetime")
    now = int(time.time() if now is None else now)
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    payload = encode(json.dumps({"_appId": app_id, "iat": now, "exp": now + ttl},
                                separators=(",", ":")).encode())
    signing = header + b"." + payload
    return (signing + b"." + encode(hmac.new(secret.encode(), signing, hashlib.sha256).digest())).decode()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    ORIGIN = "https://open.teambition.com"
    PATH = re.compile(r"/api/v3/(?:project/[A-Za-z0-9_-]+/task/query|task/query|task/[A-Za-z0-9_-]+/activity/list)")

    def __init__(self, tenant_id, *, transport=None, sleep=time.sleep, clock=time.monotonic,
                 credentials=None, interval=0.25, attempts=3, max_pages=50,
                 max_requests=1000, max_tasks=300, max_records=50000, max_seconds=310,
                 max_response_bytes=128 * 1024 * 1024, batch_mode=False,
                 max_project_tasks=10000, checkpoint=None, shared_budget=None,
                 batch_policy=None, concurrent_tasks=1, request_gate=None, cancel_event=None):
        self.tenant_id = identifier(tenant_id)
        if type(concurrent_tasks) is not int or not 1 <= concurrent_tasks <= 4:
            raise CollectionError("Invalid concurrent task limit")
        if cancel_event is not None and not callable(getattr(cancel_event, 'is_set', None)):
            raise CollectionError("Invalid collection cancellation signal")
        self.concurrent_tasks, self.request_gate, self.cancel_event = concurrent_tasks, request_gate, cancel_event
        self.injected_transport = transport is not None
        self.transport = transport or build_opener(NoRedirect()).open
        self.sleep, self.clock = sleep, clock
        self.credentials = credentials or (lambda: (os.environ.get("TEAMBITION_APP_ID"),
                                                   os.environ.get("TEAMBITION_APP_SECRET")))
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

    def get(self, path, params):
        if not self.PATH.fullmatch(path):
            raise CollectionError("Endpoint outside read-only allowlist")
        for attempt in range(self.attempts):
            if self.request_count >= self.max_requests:
                raise BatchBudgetExceeded("Collection request budget exhausted")
            self._remaining()
            if self.request_gate is None and self.last_request is not None:
                self._wait(max(0, self.interval - (self.clock() - self.last_request)))
            reservation = None
            request_params, body_limit = params, 8 * 1024 * 1024
            if self.shared_budget is not None:
                reservation = self.shared_budget.plan_request(params, detail=path == "/api/v3/task/query")
                request_params, body_limit = reservation["params"], reservation["body_limit"]
            request = Request(self.ORIGIN + path + "?" + urlencode(request_params), method="GET", headers={
                "Authorization": "Bearer " + app_jwt(*self.credentials()),
                "X-Tenant-Id": self.tenant_id, "X-Tenant-Type": "organization",
                "Accept": "application/json",
            })
            self.last_request = self.clock()
            self.request_count += 1
            delay = 2 ** attempt
            try:
                timeout = min(30, self._remaining())
                if self.shared_budget is not None:
                    self.shared_budget.before_request(reservation)
                    # Durable accounting can take time. Check the deadline and
                    # start the rate interval at the actual transport boundary.
                    timeout = min(30, self._remaining())
                    self.last_request = self.clock()
                refresh_collection_progress(requests=self.shared_budget.counters["requests"]
                                            if self.shared_budget is not None else self.request_count)
                if self.request_gate is not None:
                    self.request_gate.acquire(check=self._remaining)
                    # No owner-thread RPC or durable I/O between admission and
                    # transport: delayed bookkeeping must not bunch requests.
                    timeout = min(30, self.max_seconds - (self.clock() - self.started))
                    if timeout <= 0:
                        raise BatchBudgetExceeded("Collection time budget exhausted")
                    self.last_request = self.clock()
                with self.transport(request, timeout=timeout) as response:
                    if response.status != 200:
                        raise CollectionError("Unexpected HTTP status")
                    body = response.read(body_limit + 1)
                    if self.shared_budget is not None:
                        if len(body) > body_limit:
                            raise CollectionError("Response size limit exceeded")
                        data = json.loads(body)
                        rows = data.get("result") if isinstance(data, dict) else None
                        count = len(rows) if isinstance(rows, list) else int(isinstance(rows, dict))
                        # Settle both charges atomically before __exit__ can fail.
                        # An uncertain read/parse retains the write-ahead reservation.
                        self.shared_budget.finish_response(reservation, len(body), count)
                refresh_collection_progress(requests=self.shared_budget.counters["requests"]
                                            if self.shared_budget is not None else self.request_count)
                if self.shared_budget is None:
                    self._remaining()
                if len(body) > body_limit:
                    raise CollectionError("Response size limit exceeded")
                if self.shared_budget is not None:
                    self._remaining()
                if self.response_bytes + len(body) > self.max_response_bytes:
                    raise BatchBudgetExceeded("Collection response byte budget exhausted")
                self.response_bytes += len(body)
                if self.shared_budget is None:
                    data = json.loads(body)
                if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 200:
                    raise CollectionError("API rejected request or malformed envelope")
                return data
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status not in (429, 500, 502, 503, 504):
                    raise CollectionError("HTTP request rejected") from None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            raise CollectionError("Invalid Retry-After") from None
                    if not 0 <= delay <= 60:
                        raise CollectionError("Retry-After exceeds bounded retry budget") from None
                if status == 429 and self.request_gate is not None:
                    self.request_gate.defer(delay)
            except (URLError, TimeoutError, OSError):
                pass  # Retrying GET is safe; sending has a separate no-retry boundary.
            except (ValueError, UnicodeError):
                raise CollectionError("Malformed JSON response") from None
            finally:
                # Concurrent reservations remain charged on uncertain outcomes,
                # but no longer occupy an active-response slot after this attempt.
                end_request = getattr(self.shared_budget, 'end_request', None)
                if reservation is not None and end_request is not None:
                    end_request(reservation)
            if attempt + 1 < self.attempts:
                self._wait(delay)
        raise CollectionError("GET retry budget exhausted")

    def pages(self, path, params, *, max_items=None):
        params = dict(params)
        seen_tokens, seen_records = set(), {}
        for _ in range(self.max_pages):
            data = self.get(path, params)
            rows = data.get("result")
            if not isinstance(rows, list) or "nextPageToken" not in data:
                raise CollectionError("Invalid page envelope")
            for row in rows:
                self.record_count += 1
                if self.record_count > self.max_records:
                    raise BatchBudgetExceeded("Collection record budget exhausted")
                key = record_id(row, task_fallback=not path.endswith("/activity/list"))
                if key in seen_records:
                    if row != seen_records[key]:
                        raise CollectionError("Conflicting duplicate; collection is not a snapshot")
                    continue
                seen_records[key] = row
                if max_items is not None and len(seen_records) > max_items:
                    raise CollectionError("Task selection exceeds bounded collection budget")
                yield row
            token = data["nextPageToken"]
            if token is None or token == "":
                return
            if not isinstance(token, str) or len(token) > 8192 or token in seen_tokens:
                raise CollectionError("Invalid or repeated pagination token")
            seen_tokens.add(token)
            params["pageToken"] = token
        raise CollectionError("Pagination limit exceeded; incomplete collection")

    def collect(self, project_id, *, include_archived=False, day=None, page_size=100, window=None,
                batch_mode=None, candidate_mode="updated_since_start"):
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
        params = {'pageSize': page_size, 'includeArchived': str(include_archived).lower()}
        activity_scope = selection['mode'] == 'v3-project-activities-half-open'
        tasks = list(self.pages(f"/api/v3/project/{project_id}/task/query", params, max_items=self.max_tasks))
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
                # Validate the complete listing before skipping any histories.
                tasks = [task for task in tasks if task_is_candidate(task.get('updated'), selection)]
            except ValueError:
                raise CollectionError('Invalid task updated timestamp') from None
        output = []
        for task in tasks:
            task_id = record_id(task)
            detail = self.get("/api/v3/task/query", {"taskId": task_id}).get("result")
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
            activities = list(self.pages(f"/api/v3/task/{task_id}/activity/list", {"pageSize": page_size}))
            for activity in activities:
                validate_binding(activity, task_id)
            output.append({"task": detail, "activities": activities})
        if activity_scope:
            closing = list(self.pages(f"/api/v3/project/{project_id}/task/query", params,
                                      max_items=self.max_tasks))
            if {record_id(row): row for row in project_tasks} != {record_id(row): row for row in closing}:
                # The API has no snapshot token. This detects observable drift,
                # but cannot attest deleted/inaccessible tasks or an atomic read.
                raise CollectionError('Task changed during collection; retry a fresh bounded run')
        return {"tenant_id": self.tenant_id, "project_id": project_id, "include_archived": include_archived,
                "selection": selection,
                "batch_id": str(uuid.uuid4()),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "pagination_complete": True, "records": output}
