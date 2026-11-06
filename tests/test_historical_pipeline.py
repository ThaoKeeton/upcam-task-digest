"""Synthetic historical activity windows and local reconciliation contexts."""

from copy import deepcopy
import json
import unittest

from activity_adapter import OBSERVED_ACTIONS, parse_activity
from automated_digest import build_automated_digest, validate_automated_report
from digest_pipeline import build_digest
from teambition_readonly import CollectionError


DAY = "2026-04-07"
START = "2026-04-07T00:00:00+08:00"
END = "2026-04-08T00:00:00+08:00"
INSIDE = "2026-04-07T09:00:00+08:00"
AFTER = "2026-04-08T09:00:00+08:00"
LATER = "2026-04-08T10:00:00+08:00"
TASK = "h" * 24
FILE = "i" * 24
FIELD = "j" * 24
NAME = "虚构历史附件.pdf"
ACTIVE, RESOLVED, CLOSED = "synthetic-history-active", "synthetic-history-resolved", "synthetic-history-closed"
OWNERS = ("synthetic-history-owner-a", "synthetic-history-owner-b", "synthetic-history-owner-c")
POSITIONS = (("k" * 24, "l" * 24), ("m" * 24, "n" * 24), ("o" * 24, "p" * 24))


class RecordingSummary:
    def __init__(self):
        self.rows = []

    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        self.rows.extend(deepcopy(rows))
        return {row["task_key"]: "虚构：仍待复核。" for row in rows}


class HistoricalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "tenant_id": "synthetic-history-tenant", "project_id": "synthetic-history-project",
            "include_archived": False, "api_contract_verified": True,
            "view": {"verified": True, "evidence": "synthetic-history-contract", "task_equals": {}},
            "owner_names": dict(zip(OWNERS, ("虚构历史成员甲", "虚构历史成员乙", "虚构历史成员丙"))),
            "status_names": {ACTIVE: "虚构待处理", RESOLVED: "虚构已解决", CLOSED: "虚构已关闭"},
            "resolved_status_ids": [RESOLVED, CLOSED],
        }
        self.task = {
            "id": TASK, "projectId": self.config["project_id"], "isArchived": False,
            "content": "虚构历史窗口任务", "uniqueId": 407,
            "project": {"_id": self.config["project_id"], "uniqueIdPrefix": "SYNTH"},
            "executorId": OWNERS[0], "tfsId": ACTIVE, "isDone": False,
            "tasklistId": POSITIONS[0][0], "stageId": POSITIONS[0][1],
            "updated": "2026-04-09T09:00:00+08:00",
        }

    def event(self, action, data=None, *, when=INSIDE, event_id="synthetic-history-event"):
        return {"id": event_id, "action": action, "createTime": when,
                "boundToObjectId": TASK, "boundToObjectType": "task",
                "content": json.dumps(data if data is not None else {}, ensure_ascii=False)}

    def status(self, previous, target, **kwargs):
        return self.event("update.taskflowstatus", {
            "oldTfsId": previous, "tfsId": target,
            "oldTaskflowstatus": self.config["status_names"][previous],
            "taskflowstatus": self.config["status_names"][target], "isDone": False,
        }, **kwargs)

    def executor(self, previous, target, **kwargs):
        return self.event("update_executor", {"_oldExecutorId": previous, "_executorId": target}, **kwargs)

    def position(self, previous, target, **kwargs):
        def endpoint(index):
            return {"tasklist": {"_id": POSITIONS[index][0], "title": f"虚构分组{index}"},
                    "stage": {"_id": POSITIONS[index][1], "name": f"虚构阶段{index}"}}
        return self.event("move.cross.from.stage.to.stage",
                          {"from": endpoint(previous), "to": endpoint(target)}, **kwargs)

    def upload(self, *, name=NAME, field=FIELD, **kwargs):
        return self.event("upload.customfield.work", {
            "customfield": {"type": "work"}, "works": [{"_id": FILE, "fileName": name,
                "meta": {"boundToObjectType": "file", "payload": {
                    "boundToObjectType": "customfield", "boundToObjectId": field,
                    "scope": "task", "scopeId": TASK}}}],
        }, **kwargs)

    def field_created(self, *, name=NAME, field=FIELD, **kwargs):
        return self.event("customfield.create.v2", {
            "customfieldId": field, "customfieldName": "虚构历史附件字段",
            "oldValue": [], "oldValueString": "", "value": [{
                "_id": FILE, "fileName": name, "fileSize": 12, "thumbnail": None,
                "thumbnailUrl": None, "fileCategory": "application", "title": name,
                "source": "awos", "meta": {"boundToObjectType": "file"}, "secretLevel": 0,
            }], "valueString": name, "secretLevel": 0,
        }, **kwargs)

    def collection(self, *events):
        return {
            "tenant_id": self.config["tenant_id"], "project_id": self.config["project_id"],
            "include_archived": False, "pagination_complete": True,
            "selection": {
                "mode": "v3-project-activities-half-open", "date": DAY,
                "start": START, "end_exclusive": END, "window_kind": "calendar_day",
                "basis": "activity-createTime", "q": None, "view_equivalent": False,
                "candidate_scope": "current-visible-project-tasks",
                "historical_event_coverage": False, "snapshot_guaranteed": False,
            },
            "records": [{"task": deepcopy(self.task), "activities": list(events)}],
        }

    def report(self, *events):
        return build_digest(self.collection(*events), self.config, DAY)

    def test_later_current_update_does_not_remove_window_activity(self):
        report = self.report(self.event("create"))
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "new")

    def test_earlier_current_update_does_not_replace_window_activity_membership(self):
        self.task["updated"] = "2026-04-06T09:00:00+08:00"
        self.assertTrue(self.report(self.event("create"))["ready_to_send"])

    def test_current_update_still_requires_aware_valid_timestamp(self):
        for value in (None, "invalid", "2026-04-07T09:00:00", 1):
            with self.subTest(value=value):
                self.task["updated"] = value
                with self.assertRaises(CollectionError):
                    self.report(self.event("create"))

    def test_no_window_activity_with_update_outside_window_is_skipped(self):
        report = self.report(self.event("create", when=AFTER))
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"], [])
        self.assertEqual(report["audit"], [])

    def test_updated_inside_without_window_activity_preserves_missing_evidence_block(self):
        self.task["updated"] = INSIDE
        report = self.report(self.event("create", when=AFTER))
        self.assertFalse(report["ready_to_send"])
        self.assertEqual(len(report["digest"]["tasks"]), 1)
        self.assertTrue(report["audit"][0]["hard_review"])
        self.assertIn("动态证据不足", report["markdown"])

    def test_old_current_update_selection_cannot_be_sent(self):
        collection = self.collection(self.event("create"))
        collection["selection"].update(mode="v3-project-updated-half-open", basis="current-latest-updated")
        with self.assertRaises(CollectionError):
            build_digest(collection, self.config, DAY)

    def test_collection_timestamp_when_present_must_be_valid_and_aware(self):
        for value in (None, "invalid", "2026-04-07T09:00:00", 1):
            with self.subTest(value=value):
                collection = self.collection(self.event("create"))
                collection["collected_at"] = value
                with self.assertRaisesRegex(CollectionError, "^Invalid collection timestamp$"):
                    build_digest(collection, self.config, DAY)

    def test_event_at_exact_collection_timestamp_is_accepted(self):
        collection = self.collection(self.event("create"))
        collection["collected_at"] = INSIDE
        self.assertTrue(build_digest(collection, self.config, DAY)["ready_to_send"])

    def test_event_microsecond_after_collection_timestamp_is_rejected(self):
        collection = self.collection(self.event("create", when="2026-04-07T09:00:00.000001+08:00"))
        collection["collected_at"] = INSIDE
        with self.assertRaisesRegex(CollectionError, "^Activity timestamp exceeds collection time$"):
            build_digest(collection, self.config, DAY)

    def test_unrelated_outside_window_future_event_cannot_bypass_collection_time(self):
        for action in ("future.action", "comment", "update_executor"):
            with self.subTest(action=action):
                collection = self.collection(self.event("create"), self.event(
                    action, when=AFTER, event_id="synthetic-unrelated-future"))
                collection["collected_at"] = "2026-04-08T08:59:59.999999+08:00"
                with self.assertRaisesRegex(CollectionError, "^Activity timestamp exceeds collection time$"):
                    build_digest(collection, self.config, DAY)

    def test_future_context_cannot_reconcile_current_state(self):
        collection = self.collection(self.status(ACTIVE, RESOLVED),
                                     self.status(RESOLVED, ACTIVE, when=AFTER,
                                                 event_id="synthetic-future-context"))
        collection["collected_at"] = END
        with self.assertRaisesRegex(CollectionError, "^Activity timestamp exceeds collection time$"):
            build_digest(collection, self.config, DAY)

    def test_future_event_on_task_excluded_by_view_still_invalidates_collection(self):
        self.config["view"]["task_equals"] = {"executorId": OWNERS[2]}
        collection = self.collection(self.event("create", when=AFTER))
        collection["collected_at"] = END
        with self.assertRaisesRegex(CollectionError, "^Activity timestamp exceeds collection time$"):
            build_digest(collection, self.config, DAY)

    def test_later_reopening_validates_current_state_without_changing_window_resolution(self):
        inside = self.status(ACTIVE, RESOLVED)
        later = self.status(RESOLVED, ACTIVE, when=AFTER, event_id="synthetic-post-status")
        report = self.report(later, inside)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "resolved")
        audit = report["audit"][0]
        self.assertEqual(audit["event_counts"], {"status": 1})
        self.assertEqual(audit["activity_evidence"], [inside])
        self.assertEqual(audit.get("post_window_context"), [later])
        self.assertEqual([item["target_id"] for item in audit["status_transitions"]], [RESOLVED])
        self.assertEqual([item["target_id"] for item in audit.get("status_validation_transitions", [])],
                         [RESOLVED, ACTIVE])

    def test_later_resolution_does_not_mark_window_reopening_resolved(self):
        self.task.update(tfsId=RESOLVED, isDone=True)
        report = self.report(self.status(RESOLVED, ACTIVE),
                             self.status(ACTIVE, RESOLVED, when=AFTER, event_id="synthetic-post-status"))
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")

    def test_all_post_window_status_hops_must_reach_current_state(self):
        report = self.report(self.status(ACTIVE, RESOLVED),
                             self.status(RESOLVED, CLOSED, when=AFTER, event_id="synthetic-post-1"),
                             self.status(CLOSED, ACTIVE, when=LATER, event_id="synthetic-post-2"))
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(len(report["audit"][0]["status_transitions"]), 1)
        self.assertEqual(len(report["audit"][0].get("post_window_context", [])), 2)

    def test_post_window_status_chain_conflict_still_blocks(self):
        report = self.report(self.status(ACTIVE, RESOLVED),
                             self.status(CLOSED, ACTIVE, when=AFTER, event_id="synthetic-post-broken"))
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0]["hard_review"])

    def test_same_time_post_window_status_changes_still_block(self):
        report = self.report(self.status(ACTIVE, RESOLVED),
                             self.status(RESOLVED, CLOSED, when=AFTER, event_id="synthetic-post-1"),
                             self.status(CLOSED, ACTIVE, when=AFTER, event_id="synthetic-post-2"))
        self.assertFalse(report["ready_to_send"])

    def test_malformed_needed_post_window_status_is_not_silently_dropped(self):
        self.task.update(tfsId=RESOLVED, isDone=True)
        later = self.event("update.taskflowstatus", when=AFTER, event_id="synthetic-post-bad")
        report = self.report(self.status(ACTIVE, RESOLVED), later)
        self.assertFalse(report["ready_to_send"])
        self.assertEqual(report["audit"][0].get("post_window_context"), [later])

    def test_unrelated_post_window_streams_and_comments_are_ignored(self):
        later = [self.event(action, when=AFTER, event_id=f"synthetic-post-{index}")
                 for index, action in enumerate(("update.taskflowstatus", "update_executor",
                                                "move.cross.from.stage.to.stage", "comment", "future.action"))]
        report = self.report(self.event("create"), *later)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["audit"][0].get("post_window_context"), [])
        self.assertEqual(report["summary_inputs"], [])

    def test_later_executor_chain_reconciles_current_without_entering_window_changes(self):
        self.task["executorId"] = OWNERS[2]
        later = self.executor(OWNERS[1], OWNERS[2], when=AFTER, event_id="synthetic-post-executor")
        report = self.report(self.executor(OWNERS[0], OWNERS[1]), later)
        self.assertTrue(report["ready_to_send"])
        self.assertFalse(report["audit"][0]["executor_conflict"])
        self.assertEqual(len(report["audit"][0]["parsed_changes"]), 1)
        self.assertEqual(report["audit"][0].get("post_window_context"), [later])
        self.assertNotIn("虚构历史成员丙", report["digest"]["tasks"][0]["summary"])

    def test_broken_post_window_executor_chain_blocks(self):
        self.task["executorId"] = OWNERS[2]
        report = self.report(self.executor(OWNERS[0], OWNERS[1]),
                             self.executor(OWNERS[0], OWNERS[2], when=AFTER, event_id="synthetic-post-executor"))
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0]["executor_conflict"])

    def test_same_time_post_window_executor_changes_block(self):
        report = self.report(self.executor(OWNERS[0], OWNERS[1]),
                             self.executor(OWNERS[1], OWNERS[2], when=AFTER, event_id="synthetic-post-1"),
                             self.executor(OWNERS[2], OWNERS[0], when=AFTER, event_id="synthetic-post-2"))
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0]["executor_conflict"])

    def test_later_position_chain_reconciles_current_without_entering_window_changes(self):
        self.task.update(tasklistId=POSITIONS[2][0], stageId=POSITIONS[2][1])
        later = self.position(1, 2, when=AFTER, event_id="synthetic-post-position")
        report = self.report(self.position(0, 1), later)
        self.assertTrue(report["ready_to_send"])
        self.assertFalse(report["audit"][0]["position_conflict"])
        self.assertEqual(len(report["audit"][0]["position_changes"]), 1)
        self.assertEqual(report["audit"][0].get("post_window_context"), [later])

    def test_broken_post_window_position_chain_blocks(self):
        self.task.update(tasklistId=POSITIONS[2][0], stageId=POSITIONS[2][1])
        report = self.report(self.position(0, 1),
                             self.position(0, 2, when=AFTER, event_id="synthetic-post-position"))
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0]["position_conflict"])

    def test_same_time_post_window_position_changes_block(self):
        report = self.report(self.position(0, 1),
                             self.position(1, 2, when=AFTER, event_id="synthetic-post-1"),
                             self.position(2, 0, when=AFTER, event_id="synthetic-post-2"))
        self.assertFalse(report["ready_to_send"])
        self.assertTrue(report["audit"][0]["position_conflict"])

    def test_attachment_actions_dispatch_as_other(self):
        for raw in (self.upload(), self.field_created()):
            with self.subTest(action=raw["action"]):
                self.assertIn(raw["action"], OBSERVED_ACTIONS)
                self.assertEqual(parse_activity(raw, self.config), ("other", False))

    def test_duplicate_attachment_references_union_but_preserve_both_events(self):
        upload = self.upload()
        created = self.field_created(event_id="synthetic-field-event")
        report = self.report(upload, created)
        self.assertTrue(report["ready_to_send"])
        audit = report["audit"][0]
        self.assertEqual(audit["event_counts"], {"other": 2})
        self.assertEqual(len(audit["parsed_changes"]), 2)
        self.assertEqual(audit.get("attachment_references"), [{"file_id": FILE, "field_id": FIELD, "name": NAME}])
        self.assertIs(audit.get("attachment_conflict"), False)
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")
        self.assertEqual(report["summary_inputs"], [])

    def test_conflicting_attachment_reference_name_or_field_blocks(self):
        for changed in ({"name": "虚构冲突附件.pdf"}, {"field": "q" * 24}):
            with self.subTest(changed=changed):
                report = self.report(self.upload(), self.field_created(event_id="synthetic-field-event", **changed))
                self.assertFalse(report["ready_to_send"])
                self.assertIs(report["audit"][0].get("attachment_conflict"), True)
                self.assertTrue(report["audit"][0]["hard_review"])
                self.assertEqual(len(report["audit"][0]["parsed_changes"]), 2)

    def test_malformed_attachment_stays_unknown(self):
        report = self.report(self.event("customfield.create.v2", {"value": "unverified synthetic text"}))
        self.assertFalse(report["ready_to_send"])
        self.assertEqual(report["audit"][0]["event_counts"], {"unknown": 1})

    def test_involve_add_automation_does_not_supply_human_progress(self):
        report = self.report(self.event("involve.add", {"payload": {"fromAutomation": True}}))
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "other")
        self.assertEqual(report["summary_inputs"], [])

    def test_automatic_replay_keeps_context_and_attachment_names_out_of_formal_summary(self):
        self.task["executorId"] = OWNERS[2]
        events = [self.executor(OWNERS[0], OWNERS[1]), self.upload(event_id="synthetic-upload"),
                  self.executor(OWNERS[1], OWNERS[2], when=AFTER, event_id="synthetic-post-executor")]
        collection = self.collection(*events)
        client = RecordingSummary()
        report = build_automated_digest(collection, self.config, DAY, "synthetic-key", "gpt-6.1-sol", "max", client=client)
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(client.rows, [])
        self.assertEqual(report["digest"]["tasks"][0]["summary"], "")
        self.assertNotIn(NAME, report["markdown"])
        validate_automated_report(report, collection, self.config, "gpt-6.1-sol", "max")

    def test_only_window_comment_body_is_supplied_to_model_with_historical_context(self):
        self.task["executorId"] = OWNERS[2]
        collection = self.collection(
            self.executor(OWNERS[0], OWNERS[1]), self.upload(event_id="synthetic-upload"),
            self.executor(OWNERS[1], OWNERS[2], when=AFTER, event_id="synthetic-post-executor"),
            self.event("comment", {"comment": "虚构：仍待复核。"}, event_id="synthetic-window-comment"),
            self.event("comment", {"comment": "SYNTHETIC_POST_WINDOW_COMMENT"}, when=AFTER,
                       event_id="synthetic-post-comment"))
        client = RecordingSummary()
        report = build_automated_digest(collection, self.config, DAY, "synthetic-key", "gpt-6.1-sol", "max", client=client)
        self.assertEqual(client.rows, [{"task_key": "task-1", "title": "虚构历史窗口任务",
                                      "comment_bodies": ["虚构：仍待复核。"]}])
        self.assertEqual(report["digest"]["tasks"][0]["category"], "progress")
        self.assertNotIn(NAME, report["markdown"])
        validate_automated_report(report, collection, self.config, "gpt-6.1-sol", "max")


if __name__ == "__main__":
    unittest.main()
