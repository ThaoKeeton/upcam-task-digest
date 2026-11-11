"""Synthetic batch collection; credentials and transport never leave this process."""

import copy
import io
import json
import math
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.error import HTTPError

from teambition_readonly import Client, CollectionError


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response(io.BytesIO):
    def __init__(self, body=b"", *, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def geturl(self):
        return "https://open.teambition.com/api/mcp/v2"

    def read1(self, size=-1):
        return self.read(size)


PROJECT_TOOL = "teambition.project.task-query"
DETAIL_TOOL = "teambition.task.query"
ACTIVITY_TOOL = "teambition.task.activity"


def fictional_envelope(value):
    # Test-only success values. No live business contract is asserted here.
    return value.get("code") == 200 and value.get("errorCode") == ""


def fictional_archive(row):
    return type(row.get("isArchived")) is bool


def catalog():
    def schema(fields, required):
        return {"type": "object", "properties": {key: {"type": kind} for key, kind in fields.items()},
                "required": required, "additionalProperties": False}
    definitions = {
        PROJECT_TOOL: schema({"projectId": "string", "tql": "string", "pageSize": "integer",
                              "pageToken": "string", "enrichFields": "string", "includeArchived": "boolean"}, ["projectId"]),
        DETAIL_TOOL: schema({"taskId": "string", "includeArchived": "boolean", "enrichFields": "string"}, ["taskId"]),
        ACTIVITY_TOOL: schema({"taskId": "string", "actions": "string", "pageSize": "integer", "pageToken": "string",
                              "includeFileDetails": "boolean", "includeRtfHtml": "boolean"}, ["taskId"]),
    }
    return {"tools": [{"name": name, "inputSchema": definition} for name, definition in definitions.items()]}


def task(index, **changes):
    return dict({"id": f"fictional-task-{index:05d}", "projectId": "fictional-project",
                 "isArchived": False, "updated": "2026-01-03T10:00:00+08:00",
                 "content": f"Task {index}", "rawDetail": {"kept": True}}, **changes)


def event(task_id, index=0, **changes):
    return dict({"id": f"{task_id}-event-{index}", "boundToObjectId": task_id,
                 "boundToObjectType": "task", "createTime": "2026-01-02T10:00:00+08:00",
                 "rawActivity": {"kept": True}}, **changes)


class ProjectTransport:
    """A paginated fictional MCP service whose source can change per scan."""

    def __init__(self, tasks, clock, *, histories=None, scans=None):
        self.tasks = copy.deepcopy(tasks)
        self.clock = clock
        self.histories = histories or {}
        self.scans = scans
        self.scan_count = 0
        self.requests = []
        self.detail_ids = []
        self.activity_pages = []
        self.fail_activity = None
        self.interrupt_on_detail = None
        self.transform = None

    def __call__(self, request, timeout, *, _started=None):
        started = self.clock() if _started is None else _started
        assert request.full_url == "https://open.teambition.com/api/mcp/v2"
        assert request.method == "POST"
        assert request.get_header("Authorization", "").startswith("Bearer fictional-")
        message = json.loads(request.data)
        method = message["method"]
        if method == "tools/call":
            operation = message["params"]["name"]
            params = message["params"]["arguments"]
        else:
            operation, params = method, message.get("params", {})
        self.requests.append((operation, copy.deepcopy(params), started))
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fictional", "version": "1"}}
        elif method == "notifications/initialized":
            return Response(status=202)
        elif method == "tools/list":
            result = catalog()
        elif method == "tools/call":
            result = {"structuredContent": self.business(operation, params)}
        else:
            raise AssertionError("Unexpected MCP method")
        return Response(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode("utf-8"))

    def business(self, operation, params):
        if operation == PROJECT_TOOL:
            if "pageToken" not in params:
                self.scan_count += 1
                if self.scans is not None:
                    self.tasks = copy.deepcopy(self.scans(min(self.scan_count, 100)))
            offset = int(params.get("pageToken", "0"))
            size = params["pageSize"]
            rows = self.tasks
            if "tql" in params:
                query = params["tql"]
                if not (query.startswith("updated >= '") and query.endswith("'")):
                    raise AssertionError("Unexpected fictional TQL syntax")
                cutoff = datetime.fromisoformat(query[len("updated >= '"):-1])
                def included(row):
                    try:
                        value = datetime.fromisoformat(row.get("updated", "").replace("Z", "+00:00"))
                        return value >= cutoff
                    except (ValueError, TypeError, AttributeError):
                        return True  # Invalid rows remain visible to validation.
                rows = [row for row in rows if included(row)]
            next_token = str(offset + size) if offset + size < len(rows) else ""
            result = {"result": copy.deepcopy(rows[offset:offset + size]), "nextPageToken": next_token}
        elif operation == DETAIL_TOOL:
            task_id = params["taskId"]
            self.detail_ids.append(task_id)
            if self.interrupt_on_detail == task_id:
                self.interrupt_on_detail = None
                raise CollectionError("Synthetic interrupted collection")
            detail = next(row for row in self.tasks if row["id"] == task_id)
            result = {"result": copy.deepcopy(detail)}
        elif operation == ACTIVITY_TOOL:
            task_id = params["taskId"]
            page_index = int(params.get("pageToken", "0"))
            self.activity_pages.append((task_id, page_index))
            if self.fail_activity == (task_id, page_index):
                self.fail_activity = None
                raise CollectionError("Synthetic interrupted collection")
            pages = self.histories.get(task_id, [[event(task_id)]])
            result = {"result": copy.deepcopy(pages[page_index]),
                      "nextPageToken": str(page_index + 1) if page_index + 1 < len(pages) else ""}
        else:
            raise AssertionError("Unexpected business tool")
        result.update(code=200, errorCode="", errorMessage="")
        if self.transform:
            result = self.transform(result, operation, params)
        return result


def collector(transport, clock, **changes):
    return Client("fictional-tenant", transport=transport, clock=clock, sleep=clock.sleep,
                  credentials=lambda: "fictional-token",
                  mcp_envelope_validator=fictional_envelope, archive_contract_validator=fictional_archive,
                  batch_mode=True, **changes)


class LargeProjectCollectionTests(unittest.TestCase):
    def test_10000_task_boundary_completes_and_10001_never_returns_partial_data(self):
        for count in (10000, 10001):
            with self.subTest(tasks=count):
                clock = FakeClock()
                transport = ProjectTransport([task(index) for index in range(count)], clock)
                if count == 10001:
                    with self.assertRaisesRegex(CollectionError, "Pagination|selection"):
                        collector(transport, clock).collect("fictional-project", day="2026-01-02")
                    self.assertEqual(transport.detail_ids, [])
                    self.assertEqual(len(transport.requests), 103)
                else:
                    result = collector(transport, clock).collect("fictional-project", day="2026-01-02")
                    self.assertEqual(len(result["records"]), 10000)
                    self.assertEqual(result["collection_stats"]["requests"], 20200 + 3 * 34)
                    self.assertEqual(result["collection_stats"]["records"], 40000)

    def test_701_and_1201_tasks_complete_across_pages_and_task_batches(self):
        # Keeping the old 700 selection cap or resetting request spacing breaks this.
        for count in (701, 1201):
            with self.subTest(tasks=count):
                clock = FakeClock()
                tasks = [task(index) for index in range(count)]
                transport = ProjectTransport(tasks, clock)
                source = collector(transport, clock).collect("fictional-project", day="2026-01-02")
                self.assertTrue(source["pagination_complete"])
                self.assertEqual(len(source["records"]), count)
                self.assertEqual(source["records"][0],
                                 {"task": tasks[0], "activities": [event(tasks[0]["id"])]})
                self.assertEqual(source["records"][-1]["task"], tasks[-1])
                self.assertEqual(transport.scan_count, 2)
                self.assertGreaterEqual(source["collection_stats"]["batches"], math.ceil(count / 300))
                self.assertEqual(source["collection_stats"]["requests"], 2 * count + 2 * math.ceil(count / 100)
                                 + 3 * math.ceil(count / 300))
                self.assertTrue(all(type(value) is int for value in source["collection_stats"].values()))
                self.assertFalse(source["selection"]["snapshot_guaranteed"])
                self.assertFalse(source["selection"]["historical_event_coverage"])
                for path, query, unused in transport.requests:
                    if path == PROJECT_TOOL:
                        self.assertEqual(query["tql"], "updated >= '2026-01-02T00:00:00+08:00'")
                        self.assertEqual(query["includeArchived"], False)
                    else:
                        self.assertNotIn("tql", query)
                instants = [instant for unused, query, instant in transport.requests]
                self.assertTrue(all(right - left >= 0.25 for left, right in zip(instants, instants[1:])))

    def test_project_scan_uses_100_pages_independently_of_activity_page_limit(self):
        clock = FakeClock()
        tasks = [task(index) for index in range(101)]
        transport = ProjectTransport(tasks, clock)
        result = collector(transport, clock, max_pages=1).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(result["records"]), 101)

    def test_archive_and_custom_window_are_frozen_across_batches(self):
        clock = FakeClock()
        tasks = [task(0), task(1, isArchived=True)]
        transport = ProjectTransport(tasks, clock)
        result = collector(transport, clock, max_tasks=1).collect(
            "fictional-project", day="2026-01-02", include_archived=True,
            window=("2026-01-01T18:00:00+08:00", "2026-01-02T18:00:00+08:00"))
        self.assertEqual(result["selection"]["start"], "2026-01-01T18:00:00+08:00")
        self.assertEqual(result["selection"]["end_exclusive"], "2026-01-02T18:00:00+08:00")
        self.assertEqual(len(result["records"]), 2)
        for path, query, unused in transport.requests:
            if path == PROJECT_TOOL:
                self.assertEqual(query["tql"], "updated >= '2026-01-01T18:00:00+08:00'")
                self.assertEqual(query["includeArchived"], True)
            else:
                self.assertNotIn("tql", query)

    def test_repeated_opaque_cursors_continue_until_explicit_empty_terminal(self):
        class RepeatedCursorTransport(ProjectTransport):
            def business(self, operation, params):
                if operation not in (PROJECT_TOOL, ACTIVITY_TOOL):
                    return super().business(operation, params)
                if operation == PROJECT_TOOL:
                    if "pageToken" not in params:
                        self.scan_count += 1
                        self.project_page = 0
                    index = self.project_page
                    self.project_page += 1
                    rows = self.tasks[index:index + 1]
                else:
                    identity = params["taskId"]
                    if "pageToken" not in params:
                        self.history_page = 0
                    index = self.history_page
                    self.history_page += 1
                    self.activity_pages.append((identity, index))
                    rows = [event(identity, index)] if index < 2 else []
                return {"code": 200, "errorCode": "", "errorMessage": "",
                        "result": copy.deepcopy(rows), "nextPageToken": "opaque-repeat" if index < 2 else ""}

        clock = FakeClock()
        transport = RepeatedCursorTransport([task(0), task(1)], clock)
        source = collector(transport, clock).collect("fictional-project", day="2026-01-02")
        self.assertTrue(source["pagination_complete"])
        self.assertEqual(transport.scan_count, 2)
        self.assertEqual(len(source["records"]), 2)
        for record in source["records"]:
            self.assertEqual(record["activities"], [event(record["task"]["id"], index) for index in range(2)])
            self.assertEqual([params.get("pageToken") for name, params, _ in transport.requests
                              if name == ACTIVITY_TOOL and params["taskId"] == record["task"]["id"]],
                             [None, "opaque-repeat", "opaque-repeat"])
        self.assertEqual([params.get("pageToken") for name, params, _ in transport.requests if name == PROJECT_TOOL],
                         [None, "opaque-repeat", "opaque-repeat"] * 2)

    def test_project_limit_blocks_without_returning_truncated_records(self):
        clock = FakeClock()
        transport = ProjectTransport([task(index) for index in range(3)], clock)
        with self.assertRaises(CollectionError):
            collector(transport, clock, max_project_tasks=2).collect("fictional-project", day="2026-01-02")
        self.assertEqual(transport.detail_ids, [])


class ResumeTests(unittest.TestCase):
    def test_encrypted_store_reopens_and_preserves_attempts_and_completed_records(self):
        from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint
        def protect(value):
            return b"fictional-codec:" + bytes(item ^ 0x5a for item in value)
        def unprotect(value):
            return bytes(item ^ 0x5a for item in value[len(b"fictional-codec:"):])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-progress.sqlite3"
            clock = FakeClock()
            tasks = [task(0), task(1)]
            first = ProjectTransport(tasks, clock)
            first.interrupt_on_detail = tasks[1]["id"]
            with self.assertRaises(CollectionError):
                collector(first, clock, checkpoint=DpapiCollectionCheckpoint(
                    path, protect=protect, unprotect=unprotect)).collect("fictional-project", day="2026-01-02")
            second = ProjectTransport(tasks, clock)
            result = collector(second, clock, checkpoint=DpapiCollectionCheckpoint(
                path, protect=protect, unprotect=unprotect)).collect("fictional-project", day="2026-01-02")
            self.assertEqual(second.detail_ids, [tasks[1]["id"]])
            self.assertEqual(result["collection_stats"]["resumed_tasks"], 1)
            self.assertEqual(result["collection_stats"]["requests"], len(first.requests) + len(second.requests))

    def test_completed_tasks_resume_and_half_read_history_restarts_at_first_page(self):
        from collection_batches import MemoryCheckpoint
        clock = FakeClock()
        tasks = [task(0), task(1), task(2)]
        task_id = tasks[1]["id"]
        histories = {task_id: [[event(task_id, 0)], [event(task_id, 1)]]}
        store = MemoryCheckpoint()
        first = ProjectTransport(tasks, clock, histories=histories)
        first.fail_activity = (task_id, 1)
        with self.assertRaises(CollectionError):
            collector(first, clock, checkpoint=store, max_tasks=1).collect("fictional-project", day="2026-01-02")
        self.assertIsNotNone(store.load_task(tasks[0]["id"]))
        self.assertIsNone(store.load_task(task_id))
        resumed = ProjectTransport(tasks, clock, histories=histories)
        start_time = clock()
        result = collector(resumed, clock, checkpoint=store, max_tasks=1).collect("fictional-project", day="2026-01-02")
        self.assertEqual(resumed.detail_ids, [task_id, tasks[2]["id"]])
        self.assertEqual([index for identity, index in resumed.activity_pages if identity == task_id], [0, 1])
        self.assertEqual(len(result["records"][1]["activities"]), 2)
        self.assertEqual(result["collection_stats"]["resumed_tasks"], 1)
        self.assertEqual(result["collection_stats"]["requests"], len(first.requests) + len(resumed.requests))
        self.assertGreaterEqual(resumed.requests[0][2] - start_time, 0.25)
        self.assertIsNone(store.load_task(tasks[0]["id"]))

    def interrupted(self, store, clock):
        tasks = [task(0), task(1), task(2)]
        transport = ProjectTransport(tasks, clock)
        transport.interrupt_on_detail = tasks[2]["id"]
        with self.assertRaises(CollectionError):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        return tasks, transport

    def test_context_or_credential_change_never_reuses_progress(self):
        from collection_batches import MemoryCheckpoint
        for change in ("credentials", "day", "archive", "tenant", "policy"):
            with self.subTest(change=change):
                store, clock = MemoryCheckpoint(), FakeClock()
                tasks, unused = self.interrupted(store, clock)
                transport = ProjectTransport(tasks, clock)
                options = {"max_tasks": 1} if change == "policy" else {}
                reader = collector(transport, clock, checkpoint=store, **options)
                kwargs = {"day": "2026-01-03" if change == "day" else "2026-01-02",
                          "include_archived": change == "archive"}
                if change == "credentials":
                    reader.credentials = lambda: "fictional-rotated-token"
                if change == "tenant":
                    reader.tenant_id = "another-fictional-tenant"
                result = reader.collect("fictional-project", **kwargs)
                self.assertEqual(transport.detail_ids, [row["id"] for row in tasks])
                self.assertEqual(result["collection_stats"]["resumed_tasks"], 0)

    def test_resume_refreshes_changed_and_added_tasks_and_prunes_removed_tasks(self):
        from collection_batches import MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        tasks, unused = self.interrupted(store, clock)
        changed = dict(tasks[1], updated="2026-01-04T10:00:00+08:00", content="Changed")
        current = [tasks[0], changed, task(3)]
        transport = ProjectTransport(current, clock)
        result = collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(transport.detail_ids, [changed["id"], current[2]["id"]])
        self.assertEqual([row["task"] for row in result["records"]], current)
        self.assertEqual(result["collection_stats"]["resumed_tasks"], 1)

    def test_cache_record_identity_archive_version_and_event_binding_are_revalidated(self):
        from collection_batches import MemoryCheckpoint
        for mutation in ("identity", "project", "archive", "timezone", "binding"):
            class AlteredCheckpoint(MemoryCheckpoint):
                corrupt = False

                def load_task(self, identity):
                    value = super().load_task(identity)
                    if not self.corrupt or value is None:
                        return value
                    if mutation == "binding":
                        value["record"]["activities"][0]["boundToObjectId"] = "wrong-task"
                    else:
                        field, replacement = {"identity": ("id", "wrong-task"),
                                              "project": ("projectId", "wrong-project"),
                                              "archive": ("isArchived", True),
                                              "timezone": ("updated", "2026-01-03T00:00:00")}[mutation]
                        value["record"]["task"][field] = replacement
                    return value
            with self.subTest(mutation=mutation):
                store, clock = AlteredCheckpoint(), FakeClock()
                tasks, unused = self.interrupted(store, clock)
                store.corrupt = True
                transport = ProjectTransport(tasks, clock)
                with self.assertRaises(CollectionError):
                    collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
                self.assertEqual(transport.detail_ids, [])


class ReconciliationTests(unittest.TestCase):
    def test_equal_candidates_share_the_current_map_within_retained_json_bound(self):
        import collection_batches
        clock = FakeClock()
        old, added = task(0, content="x" * 3000), task(1)
        api = ProjectTransport([old], clock, scans=lambda index: [old] if index == 1 else [old, added])
        peaks = []
        original = collection_batches._Coordinator._scan
        def observe_scan(instance):
            result = original(instance)
            # Count distinct retained candidate objects, even if their values match.
            objects = (list(instance.candidates.values()) + list(result[0].values())
                       + [value["candidate"] for value in instance.completed.values()])
            unique = {id(value): value for value in objects}
            values = list(unique.values()) + [value["record"] for value in instance.completed.values()]
            peaks.append(sum(len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()) for value in values))
            return result
        with patch.object(collection_batches._Coordinator, "_scan", observe_scan):
            result = collector(api, clock, batch_policy=replace(
                collection_batches.BatchPolicy(), max_retained_bytes=11000)).collect(
                    "fictional-project", day="2026-01-02")
        self.assertEqual(len(result["records"]), 2)
        self.assertLessEqual(max(peaks), 11000)

    def test_closing_changes_are_refreshed_until_a_complete_pass_matches(self):
        clock = FakeClock()
        old, changed, added = task(0), task(0, updated="2026-01-04T00:00:00Z"), task(1)
        transport = ProjectTransport([old], clock, scans=lambda index: [old] if index == 1 else [changed, added])
        result = collector(transport, clock).collect("fictional-project", day="2026-01-02")
        self.assertEqual(transport.scan_count, 3)
        self.assertEqual(transport.detail_ids, [old["id"], changed["id"], added["id"]])
        self.assertEqual([record["task"] for record in result["records"]], [changed, added])

    def test_continuing_change_blocks_after_three_checks_across_resume(self):
        from collection_batches import MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        transport = ProjectTransport([task(0)], clock,
                                     scans=lambda index: [task(0, content=f"version-{index}")])
        with self.assertRaisesRegex(CollectionError, "changed|reconciliation"):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(transport.scan_count, 4)
        resumed = ProjectTransport(transport.tasks, clock)
        with self.assertRaises(CollectionError):
            collector(resumed, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(resumed.requests, [])

    def test_detail_version_drift_is_relisted_and_never_cached_as_complete(self):
        from collection_batches import MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        old = task(0)
        changed = task(0, updated="2026-01-04T00:00:00Z")
        transport = ProjectTransport([old], clock, scans=lambda index: [old] if index == 1 else [changed])
        first_detail = [True]
        def transform(value, path, query):
            if path == DETAIL_TOOL and first_detail[0]:
                first_detail[0] = False
                value["result"] = changed
            return value
        transport.transform = transform
        result = collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(transport.detail_ids, [old["id"], old["id"]])
        self.assertEqual(transport.activity_pages, [(old["id"], 0)])
        self.assertEqual(result["records"][0]["task"], changed)


class BudgetTests(unittest.TestCase):
    def test_session_time_includes_checkpoint_start_work(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        clock = FakeClock()
        class SlowStart(MemoryCheckpoint):
            def start(self, *args, **kwargs):
                state = super().start(*args, **kwargs)
                clock.sleep(2)
                return state
        api = ProjectTransport([task(0)], clock)
        with self.assertRaises(CollectionError):
            collector(api, clock, checkpoint=SlowStart(),
                      batch_policy=replace(BatchPolicy(), max_seconds=1)).collect(
                          "fictional-project", day="2026-01-02")
        self.assertEqual(api.requests, [])

    def test_checkpoint_write_delay_preserves_actual_request_spacing(self):
        from collection_batches import MemoryCheckpoint
        clock = FakeClock()
        class SlowCheckpoint(MemoryCheckpoint):
            delayed = False
            def save_counters(self, counters):
                if not self.delayed and counters["requests"] == 1:
                    self.delayed = True
                    clock.sleep(1)
                super().save_counters(counters)
        api = ProjectTransport([task(0)], clock)
        collector(api, clock, checkpoint=SlowCheckpoint()).collect("fictional-project", day="2026-01-02")
        instants = [instant for path, query, instant in api.requests]
        self.assertTrue(all(right - left >= 0.25 for left, right in zip(instants, instants[1:])))

    def test_session_expiring_during_reservation_write_prevents_transport(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        clock = FakeClock()
        class SlowCheckpoint(MemoryCheckpoint):
            def save_counters(self, counters):
                if counters["requests"] == 1:
                    clock.sleep(2)
                super().save_counters(counters)
        api = ProjectTransport([task(0)], clock)
        with self.assertRaises(CollectionError):
            collector(api, clock, checkpoint=SlowCheckpoint(),
                      batch_policy=replace(BatchPolicy(), max_seconds=1)).collect(
                          "fictional-project", day="2026-01-02")
        self.assertEqual(api.requests, [])

    def test_interruption_during_read_retains_bounded_reservation_and_can_resume(self):
        from collection_batches import MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        api = ProjectTransport([task(0)], clock)
        class InterruptedRead(Response):
            def read(self, size=-1):
                super().read(size)
                raise KeyboardInterrupt
        def transport(request, timeout):
            response = api(request, timeout)
            body = response.getvalue()
            response.close()
            if json.loads(request.data).get("params", {}).get("name") == PROJECT_TOOL:
                return InterruptedRead(body)
            return Response(body, status=response.status)
        with self.assertRaises(KeyboardInterrupt):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(store.counters["requests"], 4)
        self.assertEqual(store.counters["records"], 100)
        handshake_bytes = len(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fictional", "version": "1"}}}).encode()) + len(
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": catalog()}).encode())
        self.assertEqual(store.counters["response_bytes"], handshake_bytes + 8 * 1024 * 1024 + 1)
        result = collector(api, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertTrue(result["pagination_complete"])
        self.assertEqual(result["collection_stats"]["requests"], 11)
        self.assertEqual(result["collection_stats"]["records"], 104)

    def test_remaining_row_budget_reduces_project_and_activity_page_sizes(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        current = task(0)
        identity = current["id"]
        transport = ProjectTransport([current], clock, histories={
            identity: [[event(identity, 0), event(identity, 1)]]})
        with self.assertRaises(CollectionError):
            collector(transport, clock, checkpoint=store,
                      batch_policy=replace(BatchPolicy(), max_records=4)).collect(
                          "fictional-project", day="2026-01-02")
        business = [(name, params) for name, params, _ in transport.requests if name in (PROJECT_TOOL, DETAIL_TOOL, ACTIVITY_TOOL)]
        self.assertEqual(business[0], (PROJECT_TOOL, {"projectId": "fictional-project", "pageSize": 4,
                         "includeArchived": False, "enrichFields": "tfs,members",
                         "tql": "updated >= '2026-01-02T00:00:00+08:00'"}))
        self.assertEqual(business[2][0], ACTIVITY_TOOL)
        self.assertEqual(business[2][1]["pageSize"], 2)
        self.assertEqual(len(transport.requests), 6)
        self.assertEqual(store.counters["records"], 4)

    def test_remaining_byte_budget_reduces_read_and_rejects_a_truncated_prefix(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        api = ProjectTransport([task(0)], clock)
        reads = []
        bodies = []
        class LimitedResponse(Response):
            def read(self, size=-1):
                reads.append((len(bodies) - 1, size))
                return super().read(size)
        def transport(request, timeout):
            response = api(request, timeout)
            body = response.getvalue()
            response.close()
            bodies.append(body)
            return LimitedResponse(body, status=response.status)
        with self.assertRaisesRegex(CollectionError, "response|Response"):
            collector(transport, clock, checkpoint=store,
                      batch_policy=replace(BatchPolicy(), max_response_bytes=512)).collect(
                          "fictional-project", day="2026-01-02")
        first_reads = {index: next(size for item, size in reads if item == index) for index in range(len(bodies))}
        self.assertEqual(first_reads, {index: 512 - sum(map(len, bodies[:index])) for index in range(len(bodies))})
        self.assertEqual([name for name, _, _ in api.requests],
                         ["initialize", "notifications/initialized", "tools/list"])
        self.assertGreater(len(bodies[-1]), first_reads[len(bodies) - 1])
        self.assertEqual(store.counters["response_bytes"], 512)
        self.assertIsNone(store.load_task(task(0)["id"]))

    def test_server_returning_more_rows_than_requested_blocks_without_truncation(self):
        from collection_batches import BatchPolicy
        clock = FakeClock()
        api = ProjectTransport([task(0)], clock)
        def transform(value, path, query):
            if path == PROJECT_TOOL:
                value["result"] = [task(0), task(1)]
            return value
        api.transform = transform
        with self.assertRaisesRegex(CollectionError, "row count|record|invalid"):
            collector(api, clock, batch_policy=replace(BatchPolicy(), max_records=1)).collect(
                "fictional-project", day="2026-01-02")
        self.assertEqual(len(api.requests), 4)
        self.assertEqual(api.detail_ids, [])

    def test_response_exit_interruption_preserves_uncertain_reservations(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        api = ProjectTransport([task(0)], clock)
        received = []
        class InterruptedResponse(Response):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise KeyboardInterrupt
        def transport(request, timeout):
            response = api(request, timeout)
            body = response.getvalue()
            response.close()
            is_business = json.loads(request.data).get("params", {}).get("name") == PROJECT_TOOL
            received.append((body, is_business))
            if is_business:
                return InterruptedResponse(body)
            return Response(body, status=response.status)
        options = {"checkpoint": store, "batch_policy": replace(BatchPolicy(), max_records=4)}
        for unused in range(3):
            with self.assertRaises(KeyboardInterrupt):
                collector(transport, clock, **options).collect("fictional-project", day="2026-01-02", page_size=1)
        self.assertEqual(store.counters["requests"], 12)
        self.assertEqual(store.counters["records"], 3)
        self.assertEqual(store.counters["response_bytes"],
                         3 * (8 * 1024 * 1024 + 1) + sum(len(body) for body, business in received if not business))
        with self.assertRaises(CollectionError):
            collector(api, clock, **options).collect("fictional-project", day="2026-01-02", page_size=1)
        self.assertEqual(api.detail_ids, [])

    def test_local_request_exhaustion_rotates_once_and_restarts_partial_task(self):
        clock = FakeClock()
        tasks = [task(0), task(1), task(2)]
        transport = ProjectTransport(tasks, clock)
        result = collector(transport, clock, max_requests=7).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(transport.detail_ids.count(tasks[1]["id"]), 2)
        self.assertEqual(result["collection_stats"]["requests"], len(transport.requests))
        self.assertGreater(result["collection_stats"]["batches"], 1)

    def test_single_task_larger_than_a_batch_blocks_after_one_retry(self):
        from collection_batches import MemoryCheckpoint
        for limit in ("requests", "records", "bytes", "time"):
            with self.subTest(limit=limit):
                store, clock = MemoryCheckpoint(), FakeClock()
                current = task(0)
                identity = current["id"]
                if limit == "requests":
                    history = [[event(identity, index)] for index in range(3)]
                    options = {"max_requests": 6}
                elif limit == "records":
                    # The fictional service respects a reduced one-row page;
                    # each batch can read pages but cannot finish the history.
                    history = [[event(identity, index)] for index in range(4)]
                    options = {"max_records": 3}
                elif limit == "bytes":
                    history = [[event(identity, 0, content="x" * 10000)]]
                    options = {"max_response_bytes": 4096}
                else:
                    history = [[event(identity, index)] for index in range(5)]
                    options = {"max_seconds": 2}
                transport = ProjectTransport([current], clock, histories={identity: history})
                with self.assertRaises(CollectionError):
                    collector(transport, clock, checkpoint=store, **options).collect("fictional-project", day="2026-01-02")
                self.assertEqual(transport.detail_ids, [identity, identity])
                self.assertIsNone(store.load_task(identity))

    def test_global_request_limit_is_never_reset_by_batches_or_resume(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        transport = ProjectTransport([task(0), task(1)], clock)
        options = {"checkpoint": store, "max_tasks": 1, "batch_policy": replace(BatchPolicy(), max_requests=11)}
        with self.assertRaisesRegex(CollectionError, "session.*request|request.*session"):
            collector(transport, clock, **options).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(transport.requests), 11)
        self.assertEqual(store.counters["batches"], 2)
        self.assertTrue(all(store.load_task(task(index)["id"]) is not None for index in range(2)))
        resumed = ProjectTransport([task(0), task(1)], clock)
        with self.assertRaises(CollectionError):
            collector(resumed, clock, **options).collect("fictional-project", day="2026-01-02")
        self.assertEqual(resumed.requests, [])

    def test_global_returned_rows_include_detail_and_duplicate_rows(self):
        from collection_batches import BatchPolicy
        for duplicate in (False, True):
            clock = FakeClock()
            current = task(0)
            transport = ProjectTransport([current, current] if duplicate else [current], clock)
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(CollectionError, "session.*record|record.*session|invalid"):
                collector(transport, clock, batch_policy=replace(BatchPolicy(), max_records=1 if duplicate else 2)).collect(
                    "fictional-project", day="2026-01-02")
            self.assertEqual(len(transport.requests), 4 if duplicate else 5)
            self.assertEqual(transport.activity_pages, [])

    def test_global_bytes_retained_json_and_time_block_without_partial_cache(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        for limit, policy in (("bytes", replace(BatchPolicy(), max_response_bytes=128)),
                              ("retained", replace(BatchPolicy(), max_retained_bytes=1000)),
                              ("time", replace(BatchPolicy(), max_seconds=1))):
            with self.subTest(limit=limit):
                store, clock = MemoryCheckpoint(), FakeClock()
                current = task(0)
                identity = current["id"]
                history = [[event(identity, index, content="x" * 300)] for index in range(8)]
                transport = ProjectTransport([current], clock, histories={identity: history})
                with self.assertRaises(CollectionError):
                    collector(transport, clock, checkpoint=store, batch_policy=policy).collect(
                        "fictional-project", day="2026-01-02")
                self.assertIsNone(store.load_task(identity))
                self.assertLess(len(transport.activity_pages), 8)

    def test_checkpoint_age_counts_toward_session_time(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        transport = ProjectTransport([task(0), task(1)], clock)
        transport.interrupt_on_detail = task(1)["id"]
        options = {"checkpoint": store, "batch_policy": replace(BatchPolicy(), max_seconds=1)}
        with patch("collection_batches.time.time", return_value=1000), self.assertRaises(CollectionError):
            collector(transport, clock, **options).collect("fictional-project", day="2026-01-02")
        resumed = ProjectTransport([task(0), task(1)], clock)
        with patch("collection_batches.time.time", return_value=1002), self.assertRaises(CollectionError):
            collector(resumed, clock, **options).collect("fictional-project", day="2026-01-02")
        self.assertEqual(resumed.requests, [])

    def test_transport_retries_are_charged_as_attempts(self):
        from collection_batches import BatchPolicy
        clock = FakeClock()
        api = ProjectTransport([task(0)], clock)
        attempts = []
        def transport(request, timeout):
            attempts.append(request)
            if json.loads(request.data).get("params", {}).get("name") == DETAIL_TOOL:
                raise HTTPError("https://open.teambition.com/api/mcp/v2", 503,
                                "fictional-private-retry", {}, None)
            return api(request, timeout)
        with self.assertRaises(CollectionError):
            collector(transport, clock, batch_policy=replace(BatchPolicy(), max_requests=6)).collect(
                "fictional-project", day="2026-01-02")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(sum(json.loads(request.data).get("params", {}).get("name") == DETAIL_TOOL
                             for request in attempts), 2)


class FailureTests(unittest.TestCase):
    def test_incomplete_or_non_numeric_persisted_counters_fail_before_any_request(self):
        from collection_batches import MemoryCheckpoint
        for bad in (None, {}, {"requests": 1},
                    {"requests": 0, "records": 0, "response_bytes": 0, "retained_bytes": 0,
                     "batches": 0, "passes": 0, "credential_identity": "fictional-private"}):
            class InvalidCounters(MemoryCheckpoint):
                def start(self, *args, **kwargs):
                    state = super().start(*args, **kwargs)
                    state["counters"] = bad
                    return state
            clock = FakeClock()
            transport = ProjectTransport([task(0)], clock)
            with self.subTest(counters=bad), self.assertRaises(CollectionError):
                collector(transport, clock, checkpoint=InvalidCounters()).collect("fictional-project", day="2026-01-02")
            self.assertEqual(transport.requests, [])

    def test_counter_persistence_failure_prevents_the_network_attempt(self):
        from collection_batches import MemoryCheckpoint
        class BrokenCounters(MemoryCheckpoint):
            def save_counters(self, counters):
                if counters["requests"] == 2:
                    raise RuntimeError("fictional-private-secret")
                super().save_counters(counters)
        clock = FakeClock()
        transport = ProjectTransport([task(0)], clock)
        with self.assertRaises(CollectionError) as caught:
            collector(transport, clock, checkpoint=BrokenCounters()).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("private", str(caught.exception))

    def test_future_activity_is_rejected_before_checkpointing(self):
        from collection_batches import MemoryCheckpoint
        clock, store = FakeClock(), MemoryCheckpoint()
        current = task(0)
        identity = current["id"]
        transport = ProjectTransport([current], clock, histories={
            identity: [[event(identity, createTime="2999-01-01T00:00:00Z")]]})
        with self.assertRaisesRegex(CollectionError, "Activity timestamp exceeds collection time"):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertIsNone(store.load_task(identity))

    def test_nonterminating_cursors_incomplete_pages_and_activity_page_limit_block(self):
        from collection_batches import MemoryCheckpoint
        for failure in ("project-cycle", "activity-cycle", "missing-terminal", "pages"):
            with self.subTest(failure=failure):
                store, clock = MemoryCheckpoint(), FakeClock()
                current = task(0)
                identity = current["id"]
                histories = {identity: [[event(identity, index)] for index in range(4)]}
                transport = ProjectTransport([current], clock, histories=histories)
                def transform(value, path, query):
                    if failure == "project-cycle" and path == PROJECT_TOOL:
                        value["nextPageToken"] = "0"
                    if failure == "activity-cycle" and path == ACTIVITY_TOOL:
                        value["nextPageToken"] = "0"
                    if failure == "missing-terminal" and path == ACTIVITY_TOOL:
                        del value["nextPageToken"]
                    return value
                transport.transform = transform
                with self.assertRaises(CollectionError):
                    collector(transport, clock, checkpoint=store, max_pages=3).collect(
                        "fictional-project", day="2026-01-02")
                self.assertIsNone(store.load_task(identity))

    def test_checkpoint_errors_are_redacted_and_close_is_attempted(self):
        class BrokenCheckpoint:
            closed = False
            def start(self, *args, **kwargs):
                raise RuntimeError("fictional-private-secret https://private.invalid")
            def close(self):
                self.closed = True
        clock, store = FakeClock(), BrokenCheckpoint()
        transport = ProjectTransport([task(0)], clock)
        with self.assertRaises(CollectionError) as caught:
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(store.closed)
        self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
