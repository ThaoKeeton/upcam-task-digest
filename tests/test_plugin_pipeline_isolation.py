"""Standalone raw summaries must not change Workbench's comment-only contract."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app import PLUGIN_BACKEND_VERSION
from upcam_app.plugin_worker import PluginWorkerService
from upcam_app.smoke_data import make_smoke_service, SyntheticClient, SyntheticSummarizer
from upcam_app.worker_api import WorkerApiError
from upcam_app.workflow import DigestWorkflow


WINDOW = {"start": "2026-11-03 16:50", "end": "2026-11-04 08:50"}
HANDSHAKE = {"protocol_version": 1, "backend_version": PLUGIN_BACKEND_VERSION,
             "nonce": "0123456789abcdef0123456789abcdef"}


class PluginPipelineIsolationTests(unittest.TestCase):
    def test_plugin_detailed_export_keeps_the_read_only_diagnosis_text(self):
        source = (Path(__file__).resolve().parents[1] / "plugins/UPCAM.Workbench/DigestPage.cs").read_text(encoding="utf-8")
        method = source.split("private static string ApplyDetailedExportResult", 1)[1].split(
            "private FrameworkElement BuildHistory", 1)[0]
        self.assertNotIn("ApplyDiagnostic(", method)
        self.assertNotIn("data.Output", method)
        self.assertIn("诊断结果已保留", method)
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="upcam-pipeline-isolation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        network = patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("No live services in isolation tests"))
        network.start()
        self.addCleanup(network.stop)

    def fixture(self, kind, suffix):
        directory = self.root / (kind + "-" + suffix)
        if kind == "synthetic":
            entry = PluginWorkerService(directory, synthetic=True)
            entry.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
            service = entry._service
        else:
            seeded = make_smoke_service(directory)
            if kind == "standalone":
                entry = service = seeded
            else:
                options = {"service_factory": lambda _directory: seeded} if kind == "factory" else {}
                entry = PluginWorkerService(directory, **options)
                entry.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
                service = entry._service
                if kind == "production":
                    # Exercise the real production service constructor, then
                    # replace only external/OS dependencies with synthetic ones.
                    service.secret_store = seeded.secret_store
                    service.model_catalog = seeded.model_catalog
                    service.client_factory = SyntheticClient
                    service.startup_configurer = seeded.startup_configurer
        initial = entry.dispatch({"op": "initialize"})
        entry.dispatch({"op": "save", "payload": {
            "settings": initial["settings"], "verification_window": WINDOW}})
        entry.dispatch({"op": "refresh_models"})
        calls = {"comments": [], "raw": [], "sent": []}

        class RecordingSummarizer(SyntheticSummarizer):
            def summarize(self, key, model, effort, rows, rules, service_tier="default"):
                calls["comments"].append(rows)
                return super().summarize(key, model, effort, rows, rules, service_tier)

            def summarize_activities(self, key, model, effort, rows, rules, service_tier="default"):
                calls["raw"].append(rows)
                return super().summarize_activities(key, model, effort, rows, rules, service_tier)

        class Sender:
            def send_markdown(self, _webhook, _secret, markdown, **_kwargs):
                calls["sent"].append(markdown)
                return True

        # Deliberately no **kwargs: host selection cannot break custom factories.
        # Deliberately creates a default workflow after the plugin handshake:
        # the actual run still must inherit the plugin's legacy contract.
        def workflow(settings, credentials, data_dir, *, history, model_catalog):
            return DigestWorkflow(settings, credentials, data_dir, history=history,
                                  model_catalog=model_catalog, client_factory=service.client_factory,
                                  summarizer=RecordingSummarizer(), ding_sender=Sender())

        service.workflow_factory = workflow
        return entry, service, calls

    @staticmethod
    def change_source(service, change):
        class ChangedClient(SyntheticClient):
            def collect(self, *args, **kwargs):
                source = super().collect(*args, **kwargs)
                change(source)
                return source

        service.client_factory = ChangedClient

    def test_all_plugin_construction_paths_keep_legacy_diagnostics(self):
        for kind in ("production", "synthetic", "factory"):
            with self.subTest(kind=kind):
                entry, service, calls = self.fixture(kind, "diagnostics")

                def change(source):
                    events = source["records"][0]["activities"]
                    events[0]["action"] = "future.plugin.unsupported"
                    events[1]["content"] = json.dumps({
                        "comment": "<table>PRIVATE_PLUGIN_RICH_TEXT</table>", "renderMode": "html"})

                self.change_source(service, change)
                result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
                self.assertFalse(result["ok"], result["message"])
                self.assertIn("未识别动态", result["message"])
                self.assertNotIn("PRIVATE_PLUGIN_RICH_TEXT", json.dumps(result))
                with self.assertRaises(WorkerApiError):
                    entry.dispatch({"op": "run_manual", "payload": WINDOW})
                self.assertEqual(calls, {"comments": [], "raw": [], "sent": []})

    def test_plugin_diagnostics_do_not_add_standalone_credential_gates(self):
        for kind in ("production", "synthetic", "factory"):
            with self.subTest(kind=kind):
                entry, service, calls = self.fixture(kind, "diagnostic-credentials")
                credential = service.secret_store.load()["teambition_mcp_token"]
                self.change_source(service, lambda source: source["records"][0]["activities"][1].update(
                    content=json.dumps({"comment": credential, "renderMode": "markdown"})))
                with patch("upcam_app.worker_api.ReportCredentialGuard",
                           side_effect=AssertionError("Standalone-only diagnostic gate")) as guard:
                    result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
                self.assertTrue(result["ok"], result["message"])
                self.assertTrue(result["export_available"])
                guard.assert_not_called()
                exported = entry.dispatch({"op": "export_diagnostic", "payload": WINDOW})
                self.assertNotIn(credential, json.dumps((result, exported)))
                self.assertEqual(calls, {"comments": [], "raw": [], "sent": []})

    def test_plugin_structure_export_failures_keep_legacy_diagnostic_failure(self):
        for kind in ("production", "synthetic", "factory"):
            entry, service, _calls = self.fixture(kind, "diagnostic-export-errors")
            for failure in (ValueError("Synthetic structure export failure"),
                            RuntimeError("PRIVATE_SYNTHETIC_EXPORT_ERROR")):
                with self.subTest(kind=kind, failure=type(failure).__name__):
                    self.assertTrue(entry.dispatch({"op": "diagnose", "payload": WINDOW})["export_available"])
                    with patch("upcam_app.worker_api.safe_structure_export", side_effect=failure):
                        result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
                    self.assertFalse(result["ok"], result["message"])
                    self.assertIsNone(result["task_count"])
                    self.assertFalse(result["export_available"])
                    self.assertIsNone(service._last_diagnostic_shape)
                    if isinstance(failure, ValueError):
                        self.assertEqual(result["message"], str(failure))
                    else:
                        self.assertEqual(result["message"],
                            "只读诊断未完成；没有调用 OpenAI、没有发送钉钉，详细 API 响应不会写入记录。请检查网络、凭据和项目权限。")
                    with self.assertRaises(WorkerApiError):
                        entry.dispatch({"op": "export_diagnostic", "payload": WINDOW})

    def test_plugin_invalid_export_metadata_keeps_legacy_dispatch_failure(self):
        for kind in ("production", "synthetic", "factory"):
            with self.subTest(kind=kind):
                entry, _service, _calls = self.fixture(kind, "diagnostic-export-metadata")
                shape = {"diagnostic": {"created_at_utc": "PRIVATE_INVALID_TIMESTAMP", "id": "synthetic"}}
                with patch("upcam_app.worker_api.safe_structure_export", return_value=shape):
                    with self.assertRaises(WorkerApiError) as caught:
                        entry.dispatch({"op": "diagnose", "payload": WINDOW})
                self.assertEqual(str(caught.exception),
                                 "操作未完成；详细 API 响应和凭据没有写入日志。请检查设置并重试。")

    def test_standalone_diagnostic_keeps_new_credential_gate(self):
        entry, service, calls = self.fixture("standalone", "diagnostic-credentials")
        credential = service.secret_store.load()["teambition_mcp_token"]
        self.change_source(service, lambda source: source["records"][0]["activities"][1].update(
            content=json.dumps({"comment": credential, "renderMode": "markdown"})))
        result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
        self.assertFalse(result["ok"])
        self.assertIn("凭据", result["message"])
        self.assertNotIn(credential, json.dumps(result))
        self.assertEqual(calls, {"comments": [], "raw": [], "sent": []})

    def test_standalone_structure_export_errors_do_not_fail_scope_diagnostic(self):
        entry, service, _calls = self.fixture("standalone", "diagnostic-export-errors")
        for failure in (ValueError("PRIVATE_SYNTHETIC_EXPORT_ERROR"),
                        RuntimeError("PRIVATE_SYNTHETIC_EXPORT_ERROR")):
            with self.subTest(failure=type(failure).__name__):
                self.assertTrue(entry.dispatch({"op": "diagnose", "payload": WINDOW})["export_available"])
                with patch("upcam_app.worker_api.safe_structure_export", side_effect=failure):
                    result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
                self.assertTrue(result["ok"], result["message"])
                self.assertEqual(result["task_count"], 1)
                self.assertFalse(result["export_available"])
                self.assertIsNone(service._last_diagnostic_shape)
                self.assertNotIn("PRIVATE_SYNTHETIC_EXPORT_ERROR", json.dumps(result))
                with self.assertRaises(WorkerApiError):
                    entry.dispatch({"op": "export_diagnostic", "payload": WINDOW})

    def test_standalone_invalid_export_metadata_does_not_fail_scope_diagnostic(self):
        entry, service, _calls = self.fixture("standalone", "diagnostic-export-metadata")
        shape = {"diagnostic": {"created_at_utc": "PRIVATE_INVALID_TIMESTAMP", "id": "synthetic"}}
        with patch("upcam_app.worker_api.safe_structure_export", return_value=shape):
            result = entry.dispatch({"op": "diagnose", "payload": WINDOW})
        self.assertTrue(result["ok"], result["message"])
        self.assertFalse(result["export_available"])
        self.assertIsNone(service._last_diagnostic_shape)
        self.assertNotIn("PRIVATE_INVALID_TIMESTAMP", json.dumps(result))

    def test_all_plugin_construction_paths_send_only_comments_and_use_legacy_validation(self):
        for kind in ("production", "synthetic", "factory"):
            with self.subTest(kind=kind):
                entry, _service, calls = self.fixture(kind, "manual")
                with patch("mcp_raw_digest.validate_raw_report",
                           side_effect=AssertionError("Plugin must keep legacy send validation")):
                    try:
                        result = entry.dispatch({"op": "run_manual", "payload": WINDOW})
                    except WorkerApiError:
                        self.fail("Plugin generation must remain compatible with legacy send validation")
                self.assertEqual(result["state"], "sent")
                self.assertEqual(calls["raw"], [])
                self.assertEqual(len(calls["comments"]), 1)
                self.assertIn("comment_bodies", calls["comments"][0][0])
                self.assertNotIn("activities", calls["comments"][0][0])
                self.assertEqual(calls["sent"], [result["markdown"]])

    def test_plugin_non_comment_activity_still_needs_no_model_call(self):
        entry, service, calls = self.fixture("factory", "non-comment")

        def change(source):
            record = source["records"][0]
            record["activities"] = [record["activities"][0]]

        self.change_source(service, change)
        result = entry.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(result["state"], "sent")
        self.assertEqual(calls["comments"], [])
        self.assertEqual(calls["raw"], [])
        self.assertEqual(len(calls["sent"]), 1)

    def test_plugin_authorized_scheduler_uses_legacy_pipeline(self):
        entry, service, calls = self.fixture("factory", "scheduled")
        settings = service.settings_store.load()
        settings.update(auto_send_enabled=True, auto_send_enabled_since_local="2026-11-04T08:00:00")
        service.settings_store.save(settings)
        proof = entry.dispatch({"op": "plugin_auto_authorization", "payload": {}})

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 4, 9, 0)
                return value.astimezone(tz) if tz else value

        with patch("upcam_app.worker_api.datetime", FixedDatetime):
            result = entry.dispatch({"op": "plugin_run_due_authorized", "payload": {
                "authorization_fingerprint": proof["authorization_fingerprint"]}})
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["events"][0]["state"], "sent")
        self.assertEqual(calls["raw"], [])
        self.assertEqual(len(calls["comments"]), 1)

    def test_standalone_keeps_raw_diagnostics_generation_and_validation(self):
        entry, service, calls = self.fixture("standalone", "raw")

        def change(source):
            source["records"][0]["activities"][0]["action"] = "future.standalone.activity"

        self.change_source(service, change)
        self.assertTrue(entry.dispatch({"op": "diagnose", "payload": WINDOW})["ok"])
        with patch("automated_digest.validate_automated_report",
                   side_effect=AssertionError("Standalone must keep raw send validation")):
            result = entry.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(result["state"], "sent")
        self.assertEqual(calls["comments"], [])
        self.assertEqual(len(calls["raw"]), 1)
        self.assertIn("activities", calls["raw"][0][0])

    def test_plugin_binding_does_not_persist_mode_or_change_another_standalone(self):
        _plugin, plugin_service, _calls = self.fixture("factory", "separate-plugin")
        standalone, standalone_service, calls = self.fixture("standalone", "separate-standalone")
        self.assertNotIn("pipeline_mode", plugin_service.settings_store.load())
        self.change_source(standalone_service, lambda source: source["records"][0]["activities"][0].update(
            action="future.independent.activity"))
        self.assertTrue(standalone.dispatch({"op": "diagnose", "payload": WINDOW})["ok"])
        self.assertEqual(standalone.dispatch({"op": "run_manual", "payload": WINDOW})["state"], "sent")
        self.assertEqual(calls["comments"], [])
        self.assertEqual(len(calls["raw"]), 1)

    def test_plugin_request_cannot_select_raw_pipeline(self):
        entry, service, calls = self.fixture("factory", "untrusted-mode")
        with self.assertRaises(WorkerApiError):
            entry.dispatch({"op": "run_manual", "payload": {**WINDOW, "pipeline_mode": "raw"}})
        self.assertEqual(calls, {"comments": [], "raw": [], "sent": []})
        service.settings_store.save(dict(service.settings_store.load(), pipeline_mode="raw"))
        result = entry.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(result["state"], "sent")
        self.assertEqual(calls["raw"], [])
        self.assertEqual(len(calls["comments"]), 1)

    def test_plugin_binding_discards_a_prior_standalone_diagnostic_export(self):
        entry, service, _calls = self.fixture("standalone", "cached-diagnostic")
        self.change_source(service, lambda source: source["records"][0]["activities"][0].update(
            action="future.standalone.activity"))
        self.assertTrue(entry.dispatch({"op": "diagnose", "payload": WINDOW})["ok"])
        plugin = PluginWorkerService(service.data_dir, service_factory=lambda _directory: service)
        plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "export_diagnostic", "payload": WINDOW})
        self.assertFalse(plugin.dispatch({"op": "diagnose", "payload": WINDOW})["ok"])


if __name__ == "__main__":
    unittest.main()
