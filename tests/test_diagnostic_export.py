"""Safe shape reports contain structure, never raw task or comment text."""

import json
import unittest

from report_contract import updated_selection
from upcam_app.diagnostic_export import safe_structure_export


class SafeStructureExportTests(unittest.TestCase):
    def test_quote_json_is_parsed_strictly_without_guessing_from_json_like_text(self):
        values = [
            '{"comment":"PRIVATE_QUOTED_BODY","author":{"name":"PRIVATE_QUOTED_NAME"}}',
            'literal {"not valid JSON',
            '{"comment":"one","comment":"two"}',
            '{"comment":NaN}',
            json.dumps('{"text":"PRIVATE_DOUBLE_ENCODED"}'),
            '{"comment":1e999}',
            '{"comment":-1e999}',
        ]
        report = safe_structure_export(self.collection([
            self.event("comment", {"comment": "PRIVATE_NEW_BODY", "quote": value}, id=f"quote-{i}")
            for i, value in enumerate(values)
        ]), {}, "2026-01-02")
        checks = [e["comment_check"]["quote_checks"]["content"] for e in report["tasks"][0]["activities"]]
        self.assertIn("json_shape", checks[0])
        self.assertTrue(checks[0]["json_shape"]["json_valid"])
        self.assertEqual(checks[0]["json_shape"]["fields"]["comment"]["type"], "string")
        self.assertEqual(checks[0]["json_shape"]["fields"]["author"]["fields"]["name"]["type"], "string")
        self.assertTrue(all(not checks[i]["json_shape"]["json_valid"] for i in (1, 2, 3, 5, 6)))
        self.assertEqual(checks[4]["json_shape"]["json_scalar_type"], "string")
        self.assertEqual(checks[4]["decoded_string_shape"]["fields"]["text"]["type"], "string")
        self.assertTrue(all(e["comment_check"]["needs_review"] for e in report["tasks"][0]["activities"][:4]))
        encoded = json.dumps(report)
        for value in ("PRIVATE_QUOTED_BODY", "PRIVATE_QUOTED_NAME", "PRIVATE_DOUBLE_ENCODED", "PRIVATE_NEW_BODY"):
            self.assertNotIn(value, encoded)

    def test_quote_field_names_and_nested_text_checks_are_privacy_bounded(self):
        quote = json.dumps({"comment": "PRIVATE_BODY<p>private</p>",
                            "SYNTHETIC_PRIVATE_KEY": "PRIVATE_VALUE", "author": {"name": "PRIVATE_NAME"}})
        report = safe_structure_export(self.collection([self.event("comment", {"comment": "new", "quote": quote})]), {})
        check = report["tasks"][0]["activities"][0]["comment_check"]["quote_checks"]["content"]
        self.assertIn("json_shape", check)
        self.assertEqual(check["json_shape"]["other_field_count"], 1)
        self.assertEqual(check["decoded_text_checks"]["comment"]["markup_categories"], ["other_tag_like"])
        encoded = json.dumps(report)
        for value in ("PRIVATE_BODY", "PRIVATE_VALUE", "PRIVATE_NAME", "SYNTHETIC_PRIVATE_KEY"):
            self.assertNotIn(value, encoded)

    def test_unknown_action_protocol_token_is_exported_without_business_text(self):
        collection = self.collection([
            self.event("update_tasklist", {"from": {}, "to": {}}),
            self.event("PRIVATE_ACTION_VALUE", {}, id="private-action"),
            self.event("not an api token", {}, id="invalid-action"),
            self.event("synthetic.private.person", {}, id="private-lowercase-action"),
            self.event("synthetic-api-key-012345", {}, id="private-token-action"),
        ])
        report = safe_structure_export(collection, {}, "2026-01-02")
        events = report["tasks"][0]["activities"]
        self.assertEqual(events[0]["action"], "unknown")
        self.assertIn("action_token", events[0])
        self.assertEqual(events[0]["action_token"], "update_tasklist")
        self.assertIsNone(events[1]["action_token"])
        self.assertIsNone(events[2]["action_token"])
        self.assertIsNone(events[3]["action_token"])
        self.assertIsNone(events[4]["action_token"])
        self.assertEqual(report["format"], "upcam-teambition-safe-shape-v3")
        self.assertNotIn("PRIVATE_ACTION_VALUE", json.dumps(report))

    def event(self, action, content, **extra):
        return {
            "id": "synthetic-event", "action": action,
            "boundToObjectId": "synthetic-task", "boundToObjectType": "task",
            "createTime": "2026-01-02T01:00:00Z",
            "content": json.dumps(content, ensure_ascii=False), **extra,
        }

    def collection(self, activities, **task_fields):
        return {
            "pagination_complete": True, "selection": updated_selection("2026-01-02"),
            "records": [{"task": {"id": "synthetic-task", "executorId": "synthetic-new",
                                   "tfsId": "synthetic-done", "isDone": True, **task_fields},
                         "activities": activities}],
        }

    def test_window_membership_is_half_open_and_unknown_without_valid_time_or_scope(self):
        times = ["2026-01-01T15:59:59Z", "2026-01-01T16:00:00Z",
                 "2026-01-02T15:59:59Z", "2026-01-02T16:00:00Z", "2026-01-02T12:00:00", None]
        collection = self.collection([
            self.event("create", {}, id=f"synthetic-event-{index}", createTime=value)
            for index, value in enumerate(times)
        ])
        report = safe_structure_export(collection, {}, "2026-01-02")
        self.assertEqual([event["in_report_window"] for event in report["tasks"][0]["activities"]],
                         [False, True, True, False, None, None])
        self.assertTrue(report["selection_check"]["matches_report_window"])
        self.assertTrue(report["window_evaluation_available"])
        legacy_call = safe_structure_export(collection, {})
        self.assertFalse(legacy_call["window_evaluation_available"])
        self.assertIsNone(legacy_call["selection_check"]["matches_report_window"])
        self.assertTrue(all(event["in_report_window"] is None
                            for event in legacy_call["tasks"][0]["activities"]))
        encoded = json.dumps(report)
        for value in times[:-1]:
            self.assertNotIn(value, encoded)

    def test_custom_window_checks_selection_and_reports_binding_without_ids_or_values(self):
        window = ("2026-01-01T20:00:00Z", "2026-01-02T12:00:00Z")
        collection = self.collection([
            self.event("create", {}, createTime="2026-01-02T12:00:00Z"),
            self.event("create", {}, id="synthetic-misbound", boundToObjectId="PRIVATE_OTHER_TASK"),
        ])
        report = safe_structure_export(collection, {}, "2026-01-02", window)
        self.assertFalse(report["selection_check"]["matches_report_window"])
        events = report["tasks"][0]["activities"]
        self.assertFalse(events[0]["in_report_window"])
        self.assertTrue(events[0]["task_binding_valid"])
        self.assertFalse(events[1]["task_binding_valid"])
        self.assertNotIn("PRIVATE_OTHER_TASK", json.dumps(report))
        collection["selection"] = updated_selection("2026-01-02", *window)
        self.assertTrue(safe_structure_export(collection, {}, "2026-01-02", window)
                        ["selection_check"]["matches_report_window"])

    def test_executor_id_fields_are_allowlisted_and_hidden_fields_remain_counted(self):
        private_name = "SYNTHETIC_PRIVATE_EXECUTOR_NAME"
        collection = self.collection([self.event("update_executor", {
            "_executorId": "synthetic-new", "_oldExecutorId": "synthetic-old",
            "_privateField": "SYNTHETIC_PRIVATE_PAYLOAD", "actionVersion": 1,
            "payload": {"fromAutomation": True},
        }, payload={"fromAutomation": False})])
        config = {"owner_names": {"synthetic-new": private_name, "synthetic-old": private_name}}
        report = safe_structure_export(collection, config)
        event = report["tasks"][0]["activities"][0]
        self.assertEqual(event["content_shape"]["fields"]["_executorId"]["type"], "string")
        self.assertEqual(event["content_shape"]["fields"]["_oldExecutorId"]["type"], "string")
        self.assertEqual(event["content_shape"]["other_field_count"], 1)
        self.assertTrue(event["executor_check"]["previous_executor"]["mapping_present"])
        self.assertTrue(event["executor_check"]["target_executor"]["id_valid"])
        self.assertFalse(event["executor_check"]["automation_flags_match"])
        self.assertTrue(event["executor_check"]["needs_review"])
        encoded = json.dumps(report)
        for value in (private_name, "synthetic-new", "synthetic-old", "_privateField",
                      "SYNTHETIC_PRIVATE_PAYLOAD"):
            self.assertNotIn(value, encoded)

    def test_comment_text_checks_distinguish_supported_breaks_markup_and_control_categories(self):
        collection = self.collection([
            self.event("comment", {"comment": "PRIVATE_BODY<br>still pending",
                                   "quote": "PRIVATE_QUOTE<br>context", "renderMode": "markdown"}),
            self.event("comment", {"comment": "PRIVATE_BODY<p>SYNTHETIC_PRIVATE_RICH_TEXT</p>\u202e",
                                   "quote": "PRIVATE_QUOTE\x00\u0085", "renderMode": "markdown"},
                       id="synthetic-rich-comment"),
        ])
        report = safe_structure_export(collection, {})
        plain, rich = [event["comment_check"] for event in report["tasks"][0]["activities"]]
        self.assertEqual(plain["body_check"]["markup_categories"], ["exact_br"])
        self.assertEqual(plain["quote_checks"]["content"]["markup_categories"], ["exact_br"])
        self.assertEqual(plain["body_check"]["control_categories"], [])
        self.assertTrue(plain["needs_review"])  # Diagnostics never relax extraction gates.
        self.assertEqual(rich["body_check"]["markup_categories"], ["other_tag_like"])
        self.assertEqual(rich["body_check"]["control_categories"], ["unicode_format"])
        self.assertEqual(rich["quote_checks"]["content"]["control_categories"],
                         ["ascii_control", "unicode_control"])
        encoded = json.dumps(report)
        for value in ("PRIVATE_BODY", "PRIVATE_QUOTE", "still pending", "context", "SYNTHETIC_PRIVATE_RICH_TEXT"):
            self.assertNotIn(value, encoded)

    def test_standard_rejection_reasons_distinguish_structure_from_title_pattern(self):
        collection = self.collection([
            self.event("standard", {"title": "关联了提交: SYNTHETIC_PRIVATE_COMMIT",
                                    "customCreator": {"name": "SYNTHETIC_PRIVATE_SOURCE"}}),
            self.event("standard", {"title": "SYNTHETIC_PRIVATE_OTHER_TITLE",
                                    "customCreator": {"name": "SYNTHETIC_PRIVATE_SOURCE"}},
                       id="synthetic-other-standard"),
            self.event("standard", {"title": "关联了提交"}, id="synthetic-missing-creator"),
        ])
        report = safe_structure_export(collection, {})
        checks = [event["commit_check"] for event in report["tasks"][0]["activities"]]
        self.assertTrue(checks[0]["candidate_recognized"])
        self.assertEqual(checks[0]["rejection_reasons"], ["title_pattern_does_not_prove_source"])
        self.assertEqual(checks[1]["rejection_reasons"], ["unrecognized_title_pattern"])
        self.assertEqual(checks[2]["rejection_reasons"], ["creator_name_unavailable"])
        self.assertTrue(all(check["automatic_review_required"] for check in checks))
        encoded = json.dumps(report, ensure_ascii=False)
        for value in ("SYNTHETIC_PRIVATE_COMMIT", "SYNTHETIC_PRIVATE_SOURCE", "SYNTHETIC_PRIVATE_OTHER_TITLE"):
            self.assertNotIn(value, encoded)

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
