"""Safe shape reports contain structure, never raw task or comment text."""

import json
import unittest

from report_contract import updated_selection
from upcam_app.diagnostic_export import safe_structure_export


class SafeStructureExportTests(unittest.TestCase):
    def test_export_reports_nested_json_shapes_and_mapping_match_without_values(self):
        private_body = "SYNTHETIC_PRIVATE_COMMENT_SHOULD_NOT_LEAK"
        private_title = "SYNTHETIC_PRIVATE_TASK_TITLE_SHOULD_NOT_LEAK"
        collection = {
            "tenant_id": "synthetic-tenant-secret-value",
            "project_id": "synthetic-project-secret-value",
            "pagination_complete": True,
            "selection": updated_selection("2026-01-02"),
            "records": [{
                "task": {
                    "id": "synthetic-task-id", "projectId": "synthetic-project-id",
                    "content": private_title, "executorId": "synthetic-person-id",
                    "tfsId": "synthetic-open-id", "isDone": False,
                    "isArchived": False, "updated": "2026-01-02T08:00:00Z",
                },
                "activities": [
                    {
                        "id": "synthetic-comment-event", "action": "comment",
                        "boundToObjectId": "synthetic-task-id", "boundToObjectType": "task",
                        "createTime": "2026-01-02T01:00:00Z",
                        "content": json.dumps({"comment": private_body,
                                               "attachments": [], "customField": "private-value"}),
                    },
                    {
                        "id": "synthetic-status-event", "action": "update.taskflowstatus",
                        "boundToObjectId": "synthetic-task-id", "boundToObjectType": "task",
                        "createTime": "2026-01-02T02:00:00Z",
                        "content": json.dumps({
                            "oldTfsId": "synthetic-open-id", "tfsId": "synthetic-done-id",
                            "oldTaskflowstatus": "Synthetic open", "taskflowstatus": "Synthetic done",
                            "isDone": True,
                        }),
                    },
                ],
            }],
        }
        config = {
            "owner_names": {"synthetic-person-id": "PRIVATE PERSON NAME"},
            "status_names": {"synthetic-open-id": "PRIVATE STATUS NAME"},
            "resolved_status_ids": [],
        }

        report = safe_structure_export(collection, config)
        encoded = json.dumps(report, ensure_ascii=False)

        for private_value in (private_body, private_title, "private-value", "PRIVATE PERSON NAME",
                              "PRIVATE STATUS NAME", "synthetic-tenant-secret-value",
                              "synthetic-project-secret-value", "Synthetic open", "Synthetic done"):
            self.assertNotIn(private_value, encoded)
        comment = report["tasks"][0]["activities"][0]
        self.assertEqual(comment["action"], "comment")
        self.assertTrue(comment["content_shape"]["json_object"])
        self.assertIn("comment", comment["content_shape"]["fields"])
        self.assertEqual(comment["comment_check"]["review_reasons"],
                         ["存在尚未识别的评论字段；完整对象保留"])
        status = report["tasks"][0]["activities"][1]
        self.assertIsNone(status["status_check"]["target_status"]["name_matches_mapping"])
        self.assertFalse(status["status_check"]["target_status"]["mapping_present"])

    def test_non_json_comment_shape_is_diagnosed_without_echoing_text(self):
        collection = {
            "pagination_complete": True,
            "records": [{
                "task": {"id": "synthetic-task", "executorId": "p", "tfsId": "s"},
                "activities": [{
                    "id": "SENSITIVE EVENT ID WITH SPACES", "action": "comment", "content": "SENSITIVE RAW BODY",
                    "boundToObjectId": "synthetic-task", "boundToObjectType": "task",
                }],
            }],
        }
        report = safe_structure_export(collection, {"owner_names": {}, "status_names": {}})
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("SENSITIVE RAW BODY", encoded)
        self.assertFalse(report["tasks"][0]["activities"][0]["content_shape"]["json_object"])
        self.assertIsNone(report["tasks"][0]["activities"][0]["event_id"])
        self.assertIn("content 不是有效且无重复键的评论 JSON 对象",
                      report["tasks"][0]["activities"][0]["comment_check"]["review_reasons"])


if __name__ == "__main__":
    unittest.main()
