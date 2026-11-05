"""Fictional events keep window coverage distinct from undetermined event time."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app import diagnostic_export as exporter
from upcam_app import diagnostic_protocol as protocol


IN_WINDOW_DEFINITION = (
    "Unsupported-action events confirmed inside the report window among scanned events."
)
UNKNOWN_TIME_DEFINITION = (
    "Unsupported-action events with undetermined report-window membership because event "
    "time is missing/invalid or the report window is unavailable."
)


class DiagnosticCounterClarityTests(unittest.TestCase):
    def event(self, index, action="future.synthetic.counter", **fields):
        return {
            "id": f"synthetic-counter-event-{index}", "action": action,
            "boundToObjectId": "synthetic-counter-task", "boundToObjectType": "task",
            "createTime": "2026-01-02T01:00:00Z", "content": "{}", **fields,
        }

    def record(self, activities):
        return {"task": {"id": "synthetic-counter-task"}, "activities": activities}

    def export(self, activities, day="2026-01-02"):
        return exporter.safe_structure_export({"records": [self.record(activities)]}, {}, day)

    def legacy_catalog_report(self, unknown_window=0):
        return {
            "format": "upcam-teambition-safe-shape-v5", "tasks": [],
            "protocol_catalog": {
                "format": "upcam-action-catalog-v1", "complete": unknown_window == 0,
                "unknown_events_considered": 1, "unknown_window_events": unknown_window,
                "unidentified_events": 0, "omitted_events": 0, "unscanned_events": 0,
                "groups": [{
                    "action_fingerprint": protocol.action_fingerprint("future.synthetic.counter"),
                    "event_count": 1, "task_count": 1,
                }],
            },
        }

    def assert_explicit_counts(self, value, in_window, unknown_time):
        self.assertEqual(value.get("unknown_events_in_report_window"), in_window)
        self.assertEqual(value.get("unknown_events_with_unknown_time"), unknown_time)
        definitions = value.get("counter_definitions", {})
        self.assertEqual(definitions.get("unknown_events_considered"), IN_WINDOW_DEFINITION)
        self.assertEqual(definitions.get("unknown_window_events"), UNKNOWN_TIME_DEFINITION)

    def test_window_unknown_uses_explicit_alias_without_changing_correct_zero(self):
        report = self.export([self.event(0, content=json.dumps({
            "comment": "SYNTHETIC_PRIVATE_COMMENT", "taskflowstatus": "虚构状态",
        }))])
        catalog = report["protocol_catalog"]

        self.assertEqual(catalog["unknown_events_considered"], 1)
        self.assertEqual(catalog["unknown_window_events"], 0)
        self.assert_explicit_counts(catalog, 1, 0)
        self.assertTrue(catalog["complete"])
        self.assertEqual(report["format"], "upcam-teambition-safe-shape-v5")
        self.assertIn("explicit-counter-labels/v1", report["diagnostic"]["capabilities"])
        event = report["tasks"][0]["activities"][0]
        self.assertEqual(event["action"], "unknown")
        self.assertIsNone(event["action_token"])
        self.assertNotIn("future.synthetic.counter", json.dumps(report))
        self.assertNotIn("SYNTHETIC_PRIVATE_COMMENT", json.dumps(report))

        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 1, 0)
        self.assertEqual(result["matches"], [{"candidate": "future.synthetic.counter",
                                            "event_count": 1, "task_count": 1,
                                            "semantics_verified": False}])

    def test_bad_times_and_outside_window_have_separate_counts(self):
        times = [None, "not-a-timestamp", "2026-01-02T01:00:00",
                 "2026-01-01T15:59:59Z", "2026-01-01T16:00:00Z",
                 "2026-01-02T15:59:59Z", "2026-01-02T16:00:00Z"]
        report = self.export([self.event(i, createTime=value) for i, value in enumerate(times)]
                             + [self.event("known", "create", createTime=None)])
        catalog = report["protocol_catalog"]

        self.assert_explicit_counts(catalog, 2, 3)
        self.assertEqual(catalog["unknown_events_considered"], 2)
        self.assertEqual(catalog["unknown_window_events"], 3)
        self.assertEqual(catalog["groups"][0]["event_count"], 2)
        self.assertFalse(catalog["complete"])
        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 2, 3)
        self.assertFalse(result["catalog_complete"])
        self.assertEqual(result["matches"][0]["event_count"], 2)

    def test_missing_report_window_remains_undetermined_and_incomplete(self):
        report = self.export([self.event(0), self.event(1, "create")], day=None)
        self.assert_explicit_counts(report["protocol_catalog"], 0, 1)
        self.assertFalse(report["protocol_catalog"]["complete"])
        self.assertEqual(report["protocol_catalog"]["unscanned_events"], 0)
        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 0, 1)
        self.assertFalse(result["catalog_complete"])
        self.assertEqual(result["matches"], [])

    def test_cli_supplies_fixed_labels_for_legacy_v5_without_aliases(self):
        report = self.legacy_catalog_report(unknown_window=2)
        # Input prose cannot redefine public counters or be reflected in output.
        report["protocol_catalog"]["counter_definitions"] = {
            "unknown_window_events": "SYNTHETIC_PRIVATE_MISLEADING_DEFINITION",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-counter-diagnostic.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            output = io.StringIO()
            with patch("sys.stdout", output), patch(
                    "urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
                status = protocol.main(["--input", str(path), "--action", "future.synthetic.counter"])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assert_explicit_counts(result, 1, 2)
        self.assertEqual(result["unknown_events_considered"], 1)
        self.assertEqual(result["unknown_window_events"], 2)
        self.assertFalse(result["catalog_complete"])
        self.assertNotIn("SYNTHETIC_PRIVATE_MISLEADING_DEFINITION", output.getvalue())

    def test_legacy_catalog_without_unknown_time_field_keeps_zero_default(self):
        report = self.legacy_catalog_report()
        report["protocol_catalog"].pop("unknown_window_events")
        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 1, 0)
        self.assertTrue(result["catalog_complete"])

    def test_detail_only_legacy_report_counts_undetermined_membership_separately(self):
        report = {
            "format": "upcam-teambition-safe-shape-v4", "tasks": [{"activities": [
                {"action": "unknown", "in_report_window": True},
                {"action": "unknown", "in_report_window": False},
                {"action": "unknown", "in_report_window": None},
                {"action": "unknown"},
                {"action": "create", "in_report_window": None},
            ]}],
        }
        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 1, 2)
        self.assertEqual(result["unknown_events_considered"], 1)
        self.assertEqual(result["unknown_window_events"], 2)
        self.assertEqual(result["unidentified_events"], 1)
        self.assertEqual(result["matches"], [])

    def test_matching_explicit_aliases_are_accepted(self):
        report = self.legacy_catalog_report(unknown_window=2)
        report["protocol_catalog"].update(unknown_events_in_report_window=1,
                                           unknown_events_with_unknown_time=2)
        result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
        self.assert_explicit_counts(result, 1, 2)

    def test_inconsistent_or_non_integer_aliases_are_rejected(self):
        changes = [
            {"unknown_events_in_report_window": value}
            for value in (0, 2, True, 1.0, "1", None, -1)
        ] + [
            {"unknown_events_with_unknown_time": value}
            for value in (1, False, 0.0, "0", None, -1)
        ]
        for change in changes:
            with self.subTest(change=change):
                report = self.legacy_catalog_report()
                report["protocol_catalog"].update(change)
                with self.assertRaises(ValueError):
                    protocol.match_action_candidates(report, ["future.synthetic.counter"])

    def test_cli_rejects_inconsistent_alias_without_outputting_matches_or_input(self):
        report = self.legacy_catalog_report()
        report["protocol_catalog"]["unknown_events_in_report_window"] = 0
        report["synthetic_private"] = "SYNTHETIC_PRIVATE_REJECTED_CONTENT"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-invalid-counter.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            output, error = io.StringIO(), io.StringIO()
            with patch("sys.stdout", output), patch("sys.stderr", error):
                status = protocol.main(["--input", str(path), "--action", "future.synthetic.counter"])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("SYNTHETIC_PRIVATE_REJECTED_CONTENT", error.getvalue())

    def test_catalog_capacity_limits_keep_explicit_partial_counts(self):
        cases = [
            ("_MAX_CATALOG_GROUPS", [self.record([
                self.event(0, "future.synthetic.first"), self.event(1, "future.synthetic.second")])],
             2, 1, 0),
            ("_MAX_CATALOG_EVENTS", [self.record([
                self.event(0, "create"), self.event(1, "future.synthetic.second")])],
             0, 0, 1),
            ("_MAX_CATALOG_TASKS", [self.record([self.event(0, "create")]),
                                    self.record([self.event(1, "future.synthetic.second")])],
             0, 0, 1),
        ]
        for limit, records, in_window, omitted, unscanned in cases:
            with self.subTest(limit=limit), patch.object(exporter, limit, 1):
                report = exporter.safe_structure_export({"records": records}, {}, "2026-01-02")
            catalog = report["protocol_catalog"]
            self.assert_explicit_counts(catalog, in_window, 0)
            self.assertFalse(catalog["complete"])
            self.assertEqual(catalog["omitted_events"], omitted)
            self.assertEqual(catalog["unscanned_events"], unscanned)
            result = protocol.match_action_candidates(report, ["future.synthetic.second"])
            self.assert_explicit_counts(result, in_window, 0)
            self.assertFalse(result["catalog_complete"])
            self.assertEqual(result["omitted_events"], omitted)
            self.assertEqual(result["unscanned_events"], unscanned)

    def test_omitted_event_details_do_not_lose_catalog_counter_labels(self):
        for limit in ("_MAX_EVENT_DETAIL_BYTES", "_MAX_TASK_METADATA_BYTES"):
            with self.subTest(limit=limit), patch.object(exporter, limit, 1):
                report = self.export([self.event(0)])
            self.assertTrue(report["detail_truncated"])
            self.assertEqual(report["truncated_activity_count"], 1)
            self.assert_explicit_counts(report["protocol_catalog"], 1, 0)
            self.assertTrue(report["protocol_catalog"]["complete"])
            result = protocol.match_action_candidates(report, ["future.synthetic.counter"])
            self.assert_explicit_counts(result, 1, 0)
            self.assertTrue(result["catalog_complete"])
            self.assertEqual(result["matches"][0]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
