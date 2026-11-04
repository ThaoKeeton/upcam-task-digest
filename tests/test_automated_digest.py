import json
from pathlib import Path
import unittest

from automated_digest import (
    AutomatedDigestBlocked, build_automated_digest, diagnose_automatic_source,
    validate_automated_report,
)
from report_contract import updated_selection
from teambition_readonly import CollectionError


class RecordingSummarizer:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def summarize(self, api_key, model, effort, rows, editorial_rules, service_tier="default"):
        self.calls.append((api_key, model, effort, rows, editorial_rules, service_tier))
        if self.response is not None:
            return self.response
        return {row["task_key"]: "虚构：已定位导出异常，仍待修复。" for row in rows}


class AutomatedDigestTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / "fixtures"
        self.collection = json.loads((fixtures / "collection.json").read_text(encoding="utf-8"))
        self.config = json.loads((fixtures / "config.json").read_text(encoding="utf-8"))
        self.collection["selection"] = updated_selection("2026-01-02")
        self.collection["batch_id"] = "00000000-0000-4000-8000-000000000001"
        self.collection["collected_at"] = "2026-01-03T01:00:00+00:00"
        self.config.update(
            api_contract_verified=True,
            view={"verified": True, "evidence": "fictional verification", "task_equals": {}},
        )

    def build(self, client):
        return build_automated_digest(
            self.collection, self.config, "2026-01-02", "synthetic-key", "gpt-6-luna", "max",
            client=client,
        )

    def test_only_pseudonymous_keys_titles_and_comment_bodies_are_sent(self):
        client = RecordingSummarizer()
        report = self.build(client)
        self.assertEqual(report["report_type"], "automated-v1")
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(len(client.calls), 1)
        rows = client.calls[0][3]
        self.assertEqual(set(rows[0]), {"task_key", "title", "comment_bodies"})
        self.assertTrue(rows[0]["task_key"].startswith("task-"))
        encoded = json.dumps(rows, ensure_ascii=False)
        for private_value in ("demo-old-task", "demo-person", "www.teambition.com", "demo-event-1"):
            self.assertNotIn(private_value, encoded)
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_acknowledgement_only_comments_need_no_model_call_and_validate(self):
        record = self.collection["records"][0]
        record["activities"] = [record["activities"][1]]
        record["task"]["updated"] = "2026-01-02T15:00:00Z"
        client = RecordingSummarizer()
        report = self.build(client)
        self.assertEqual(client.calls, [])
        self.assertTrue(report["ready_to_send"])
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_hard_source_or_event_issue_blocks_before_model_submission(self):
        self.collection["records"][0]["activities"].append({
            "id": "demo-unknown-event",
            "action": "future.unknown.action",
            "boundToObjectId": "demo-old-task",
            "boundToObjectType": "task",
            "createTime": "2026-01-02T12:00:00Z",
            "content": "fictional unknown event",
        })
        client = RecordingSummarizer()
        with self.assertRaises(AutomatedDigestBlocked) as raised:
            self.build(client)
        self.assertEqual(client.calls, [])
        message = str(raised.exception)
        self.assertIn("未识别动态类型", message)
        self.assertIn("future.unknown.action", message)
        self.assertIn("demo-old-task", message)
        self.assertNotIn("fictional unknown event", message)
        self.assertIn("未调用 OpenAI", message)

    def test_malformed_unhashable_action_is_reported_without_echoing_payload(self):
        collection = json.loads(json.dumps(self.collection))
        collection["records"][0]["activities"].append({
            "id": "demo-malformed-event",
            "action": ["synthetic-sensitive-action"],
            "boundToObjectId": "demo-old-task",
            "boundToObjectType": "task",
            "createTime": "2026-01-02T04:00:00Z",
            "content": "synthetic-private-payload",
        })

        with self.assertRaises(AutomatedDigestBlocked) as raised:
            build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                   "gpt-6-luna", "max", client=RecordingSummarizer())

        self.assertIn("未识别动态类型", str(raised.exception))
        self.assertIn("原值已隐藏", str(raised.exception))
        self.assertNotIn("synthetic-sensitive-action", str(raised.exception))
        self.assertNotIn("synthetic-private-payload", str(raised.exception))

    def test_missing_member_mapping_identifies_id_without_exposing_comment_text(self):
        config = json.loads(json.dumps(self.config))
        config["owner_names"].pop("demo-person")
        client = RecordingSummarizer()

        with self.assertRaises(AutomatedDigestBlocked) as raised:
            build_automated_digest(self.collection, config, "2026-01-02", "synthetic-key",
                                   "gpt-6-luna", "max", client=client)

        message = str(raised.exception)
        self.assertIn("缺少成员姓名映射", message)
        self.assertIn("demo-person", message)
        self.assertNotIn("已定位导出异常", message)
        self.assertEqual(client.calls, [])

    def test_status_chain_conflict_explains_local_reason_before_openai(self):
        collection = json.loads(json.dumps(self.collection))
        event = collection["records"][0]["activities"][-1]
        event["createTime"] = "2026-01-02T11:00:00Z"
        client = RecordingSummarizer()

        with self.assertRaises(AutomatedDigestBlocked) as raised:
            build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                   "gpt-6-luna", "max", client=client)

        self.assertIn("状态事件序列与任务当前状态不一致", str(raised.exception))
        self.assertEqual(client.calls, [])

    def test_malformed_comment_structure_is_described_without_logging_body(self):
        collection = json.loads(json.dumps(self.collection))
        collection["records"][0]["activities"][0]["content"] = (
            '{"comment":"synthetic-private-body","unrecognized":"field"}')
        client = RecordingSummarizer()

        with self.assertRaises(AutomatedDigestBlocked) as raised:
            build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                   "gpt-6-luna", "max", client=client)

        self.assertIn("评论动态结构未通过本地校验", str(raised.exception))
        self.assertIn("尚未识别的评论字段", str(raised.exception))
        self.assertNotIn("synthetic-private-body", str(raised.exception))
        self.assertEqual(client.calls, [])

    def test_read_only_diagnostic_pass_does_not_call_summary_client(self):
        diagnostic = diagnose_automatic_source(
            self.collection, self.config, "2026-01-02")

        self.assertTrue(diagnostic["ok"])
        self.assertIn("只读校验通过", diagnostic["message"])
        self.assertIn("未调用 OpenAI", diagnostic["message"])

    def test_read_only_diagnostic_reports_missing_state_mappings(self):
        config = json.loads(json.dumps(self.config))
        del config["status_names"]["demo-open"]

        diagnostic = diagnose_automatic_source(
            self.collection, config, "2026-01-02")

        self.assertFalse(diagnostic["ok"])
        self.assertIn("缺少状态名称映射", diagnostic["message"])
        self.assertIn("demo-open", diagnostic["message"])
        self.assertIn("未发送钉钉", diagnostic["message"])
        self.assertNotIn("状态动态字段或关联提交", diagnostic["message"])

    def test_status_name_mismatch_is_distinguished_from_missing_mapping(self):
        collection = json.loads(json.dumps(self.collection))
        status_event = {
            "id": "demo-status-event", "action": "update.taskflowstatus",
            "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
            "createTime": "2026-01-02T11:00:00Z",
            "content": json.dumps({
                "tfsId": "demo-open", "oldTfsId": "demo-open",
                "taskflowstatus": "synthetic-wrong-name", "oldTaskflowstatus": "待处理",
                "isDone": False,
            }, ensure_ascii=False),
        }
        collection["records"][0]["activities"] = [status_event]

        diagnostic = diagnose_automatic_source(collection, self.config, "2026-01-02")

        self.assertFalse(diagnostic["ok"])
        self.assertIn("状态事件中的名称与已保存映射不一致", diagnostic["message"])
        self.assertNotIn("synthetic-wrong-name", diagnostic["message"])
        self.assertNotIn("synthetic status marker", diagnostic["message"])

    def test_unrecognized_standard_event_is_not_claimed_as_linked_commit_evidence(self):
        self.collection["records"][0]["activities"] = [{
            "id": "synthetic-unrecognized-standard", "action": "standard",
            "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
            "createTime": "2026-01-02T11:00:00Z",
            "content": json.dumps({"title": "SYNTHETIC_PRIVATE_OTHER_TITLE",
                                   "customCreator": {"name": "SYNTHETIC_PRIVATE_SOURCE"}}),
        }]

        diagnostic = diagnose_automatic_source(self.collection, self.config, "2026-01-02")

        self.assertFalse(diagnostic["ok"])
        self.assertIn("standard 动态尚未符合已核验结构或标题规则", diagnostic["message"])
        self.assertNotIn("关联提交动态仅有标题模式", diagnostic["message"])
        self.assertNotIn("SYNTHETIC_PRIVATE_OTHER_TITLE", diagnostic["message"])
        self.assertNotIn("SYNTHETIC_PRIVATE_SOURCE", diagnostic["message"])

    def test_out_of_window_comment_and_standard_content_do_not_block_current_report(self):
        record = self.collection["records"][0]
        record["activities"] = [
            {"id": "synthetic-current-create", "action": "create",
             "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
             "createTime": "2026-01-02T01:00:00Z", "content": "{}"},
            {"id": "synthetic-historical-comment", "action": "comment",
             "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
             "createTime": "2026-01-01T01:00:00Z", "content": "SYNTHETIC_INVALID_COMMENT_JSON"},
            {"id": "synthetic-future-standard", "action": "standard",
             "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
             "createTime": "2026-01-03T01:00:00Z", "content": "SYNTHETIC_INVALID_STANDARD_JSON"},
        ]

        diagnostic = diagnose_automatic_source(self.collection, self.config, "2026-01-02")
        report = self.build(RecordingSummarizer())

        self.assertTrue(diagnostic["ok"])
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["audit"][0]["event_ids"], ["synthetic-current-create"])

    def test_out_of_window_identity_binding_and_unknown_time_still_block(self):
        record = self.collection["records"][0]
        record["activities"] = [{
            "id": "synthetic-historical-comment", "action": "comment",
            "boundToObjectId": "synthetic-other-task", "boundToObjectType": "task",
            "createTime": "2026-01-01T01:00:00Z", "content": "SYNTHETIC_INVALID_COMMENT_JSON",
        }]
        with self.assertRaises(CollectionError):
            self.build(RecordingSummarizer())
        record["activities"][0].update(boundToObjectId="demo-old-task", createTime="2026-01-01T01:00:00")

        diagnostic = diagnose_automatic_source(self.collection, self.config, "2026-01-02")

        self.assertFalse(diagnostic["ok"])
        self.assertIn("createTime", diagnostic["message"])
        self.assertNotIn("SYNTHETIC_INVALID_COMMENT_JSON", diagnostic["message"])

    def test_verified_empty_window_renders_no_activity_without_openai_request(self):
        self.collection["records"] = []
        client = RecordingSummarizer()
        report = build_automated_digest(
            self.collection, self.config, "2026-01-02", "", "gpt-6-luna", "max", client=client,
        )
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(client.calls, [])
        self.assertIn("筛选时间段内未发现任务动态", report["markdown"])
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_incomplete_collection_is_not_reported_as_no_activity(self):
        self.collection["records"] = []
        self.collection["pagination_complete"] = False
        client = RecordingSummarizer()
        with self.assertRaises(CollectionError):
            build_automated_digest(
                self.collection, self.config, "2026-01-02", "", "gpt-6-luna", "max", client=client,
            )
        self.assertEqual(client.calls, [])

    def test_empty_summary_for_substantive_comments_blocks_delivery(self):
        client = RecordingSummarizer({"task-1": ""})
        with self.assertRaises(AutomatedDigestBlocked):
            self.build(client)

    def test_stale_model_or_changed_source_cannot_validate(self):
        report = self.build(RecordingSummarizer())
        with self.assertRaises(AutomatedDigestBlocked):
            validate_automated_report(report, self.collection, self.config, "different-model", "max")
        changed_collection = json.loads(json.dumps(self.collection))
        changed_collection["records"][0]["task"]["content"] = "Fictional changed task"
        with self.assertRaises(AutomatedDigestBlocked):
            validate_automated_report(report, changed_collection, self.config, "gpt-6-luna", "max")


if __name__ == "__main__":
    unittest.main()
