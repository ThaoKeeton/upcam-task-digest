"""Synthetic regression tests for fresh, identifiable diagnostic exports."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from report_contract import SHANGHAI, updated_selection
from upcam_app import APP_VERSION, RULESET_REVISION
from upcam_app.diagnostic_export import safe_structure_export
from upcam_app.smoke_data import make_smoke_service


class DiagnosticProvenanceTests(unittest.TestCase):
    def test_metadata_identifies_version_creation_and_exact_window_without_api_text(self):
        start = datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI)
        end = datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI)
        collection = {"records": [], "pagination_complete": True,
                      "selection": updated_selection("2026-01-02", start, end)}
        before = datetime.now(timezone.utc)
        report = safe_structure_export(collection, {}, "2026-01-02", (start, end))
        other = safe_structure_export(collection, {}, "2026-01-02", (start, end))
        self.assertIn("diagnostic", report)
        meta = report["diagnostic"]
        self.assertEqual(report["format"], "upcam-teambition-safe-shape-v4")
        self.assertEqual(meta["application_version"], APP_VERSION)
        self.assertEqual(meta["ruleset_revision"], RULESET_REVISION)
        self.assertRegex(meta["id"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(meta["id"], other["diagnostic"]["id"])
        created = datetime.fromisoformat(meta["created_at_utc"])
        self.assertEqual(created.utcoffset(), timedelta(0))
        self.assertLessEqual(before, created)
        self.assertLessEqual(created, datetime.now(timezone.utc))
        self.assertEqual(meta["report_window"], {
            "start": start.isoformat(), "end_exclusive": end.isoformat(),
            "timezone": "Asia/Shanghai"})

    def test_legacy_scope_remains_explicitly_unknown(self):
        report = safe_structure_export({"records": []}, {})
        self.assertIn("diagnostic", report)
        self.assertIsNone(report["diagnostic"]["report_window"])
        self.assertFalse(report["window_evaluation_available"])

    def test_unmapped_current_member_id_is_distinct_from_task_id_and_omits_names(self):
        member = "b" * 24
        collection = {"records": [{"task": {
            "id": "synthetic-task", "executorId": member, "content": "PRIVATE_TASK_TITLE"},
            "activities": []}]}
        report = safe_structure_export(collection, {})
        self.assertIn("unmapped_current_owner_id", report["tasks"][0])
        self.assertEqual(report["tasks"][0]["unmapped_current_owner_id"], member)
        self.assertEqual(report["tasks"][0]["task_id"], "synthetic-task")
        mapped = safe_structure_export(collection, {"owner_names": {member: "PRIVATE_MEMBER_NAME"}})
        self.assertIsNone(mapped["tasks"][0]["unmapped_current_owner_id"])
        invalid = deepcopy(collection)
        invalid["records"][0]["task"]["executorId"] = "not an identifier PRIVATE_VALUE"
        self.assertIsNone(safe_structure_export(invalid, {})["tasks"][0]["unmapped_current_owner_id"])
        encoded = json.dumps([report, mapped, safe_structure_export(invalid, {})])
        for value in ("PRIVATE_TASK_TITLE", "PRIVATE_MEMBER_NAME", "PRIVATE_VALUE"):
            self.assertNotIn(value, encoded)


class DiagnosticExportContextTests(unittest.TestCase):
    def prepare(self, directory):
        service = make_smoke_service(Path(directory))
        end = datetime.now().replace(second=0, microsecond=0)
        window = {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                  "end": end.strftime("%Y-%m-%d %H:%M")}
        service.dispatch({"op": "save", "payload": {
            "settings": service.settings_store.load(), "verification_window": window}})
        result = service.dispatch({"op": "diagnose", "payload": window})
        self.assertTrue(result["ok"], result["message"])
        return service, window, result

    def test_current_export_has_unique_safe_filename_and_writes_cached_id(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, diagnostic = self.prepare(directory)
            self.assertIn("diagnostic_file_stem", diagnostic)
            stem = diagnostic["diagnostic_file_stem"]
            self.assertRegex(stem, r"^teambition-diagnostic-v[0-9.]+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
            shape = service.dispatch({"op": "export_diagnostic", "payload": window})["shape"]
            target = Path(directory) / (stem + ".json")
            service.dispatch({"op": "export_diagnostic_to_file", "payload": {**window, "path": str(target)}})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), shape)
            self.assertNotIn("synthetic-app-secret", target.read_text(encoding="utf-8"))

    def test_changed_window_cannot_export_old_diagnostic_or_write_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _ = self.prepare(directory)
            changed = {**window, "start": (datetime.strptime(window["start"], "%Y-%m-%d %H:%M")
                        - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")}
            target = Path(directory) / "stale.json"
            for operation, payload in (("export_diagnostic", changed),
                                       ("export_diagnostic_to_file", {**changed, "path": str(target)})):
                with self.assertRaisesRegex(ValueError, "重新运行只读诊断"):
                    service.dispatch({"op": operation, "payload": payload})
            self.assertFalse(target.exists())

    def test_changed_saved_configuration_or_teambition_identity_invalidates_export(self):
        for change in ("project", "mapping", "filter", "credential"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                service, window, _ = self.prepare(directory)
                settings = service.settings_store.load()
                if change == "project":
                    settings["project_id"] = "another-synthetic-project"
                elif change == "mapping":
                    settings["owner_names"]["demo-person"] = "虚构新姓名"
                elif change == "filter":
                    settings["view"]["task_equals"] = {"executorId": "demo-person"}
                else:
                    credentials = service.secret_store.load()
                    credentials["teambition_app_secret"] = "another-synthetic-secret"
                    service.secret_store.save(credentials)
                service.settings_store.save(settings)
                with self.assertRaisesRegex(ValueError, "重新运行只读诊断"):
                    service.dispatch({"op": "export_diagnostic", "payload": window})

    def test_failed_refresh_discards_previous_export_even_when_precondition_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _ = self.prepare(directory)
            credentials = service.secret_store.load()
            credentials.pop("teambition_app_secret")
            service.secret_store.save(credentials)
            with self.assertRaisesRegex(ValueError, "只读诊断需要"):
                service.dispatch({"op": "diagnose", "payload": window})
            with self.assertRaisesRegex(ValueError, "重新运行只读诊断"):
                service.dispatch({"op": "export_diagnostic", "payload": window})

    def test_export_without_an_explicit_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _window, _ = self.prepare(directory)
            with self.assertRaisesRegex(ValueError, "重新运行只读诊断"):
                service.dispatch({"op": "export_diagnostic"})

    def test_export_never_recollects_calls_openai_or_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _ = self.prepare(directory)
            with patch.object(service, "_client", side_effect=AssertionError("No new collection")), patch(
                    "urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
                service.dispatch({"op": "export_diagnostic", "payload": window})


if __name__ == "__main__":
    unittest.main()
