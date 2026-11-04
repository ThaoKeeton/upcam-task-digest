"""Only synthetic protocol identifiers; never use production credentials."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app.diagnostic_export import safe_structure_export


class ProtocolDiagnosticTests(unittest.TestCase):
    def export(self, action="future.synthetic_action", content=None):
        return safe_structure_export({"records": [{"task": {"id": "synthetic-task"}, "activities": [{
            "id": "synthetic-event", "action": action,
            "boundToObjectId": "synthetic-task", "boundToObjectType": "task",
            "createTime": "2026-01-02T01:00:00Z", "content": json.dumps(content or {}),
        }]}]}, {}, "2026-01-02")

    def test_unknown_protocol_has_domain_separated_fingerprint_without_plaintext(self):
        token = "future.synthetic_action"
        report = self.export(token)
        event = report["tasks"][0]["activities"][0]
        self.assertIn("action_fingerprint", event)
        fp = event["action_fingerprint"]
        self.assertEqual(fp["algorithm"], "sha256-upcam-action-v1")
        self.assertEqual(fp["sha256"], hashlib.sha256(b"upcam-teambition-action/v1\0" + token.encode()).hexdigest())
        self.assertEqual(fp["length"], len(token))
        self.assertNotIn(token, json.dumps(report))
        self.assertEqual(event["action"], "unknown")
        self.assertIsNone(event["action_token"])

    def test_non_protocol_or_overlong_value_never_leaks_or_hashes(self):
        for value in (None, False, 7, {"private": "PRIVATE_VALUE"}, ["PRIVATE_VALUE"],
                      "PRIVATE PERSON NAME", "a" * 81, "value\nPRIVATE_VALUE", "\ud800"):
            with self.subTest(kind=type(value).__name__):
                report = self.export(value)
                event = report["tasks"][0]["activities"][0]
                self.assertIn("action_fingerprint", event)
                self.assertIsNone(event["action_fingerprint"]["sha256"])
                encoded = json.dumps(report)
                for private in ("PRIVATE_VALUE", "PRIVATE PERSON NAME", "value\\n"):
                    self.assertNotIn(private, encoded)

    def test_fingerprint_changes_with_case_and_exact_spelling(self):
        values = ["future.synthetic_action", "Future.synthetic_action", "future.synthetic.action"]
        fingerprints = [self.export(v)["tasks"][0]["activities"][0].get("action_fingerprint") for v in values]
        self.assertTrue(all(fingerprints))
        self.assertEqual(len({f["sha256"] for f in fingerprints}), 3)

    def test_source_presence_metadata_never_proves_or_exports_a_commit(self):
        report = self.export("standard", {"title": "关联了提交: SYNTHETIC_PRIVATE_TITLE",
            "customCreator": {"name": "SYNTHETIC_PRIVATE_CREATOR"},
            "provider": "SYNTHETIC_PRIVATE_PROVIDER", "repository": {
                "url": "https://private.example/synthetic-repo", "_private_dynamic_key": "PRIVATE_VALUE"},
            "commit": {"sha": "c" * 40}})
        check = report["tasks"][0]["activities"][0]["commit_check"]
        self.assertIn("source_field_shapes", check)
        self.assertIn("provider", check["source_field_shapes"]["content"])
        self.assertIn("repository", check["source_field_shapes"]["content"])
        self.assertFalse(check["source_verified"])
        self.assertTrue(check["automatic_review_required"])
        encoded = json.dumps(report)
        for private in ("SYNTHETIC_PRIVATE_TITLE", "SYNTHETIC_PRIVATE_CREATOR", "SYNTHETIC_PRIVATE_PROVIDER",
                        "private.example", "_private_dynamic_key", "PRIVATE_VALUE", "c" * 40):
            self.assertNotIn(private, encoded)

    def test_title_and_creator_alone_explicitly_have_no_independent_source_fields(self):
        report = self.export("standard", {"title": "关联了提交", "customCreator": {"name": "虚构集成"}})
        check = report["tasks"][0]["activities"][0]["commit_check"]
        self.assertIn("source_field_shapes", check)
        self.assertEqual(check["source_field_shapes"], {"content": {}, "event": {}})
        self.assertFalse(check["source_verified"])

    def test_markup_diagnostic_is_metadata_only_and_never_approves_body(self):
        report = self.export("comment", {"comment": "<p>PRIVATE_BODY</p>", "renderMode": "markdown"})
        check = report["tasks"][0]["activities"][0]["comment_check"]
        self.assertIn("markup_shape", check)
        self.assertFalse(check["markup_shape"]["rendering_verified"])
        self.assertTrue(check["needs_review"])
        self.assertNotIn("PRIVATE_BODY", json.dumps(report))


class OfflineProtocolMatchTests(unittest.TestCase):
    def catalog_report(self):
        from upcam_app.diagnostic_protocol import action_fingerprint
        return {"format": "upcam-teambition-safe-shape-v5", "window_evaluation_available": True,
                "tasks": [], "protocol_catalog": {
                    "format": "upcam-action-catalog-v1", "complete": True,
                    "unknown_events_considered": 9, "unidentified_events": 1,
                    "omitted_events": 0, "unscanned_events": 0,
                    "groups": [{"action_fingerprint": action_fingerprint("future.synthetic_action"),
                                "event_count": 8, "task_count": 2}]}}

    def test_catalog_preserves_matching_after_event_details_are_omitted_without_double_counting(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = self.catalog_report()
        # Details may be a partial view of the same catalog and must not be added again.
        report["tasks"] = ProtocolDiagnosticTests().export()["tasks"]
        result = match_action_candidates(report, ["future.synthetic_action"])
        self.assertEqual(result["unknown_events_considered"], 9)
        self.assertEqual(result["unidentified_events"], 1)
        self.assertEqual(result["matches"][0]["event_count"], 8)
        self.assertEqual(result["matches"][0]["task_count"], 2)
        self.assertTrue(result["catalog_complete"])

    def test_catalog_limits_remain_explicit_in_offline_match_result(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = self.catalog_report()
        report["protocol_catalog"].update(complete=False, omitted_events=3,
                                           unknown_events_considered=12, unscanned_events=7)
        result = match_action_candidates(report, ["future.unmatched_action"])
        self.assertFalse(result["catalog_complete"])
        self.assertEqual(result["omitted_events"], 3)
        self.assertEqual(result["unscanned_events"], 7)
        self.assertEqual(result["unknown_events_considered"], 12)

    def test_unknown_window_membership_is_not_reported_as_complete_absence(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = self.catalog_report()
        report["protocol_catalog"].update(complete=False, unknown_window_events=3)
        result = match_action_candidates(report, ["future.unmatched_action"])
        self.assertEqual(result.get("unknown_window_events"), 3)
        self.assertFalse(result["catalog_complete"])
        report["protocol_catalog"]["complete"] = True
        with self.assertRaises(ValueError):
            match_action_candidates(report, ["future.unmatched_action"])

    def test_malformed_or_inconsistent_catalog_cannot_hide_events(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        changes = [dict(unknown_events_considered=0), dict(unidentified_events=-1),
                   dict(unidentified_events=True), dict(omitted_events=1),
                   dict(complete=True, unscanned_events=1), dict(complete="yes"), dict(groups="PRIVATE_VALUE"),
                   dict(unknown_window_events=-1), dict(unknown_window_events=True)]
        for change in changes:
            report = self.catalog_report()
            report["protocol_catalog"].update(change)
            with self.subTest(keys=list(change)), self.assertRaises(ValueError):
                match_action_candidates(report, ["future.synthetic_action"])
        report = self.catalog_report()
        report["protocol_catalog"]["groups"] *= 2
        report["protocol_catalog"]["unknown_events_considered"] = 17
        with self.assertRaises(ValueError):
            match_action_candidates(report, ["future.synthetic_action"])

    def test_offline_cli_outputs_only_candidate_matches(self):
        from upcam_app import diagnostic_protocol as protocol
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "虚构诊断.json"
            path.write_text(json.dumps(ProtocolDiagnosticTests().export()), encoding="utf-8")
            output = io.StringIO()
            with patch("sys.stdout", output), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
                self.assertEqual(protocol.main(["--input", str(path), "--action", "future.synthetic_action"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["matches"][0]["event_count"], 1)
            self.assertNotIn("synthetic-task", output.getvalue())

    def test_offline_cli_rejects_duplicate_keys_and_size_without_echoing_input(self):
        from upcam_app import diagnostic_protocol as protocol
        for payload in ('{"PRIVATE_CONTENT":1,"PRIVATE_CONTENT":2}', 'PRIVATE_CONTENT' * 10):
            with self.subTest(payload_kind=payload[0]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "诊断.json"
                path.write_text(payload, encoding="utf-8")
                error = io.StringIO()
                with patch("sys.stderr", error), patch.object(protocol, "_MAX_FILE_BYTES", 64):
                    self.assertEqual(protocol.main(["--input", str(path), "--action", "future.synthetic_action"]), 2)
                self.assertNotIn("PRIVATE_CONTENT", error.getvalue())

    def test_offline_match_identifies_exact_candidate_without_guessing_meaning(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = ProtocolDiagnosticTests().export()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
            result = match_action_candidates(report, ["future.wrong_action", "future.synthetic_action"])
        self.assertEqual(result["unknown_events_considered"], 1)
        self.assertEqual(result["unidentified_events"], 0)
        self.assertEqual(result["matches"], [{"candidate": "future.synthetic_action", "event_count": 1,
                                            "task_count": 1, "semantics_verified": False}])

    def test_legacy_missing_fingerprints_remain_unidentified_not_zero_unknowns(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = {"format": "upcam-teambition-safe-shape-v4", "tasks": [{"task_id": "synthetic-task", "activities": [{
            "action": "unknown", "action_token": None, "in_report_window": True}]}]}
        result = match_action_candidates(report, ["future.synthetic_action"])
        self.assertEqual(result["unidentified_events"], 1)
        self.assertEqual(result["matches"], [])

    def test_outside_or_unknown_window_and_invalid_fingerprints_do_not_match(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        report = ProtocolDiagnosticTests().export()
        report.pop("protocol_catalog", None)
        event = report["tasks"][0]["activities"][0]
        for membership in (False, None):
            event["in_report_window"] = membership
            self.assertEqual(match_action_candidates(report, ["future.synthetic_action"])["unknown_events_considered"], 0)
        event["in_report_window"] = True
        event["action_fingerprint"] = {"algorithm": "untrusted", "sha256": "a" * 64}
        self.assertEqual(match_action_candidates(report, ["future.synthetic_action"])["unidentified_events"], 1)

    def test_matcher_rejects_unbounded_candidates_and_malformed_diagnostics(self):
        from upcam_app.diagnostic_protocol import match_action_candidates
        for candidates in (["candidate"] * 129, ["PRIVATE PERSON NAME"], [None], "future.synthetic_action"):
            with self.assertRaises(ValueError):
                match_action_candidates(ProtocolDiagnosticTests().export(), candidates)
        for report in ({}, {"format": "raw-api", "tasks": []}, {"format": "upcam-teambition-safe-shape-v5", "tasks": {}}):
            with self.assertRaises(ValueError):
                match_action_candidates(report, ["future.synthetic_action"])


if __name__ == "__main__":
    unittest.main()
