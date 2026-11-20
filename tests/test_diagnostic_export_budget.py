"""Small fictional exports exercise byte budgets and independent protocol coverage."""

import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from report_contract import updated_selection
from upcam_app import diagnostic_export as exporter


class DiagnosticExportBudgetTests(unittest.TestCase):
    def event(self, task_id, index, action="create", content=None, **fields):
        return {
            "id": f"synthetic-event-{index}", "action": action,
            "boundToObjectId": task_id, "boundToObjectType": "task",
            "createTime": "2026-01-02T01:00:00Z",
            "content": json.dumps(content or {}, ensure_ascii=False), **fields,
        }

    def record(self, task_id, activities):
        return {
            "task": {"id": task_id, "content": "PRIVATE_TASK_TITLE", "executorId": "synthetic-owner",
                     "tfsId": "synthetic-status", "isDone": False},
            "activities": activities,
        }

    def collection(self, records):
        return {"records": records, "pagination_complete": True,
                "selection": updated_selection("2026-01-02")}

    def export(self, records, day="2026-01-02"):
        report = exporter.safe_structure_export(self.collection(records), {}, day)
        self.assertIsInstance(report.get("protocol_catalog"), dict, "Independent protocol coverage is missing")
        return report

    def encoded(self, report):
        return json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")

    def digest(self, action):
        return hashlib.sha256(b"upcam-teambition-action/v1\0" + action.encode("ascii")).hexdigest()

    def group(self, report, action):
        return next(group for group in report["protocol_catalog"]["groups"]
                    if group["action_fingerprint"]["sha256"] == self.digest(action))

    def assert_catalog_accounting(self, catalog):
        self.assertEqual(catalog["unknown_events_considered"],
                         sum(group["event_count"] for group in catalog["groups"])
                         + catalog["unidentified_events"] + catalog["omitted_events"])

    def test_two_700_comment_tasks_fit_export_limit_and_keep_late_unknowns(self):
        # About 1.9 MB of source previously expanded to more than the CLI's 32 MiB ceiling.
        body = "synthetic line<br>" * 64
        records = []
        for task_index in range(2):
            task_id = f"synthetic-task-{task_index}"
            events = [self.event(task_id, i, "comment", {"comment": body, "renderMode": "markdown"})
                      for i in range(700)]
            events.append(self.event(task_id, "late-shared", "future.synthetic.shared"))
            if task_index == 1:
                events.append(self.event(task_id, "late-other", "future.synthetic.late"))
            records.append(self.record(task_id, events))
        self.assertLess(len(self.encoded(self.collection(records))), 2_500_000)

        report = exporter.safe_structure_export(self.collection(records), {}, "2026-01-02")

        self.assertLess(len(self.encoded(report)), 16 * 1024 * 1024)
        self.assertTrue(report["detail_truncated"])
        self.assertTrue(report["pagination_complete"])
        self.assertEqual(report["task_count"], 2)
        self.assertEqual(report["activity_count"], 1403)
        self.assertEqual(report["truncated_activity_count"],
                         1403 - sum(len(task["activities"]) for task in report["tasks"]))
        for task, original in zip(report["tasks"], records):
            self.assertEqual(task["truncated_activity_count"],
                             len(original["activities"]) - len(task["activities"]))
        catalog = report["protocol_catalog"]
        self.assertTrue(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 3)
        self.assertEqual(catalog["unscanned_events"], 0)
        self.assertEqual(self.group(report, "future.synthetic.shared")["event_count"], 2)
        self.assertEqual(self.group(report, "future.synthetic.shared")["task_count"], 2)
        self.assertEqual(self.group(report, "future.synthetic.late")["event_count"], 1)
        self.assertIn("protocol-catalog/v1", report["diagnostic"]["capabilities"])
        self.assert_catalog_accounting(catalog)
        self.assertNotIn(body, self.encoded(report).decode())

    def test_event_budget_stops_building_details_but_catalog_scans_remaining_events(self):
        task_id = "synthetic-task"
        records = [self.record(task_id, [
            self.event(task_id, "large", "comment", {"comment": "synthetic line<br>" * 64}),
            self.event(task_id, "large-2", "comment", {"comment": "synthetic line<br>" * 64}),
            self.event(task_id, "large-3", "comment", {"comment": "synthetic line<br>" * 64}),
            self.event(task_id, "late", "future.synthetic.late"),
            self.event(task_id, "later", "future.synthetic.late"),
        ])]
        with patch.object(exporter, "_MAX_EVENT_DETAIL_BYTES", 1024, create=True), \
                patch.object(exporter, "markup_shape", wraps=exporter.markup_shape) as markup:
            report = self.export(records)

        self.assertLessEqual(markup.call_count, 1)
        self.assertEqual(report["tasks"][0]["activities"], [])
        self.assertEqual(report["tasks"][0]["truncated_activity_count"], 5)
        self.assertEqual(report["truncated_activity_count"], 5)
        self.assertEqual(report["export_limits"]["charged_event_detail_bytes"], 0)
        self.assertIn("event_detail_byte_budget", report["detail_truncation_reasons"])
        self.assertTrue(report["protocol_catalog"]["complete"])
        self.assertEqual(self.group(report, "future.synthetic.late")["event_count"], 2)

    def test_window_and_unknown_time_events_survive_a_thousand_old_details(self):
        task_id = "synthetic-task"
        events = [self.event(task_id, f"old-{i}", createTime="2020-01-01T00:00:00Z")
                  for i in range(1000)]
        events.extend([
            self.event(task_id, "window", content={"comment": "PRIVATE_WINDOW_BODY"}),
            self.event(task_id, "unknown-time", createTime=None),
        ])
        records = [self.record(task_id, events)]
        original = deepcopy(records)

        report = self.export(records)

        details = report["tasks"][0]["activities"]
        self.assertEqual([item["event_id"] for item in details[:2]],
                         ["synthetic-event-window", "synthetic-event-unknown-time"])
        self.assertEqual([item["in_report_window"] for item in details[:2]], [True, None])
        self.assertEqual(len(details), 1000)
        self.assertEqual(report["activity_count"], 1002)
        self.assertEqual(report["truncated_activity_count"], 2)
        self.assertEqual(report["tasks"][0]["truncated_activity_count"], 2)
        self.assertIn("activity_count_limit", report["detail_truncation_reasons"])
        self.assertTrue(report["pagination_complete"])
        self.assertEqual(records, original)
        self.assertNotIn("PRIVATE_WINDOW_BODY", self.encoded(report).decode())

    def test_old_detail_bytes_cannot_displace_a_later_window_event(self):
        task_id = "synthetic-task"
        records = [self.record(task_id, [
            self.event(task_id, "old-large", "comment", {"comment": "PRIVATE_OLD_BODY<br>" * 64},
                       createTime="2020-01-01T00:00:00Z"),
            self.event(task_id, "window"),
        ])]
        with patch.object(exporter, "_MAX_EVENT_DETAIL_BYTES", 4096):
            report = self.export(records)

        self.assertEqual([item["event_id"] for item in report["tasks"][0]["activities"]],
                         ["synthetic-event-window"])
        self.assertEqual(report["truncated_activity_count"], 1)
        self.assertLessEqual(report["export_limits"]["charged_event_detail_bytes"], 4096)
        self.assertIn("event_detail_byte_budget", report["detail_truncation_reasons"])
        self.assertNotIn("PRIVATE_OLD_BODY", self.encoded(report).decode())

    def test_old_details_cannot_take_global_budget_before_other_tasks_window_events(self):
        first_id, second_id = "synthetic-first", "synthetic-second"
        first_events = [self.event(first_id, "first-window")]
        first_events.extend(self.event(first_id, f"old-{i}", createTime="2020-01-01T00:00:00Z")
                            for i in range(100))
        records = [self.record(first_id, first_events),
                   self.record(second_id, [self.event(second_id, "second-window")])]
        with patch.object(exporter, "_MAX_EVENT_DETAIL_BYTES", 6000):
            report = self.export(records)

        self.assertEqual([item["event_id"] for item in report["tasks"][1]["activities"]],
                         ["synthetic-event-second-window"])
        self.assertEqual(report["tasks"][1]["truncated_activity_count"], 0)
        self.assertEqual(report["truncated_activity_count"],
                         102 - sum(len(row["activities"]) for row in report["tasks"]))
        self.assertLessEqual(report["export_limits"]["charged_event_detail_bytes"], 6000)

    def test_missing_window_keeps_source_order_without_inferring_relevance(self):
        task_id = "synthetic-task"
        records = [self.record(task_id, [
            self.event(task_id, "old", createTime="2020-01-01T00:00:00Z"),
            self.event(task_id, "later"),
            self.event(task_id, "unknown-time", createTime=None),
        ])]
        report = self.export(records, day=None)
        self.assertEqual([item["event_id"] for item in report["tasks"][0]["activities"]],
                         ["synthetic-event-old", "synthetic-event-later", "synthetic-event-unknown-time"])
        self.assertTrue(all(item["in_report_window"] is None
                            for item in report["tasks"][0]["activities"]))

    def test_task_metadata_budget_can_omit_every_task_without_losing_catalog(self):
        records = [self.record(f"synthetic-task-{i}", [
            self.event(f"synthetic-task-{i}", "unknown", "future.synthetic.late")]) for i in range(2)]
        with patch.object(exporter, "_MAX_TASK_METADATA_BYTES", 1, create=True):
            report = self.export(records)

        self.assertEqual(report["tasks"], [])
        self.assertEqual(report["truncated_task_count"], 2)
        self.assertEqual(report["truncated_activity_count"], 2)
        self.assertTrue(report["detail_truncated"])
        self.assertIn("task_metadata_byte_budget", report["detail_truncation_reasons"])
        self.assertEqual(report["export_limits"]["charged_task_metadata_bytes"], 0)
        self.assertEqual(self.group(report, "future.synthetic.late")["task_count"], 2)
        self.assertTrue(report["protocol_catalog"]["complete"])

    def test_global_json_budget_fallback_keeps_catalog_and_true_omission_counts(self):
        task_id = "synthetic-task"
        records = [self.record(task_id, [
            self.event(task_id, "large", "comment", {"comment": "synthetic line<br>" * 64}),
            self.event(task_id, "late", "future.synthetic.late"),
        ])]
        with patch.object(exporter, "_MAX_EXPORT_BYTES", 6000, create=True):
            report = self.export(records)

        self.assertLessEqual(len(self.encoded(report)), 6000)
        self.assertEqual(report["tasks"], [])
        self.assertEqual(report["truncated_task_count"], 1)
        self.assertEqual(report["truncated_activity_count"], 2)
        self.assertIn("json_byte_budget", report["detail_truncation_reasons"])
        self.assertEqual(report["export_limits"]["charged_task_metadata_bytes"], 0)
        self.assertEqual(report["export_limits"]["charged_event_detail_bytes"], 0)
        self.assertTrue(report["protocol_catalog"]["complete"])
        self.assertEqual(self.group(report, "future.synthetic.late")["event_count"], 1)

    def test_catalog_is_independent_of_per_task_detail_limit_and_task_ids(self):
        records = [self.record("synthetic-same-task-id", [
            self.event("synthetic-same-task-id", f"known-{i}"),
            self.event("synthetic-same-task-id", f"first-{i}", "future.synthetic.shared"),
            self.event("synthetic-same-task-id", f"second-{i}", "future.synthetic.shared"),
        ]) for i in range(2)]
        with patch.object(exporter, "_MAX_ACTIVITIES_PER_TASK", 1):
            report = self.export(records)

        self.assertEqual([task["truncated_activity_count"] for task in report["tasks"]], [2, 2])
        self.assertEqual(report["truncated_activity_count"], 4)
        group = self.group(report, "future.synthetic.shared")
        self.assertEqual(group["event_count"], 4)
        self.assertEqual(group["task_count"], 2)
        self.assertTrue(report["protocol_catalog"]["complete"])
        self.assertNotIn("synthetic-same-task-id", json.dumps(report["protocol_catalog"]))

    def test_catalog_group_limit_counts_omitted_unknowns_and_keeps_existing_groups(self):
        actions = ["future.synthetic.a", "future.synthetic.b", "future.synthetic.c",
                   "future.synthetic.a", "future.synthetic.c", {"PRIVATE_ACTION": "PRIVATE_VALUE"}, "create"]
        task_id = "synthetic-task"
        records = [self.record(task_id, [self.event(task_id, i, action) for i, action in enumerate(actions)])]
        with patch.object(exporter, "_MAX_CATALOG_GROUPS", 2, create=True):
            report = self.export(records)

        catalog = report["protocol_catalog"]
        self.assertFalse(catalog["complete"])
        self.assertEqual(len(catalog["groups"]), 2)
        self.assertEqual(catalog["unknown_events_considered"], 6)
        self.assertEqual(catalog["unidentified_events"], 1)
        self.assertEqual(catalog["omitted_events"], 2)
        self.assertEqual(catalog["unscanned_events"], 0)
        self.assertEqual(self.group(report, "future.synthetic.a")["event_count"], 2)
        self.assert_catalog_accounting(catalog)

    def test_catalog_event_limit_includes_known_events_and_counts_all_unscanned(self):
        task_id = "synthetic-task"
        actions = ["create", "comment", "future.synthetic.first", "future.synthetic.late", "future.synthetic.late"]
        records = [self.record(task_id, [self.event(task_id, i, action) for i, action in enumerate(actions)])]
        with patch.object(exporter, "_MAX_CATALOG_EVENTS", 3, create=True):
            report = self.export(records)

        catalog = report["protocol_catalog"]
        self.assertFalse(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 1)
        self.assertEqual(catalog["unscanned_events"], 2)
        self.assertEqual(catalog["limits"]["events"], 3)
        self.assert_catalog_accounting(catalog)

    def test_catalog_task_limit_counts_later_source_events_as_unscanned(self):
        records = [self.record(f"synthetic-task-{i}", [
            self.event(f"synthetic-task-{i}", j, "future.synthetic.shared") for j in range(i + 1)])
            for i in range(2)]
        with patch.object(exporter, "_MAX_CATALOG_TASKS", 1, create=True):
            report = self.export(records)

        catalog = report["protocol_catalog"]
        self.assertFalse(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 1)
        self.assertEqual(catalog["unscanned_events"], 2)
        self.assertEqual(catalog["limits"]["tasks"], 1)
        self.assertFalse(report["detail_truncated"])

    def test_missing_window_never_claims_complete_or_unknown_absence(self):
        task_id = "synthetic-task"
        records = [self.record(task_id, [self.event(task_id, "unknown", "future.synthetic.hidden"),
                                        self.event(task_id, "known")])]

        report = self.export(records, day=None)

        catalog = report["protocol_catalog"]
        self.assertFalse(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 0)
        self.assertEqual(catalog["unknown_window_events"], 1)
        self.assertEqual(catalog["unscanned_events"], 0)
        self.assertEqual(catalog["groups"], [])

    def test_unknown_time_and_outside_window_have_distinct_catalog_accounting(self):
        task_id = "synthetic-task"
        times = [None, "2026-01-02T01:00:00", "2026-01-02T16:00:00Z", "2026-01-02T01:00:00Z"]
        records = [self.record(task_id, [self.event(task_id, i, "future.synthetic.hidden", createTime=value)
                                        for i, value in enumerate(times)])]

        report = self.export(records)

        catalog = report["protocol_catalog"]
        self.assertFalse(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 1)
        self.assertEqual(catalog["unknown_window_events"], 2)
        self.assertEqual(catalog["unscanned_events"], 0)
        self.assertEqual(self.group(report, "future.synthetic.hidden")["event_count"], 1)
        self.assert_catalog_accounting(catalog)

    def test_catalog_has_only_fingerprints_counts_and_limits_without_private_values(self):
        task_id = "synthetic-task"
        actions = [None, {"PRIVATE_KEY": "PRIVATE_VALUE"}, ["PRIVATE_VALUE"], "PRIVATE PERSON NAME",
                   "x" * 81, "https://private.invalid/PRIVATE_VALUE", "\ud800", "future.synthetic.hidden"]
        records = [self.record(task_id, [self.event(task_id, i, value) for i, value in enumerate(actions)])]

        report = self.export(records)

        catalog = report["protocol_catalog"]
        self.assertTrue(catalog["complete"])
        self.assertEqual(catalog["unknown_events_considered"], 8)
        self.assertEqual(catalog["unidentified_events"], 7)
        self.assertEqual(len(catalog["groups"]), 1)
        encoded = json.dumps(report, ensure_ascii=False)
        catalog_json = json.dumps(catalog)
        for private in ("PRIVATE_", "PRIVATE PERSON NAME", "private.invalid", "x" * 81,
                        "future.synthetic.hidden", "\ud800"):
            self.assertNotIn(private, encoded)
        self.assertNotIn(task_id, catalog_json)
        self.assertEqual(catalog["format"], "upcam-action-catalog-v1")
        self.assertEqual(catalog["limits"], {"tasks": 1000, "events": 50000, "groups": 1024})
        self.assert_catalog_accounting(catalog)

    def test_omission_counts_include_malformed_task_and_activity_records(self):
        task_id = "synthetic-task"
        records = [None, self.record(task_id, [None, self.event(task_id, "known")])]

        report = self.export(records)

        self.assertEqual(report["truncated_task_count"], 1)
        self.assertEqual(report["truncated_activity_count"], 1)
        self.assertEqual(report["tasks"][0]["truncated_activity_count"], 1)
        self.assertTrue(report["detail_truncated"])
        self.assertFalse(report["protocol_catalog"]["complete"])


if __name__ == "__main__":
    unittest.main()
