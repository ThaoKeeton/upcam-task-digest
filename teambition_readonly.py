"""Small, fail-closed Teambition enterprise v3 GET client (stdlib only)."""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CollectionError(RuntimeError):
    """Messages deliberately exclude response bodies, URLs and credentials."""


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
                 credentials=None, interval=0.25, attempts=3, max_pages=10000):
        self.tenant_id = identifier(tenant_id)
        self.transport = transport or build_opener(NoRedirect()).open
        self.sleep, self.clock = sleep, clock
        self.credentials = credentials or (lambda: (os.environ.get("TEAMBITION_APP_ID"),
                                                   os.environ.get("TEAMBITION_APP_SECRET")))
        if interval < 0.25 or not 1 <= attempts <= 5 or not 1 <= max_pages <= 10000:
            raise CollectionError("Invalid request limits")
        self.interval, self.attempts, self.max_pages = interval, attempts, max_pages
        self.last_request = None

    def get(self, path, params):
        if not self.PATH.fullmatch(path):
            raise CollectionError("Endpoint outside read-only allowlist")
        for attempt in range(self.attempts):
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (self.clock() - self.last_request)))
            request = Request(self.ORIGIN + path + "?" + urlencode(params), method="GET", headers={
                "Authorization": "Bearer " + app_jwt(*self.credentials()),
                "X-Tenant-Id": self.tenant_id, "X-Tenant-Type": "organization",
                "Accept": "application/json",
            })
            self.last_request = self.clock()
            delay = 2 ** attempt
            try:
                with self.transport(request, timeout=30) as response:
                    if response.status != 200:
                        raise CollectionError("Unexpected HTTP status")
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise CollectionError("Response size limit exceeded")
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
            except (URLError, TimeoutError, OSError):
                pass  # Retrying GET is safe; sending has a separate no-retry boundary.
            except (ValueError, UnicodeError):
                raise CollectionError("Malformed JSON response") from None
            if attempt + 1 < self.attempts:
                self.sleep(delay)
        raise CollectionError("GET retry budget exhausted")

    def pages(self, path, params):
        params = dict(params)
        seen_tokens, seen_records = set(), {}
        for _ in range(self.max_pages):
            data = self.get(path, params)
            rows = data.get("result")
            if not isinstance(rows, list) or "nextPageToken" not in data:
                raise CollectionError("Invalid page envelope")
            for row in rows:
                key = record_id(row, task_fallback=not path.endswith("/activity/list"))
                if key in seen_records:
                    if row != seen_records[key]:
                        raise CollectionError("Conflicting duplicate; collection is not a snapshot")
                    continue
                seen_records[key] = row
                yield row
            token = data["nextPageToken"]
            if token is None or token == "":
                return
            if not isinstance(token, str) or len(token) > 8192 or token in seen_tokens:
                raise CollectionError("Invalid or repeated pagination token")
            seen_tokens.add(token)
            params["pageToken"] = token
        raise CollectionError("Pagination limit exceeded; incomplete collection")

    def collect(self, project_id, *, include_archived=False):
        project_id = identifier(project_id)
        if type(include_archived) is not bool:
            raise CollectionError("include_archived must be boolean")
        tasks = list(self.pages(f"/api/v3/project/{project_id}/task/query", {
            "pageSize": 100, "includeArchived": str(include_archived).lower(),
        }))
        output = []
        for task in tasks:
            task_id = record_id(task)
            detail = self.get("/api/v3/task/query", {"taskId": task_id}).get("result")
            if isinstance(detail, list) and len(detail) == 1:
                detail = detail[0]
            if record_id(detail) != task_id or detail.get("projectId") != project_id:
                raise CollectionError("Detail identity or project mismatch")
            if type(detail.get("isArchived")) is not bool:
                raise CollectionError("Missing archive flag")
            if detail["isArchived"] and not include_archived:
                continue
            # Never prune by task created/updated/current status or stop on event age.
            activities = list(self.pages(f"/api/v3/task/{task_id}/activity/list", {"pageSize": 100}))
            output.append({"task": detail, "activities": activities})
        return {"tenant_id": self.tenant_id, "project_id": project_id, "include_archived": include_archived,
                "pagination_complete": True, "records": output}
