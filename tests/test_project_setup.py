"""Synthetic-only tests for the in-app Teambition project setup flow."""

import copy
import json
from pathlib import Path
import unittest

from report_contract import updated_selection
from upcam_app.project_setup import (
    ProjectSetupError, analyze_project_probe, merge_owner_mappings, parse_owner_mapping_tsv,
)


FIXTURE = Path(__file__).parent / "fixtures" / "collection.json"


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.collection = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.collection["selection"] = updated_selection("2026-01-02")
        self.collection["records"][0]["task"]["updated"] = "2026-01-02T00:30:00Z"
        self.expected_selection = self.collection["selection"]

    def analyze(self, collection=None):
        return analyze_project_probe(
            self.collection if collection is None else collection,
            tenant_id="demo-org", project_id="demo-project",
            expected_selection=self.expected_selection,
            include_archived=False,
        )

    def test_extracts_observed_ids_and_only_explicit_status_name_suggestions(self):
        probe = self.analyze()

        self.assertEqual(probe.owner_ids, ("demo-person",))
        self.assertEqual(probe.status_ids, ("demo-done", "demo-open"))
        self.assertEqual(probe.status_name_suggestions, {
            "demo-done": "已解决", "demo-open": "待处理",
        })
        self.assertEqual(probe.task_count, 1)
        self.assertEqual(probe.activity_count, 3)
        self.assertTrue(probe.api_contract_verified)
        self.assertFalse(probe.view_verified)

    def test_incomplete_pagination_cannot_verify_or_produce_candidates(self):
        broken = copy.deepcopy(self.collection)
        broken["pagination_complete"] = False

        with self.assertRaises(ProjectSetupError):
            self.analyze(broken)

    def test_task_updated_outside_activity_window_does_not_invalidate_real_field_sample(self):
        for updated in ("2020-01-01T00:00:00Z", "2026-01-05T12:00:00+08:00"):
            with self.subTest(updated=updated):
                collection = copy.deepcopy(self.collection)
                collection["records"][0]["task"]["updated"] = updated
                self.assertTrue(self.analyze(collection).api_contract_verified)

    def test_malformed_task_version_timestamp_still_blocks_field_verification(self):
        for updated in (None, "invalid", "2026-01-02T10:00:00"):
            with self.subTest(updated=updated):
                collection = copy.deepcopy(self.collection)
                collection["records"][0]["task"]["updated"] = updated
                with self.assertRaises(ProjectSetupError):
                    self.analyze(collection)

    def test_wrong_project_or_query_window_cannot_be_verified(self):
        wrong_project = copy.deepcopy(self.collection)
        wrong_project["project_id"] = "other-project"
        wrong_window = copy.deepcopy(self.collection)
        wrong_window["selection"]["start"] = "2026-01-02T01:00:00+08:00"

        for broken in (wrong_project, wrong_window):
            with self.subTest(broken=broken["project_id"]):
                with self.assertRaises(ProjectSetupError):
                    self.analyze(broken)

    def test_empty_tasks_or_missing_activity_schema_cannot_verify_fields(self):
        empty = copy.deepcopy(self.collection)
        empty["records"] = []
        no_activity = copy.deepcopy(self.collection)
        no_activity["records"][0]["activities"] = []

        for broken in (empty, no_activity):
            with self.subTest(records=len(broken["records"])):
                with self.assertRaises(ProjectSetupError):
                    self.analyze(broken)

    def test_conflicting_event_names_are_not_suggested_as_verified_mappings(self):
        conflict = copy.deepcopy(self.collection)
        event = copy.deepcopy(conflict["records"][0]["activities"][-1])
        event["id"] = "demo-event-conflict"
        event["content"] = json.dumps({
            "tfsId": "demo-done", "oldTfsId": "demo-open",
            "taskflowstatus": "另一个名称", "oldTaskflowstatus": "待处理",
        }, ensure_ascii=False)
        conflict["records"][0]["activities"].append(event)

        probe = self.analyze(conflict)

        self.assertNotIn("demo-done", probe.status_name_suggestions)
        self.assertIn("demo-done", probe.conflicting_status_ids)

    def test_bulk_owner_mapping_import_accepts_json_objects_and_tab_rows(self):
        self.assertEqual(
            parse_owner_mapping_tsv('{"demo-person":"虚构成员","demo-person-2":"虚构乙"}'),
            {"demo-person": "虚构成员", "demo-person-2": "虚构乙"},
        )
        self.assertEqual(parse_owner_mapping_tsv("demo-person\t虚构成员\n"),
                         {"demo-person": "虚构成员"})

    def test_bulk_owner_mapping_import_is_atomic_and_rejects_malformed_or_duplicate_rows(self):
        for source in ("demo-person,虚构成员", "demo-person\t\n",
                       "demo-person\t虚构成员\ndemo-person\t另一个姓名",
                       '{"demo-person":"虚构成员","demo-person":"另一个姓名"}',
                       '{"demo-person":"虚构成员","demo-person-2":"  "}',
                       '{"demo-person":"虚构成员",}'):
            with self.subTest(source=source), self.assertRaises(ProjectSetupError):
                parse_owner_mapping_tsv(source)

    def test_bulk_owner_mapping_merge_refuses_silent_name_override(self):
        self.assertEqual(
            merge_owner_mappings({"demo-person": "虚构成员"}, {"other-person": "虚构乙"}),
            {"demo-person": "虚构成员", "other-person": "虚构乙"},
        )
        with self.assertRaises(ProjectSetupError):
            merge_owner_mappings({"demo-person": "虚构成员"}, {"demo-person": "另一个姓名"})


if __name__ == "__main__":
    unittest.main()
