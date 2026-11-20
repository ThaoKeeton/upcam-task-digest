"""Safe local explanations for synthetic historical reconciliation evidence."""

import json
import unittest

from automated_digest import diagnose_automatic_source
from report_contract import updated_selection
from upcam_app.detailed_diagnostic import build_detailed_snapshot
import test_historical_pipeline as fixtures


class HistoricalDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.sample = fixtures.HistoricalPipelineTests()
        self.sample.setUp()

    def diagnose(self, collection):
        return diagnose_automatic_source(collection, self.sample.config, fixtures.DAY)

    def test_unmapped_owner_does_not_create_false_historical_status_conflict(self):
        sample = self.sample
        sample.task.update(tfsId=fixtures.ACTIVE, isDone=False)
        collection = sample.collection(
            sample.status(fixtures.ACTIVE, fixtures.RESOLVED),
            sample.status(fixtures.RESOLVED, fixtures.ACTIVE, when=fixtures.AFTER,
                          event_id="synthetic-diagnostic-later-status"))
        sample.config["owner_names"] = {}
        diagnostic = self.diagnose(collection)
        self.assertFalse(diagnostic["ok"])
        self.assertIn("缺少成员姓名映射", diagnostic["message"])
        self.assertNotIn("状态事件序列与任务当前状态不一致", diagnostic["message"])

    def test_invalid_later_status_context_has_specific_safe_explanation(self):
        sample = self.sample
        sample.task.update(tfsId=fixtures.ACTIVE, isDone=False)
        bad = sample.event("update.taskflowstatus", {"unsupported": "never echo this"},
                           when=fixtures.AFTER, event_id="synthetic-malformed-context")
        diagnostic = self.diagnose(sample.collection(
            sample.status(fixtures.ACTIVE, fixtures.RESOLVED), bad))
        self.assertFalse(diagnostic["ok"])
        self.assertIn("状态动态字段结构未通过校验", diagnostic["message"])
        self.assertNotIn("never echo this", diagnostic["message"])

    def test_attachment_identity_conflict_explains_reason_without_echoing_names(self):
        sample = self.sample
        diagnostic = self.diagnose(sample.collection(
            sample.upload(name="private-fictional-first.pdf"),
            sample.field_created(name="private-fictional-second.pdf",
                                 event_id="synthetic-conflicting-reference")))
        self.assertFalse(diagnostic["ok"])
        self.assertIn("同一文件 ID 的字段或名称证据互相冲突", diagnostic["message"])
        self.assertNotIn("private-fictional", diagnostic["message"])

    def test_unsupported_attachment_structure_has_specific_safe_explanation(self):
        sample = self.sample
        raw = sample.upload()
        payload = json.loads(raw["content"])
        payload["unknown"] = "private-fictional-metadata"
        raw["content"] = json.dumps(payload)
        diagnostic = self.diagnose(sample.collection(raw))
        self.assertFalse(diagnostic["ok"])
        self.assertIn("自定义字段附件动态", diagnostic["message"])
        self.assertNotIn("private-fictional-metadata", diagnostic["message"])

    def test_future_event_failure_is_chinese_and_does_not_echo_payload(self):
        sample = self.sample
        collection = sample.collection(sample.event(
            "comment", {"comment": "private-fictional-comment"}, when=fixtures.AFTER))
        collection["collected_at"] = fixtures.END
        diagnostic = self.diagnose(collection)
        self.assertFalse(diagnostic["ok"])
        self.assertIn("动态时间晚于本次采集完成时间", diagnostic["message"])
        self.assertNotIn("private-fictional-comment", diagnostic["message"])

    def test_success_message_limits_coverage_to_current_readable_project(self):
        diagnostic = self.diagnose(self.sample.collection(self.sample.event("create")))
        self.assertTrue(diagnostic["ok"])
        self.assertIn("当前项目范围内 1 个可读任务", diagnostic["message"])
        self.assertIn("1 个任务有窗口内动态", diagnostic["message"])
        self.assertIn("不代表已删除、已不可见任务", diagnostic["message"])
        self.assertNotIn("完整采集", diagnostic["message"])

    def test_candidate_success_message_discloses_unadvanced_updated_limit(self):
        collection = self.sample.collection(self.sample.event("create"))
        collection["selection"] = updated_selection(fixtures.DAY)
        diagnostic = self.diagnose(collection)
        self.assertTrue(diagnostic["ok"])
        self.assertIn("读取 1 个候选任务", diagnostic["message"])
        self.assertIn("1 个任务有窗口内动态", diagnostic["message"])
        self.assertIn("未推进更新时间的旧任务动态可能漏采", diagnostic["message"])
        self.assertIn("详细诊断导出", diagnostic["message"])
        self.assertNotIn("扫描当前项目范围内 1 个可读任务", diagnostic["message"])
        self.assertIn("不代表已删除、已不可见任务", diagnostic["message"])
        self.assertNotIn("完整采集", diagnostic["message"])

    def test_private_export_preserves_new_coverage_contract_without_claiming_sendability(self):
        sample = self.sample
        collection = sample.collection(sample.event("create"))
        metadata = {"id": "synthetic-history-export", "created_at_utc": "2026-04-09T12:00:00Z",
                    "application_version": "synthetic-version", "ruleset_revision": "synthetic-rules",
                    "capabilities": [], "report_window": {"start": fixtures.START,
                        "end_exclusive": fixtures.END, "timezone": "Asia/Shanghai"}}
        exported = build_detailed_snapshot(collection, sample.config, metadata, credentials={})
        self.assertEqual(exported["scope"]["selection"], collection["selection"])
        self.assertFalse(exported["sendable"])
        self.assertTrue(exported["data_altered"])


if __name__ == "__main__":
    unittest.main()
