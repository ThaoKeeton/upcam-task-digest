"""Synthetic batch collection; credentials and transport never leave this process."""

import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from teambition_readonly import Client, CollectionError


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response(io.BytesIO):
    status = 200


def task(index, **changes):
    return dict({"id": f"fictional-task-{index:05d}", "projectId": "fictional-project",
                 "isArchived": False, "updated": "2026-01-03T10:00:00+08:00",
                 "content": f"Task {index}", "rawDetail": {"kept": True}}, **changes)


def event(task_id, index=0, **changes):
    return dict({"id": f"{task_id}-event-{index}", "boundToObjectId": task_id,
                 "boundToObjectType": "task", "createTime": "2026-01-02T10:00:00+08:00",
                 "rawActivity": {"kept": True}}, **changes)


class ProjectTransport:
    """A paginated fictional API whose observable source can change per scan."""

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

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        params = parse_qs(url.query)
        self.requests.append((url.path, params, self.clock()))
        if url.path == "/api/v3/project/fictional-project/task/query":
            if "pageToken" not in params:
                self.scan_count += 1
                if self.scans is not None:
                    self.tasks = copy.deepcopy(self.scans(min(self.scan_count, 100)))
            offset = int(params.get("pageToken", ["0"])[0])
            size = int(params["pageSize"][0])
            rows = self.tasks[offset:offset + size]
            next_token = str(offset + size) if offset + size < len(self.tasks) else ""
            result = {"code": 200, "result": rows, "nextPageToken": next_token}
        elif url.path == "/api/v3/task/query":
            task_id = params["taskId"][0]
            self.detail_ids.append(task_id)
            if self.interrupt_on_detail == task_id:
                self.interrupt_on_detail = None
                raise CollectionError("Synthetic interrupted collection")
            detail = next(row for row in self.tasks if row["id"] == task_id)
            result = {"code": 200, "result": copy.deepcopy(detail)}
        else:
            task_id = url.path.split("/")[-3]
            page_index = int(params.get("pageToken", ["0"])[0])
            self.activity_pages.append((task_id, page_index))
            if self.fail_activity == (task_id, page_index):
                self.fail_activity = None
                raise CollectionError("Synthetic interrupted collection")
            pages = self.histories.get(task_id, [[event(task_id)]])
            result = {"code": 200, "result": copy.deepcopy(pages[page_index]),
                      "nextPageToken": str(page_index + 1) if page_index + 1 < len(pages) else ""}
        if self.transform:
            result = self.transform(result, url.path, params)
        return Response(json.dumps(result).encode("utf-8"))


def collector(transport, clock, **changes):
    return Client("fictional-tenant", transport=transport, clock=clock, sleep=clock.sleep,
                  credentials=lambda: ("fictional-app", "fictional-secret"),
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
                    self.assertEqual(len(transport.requests), 100)
                else:
                    result = collector(transport, clock).collect("fictional-project", day="2026-01-02")
                    self.assertEqual(len(result["records"]), 10000)
                    self.assertEqual(result["collection_stats"]["requests"], 20200)
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
                self.assertEqual(source["collection_stats"]["requests"], 2 * count + 2 * math.ceil(count / 100))
                self.assertTrue(all(type(value) is int for value in source["collection_stats"].values()))
                self.assertFalse(source["selection"]["snapshot_guaranteed"])
                self.assertFalse(source["selection"]["historical_event_coverage"])
                for path, query, unused in transport.requests:
                    self.assertNotIn("q", query)
                    if "/project/" in path:
                        self.assertEqual(query["includeArchived"], ["false"])
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
            self.assertNotIn("q", query)
            if "/project/" in path:
                self.assertEqual(query["includeArchived"], ["true"])

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
                    reader.credentials = lambda: ("another-fictional-app", "another-fictional-secret")
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
            if path == "/api/v3/task/query" and first_detail[0]:
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
            return InterruptedRead(body)
        with self.assertRaises(KeyboardInterrupt):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertEqual(store.counters["requests"], 1)
        self.assertEqual(store.counters["records"], 100)
        self.assertEqual(store.counters["response_bytes"], 8 * 1024 * 1024 + 1)
        result = collector(api, clock, checkpoint=store).collect("fictional-project", day="2026-01-02")
        self.assertTrue(result["pagination_complete"])
        self.assertEqual(result["collection_stats"]["requests"], 5)
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
        self.assertEqual(transport.requests[0][1]["pageSize"], ["4"])
        self.assertEqual(transport.requests[2][1]["pageSize"], ["2"])
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(store.counters["records"], 4)

    def test_remaining_byte_budget_reduces_read_and_rejects_a_truncated_prefix(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        api = ProjectTransport([task(0)], clock)
        reads = []
        bodies = []
        class LimitedResponse(Response):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        def transport(request, timeout):
            response = api(request, timeout)
            body = response.getvalue()
            response.close()
            bodies.append(body)
            return LimitedResponse(body)
        with self.assertRaisesRegex(CollectionError, "Response size limit"):
            collector(transport, clock, checkpoint=store,
                      batch_policy=replace(BatchPolicy(), max_response_bytes=512)).collect(
                          "fictional-project", day="2026-01-02")
        self.assertEqual(reads[0], 512)
        self.assertEqual(reads[1], 512 - len(bodies[0]))
        self.assertEqual(reads[2], 512 - len(bodies[0]) - len(bodies[1]))
        self.assertEqual(store.counters["response_bytes"], 512)
        self.assertIsNone(store.load_task(task(0)["id"]))

    def test_server_returning_more_rows_than_requested_blocks_without_truncation(self):
        from collection_batches import BatchPolicy
        clock = FakeClock()
        api = ProjectTransport([task(0)], clock)
        def transform(value, path, query):
            if "/project/" in path:
                value["result"] = [task(0), task(1)]
            return value
        api.transform = transform
        with self.assertRaisesRegex(CollectionError, "row count|record"):
            collector(api, clock, batch_policy=replace(BatchPolicy(), max_records=1)).collect(
                "fictional-project", day="2026-01-02")
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(api.detail_ids, [])

    def test_response_exit_interruption_preserves_received_rows_and_bytes(self):
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
            received.append(body)
            return InterruptedResponse(body)
        options = {"checkpoint": store, "batch_policy": replace(BatchPolicy(), max_records=4)}
        for unused in range(3):
            with self.assertRaises(KeyboardInterrupt):
                collector(transport, clock, **options).collect("fictional-project", day="2026-01-02")
        self.assertEqual(store.counters["requests"], 3)
        self.assertEqual(store.counters["records"], 3)
        self.assertEqual(store.counters["response_bytes"], sum(map(len, received)))
        with self.assertRaises(CollectionError):
            collector(api, clock, **options).collect("fictional-project", day="2026-01-02")

    def test_local_request_exhaustion_rotates_once_and_restarts_partial_task(self):
        clock = FakeClock()
        tasks = [task(0), task(1), task(2)]
        transport = ProjectTransport(tasks, clock)
        result = collector(transport, clock, max_requests=4).collect("fictional-project", day="2026-01-02")
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
                    options = {"max_requests": 3}
                elif limit == "records":
                    history = [[event(identity, index) for index in range(4)]]
                    options = {"max_records": 3}
                elif limit == "bytes":
                    history = [[event(identity, 0, content="x" * 2000)]]
                    options = {"max_response_bytes": 1024}
                else:
                    history = [[event(identity, index)] for index in range(5)]
                    options = {"max_seconds": 1}
                transport = ProjectTransport([current], clock, histories={identity: history})
                with self.assertRaises(CollectionError):
                    collector(transport, clock, checkpoint=store, **options).collect("fictional-project", day="2026-01-02")
                self.assertEqual(transport.detail_ids, [identity, identity])
                self.assertIsNone(store.load_task(identity))

    def test_global_request_limit_is_never_reset_by_batches_or_resume(self):
        from collection_batches import BatchPolicy, MemoryCheckpoint
        store, clock = MemoryCheckpoint(), FakeClock()
        transport = ProjectTransport([task(0), task(1)], clock)
        options = {"checkpoint": store, "max_tasks": 1, "batch_policy": replace(BatchPolicy(), max_requests=5)}
        with self.assertRaisesRegex(CollectionError, "session.*request|request.*session"):
            collector(transport, clock, **options).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(transport.requests), 5)
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
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(CollectionError, "session.*record|record.*session"):
                collector(transport, clock, batch_policy=replace(BatchPolicy(), max_records=1 if duplicate else 2)).collect(
                    "fictional-project", day="2026-01-02")
            self.assertEqual(len(transport.requests), 1 if duplicate else 2)
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
            if urlsplit(request.full_url).path == "/api/v3/task/query":
                raise URLError("fictional-private-retry")
            return api(request, timeout)
        with self.assertRaises(CollectionError):
            collector(transport, clock, batch_policy=replace(BatchPolicy(), max_requests=2)).collect(
                "fictional-project", day="2026-01-02")
        self.assertEqual(len(attempts), 2)


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

    def test_cyclic_tokens_incomplete_pages_and_activity_page_limit_block(self):
        from collection_batches import MemoryCheckpoint
        for failure in ("project-cycle", "activity-cycle", "missing-terminal", "pages"):
            with self.subTest(failure=failure):
                store, clock = MemoryCheckpoint(), FakeClock()
                current = task(0)
                identity = current["id"]
                histories = {identity: [[event(identity, index)] for index in range(4)]}
                transport = ProjectTransport([current], clock, histories=histories)
                def transform(value, path, query):
                    if failure == "project-cycle" and "/project/" in path:
                        value["nextPageToken"] = "0"
                    if failure == "activity-cycle" and path.endswith("activity/list"):
                        value["nextPageToken"] = "0"
                    if failure == "missing-terminal" and path.endswith("activity/list"):
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
