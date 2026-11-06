"""Large project diagnostics use synthetic records and keep export limits explicit."""

import json
import unittest

from upcam_app import detailed_diagnostic as detailed
from upcam_app.diagnostic_export import safe_structure_export
from report_contract import updated_selection


DAY = "2026-02-04"
START = "2026-02-04T00:00:00+08:00"
END = "2026-02-05T00:00:00+08:00"


class LargeCollectionDiagnosticTests(unittest.TestCase):
    def sample(self, count):
        records = []
        for index in range(count):
            task_id = f"synthetic-large-diagnostic-{index}"
            records.append({"task": {"id": task_id, "projectId": "synthetic-large-project",
                "updated": "2020-01-01T00:00:00Z", "isArchived": False,
                "content": "SYNTHETIC_PRIVATE_TITLE", "executorId": "synthetic-owner",
                "tfsId": "synthetic-status", "isDone": False}, "activities": []})
        task = records[-1]
        task["activities"] = [{"id": "synthetic-late-record-event", "action": "unsupported.synthetic.large",
            "boundToObjectId": task["task"]["id"], "boundToObjectType": "task",
            "createTime": "2026-02-04T10:00:00+08:00",
            "content": json.dumps({"comment": "SYNTHETIC_PRIVATE_BODY"})}]
        collection = {"tenant_id": "synthetic-large-tenant", "project_id": "synthetic-large-project",
            "include_archived": False, "pagination_complete": True,
            "selection": updated_selection(DAY), "records": records}
        config = {"owner_names": {"synthetic-owner": "虚构成员"},
                  "status_names": {"synthetic-status": "虚构状态"}, "resolved_status_ids": []}
        metadata = {"id": "synthetic-large-export", "application_version": "synthetic",
            "ruleset_revision": "synthetic", "created_at_utc": "2026-02-05T01:00:00Z",
            "capabilities": [], "report_window": {"start": START, "end_exclusive": END,
                                                     "timezone": "Asia/Shanghai"}}
        return collection, config, metadata

    def test_detailed_export_accepts_more_than_700_input_tasks(self):
        collection, config, metadata = self.sample(1201)
        result = detailed.build_detailed_snapshot(collection, config, metadata, credentials={})
        self.assertEqual(result["counts"]["tasks"], 1201)
        self.assertEqual(result["counts"]["in_window_activities"], 1)
        self.assertEqual(result["counts"]["exported_activities"], 1)
        self.assertFalse(result["sendable"])
        self.assertTrue(result["data_altered"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode()),
                             detailed.MAX_DETAILED_BYTES)

    def test_safe_export_prioritizes_relevant_task_after_old_metadata_ceiling(self):
        collection, config, _metadata = self.sample(1201)
        result = safe_structure_export(collection, config, DAY)
        self.assertEqual(result["task_count"], 1201)
        self.assertEqual(result["activity_count"], 1)
        self.assertEqual(result["tasks"][0]["task_id"], collection["records"][-1]["task"]["id"])
        self.assertEqual(result["tasks"][0]["activities"][0]["event_id"], "synthetic-late-record-event")
        self.assertTrue(result["detail_truncated"])
        self.assertGreater(result["truncated_task_count"], 0)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SYNTHETIC_PRIVATE_TITLE", encoded)
        self.assertNotIn("SYNTHETIC_PRIVATE_BODY", encoded)


if __name__ == "__main__":
    unittest.main()
