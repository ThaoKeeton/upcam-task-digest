"""Synthetic evidence for selecting formal task content without diagnostics."""

from copy import deepcopy
import unittest

from formal_content import FormalContentError, formal_task_content


class FormalContentTests(unittest.TestCase):
    def audit(self, **changes):
        return dict({
            "event_counts": {"created": 0, "comment": 0, "status": 0, "other": 0, "unknown": 0},
            "status_transitions": [], "parsed_changes": [],
        }, **changes)

    def comment(self, files=None, **changes):
        return dict({"extraction_needs_review": False,
                     "payload_evidence": {"files": [] if files is None else files},
                     "extra_evidence": {}}, **changes)

    def test_resolution_takes_precedence_over_comment_and_creation(self):
        audit = self.audit(event_counts={"created": 1, "comment": 1, "status": 1},
                           status_transitions=[{"resolved": True, "previous_resolved": False,
                                                "entered_resolved": True}])
        self.assertEqual(formal_task_content(audit, None, "虚构：复查通过。", True),
                         {"category": "resolved", "summary": "虚构：复查通过。"})

    def test_last_transition_controls_resolution_after_reopening(self):
        audit = self.audit(event_counts={"status": 2},
                           status_transitions=[{"resolved": True, "previous_resolved": False,
                                                "entered_resolved": True},
                                               {"resolved": False, "previous_resolved": True,
                                                "entered_resolved": False}])
        self.assertEqual(formal_task_content(audit, None, "", False),
                         {"category": "other", "summary": ""})
        self.assertEqual(formal_task_content(audit, None, "虚构：复查受阻。", True),
                         {"category": "progress", "summary": "虚构：复查受阻。"})

    def test_substantive_summary_takes_precedence_over_creation(self):
        audit = self.audit(event_counts={"created": 1, "comment": 1})
        self.assertEqual(formal_task_content(audit, None, "虚构：下一步复查。", True),
                         {"category": "progress", "summary": "虚构：下一步复查。"})

    def test_creation_without_supplement_keeps_explicit_empty_summary(self):
        audit = self.audit(event_counts={"created": 1, "other": 3})
        self.assertEqual(formal_task_content(audit, None, "", False),
                         {"category": "new", "summary": ""})

    def test_acknowledgement_does_not_supply_a_summary_or_progress(self):
        comment = self.comment(text="收到", omit_from_summary=True,
                               omission_reason="仅确认性回复候选；原文保留")
        result = formal_task_content(self.audit(event_counts={"comment": 1}),
                                     {"comments": [comment]}, "", False)
        self.assertEqual(result, {"category": "other", "summary": ""})

    def test_no_comment_entry_keeps_other_task_without_supplement(self):
        self.assertEqual(formal_task_content(self.audit(event_counts={"other": 2}),
                                            None, "", False),
                         {"category": "other", "summary": ""})

    def test_diagnostic_text_is_never_used_as_formal_content(self):
        injected = "新建记录 1 条；评论 9 条；原文摘录：SYNTHETIC_RAW_TEXT；来源未核实"
        audit = self.audit(event_counts={"comment": 1}, summary=injected,
                           descriptions=[injected], activity_evidence=[{"content": injected}])
        entry = {"comments": [self.comment(text=injected, raw_content=injected)],
                 "semantic_summary": injected}
        self.assertEqual(formal_task_content(audit, entry, "虚构：保留风险。", True),
                         {"category": "progress", "summary": "虚构：保留风险。"})

    def test_system_owner_chain_does_not_append_administrative_text(self):
        audit = self.audit(parsed_changes=[
            {"action": "update_executor", "needs_review": False,
             "previous_name": "虚构甲；原文摘录：甲", "target_name": "虚构乙",
             "from_automation": True},
            {"action": "update_executor", "needs_review": False,
             "previous_name": "虚构乙", "target_name": "虚构丙"},
        ])
        self.assertEqual(formal_task_content(audit, None, "虚构：等待验证。", True),
                         {"category": "progress", "summary": "虚构：等待验证。"})

    def test_owner_only_event_keeps_an_explicit_empty_supplement(self):
        audit = self.audit(event_counts={"other": 1}, parsed_changes=[{
            "action": "update_executor", "needs_review": False,
            "previous_name": "虚构甲", "target_name": "虚构乙", "from_automation": True,
        }])
        self.assertEqual(formal_task_content(audit, None, "", False),
                         {"category": "other", "summary": ""})

    def test_owner_change_needing_review_does_not_supply_display_names(self):
        audit = self.audit(parsed_changes=[{
            "action": "update_executor", "needs_review": True,
            "previous_name": "SYNTHETIC_UNVERIFIED_OLD", "target_name": "SYNTHETIC_UNVERIFIED_NEW",
        }])
        self.assertEqual(formal_task_content(audit, None, "", False),
                         {"category": "other", "summary": ""})

    def test_validated_file_metadata_does_not_supply_a_formal_summary(self):
        entry = {"comments": [
            self.comment(["a" * 24, "b" * 24], extra_evidence={"files": ["a" * 24, "b" * 24]}),
            self.comment(["b" * 24]),
            self.comment(payload_evidence={}, extra_evidence={"files": ["c" * 24]}),
        ]}
        self.assertEqual(formal_task_content(self.audit(event_counts={"comment": 3}),
                                            entry, "", False),
                         {"category": "other", "summary": ""})

    def test_material_comment_summary_has_no_appended_file_processing_notice(self):
        entry = {"comments": [self.comment(["a" * 24], file_count=999,
                                           attachment_only=True)]}
        self.assertEqual(formal_task_content(self.audit(), entry, "虚构：仍待复查。", True),
                         {"category": "progress", "summary": "虚构：仍待复查。"})

    def test_explicit_communication_about_deliverables_and_handoff_is_preserved(self):
        summary = "虚构：三份验收报告已补齐并交接给值班同事；兼容风险仍待复查。"
        entry = {"comments": [self.comment(["a" * 24])]}
        self.assertEqual(formal_task_content(self.audit(), entry, summary, True),
                         {"category": "progress", "summary": summary})

    def test_unreviewed_file_evidence_is_not_counted(self):
        entry = {"comments": [self.comment(["a" * 24], extraction_needs_review=True)]}
        self.assertEqual(formal_task_content(self.audit(), entry, "", False),
                         {"category": "other", "summary": ""})

    def test_invalid_file_lists_are_rejected_without_partial_counts(self):
        for files in (["a" * 24, "bad id"], ["a" * 24, {"id": "b" * 24}],
                      ["a" * 24, "a" * 24], None, "a" * 24):
            with self.subTest(files_type=type(files).__name__):
                entry = {"comments": [self.comment(payload_evidence={"files": files})]}
                with self.assertRaises(FormalContentError) as raised:
                    formal_task_content(self.audit(), entry, "", False)
                self.assertNotIn("bad id", str(raised.exception))

    def test_conflicting_file_sources_are_rejected_including_empty_payload(self):
        for payload_files in ([], ["a" * 24]):
            with self.subTest(payload_empty=not payload_files):
                entry = {"comments": [self.comment(payload_files,
                                                  extra_evidence={"files": ["b" * 24]})]}
                with self.assertRaises(FormalContentError):
                    formal_task_content(self.audit(), entry, "", False)

    def test_display_notices_and_moves_supply_no_counts_or_code_claims(self):
        audit = self.audit(event_counts={"other": 3},
                           parsed_changes=[
                               {"action": "standard", "pattern": "association-display-notice",
                                "association_kind": kind, "source_verified": False,
                                "object_verified": False, "title": "SYNTHETIC_NOTICE_TEXT"}
                               for kind in ("commit", "merge-request")
                           ] + [{"action": "move.cross.from.stage.to.stage",
                                 "pattern": "task-position-movement", "completion_evidence": False,
                                 "target": {"stage_label": "已完成"}}])
        self.assertEqual(formal_task_content(audit, None, "", False),
                         {"category": "other", "summary": ""})
        self.assertEqual(formal_task_content(audit, None, "虚构：等待复查。", True),
                         {"category": "progress", "summary": "虚构：等待复查。"})

    def test_inputs_and_evidence_flags_are_not_mutated(self):
        audit = self.audit(parsed_changes=[{"action": "standard", "source_verified": False,
                                           "object_verified": False, "needs_review": False}])
        entry = {"comments": [self.comment(["a" * 24])], "needs_review": True}
        original = deepcopy((audit, entry))
        formal_task_content(audit, entry, "", False)
        self.assertEqual((audit, entry), original)

    def test_malformed_selector_inputs_raise_fixed_local_errors(self):
        cases = [
            (None, None, "", False), (self.audit(event_counts={"created": True}), None, "", False),
            (self.audit(event_counts={"created": -1}), None, "", False),
            (self.audit(status_transitions=[{"resolved": "true"}]), None, "", False),
            (self.audit(parsed_changes=[None]), None, "", False),
            (self.audit(), {"comments": "SYNTHETIC_INVALID"}, "", False),
            (self.audit(), {"comments": [None]}, "", False),
            (self.audit(), None, None, False), (self.audit(), None, "", "true"),
        ]
        for args in cases:
            with self.subTest(audit_type=type(args[0]).__name__, proposed_type=type(args[2]).__name__):
                with self.assertRaises(FormalContentError) as raised:
                    formal_task_content(*args)
                self.assertNotIn("SYNTHETIC_INVALID", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
