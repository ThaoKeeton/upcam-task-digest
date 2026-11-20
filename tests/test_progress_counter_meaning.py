"""Fictional full-project scans must not become report-window update counts."""

import unittest

from collection_progress import observe_collection
from digest_pipeline import build_digest
from test_batched_collection import FakeClock, ProjectTransport, collector, event, task


class ProgressCounterMeaningTests(unittest.TestCase):
    def test_listing_1000_candidates_can_end_as_one_window_update(self):
        # Filtering task.updated by the report end would miss the last task;
        # counting the scanned records as updates would add 1200 false items.
        tasks = [task(index, updated="2026-01-03T10:00:00+08:00", uniqueId=index + 1,
                      executorId="fictional-owner", tfsId="fictional-open", isDone=False,
                      project={"_id": "fictional-project", "uniqueIdPrefix": "SYNTH"})
                 for index in range(1201)]
        histories = {row["id"]: [[]] for row in tasks}
        histories[tasks[0]["id"]] = [[event(tasks[0]["id"], action="create", content="",
                                            createTime="2026-01-01T23:59:59+08:00")]]
        histories[tasks[1]["id"]] = [[event(tasks[1]["id"], action="create", content="",
                                            createTime="2026-01-03T00:00:00+08:00")]]
        histories[tasks[-1]["id"]] = [[event(tasks[-1]["id"], action="create", content="",
                                             createTime="2026-01-02T00:00:00+08:00")]]
        clock = FakeClock()
        transport = ProjectTransport(tasks, clock, histories=histories)
        observed = []
        with observe_collection(observed.append):
            source = collector(transport, clock).collect("fictional-project", day="2026-01-02")
        config = {"tenant_id": "fictional-tenant", "project_id": "fictional-project",
                  "include_archived": False, "api_contract_verified": True,
                  "view": {"verified": True, "evidence": "synthetic scope", "task_equals": {}},
                  "resolved_status_ids": [], "status_names": {"fictional-open": "待处理"},
                  "owner_names": {"fictional-owner": "虚构成员"}}
        report = build_digest(source, config, "2026-01-02")
        self.assertIn({"phase": "listing", "completed": 0, "total": 1000,
                       "requests": 13, "batches": 1, "passes": 0}, observed)
        self.assertTrue(any(value["completed"] == 1201 and value["total"] == 1201
                            for value in observed))
        self.assertEqual(len(source["records"]), 1201)
        self.assertEqual(source["collection_stats"]["tasks_scanned"], 1201)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual([row["task_id"] for row in report["audit"]], [tasks[-1]["id"]])
        self.assertEqual(len(report["digest"]["tasks"]), 1)
        self.assertEqual(report["audit"][0]["event_counts"], {"created": 1})
        self.assertIn("📊 更新 1 项", report["markdown"])


if __name__ == "__main__":
    unittest.main()
