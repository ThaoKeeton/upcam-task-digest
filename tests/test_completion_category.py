"""Technical completion and the user-selected resolved category are distinct."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from automated_digest import diagnose_automatic_source, requires_openai_summary
from digest_pipeline import build_digest
from report_contract import updated_selection


ROOT = Path(__file__).resolve().parents[1]


class CompletionCategoryTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "tests/fixtures/config.json").read_text())
        self.config["api_contract_verified"] = True
        self.config["view"].update(verified=True, evidence="synthetic-status-contract")
        self.config["status_names"]["synthetic-closed"] = "虚构关闭状态"
        self.collection = json.loads((ROOT / "tests/fixtures/collection.json").read_text())
        self.collection["selection"] = updated_selection("2026-01-02")
        self.record = self.collection["records"][0]
        self.record["task"].update(updated="2026-01-02T03:00:00Z", tfsId="synthetic-closed", isDone=True)
        self.record["activities"] = [{
            "id": "synthetic-status-1", "action": "update.taskflowstatus",
            "boundToObjectId": self.record["task"]["id"], "boundToObjectType": "task",
            "createTime": "2026-01-02T02:00:00Z",
            "content": json.dumps({"oldTfsId": "demo-open", "tfsId": "synthetic-closed",
                                   "oldTaskflowstatus": "待处理", "taskflowstatus": "虚构关闭状态", "isDone": False})}]

    def test_completed_nonresolved_state_is_not_a_conflict_or_resolution(self):
        report = build_digest(self.collection, self.config, "2026-01-02")
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")
        self.assertTrue(diagnose_automatic_source(self.collection, self.config, "2026-01-02")["ok"])
        self.assertFalse(requires_openai_summary(self.collection, self.config, "2026-01-02"))

    def test_resolved_target_still_requires_valid_positive_current_completion(self):
        self.config["resolved_status_ids"].append("synthetic-closed")
        for flag in (False, None, "true", 1):
            with self.subTest(flag=flag):
                self.record["task"]["isDone"] = flag
                report = build_digest(self.collection, self.config, "2026-01-02")
                self.assertFalse(report["ready_to_send"])
                self.assertEqual(report["digest"]["tasks"][0]["category"], "other")

    def test_chain_and_current_target_checks_are_retained(self):
        self.record["task"]["tfsId"] = "demo-open"
        self.assertFalse(build_digest(self.collection, self.config, "2026-01-02")["ready_to_send"])


if __name__ == "__main__":
    unittest.main()
