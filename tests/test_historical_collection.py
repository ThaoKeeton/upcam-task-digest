"""Historical-window regressions with fictional task and activity records."""

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import unittest

from digest_pipeline import build_digest
from report_contract import updated_selection
from teambition_readonly import CollectionError
from test_collection import FakeTransport, client, page


FIXTURES = Path(__file__).parent / "fixtures"


class HistoricalCollectionTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((FIXTURES / "collection.json").read_text())["records"][0]
        self.config = json.loads((FIXTURES / "config.json").read_text())
        self.config["api_contract_verified"] = True
        self.config["view"] = {"verified": True, "evidence": "synthetic-test-only", "task_equals": {}}
        self.day = "2026-01-02"

    def transport(self, task, activities=()):
        return FakeTransport(page([task]), {"code": 200, "result": task},
                             page(activities), page([task]))

    def test_task_updated_after_report_end_still_collects_window_activity(self):
        task = dict(self.record["task"], updated="2026-01-05T12:00:00+08:00")
        transport = self.transport(task, self.record["activities"][:2])
        collection = client(transport).collect("demo-project", day=self.day)
        report = build_digest(collection, self.config, self.day)
        self.assertEqual(len(report["digest"]["tasks"]), 1)
        self.assertEqual(report["audit"][0]["event_counts"], {"comment": 2})
        for request in (transport.requests[0], transport.requests[-1]):
            self.assertNotIn("q", parse_qs(urlsplit(request.full_url).query))
        self.assertEqual(collection["selection"]["basis"], "activity-createTime")
        self.assertFalse(collection["selection"]["historical_event_coverage"])
        self.assertFalse(collection["selection"]["snapshot_guaranteed"])

    def test_activity_milliseconds_after_task_updated_is_not_excluded(self):
        task = dict(self.record["task"], updated="2026-01-01T23:59:59.990+08:00")
        event = {"id": "fictional-skew-event", "action": "create", "content": "",
                 "createTime": "2026-01-02T00:00:00.108+08:00",
                 "boundToObjectId": task["id"], "boundToObjectType": "task"}
        collection = client(self.transport(task, [event])).collect("demo-project", day=self.day)
        self.assertEqual(build_digest(collection, self.config, self.day)["audit"][0]["event_ids"],
                         [event["id"]])

    def test_inactive_old_project_task_does_not_become_empty_evidence_failure(self):
        task = dict(self.record["task"], updated="2020-01-01T00:00:00Z")
        collection = client(self.transport(task)).collect("demo-project", day=self.day)
        report = build_digest(collection, self.config, self.day)
        self.assertEqual(report["digest"]["tasks"], [])
        self.assertTrue(report["ready_to_send"])

    def test_closing_project_scan_rejects_added_removed_or_changed_tasks(self):
        task = self.record["task"]
        for closing in ([], [dict(task, updated="2026-01-05T12:00:00Z")],
                        [task, dict(task, id="fictional-added-task")]):
            with self.subTest(size=len(closing)):
                transport = FakeTransport(page([task]), {"code": 200, "result": task},
                                          page(), page(closing))
                with self.assertRaisesRegex(CollectionError, "changed during collection"):
                    client(transport).collect("demo-project", day=self.day)

    def test_empty_project_is_rechecked_and_incomplete_closing_read_blocks(self):
        transport = FakeTransport(page(), page())
        collection = client(transport).collect("demo-project", day=self.day)
        self.assertEqual(collection["records"], [])
        self.assertEqual(len(transport.requests), 2)
        with self.assertRaises(CollectionError):
            client(FakeTransport(page(), page([], "cycle"), page([], "cycle"))).collect(
                "demo-project", day=self.day)

    def test_activity_pages_are_exhausted_without_time_or_order_shortcut(self):
        task = dict(self.record["task"], updated="2026-01-05T12:00:00+08:00")
        events = copy.deepcopy(self.record["activities"][:2])
        old = dict(events[0], id="fictional-old-event", createTime="2020-01-01T00:00:00Z")
        transport = FakeTransport(page([task]), {"code": 200, "result": task},
                                  page([old], "next-history"), page(events), page([task]))
        result = client(transport).collect("demo-project", day=self.day)
        self.assertEqual(len(result["records"][0]["activities"]), 3)
        self.assertEqual(build_digest(result, self.config, self.day)["audit"][0]["event_counts"],
                         {"comment": 2})
        self.assertEqual(result["selection"], updated_selection(self.day))


if __name__ == "__main__":
    unittest.main()
