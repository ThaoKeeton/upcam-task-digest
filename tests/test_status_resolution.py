"""Synthetic status IDs distinguish membership from resolution in a window."""

from copy import deepcopy
import json
import unittest

from activity_adapter import parse_activity, status_transition
from digest_pipeline import build_digest
from formal_content import FormalContentError, formal_task_content
from report_contract import updated_selection


ACTIVE = "synthetic-window-active"
RESOLVED = "synthetic-window-resolved"
CLOSED = "synthetic-window-closed"
OTHER = "synthetic-window-other"
TASK = "synthetic-window-task"
DAY = "2026-02-03"
WINDOW = ("2026-02-02T16:00:00+00:00", "2026-02-03T16:00:00+00:00")


def synthetic_config():
    return {
        "tenant_id": "synthetic-window-tenant",
        "project_id": "synthetic-window-project",
        "api_contract_verified": True,
        "include_archived": False,
        "view": {"verified": True, "evidence": "synthetic-window-contract", "task_equals": {}},
        "owner_names": {"synthetic-window-person": "虚构窗口成员"},
        "status_names": {
            ACTIVE: "虚构待处理",
            RESOLVED: "虚构终态同名",
            CLOSED: "虚构终态同名",
            OTHER: "虚构已解决但未配置",
        },
        "resolved_status_ids": [RESOLVED, CLOSED],
    }


def synthetic_event(config, previous, target, *, index=1,
                    when="2026-02-03T01:00:00Z", event_is_done=False):
    return {
        "id": f"synthetic-window-event-{index}",
        "action": "update.taskflowstatus",
        "boundToObjectId": TASK,
        "boundToObjectType": "task",
        "createTime": when,
        "content": json.dumps({
            "oldTfsId": previous, "tfsId": target,
            "oldTaskflowstatus": config["status_names"][previous],
            "taskflowstatus": config["status_names"][target],
            "isDone": event_is_done,
        }, ensure_ascii=False),
    }


class StatusResolutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = synthetic_config()

    def transition(self, previous, target, *, event_is_done=False):
        event = synthetic_event(self.config, previous, target, event_is_done=event_is_done)
        return status_transition(event["content"], self.config)

    def formal(self, *path, proposed="", substantive=False, created=0):
        return formal_task_content({
            "event_counts": {"status": len(path), "created": created},
            "status_transitions": [self.transition(previous, target) for previous, target in path],
            "parsed_changes": [],
        }, None, proposed, substantive)

    def test_transition_preserves_target_membership_and_records_real_entry(self):
        cases = [
            (ACTIVE, RESOLVED, False, True, True),
            (RESOLVED, CLOSED, True, True, False),
            (CLOSED, ACTIVE, True, False, False),
            (ACTIVE, OTHER, False, False, False),
            (RESOLVED, RESOLVED, True, True, False),
        ]
        for previous, target, was_resolved, is_resolved, entered in cases:
            with self.subTest(previous=previous, target=target):
                transition = self.transition(previous, target)
                self.assertIs(transition.get("previous_resolved"), was_resolved)
                self.assertIs(transition["resolved"], is_resolved)
                self.assertIs(transition.get("entered_resolved"), entered)

    def test_parse_activity_keeps_target_membership_boolean_for_internal_move(self):
        event = synthetic_event(self.config, RESOLVED, CLOSED, event_is_done=False)
        self.assertEqual(parse_activity(event, self.config), ("status", True))

    def test_event_boolean_does_not_determine_entry_or_target_membership(self):
        for flag in (False, True):
            with self.subTest(event_is_done=flag):
                entry = self.transition(ACTIVE, RESOLVED, event_is_done=flag)
                internal = self.transition(RESOLVED, CLOSED, event_is_done=flag)
                self.assertIs(entry.get("entered_resolved"), True)
                self.assertIs(internal.get("entered_resolved"), False)
                self.assertIs(entry["event_is_done"], flag)
                self.assertIs(internal["event_is_done"], flag)

    def test_status_names_never_define_resolved_membership(self):
        self.config["status_names"][RESOLVED] = "虚构处理中"
        self.assertIs(self.transition(ACTIVE, RESOLVED)["resolved"], True)
        self.assertIs(self.transition(ACTIVE, OTHER)["resolved"], False)

    def test_same_named_resolved_ids_are_valid_internal_transition(self):
        transition = self.transition(RESOLVED, CLOSED)
        self.assertIsNotNone(transition)
        self.assertNotEqual(transition["previous_id"], transition["target_id"])
        self.assertIs(transition.get("entered_resolved"), False)

    def test_missing_previous_id_mapping_cannot_be_replaced_by_same_name(self):
        event = synthetic_event(self.config, RESOLVED, CLOSED)
        del self.config["status_names"][RESOLVED]
        self.assertIsNone(status_transition(event["content"], self.config))

    def test_internal_move_without_entry_is_other_or_comment_progress(self):
        self.assertEqual(self.formal((RESOLVED, CLOSED)), {"category": "other", "summary": ""})
        self.assertEqual(self.formal((RESOLVED, CLOSED), proposed="虚构：仍待复核。", substantive=True),
                         {"category": "progress", "summary": "虚构：仍待复核。"})

    def test_internal_move_does_not_override_creation(self):
        self.assertEqual(self.formal((RESOLVED, CLOSED), created=1),
                         {"category": "new", "summary": ""})

    def test_entry_then_internal_move_remains_resolved_once(self):
        self.assertEqual(self.formal((ACTIVE, RESOLVED), (RESOLVED, CLOSED)),
                         {"category": "resolved", "summary": ""})

    def test_reopening_at_window_end_removes_resolution(self):
        self.assertEqual(self.formal((ACTIVE, RESOLVED), (RESOLVED, CLOSED), (CLOSED, ACTIVE)),
                         {"category": "other", "summary": ""})

    def test_reentry_after_reopening_is_resolved(self):
        self.assertEqual(self.formal((ACTIVE, RESOLVED), (RESOLVED, ACTIVE), (ACTIVE, CLOSED)),
                         {"category": "resolved", "summary": ""})

    def test_unchanged_resolved_id_is_not_entry(self):
        self.assertEqual(self.formal((RESOLVED, RESOLVED)), {"category": "other", "summary": ""})

    def test_formal_requires_boolean_consistent_entry_evidence(self):
        valid = {"resolved": True, "previous_resolved": True, "entered_resolved": False}
        invalid = [
            {"resolved": True},
            dict(valid, previous_resolved="false"),
            dict(valid, entered_resolved=1),
            dict(valid, entered_resolved=True),
            {"resolved": False, "previous_resolved": False, "entered_resolved": True},
        ]
        for transition in invalid:
            with self.subTest(transition=transition):
                with self.assertRaises(FormalContentError):
                    formal_task_content({"status_transitions": [transition]}, None, "", False)

    def test_parsing_and_classification_preserve_source_and_mapping(self):
        original_config = deepcopy(self.config)
        transitions = [self.transition(ACTIVE, RESOLVED), self.transition(RESOLVED, CLOSED)]
        audit = {"status_transitions": transitions}
        original_audit = deepcopy(audit)
        formal_task_content(audit, None, "", False)
        self.assertEqual(self.config, original_config)
        self.assertEqual(audit, original_audit)


class ResolutionWindowPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = synthetic_config()

    def report(self, path, current, is_done=True):
        events = [synthetic_event(self.config, previous, target, index=index, when=when)
                  for index, (previous, target, when) in enumerate(path, start=1)]
        collection = {
            "tenant_id": self.config["tenant_id"], "project_id": self.config["project_id"],
            "include_archived": False, "pagination_complete": True,
            "selection": updated_selection(DAY, *WINDOW),
            "records": [{"task": {
                "id": TASK, "projectId": self.config["project_id"], "isArchived": False,
                "content": "虚构窗口回归任务", "executorId": "synthetic-window-person",
                "tfsId": current, "isDone": is_done, "updated": "2026-02-03T03:00:00Z",
            }, "activities": events}],
        }
        return build_digest(collection, self.config, DAY, window=WINDOW)

    def test_internal_resolved_move_alone_is_not_new_resolution(self):
        report = self.report([(RESOLVED, CLOSED, "2026-02-03T01:00:00Z")], CLOSED)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")

    def test_outside_window_entry_does_not_make_internal_move_a_resolution(self):
        report = self.report([(ACTIVE, RESOLVED, "2026-02-02T15:59:59Z"),
                              (RESOLVED, CLOSED, "2026-02-03T01:00:00Z")], CLOSED)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")

    def test_entry_then_internal_move_is_resolved_in_same_window(self):
        report = self.report([(RESOLVED, CLOSED, "2026-02-03T02:00:00Z"),
                              (ACTIVE, RESOLVED, "2026-02-03T01:00:00Z")], CLOSED)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "resolved")
        self.assertEqual(len(report["digest"]["tasks"]), 1)

    def test_entry_then_reopening_is_not_resolved_at_window_end(self):
        report = self.report([(ACTIVE, RESOLVED, "2026-02-03T01:00:00Z"),
                              (RESOLVED, ACTIVE, "2026-02-03T02:00:00Z")], ACTIVE, is_done=False)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")


if __name__ == "__main__":
    unittest.main()
