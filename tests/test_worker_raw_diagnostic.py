"""Offline worker diagnostics follow raw activity scope, not semantic adapters."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app.smoke_data import make_smoke_service, SyntheticClient
from upcam_app.worker_api import WorkerApiError


class WorkerRawDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.network = patch("urllib.request.OpenerDirector.open",
                             side_effect=AssertionError("No network in worker diagnostics"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.service = make_smoke_service(self.root / "data")
        self.service.dispatch({"op": "initialize"})
        end = datetime.now().replace(second=0, microsecond=0)
        self.window = {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                       "end": end.strftime("%Y-%m-%d %H:%M")}
        self.service.dispatch({"op": "save", "payload": {
            "settings": self.service.settings_store.load(), "verification_window": self.window}})
        self.service.workflow_factory = lambda *_args, **_kwargs: self.fail(
            "Read-only diagnostics must not construct a sending workflow")

    def source_change(self, change):
        class ChangedClient(SyntheticClient):
            def collect(self, *args, **kwargs):
                source = super().collect(*args, **kwargs)
                change(source)
                return source

        self.service.client_factory = ChangedClient

    def diagnose(self):
        return self.service.dispatch({"op": "diagnose", "payload": self.window})

    def test_unknown_actions_and_rich_comments_pass_scope_diagnosis(self):
        def change(source):
            events = source["records"][0]["activities"]
            events[0]["action"] = "future.synthetic.activity"
            events[1]["content"] = json.dumps({
                "comment": "<table><tr><td>PRIVATE_RICH_COMMENT</td></tr></table>",
                "renderMode": "html", "futurePayload": {"value": "PRIVATE_CUSTOM_VALUE"}})

        self.source_change(change)
        result = self.diagnose()
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["task_count"], 1)
        self.assertNotIn("PRIVATE_RICH_COMMENT", json.dumps(result))
        self.assertNotIn("PRIVATE_CUSTOM_VALUE", json.dumps(result))
        self.assertEqual(self.service.dispatch({"op": "history"})["runs"], [])

    def test_shape_export_failure_preserves_scope_result_and_invalidates_prior_export(self):
        for failure in (ValueError("PRIVATE_EXPORT_VALUE"), RuntimeError("PRIVATE_EXPORT_RUNTIME")):
            with self.subTest(failure=type(failure).__name__):
                self.assertTrue(self.diagnose()["export_available"])
                with patch("upcam_app.worker_api.safe_structure_export", side_effect=failure):
                    result = self.diagnose()
                self.assertTrue(result["ok"], result["message"])
                self.assertEqual(result["task_count"], 1)
                self.assertFalse(result["export_available"])
                self.assertIn("导出", result["message"])
                self.assertNotIn("PRIVATE_EXPORT", json.dumps(result))
                self.assertNotIn("diagnostic_summary", result)
                self.assertNotIn("diagnostic_file_stem", result)
                with self.assertRaises(WorkerApiError):
                    self.service.dispatch({"op": "export_diagnostic", "payload": self.window})

    def test_missing_safe_metadata_stops_detailed_write_without_failing_scope_diagnosis(self):
        target = self.root / "detailed.json"
        with patch("upcam_app.worker_api.safe_structure_export",
                   side_effect=ValueError("PRIVATE_EXPORT_ERROR")):
            result = self.service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                **self.window, "path": str(target), "include_project_content": True}})
        self.assertTrue(result["ok"], result["message"])
        self.assertFalse(result["export_available"])
        self.assertFalse(result["detailed_export"]["saved"])
        self.assertFalse(target.exists())
        self.assertNotIn("PRIVATE_EXPORT_ERROR", json.dumps(result))

    def test_invalid_export_metadata_cannot_replace_scope_result_or_remain_exportable(self):
        shape = {"diagnostic": {"created_at_utc": "PRIVATE_INVALID_TIMESTAMP", "id": "synthetic"}}
        with patch("upcam_app.worker_api.safe_structure_export", return_value=shape):
            try:
                result = self.diagnose()
            except WorkerApiError:
                self.fail("Export metadata failure must not discard the scope diagnosis")
        self.assertTrue(result["ok"], result["message"])
        self.assertFalse(result["export_available"])
        self.assertNotIn("PRIVATE_INVALID_TIMESTAMP", json.dumps(result))
        with self.assertRaises(WorkerApiError):
            self.service.dispatch({"op": "export_diagnostic", "payload": self.window})

    def test_source_failure_is_retained_when_shape_export_also_fails(self):
        self.source_change(lambda source: source.update(pagination_complete=False))
        with patch("upcam_app.worker_api.safe_structure_export",
                   side_effect=RuntimeError("PRIVATE_EXPORT_ERROR")):
            result = self.diagnose()
        self.assertFalse(result["ok"])
        self.assertIn("分页", result["message"])
        self.assertFalse(result["export_available"])
        self.assertNotIn("PRIVATE_EXPORT_ERROR", json.dumps(result))

    def test_known_credentials_in_source_are_not_reported_as_ready(self):
        def change(source):
            source["records"][0]["activities"][0]["content"] = "synthetic-mcp-token"

        self.source_change(change)
        result = self.diagnose()
        self.assertFalse(result["ok"])
        self.assertIn("凭据", result["message"])
        self.assertNotIn("synthetic-mcp-token", json.dumps(result, ensure_ascii=False))

    def test_invalid_activity_time_still_blocks_scope_diagnosis(self):
        self.source_change(lambda source: source["records"][0]["activities"][0].update(
            createTime="2026-11-10T01:00:00"))
        result = self.diagnose()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["task_count"])

    def test_known_credentials_in_diagnostic_identity_cannot_be_exported(self):
        def change(source):
            record = source["records"][0]
            record["task"]["id"] = "synthetic-mcp-token"
            for event in record["activities"]:
                event["boundToObjectId"] = "synthetic-mcp-token"

        self.source_change(change)
        result = self.diagnose()
        self.assertFalse(result["ok"])
        self.assertFalse(result["export_available"])
        self.assertNotIn("synthetic-mcp-token", json.dumps(result))
        with self.assertRaises(WorkerApiError):
            self.service.dispatch({"op": "export_diagnostic", "payload": self.window})


if __name__ == "__main__":
    unittest.main()
