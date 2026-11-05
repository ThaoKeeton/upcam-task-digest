"""Observed-display semantics use entirely fictional notification evidence."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from automated_digest import (AutomatedDigestBlocked, build_automated_digest,
    diagnose_automatic_source, requires_openai_summary, validate_automated_report)
from digest_pipeline import build_digest
from report_contract import updated_selection


ROOT = Path(__file__).resolve().parents[1]


class NoticePipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "tests/fixtures/config.json").read_text())
        self.config["api_contract_verified"] = True
        self.config["view"].update(verified=True, evidence="synthetic-display-notice-contract")
        self.collection = json.loads((ROOT / "tests/fixtures/collection.json").read_text())
        self.collection["selection"] = updated_selection("2026-01-02")
        self.record = self.collection["records"][0]
        self.record["task"].update(updated="2026-01-02T03:00:00Z")
        self.record["activities"] = [self.event("commit", "notice-1")]

    def event(self, kind, identity):
        phrase = "提交" if kind == "commit" else "合并请求"
        return {"id": "synthetic-" + identity, "action": "standard",
                "boundToObjectId": "demo-old-task", "boundToObjectType": "task",
                "createTime": "2026-01-02T02:00:00Z",
                "content": json.dumps({"title": "关联了" + phrase + " 「SYNTHETIC_PRIVATE_NOTICE_BODY\n」",
                                       "customCreator": {"name": "SYNTHETIC_UNVERIFIED_ACCOUNT"}})}

    def test_display_notices_are_counted_without_claiming_real_commits_or_resolution(self):
        self.record["activities"].append(self.event("merge-request", "notice-2"))
        report = build_digest(self.collection, self.config, "2026-01-02")
        self.assertTrue(report["ready_to_send"])
        task = report["digest"]["tasks"][0]
        self.assertEqual(task["category"], "other")
        self.assertIn("Teambition 显示关联提交 1 条", task["summary"])
        self.assertIn("Teambition 显示关联合并请求 1 条", task["summary"])
        self.assertIn("代码来源、实际提交及合并结果未核实", task["summary"])
        self.assertNotIn("SYNTHETIC_PRIVATE_NOTICE_BODY", task["summary"])
        self.assertNotIn("SYNTHETIC_UNVERIFIED_ACCOUNT", task["summary"])
        self.assertEqual(report["summary_inputs"], [])
        for parsed in report["audit"][0]["parsed_changes"]:
            self.assertFalse(parsed["source_verified"])
            self.assertFalse(parsed["object_verified"])

    def test_distinct_events_with_same_title_count_twice_and_exact_event_duplicate_once(self):
        other = self.event("commit", "notice-2")
        self.record["activities"] += [other, deepcopy(other)]
        report = build_digest(self.collection, self.config, "2026-01-02")
        self.assertIn("Teambition 显示关联提交 2 条", report["digest"]["tasks"][0]["summary"])
        self.assertEqual(len(report["audit"][0]["event_ids"]), 2)

    def test_notice_only_automated_report_uses_zero_model_requests_and_revalidates(self):
        class NoModel:
            def summarize(self, *_args, **_kwargs):
                raise AssertionError("Display notifications must not request a model")
        self.assertTrue(diagnose_automatic_source(self.collection, self.config, "2026-01-02")["ok"])
        self.assertFalse(requires_openai_summary(self.collection, self.config, "2026-01-02"))
        report = build_automated_digest(self.collection, self.config, "2026-01-02", "synthetic-key",
                                       "gpt-6-luna", "max", client=NoModel())
        self.assertTrue(report["ready_to_send"])
        validate_automated_report(report, self.collection, self.config, "gpt-6-luna", "max")

    def test_supported_notice_not_misdiagnosed_when_unrelated_unknown_action_blocks(self):
        unknown = self.event("commit", "unknown")
        unknown["action"] = "future.synthetic.unsupported"
        self.record["activities"].append(unknown)
        diagnosis = diagnose_automatic_source(self.collection, self.config, "2026-01-02")
        self.assertFalse(diagnosis["ok"])
        self.assertIn("future.synthetic.unsupported", diagnosis["message"])
        self.assertNotIn("关联提交动态仅有标题模式", diagnosis["message"])
        self.assertNotIn("standard 动态尚未符合", diagnosis["message"])
        self.assertNotIn("SYNTHETIC_PRIVATE_NOTICE_BODY", diagnosis["message"])

    def test_legacy_title_only_candidates_and_unknown_standard_still_block(self):
        for title in ("关联了提交", "关联了提交：虚构候选", "虚构不明通知"):
            self.record["activities"][0]["content"] = json.dumps({
                "title": title, "customCreator": {"name": "SYNTHETIC_SOURCE"}})
            self.assertFalse(diagnose_automatic_source(self.collection, self.config, "2026-01-02")["ok"])
            with self.assertRaises(AutomatedDigestBlocked):
                requires_openai_summary(self.collection, self.config, "2026-01-02")

    def test_unverified_source_fields_never_override_display_only_contract(self):
        for field in ("provider", "commit", "payload", "source_verified"):
            event = self.event("commit", "forged")
            event[field] = "synthetic-forged-source"
            self.record["activities"] = [event]
            self.assertFalse(diagnose_automatic_source(self.collection, self.config, "2026-01-02")["ok"])

    def test_notice_outside_window_has_no_current_evidence(self):
        self.record["activities"][0]["createTime"] = "2026-01-02T16:00:00Z"
        self.assertFalse(diagnose_automatic_source(self.collection, self.config, "2026-01-02")["ok"])

    def test_safe_export_distinguishes_supported_display_from_verified_code(self):
        from upcam_app.diagnostic_export import safe_structure_export
        shape = safe_structure_export(self.collection, self.config, "2026-01-02")
        check = shape["tasks"][0]["activities"][0]["commit_check"]
        self.assertTrue(check.get("display_notice_supported"))
        self.assertFalse(check["automatic_review_required"])
        self.assertFalse(check["source_verified"])
        self.assertFalse(check.get("object_verified", True))
        self.assertNotIn("SYNTHETIC_PRIVATE_NOTICE_BODY", json.dumps(shape))


if __name__ == "__main__":
    unittest.main()
