"""Synthetic updated candidates: fewer histories, explicit completeness boundary."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from collection_batches import BatchPolicy, MemoryCheckpoint
from digest_pipeline import build_digest
from report_contract import SHANGHAI, updated_selection, validated_report_source
from teambition_readonly import CollectionError
from test_batched_collection import FakeClock, ProjectTransport, collector, event, task, PROJECT_TOOL
from test_collection import FakeTransport, client, page, FIXTURES
import json


DAY = "2026-01-02"
START = datetime(2026, 1, 2, tzinfo=SHANGHAI)


class CandidatePrefilterTests(unittest.TestCase):
    def test_default_skips_old_histories_and_keeps_after_end_updates(self):
        clock = FakeClock()
        tasks = [task(i, updated="2020-01-01T00:00:00Z") for i in range(1200)]
        for row in tasks[-12:]:
            row["updated"] = "2026-01-05T12:00:00+08:00"
        transport = ProjectTransport(tasks, clock)
        source = collector(transport, clock).collect("fictional-project", day=DAY)
        self.assertEqual(transport.detail_ids, [row["id"] for row in tasks[-12:]])
        self.assertEqual(len(source["records"]), 12)
        self.assertEqual(source["collection_stats"]["requests"], 29)
        self.assertEqual(source["collection_stats"]["project_tasks_listed"], 12)
        self.assertEqual(source["selection"]["candidate_scope"], "current-visible-project-tasks-updated-since")
        self.assertEqual(source["selection"]["candidate_updated_since"], "2026-01-02T00:00:00+08:00")
        self.assertEqual(source["selection"]["candidate_overlap_seconds"], 0)
        self.assertEqual(source["selection"]["q"], "updated >= '2026-01-02T00:00:00+08:00'")
        self.assertFalse(source["selection"]["activity_updated_index_guaranteed"])
        self.assertFalse(source["selection"]["historical_event_coverage"])
        self.assertFalse(source["selection"]["snapshot_guaranteed"])
        self.assertTrue(all(params.get("tql") == source["selection"]["q"]
                            for operation, params, _ in transport.requests if operation == PROJECT_TOOL))

    def test_window_start_cutoff_is_inclusive_and_has_no_upper_limit(self):
        cutoff = START
        updates = [cutoff - timedelta(microseconds=1), cutoff, cutoff.astimezone(timezone.utc),
                   START + timedelta(milliseconds=10), START,
                   START + timedelta(days=10)]
        clock = FakeClock()
        tasks = [task(i, updated=instant.isoformat()) for i, instant in enumerate(updates)]
        transport = ProjectTransport(tasks, clock)
        source = collector(transport, clock).collect("fictional-project", day=DAY)
        self.assertEqual([record["task"]["id"] for record in source["records"]],
                         [row["id"] for row in tasks[1:]])

    def test_nonbatched_default_filters_before_detail_and_activity_requests(self):
        base = json.loads((FIXTURES / "collection.json").read_text())["records"][0]["task"]
        old = dict(base, id="synthetic-old", updated="2020-01-01T00:00:00Z")
        recent = dict(base, updated="2026-01-05T00:00:00+08:00")
        transport = FakeTransport(page([recent]), {"code": 200, "result": recent},
                                  page(), page([recent]))
        source = client(transport).collect("demo-project", day=DAY)
        self.assertEqual([record["task"]["id"] for record in source["records"]], [base["id"]])
        self.assertEqual(len(transport.requests), 4)

    def test_full_project_audit_can_find_activity_without_a_recent_task_timestamp(self):
        clock = FakeClock()
        old = task(0, updated="2020-01-01T00:00:00Z")
        transport = ProjectTransport([old], clock)
        source = collector(transport, clock).collect("fictional-project", day=DAY,
                                                    candidate_mode="full_project")
        self.assertEqual(source["records"][0]["activities"], [event(old["id"])])
        self.assertEqual(source["selection"], updated_selection(DAY, candidate_mode="full_project"))
        self.assertEqual(source["selection"]["candidate_scope"], "current-visible-project-tasks")
        self.assertFalse(source["selection"]["historical_event_coverage"])

    def test_candidate_optimization_does_not_claim_to_cover_unadvanced_updated(self):
        clock = FakeClock()
        old = task(0, updated="2020-01-01T00:00:00Z")
        transport = ProjectTransport([old], clock)
        source = collector(transport, clock).collect("fictional-project", day=DAY)
        self.assertEqual(source["records"], [])
        self.assertEqual(transport.activity_pages, [])
        self.assertFalse(source["selection"]["activity_updated_index_guaranteed"])
        self.assertEqual(source["collection_stats"]["requests"], 5)

    def test_invalid_updated_or_scope_is_rejected_even_before_candidate_filter(self):
        for changes in ({"updated": "invalid"}, {"updated": None},
                        {"updated": "2020-01-01T00:00:00"},
                        {"projectId": "other", "updated": "2026-01-02T00:00:00Z"},
                        {"isArchived": True, "updated": "2026-01-02T00:00:00Z"}):
            with self.subTest(changes=changes):
                clock = FakeClock()
                transport = ProjectTransport([task(0, **changes)], clock)
                with self.assertRaises(CollectionError):
                    collector(transport, clock).collect("fictional-project", day=DAY)
                self.assertEqual(transport.detail_ids, [])

    def test_closing_scan_discovers_newly_updated_candidate(self):
        clock = FakeClock()
        old = task(0, updated="2020-01-01T00:00:00Z")
        recent = dict(old, updated="2026-01-06T00:00:00Z")
        transport = ProjectTransport([old], clock, scans=lambda n: [old] if n == 1 else [recent])
        source = collector(transport, clock).collect("fictional-project", day=DAY)
        self.assertEqual([record["task"] for record in source["records"]], [recent])
        self.assertEqual(transport.detail_ids, [recent["id"]])
        self.assertEqual(transport.scan_count, 3)

    def test_cache_from_other_candidate_mode_is_not_reused(self):
        clock = FakeClock()
        rows = [task(0), task(1)]
        transport = ProjectTransport(rows, clock)
        store = MemoryCheckpoint()
        transport.interrupt_on_detail = rows[1]["id"]
        with self.assertRaises(CollectionError):
            collector(transport, clock, checkpoint=store).collect("fictional-project", day=DAY)
        self.assertIn(rows[0]["id"], store.tasks)
        transport.detail_ids.clear()
        source = collector(transport, clock, checkpoint=store).collect(
            "fictional-project", day=DAY, candidate_mode="full_project")
        self.assertEqual(transport.detail_ids, [row["id"] for row in rows])
        self.assertEqual(source["collection_stats"]["resumed_tasks"], 0)

    def test_candidate_mode_validation_happens_before_any_request(self):
        for mode in (None, False, "unknown", "updated_window"):
            transport = FakeTransport()
            with self.subTest(mode=mode), self.assertRaises(CollectionError):
                client(transport).collect("demo-project", day=DAY, candidate_mode=mode)
            self.assertEqual(transport.requests, [])

    def test_excluded_rows_do_not_bypass_listing_limits_or_pagination_checks(self):
        old = task(0, updated="2020-01-01T00:00:00Z")
        for policy in (replace(BatchPolicy(), max_retained_bytes=100),
                       replace(BatchPolicy(), max_project_tasks=1)):
            clock = FakeClock()
            rows = [old, dict(old, id="synthetic-other-old")]
            transport = ProjectTransport(rows, clock)
            with self.subTest(policy=policy), self.assertRaises(CollectionError):
                collector(transport, clock, batch_policy=policy).collect(
                    "fictional-project", day=DAY, candidate_mode="full_project")
            self.assertEqual(transport.detail_ids, [])
        base = json.loads((FIXTURES / "collection.json").read_text())["records"][0]["task"]
        old = dict(base, updated="2020-01-01T00:00:00Z")
        transport = FakeTransport(page([old], "cycle"), page([], "cycle"))
        with self.assertRaisesRegex(CollectionError, "Pagination limit exceeded"):
            client(transport, max_pages=2).collect("demo-project", day=DAY, candidate_mode="full_project")
        self.assertEqual(len(transport.requests), 2)

    def test_candidate_query_does_not_change_the_actual_half_open_report_window(self):
        record = json.loads((FIXTURES / "collection.json").read_text())["records"][0]
        row = dict(record["task"], updated="2026-01-05T00:00:00+08:00")
        times = ["2026-01-01T23:59:59.999+08:00", "2026-01-02T00:00:00+08:00",
                 "2026-01-02T23:59:59.999+08:00", "2026-01-03T00:00:00+08:00"]
        events = [{"id": f"synthetic-window-{i}", "action": "create", "content": "",
                   "boundToObjectId": row["id"], "boundToObjectType": "task", "createTime": instant}
                  for i, instant in enumerate(times)]
        transport = FakeTransport(page([row]), {"code": 200, "result": row},
                                  page(events[:1], "history-next"), page(events[1:]), page([row]))
        source = client(transport).collect("demo-project", day=DAY)
        self.assertEqual(source["records"][0]["activities"], events)
        config = json.loads((FIXTURES / "config.json").read_text())
        config["api_contract_verified"] = True
        config["view"].update(verified=True, evidence="synthetic-candidate-window")
        report = build_digest(source, config, DAY)
        self.assertEqual(report["audit"][0]["event_ids"], ["synthetic-window-1", "synthetic-window-2"])

    def test_pipeline_accepts_historical_full_scan_contract_and_new_candidates(self):
        config = json.loads((FIXTURES / "config.json").read_text())
        config["api_contract_verified"] = True
        config["view"].update(verified=True, evidence="synthetic-candidate-test")
        for mode in ("updated_since_start", "full_project"):
            collection = json.loads((FIXTURES / "collection.json").read_text())
            collection["selection"] = updated_selection(DAY, candidate_mode=mode)
            report = build_digest(collection, config, DAY)
            self.assertEqual(validated_report_source(report), ("demo-org", "demo-project", DAY))
            changed = deepcopy(collection)
            changed["selection"]["candidate_updated_since"] = "2020-01-01T00:00:00Z"
            with self.subTest(mode=mode), self.assertRaises(CollectionError):
                build_digest(changed, config, DAY)


if __name__ == "__main__":
    unittest.main()
