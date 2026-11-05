"""Formal production-path formatting uses fictional identities and content."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from automated_digest import (AutomatedDigestBlocked, build_automated_digest, fingerprint,
                              validate_automated_report)
from report_contract import updated_selection


ROOT = Path(__file__).resolve().parents[1]


class FixedSummaries:
    def __init__(self):
        self.rows = []

    def summarize(self, _key, _model, _effort, rows, _rules, **_kwargs):
        self.rows = deepcopy(rows)
        return {row["task_key"]: "虚构：已定位入口限制；下一步回归边界，仍有兼容风险。" for row in rows}


class FormalDigestTests(unittest.TestCase):
    def setUp(self):
        self.collection = json.loads((ROOT / "tests/fixtures/collection.json").read_text())
        self.config = json.loads((ROOT / "tests/fixtures/config.json").read_text())
        self.config.update(api_contract_verified=True,
                           view={"verified": True, "evidence": "synthetic-formal-contract", "task_equals": {}},
                           task_number_prefix="", task_link_view_id="f" * 24)
        self.window = ("2026-01-02T08:00:00+08:00", "2026-01-02T12:00:00+08:00")
        self.collection["selection"] = updated_selection("2026-01-02", *self.window)
        self.record = self.collection["records"][0]
        self.record["task"].update(content="示例任务：保留冒号", uniqueId=321,
                                   project={"_id": "demo-project", "uniqueIdPrefix": "DEMO"},
                                   updated="2026-01-02T01:30:00Z")
        self.record["activities"] = [self.event("comment", "comment-1", {"comment": "虚构：已有定位，下一步回归。"})]

    def event(self, action, identity, content, when="2026-01-02T01:00:00Z"):
        return {"id": "synthetic-" + identity, "action": action,
                "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
                "createTime": when, "content": json.dumps(content, ensure_ascii=False)}

    def build(self, client=None):
        return build_automated_digest(self.collection, self.config, "2026-01-02", "synthetic-key",
                                      "gpt-6-luna", "max", client=client or FixedSummaries(), window=self.window)

    def test_actual_formal_path_has_range_business_number_preserved_colon_and_material_summary(self):
        client = FixedSummaries()
        report = self.build(client)
        task = report["digest"]["tasks"][0]
        self.assertEqual(task["category"], "progress")
        self.assertEqual(task["title"], "DEMO-321 示例任务：保留冒号")
        self.assertEqual(task["summary"], "虚构：已定位入口限制；下一步回归边界，仍有兼容风险。")
        self.assertEqual(task["url"], "https://www.teambition.com/project/demo-project/tasks/view/" + "f" * 24 + "/task/demo-old-task")
        self.assertIn("🕒 时间范围：2026-01-02 08:00 — 12:00", report["markdown"])
        self.assertIn("📊 更新 1 项：工作进展 1 项", report["markdown"])
        self.assertNotIn("🗓️ 日期", report["markdown"])
        self.assertNotIn("评论 1 条", report["markdown"])
        self.assertEqual(client.rows[0]["title"], "示例任务：保留冒号")
        self.assertNotIn("DEMO-321", json.dumps(client.rows))
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_resolved_status_event_takes_priority_over_substantive_comment_after_summary(self):
        self.record["task"].update(tfsId="demo-done", isDone=True)
        self.record["activities"].append(self.event("update.taskflowstatus", "resolve", {
            "oldTfsId": "demo-open", "tfsId": "demo-done", "oldTaskflowstatus": "待处理",
            "taskflowstatus": "已解决", "isDone": False}, "2026-01-02T01:05:00Z"))
        report = self.build()
        self.assertEqual(report["digest"]["tasks"][0]["category"], "resolved")
        self.assertNotIn("状态变更", report["digest"]["tasks"][0]["summary"])

    def test_reopen_does_not_retain_resolved_category(self):
        self.record["activities"] += [self.event("update.taskflowstatus", "resolved-first", {
            "oldTfsId": "demo-open", "tfsId": "demo-done", "oldTaskflowstatus": "待处理",
            "taskflowstatus": "已解决", "isDone": True}, "2026-01-02T01:05:00Z"),
            self.event("update.taskflowstatus", "reopen-last", {
                "oldTfsId": "demo-done", "tfsId": "demo-open", "oldTaskflowstatus": "已解决",
                "taskflowstatus": "待处理", "isDone": True}, "2026-01-02T01:10:00Z")]
        self.assertEqual(self.build()["digest"]["tasks"][0]["category"], "progress")

    def test_new_and_acknowledgement_summaries_are_explicitly_empty_without_generic_counts(self):
        for action, body, category in (("create", {}, "new"), ("comment", {"comment": "收到"}, "other")):
            self.record["activities"] = [self.event(action, "empty", body)]
            task = self.build()["digest"]["tasks"][0]
            self.assertEqual(task["summary"], "")
            self.assertEqual(task["category"], category)

    def test_notice_and_position_diagnostics_do_not_become_formal_task_summary(self):
        from test_observed_movement import position_payload, NEW_LIST, NEW_STAGE
        self.record["task"].update(tasklistId=NEW_LIST, stageId=NEW_STAGE)
        self.record["activities"] += [self.event("involve.add", "auto-involvers", {}),
            self.event("move.cross.from.stage.to.stage", "move", position_payload()),
            self.event("standard", "notice", {"title": "关联了提交 「虚构通知正文」",
                                                "customCreator": {"name": "unverified-synthetic"}})]
        report = self.build()
        summary = report["digest"]["tasks"][0]["summary"]
        self.assertEqual(summary, "虚构：已定位入口限制；下一步回归边界，仍有兼容风险。")
        for marker in ("条", "未核实", "来源", "位置变更", "通知正文", "参与者"):
            self.assertNotIn(marker, summary)
        self.assertEqual(len(report["audit"][0]["activity_evidence"]), 4)

    def test_five_files_and_automated_owner_change_stay_out_of_communication_summary(self):
        self.record["activities"][0]["content"] = json.dumps({
            "comment": "虚构：已有定位，下一步回归。", "attachments": [], "dingFiles": [],
            "files": [char * 24 for char in "abcde"], "renderMode": "markdown",
            "isOnlyNotifyMentions": False,
        }, ensure_ascii=False)
        previous_owner = self.record["task"]["executorId"]
        self.config["owner_names"]["synthetic-new-owner"] = "虚构成员乙"
        self.record["task"]["executorId"] = "synthetic-new-owner"
        self.record["activities"].append(self.event("update_executor", "assign", {
            "_oldExecutorId": previous_owner, "_executorId": "synthetic-new-owner",
            "payload": {"fromAutomation": True},
        }, "2026-01-02T01:05:00Z"))
        client = FixedSummaries()
        report = self.build(client)
        self.assertEqual(report["digest"]["tasks"][0]["summary"],
                         "虚构：已定位入口限制；下一步回归边界，仍有兼容风险。")
        self.assertIn("负责人：虚构成员乙", report["markdown"])
        for noise in ("评论附有", "未读取文件内容", "执行人变更"):
            self.assertNotIn(noise, report["markdown"])
        self.assertEqual(len(report["audit"][0]["activity_evidence"]), 2)
        self.assertTrue(report["audit"][0]["parsed_changes"][0]["from_automation"])
        self.assertEqual(report["summary_inputs"][0]["comments"][0]["file_count"], 5)
        self.assertEqual(client.rows[0]["comment_bodies"], ["虚构：已有定位，下一步回归。"])
        for file_id in [char * 24 for char in "abcde"]:
            self.assertNotIn(file_id, json.dumps(client.rows))
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_owner_only_activity_keeps_current_owner_without_requesting_a_model(self):
        previous_owner = self.record["task"]["executorId"]
        self.config["owner_names"]["synthetic-new-owner"] = "虚构成员乙"
        self.record["task"]["executorId"] = "synthetic-new-owner"
        self.record["activities"] = [self.event("update_executor", "assign-only", {
            "_oldExecutorId": previous_owner, "_executorId": "synthetic-new-owner",
            "payload": {"fromAutomation": True},
        })]
        class NoModel:
            def summarize(self, *_args, **_kwargs):
                raise AssertionError("Assignee-only activity must not request a model")
        report = self.build(NoModel())
        task = report["digest"]["tasks"][0]
        self.assertEqual(task["category"], "other")
        self.assertEqual(task["summary"], "")
        self.assertIn("负责人：虚构成员乙", report["markdown"])
        self.assertNotIn("执行人变更", report["markdown"])
        self.assertNotIn("未发现任务动态", report["markdown"])
        self.assertEqual(len(report["audit"][0]["parsed_changes"]), 1)
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_missing_actual_number_blocks_before_openai_instead_of_guessing(self):
        self.record["task"].pop("uniqueId")
        client = FixedSummaries()
        with self.assertRaises(AutomatedDigestBlocked):
            self.build(client)
        self.assertEqual(client.rows, [])

    def test_changing_window_in_report_digest_fails_independent_reconstruction(self):
        report = self.build()
        self.assertIn("time_range", report["digest"])
        report["digest"]["time_range"]["end_exclusive"] = "2026-01-02T13:00:00+08:00"
        report["automation"].pop("report_sha256")
        report["automation"]["report_sha256"] = fingerprint(report)
        with self.assertRaisesRegex(AutomatedDigestBlocked, "Rendered report differs"):
            validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_detailed_diagnostic_markers_are_never_accepted_as_sendable_source(self):
        for fields in ({"data_altered": True}, {"sendable": False},
                       {"format": "upcam-teambition-detailed-diagnostic-v1"}):
            with self.subTest(marker=list(fields)):
                collection = dict(self.collection, **fields)
                client = FixedSummaries()
                with self.assertRaisesRegex(AutomatedDigestBlocked, "诊断导出"):
                    build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                           "gpt-6-luna", "max", client=client, window=self.window)
                self.assertEqual(client.rows, [])


if __name__ == "__main__":
    unittest.main()
