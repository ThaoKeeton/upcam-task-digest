"""Fictional position events; no project content or identities from uploads."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from activity_adapter import parse_activity
from automated_digest import diagnose_automatic_source, requires_openai_summary
from digest_pipeline import build_digest
from report_contract import updated_selection


ROOT = Path(__file__).resolve().parents[1]
OLD_LIST, NEW_LIST = "1" * 24, "2" * 24
OLD_STAGE, NEW_STAGE = "3" * 24, "4" * 24


def position_payload():
    return {
        "from": {"tasklist": {"_id": OLD_LIST, "title": "虚构待办分组"},
                 "stage": {"_id": OLD_STAGE, "name": "虚构阶段甲"}},
        "to": {"tasklist": {"_id": NEW_LIST, "title": "虚构完成分组"},
               "stage": {"_id": NEW_STAGE, "name": "虚构阶段乙"}},
    }


class ObservedMovementTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "tests/fixtures/config.json").read_text())
        self.config["api_contract_verified"] = True
        self.config["view"].update(verified=True, evidence="synthetic-position-contract")
        self.collection = json.loads((ROOT / "tests/fixtures/collection.json").read_text())
        self.collection["selection"] = updated_selection("2026-01-02")
        self.record = self.collection["records"][0]
        self.record["task"].update(updated="2026-01-02T03:00:00Z", tasklistId=NEW_LIST,
                                   stageId=NEW_STAGE, isDone=False, tfsId="demo-open")
        self.event = {"id": "synthetic-position-1", "action": "move.cross.from.stage.to.stage",
                      "boundToObjectId": self.record["task"]["id"], "boundToObjectType": "task",
                      "createTime": "2026-01-02T02:00:00Z", "content": json.dumps(position_payload())}
        self.record["activities"] = [self.event]

    def report(self):
        return build_digest(self.collection, self.config, "2026-01-02")

    def test_evidenced_move_is_other_and_not_resolution_even_if_destination_looks_complete(self):
        self.assertEqual(parse_activity(self.event, self.config), ("other", False))
        report = self.report()
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")
        self.assertIn("任务位置变更 1 条", report["digest"]["tasks"][0]["summary"])
        self.assertEqual(len(report["audit"][0]["position_changes"]), 1)
        self.assertEqual(report["summary_inputs"], [])
        self.assertFalse(requires_openai_summary(self.collection, self.config, "2026-01-02"))

    def test_current_done_flag_does_not_make_a_move_a_resolution(self):
        self.record["task"].update(isDone=True, tfsId="demo-done")
        self.assertEqual(self.report()["digest"]["tasks"][0]["category"], "other")
        self.assertEqual(self.report()["audit"][0]["status_transitions"], [])

    def test_conflicting_current_position_blocks_without_model(self):
        self.record["task"]["stageId"] = "5" * 24
        report = self.report()
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0].get("position_conflict"))
        diagnosis = diagnose_automatic_source(self.collection, self.config, "2026-01-02")
        self.assertFalse(diagnosis["ok"])
        self.assertIn("位置", diagnosis["message"])
        self.assertIn("当前", diagnosis["message"])

    def test_sorted_chain_and_event_identity_dedup_remain_authoritative(self):
        earlier = deepcopy(self.event)
        earlier["id"] = "synthetic-position-0"
        earlier["createTime"] = "2026-01-02T01:00:00Z"
        payload = position_payload()
        payload["to"] = deepcopy(payload["from"])
        payload["from"]["tasklist"]["_id"] = "6" * 24
        payload["from"]["stage"]["_id"] = "7" * 24
        earlier["content"] = json.dumps(payload)
        self.record["activities"] = [self.event, earlier, deepcopy(earlier)]
        report = self.report()
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(len(report["audit"][0]["position_changes"]), 2)
        self.assertIn("任务位置变更 2 条", report["digest"]["tasks"][0]["summary"])
        self.record["activities"].reverse()
        self.assertEqual(report, self.report())
        bad = json.loads(self.event["content"])
        bad["from"]["stage"]["_id"] = "8" * 24
        self.event["content"] = json.dumps(bad)
        self.assertFalse(self.report()["ready_to_send"])

    def test_same_time_multiple_moves_are_ambiguous(self):
        another = deepcopy(self.event)
        another["id"] = "synthetic-position-2"
        self.record["activities"].append(another)
        self.assertFalse(self.report()["ready_to_send"])

    def test_move_at_exclusive_end_does_not_enter_report(self):
        self.event["createTime"] = "2026-01-02T16:00:00Z"
        report = self.report()
        self.assertEqual(report["audit"][0]["event_ids"], [])
        self.assertFalse(report["ready_to_send"])  # Updated task has no window event.

    def test_malformed_move_shape_and_noop_remain_unknown(self):
        cases = []
        value = position_payload(); value["extra"] = True; cases.append(value)
        value = position_payload(); value["to"]["stage"]["unknown"] = "x"; cases.append(value)
        value = position_payload(); value["to"]["stage"]["_id"] = "bad id"; cases.append(value)
        value = position_payload(); value["to"]["tasklist"]["title"] = "<b>unsafe</b>"; cases.append(value)
        value = position_payload(); value["to"]["stage"]["name"] = "bad\x00name"; cases.append(value)
        value = position_payload(); value["to"] = deepcopy(value["from"]); cases.append(value)
        for payload in cases:
            with self.subTest(case=len(json.dumps(payload))):
                self.event["content"] = json.dumps(payload)
                self.assertEqual(parse_activity(self.event, self.config), ("unknown", False))
                self.assertFalse(self.report()["ready_to_send"])

    def test_new_unrecognized_move_token_still_blocks(self):
        self.event["action"] = "move.future.synthetic.stage"
        self.assertEqual(parse_activity(self.event, self.config), ("unknown", False))
        self.assertFalse(self.report()["ready_to_send"])

    def test_safe_export_reports_move_structure_without_exporting_location_names(self):
        from upcam_app.diagnostic_export import safe_structure_export
        shape = safe_structure_export(self.collection, self.config, "2026-01-02")
        event = shape["tasks"][0]["activities"][0]
        self.assertTrue(event.get("movement_check", {}).get("adapter_supported"))
        self.assertFalse(event["movement_check"]["completion_evidence"])
        self.assertNotIn("虚构完成分组", json.dumps(shape, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
