from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
import os
from unittest.mock import patch

from upcam_app.config import DEFAULT_SETTINGS, SettingsStore
from upcam_app.history import HistoryStore
from upcam_app.secrets import DpapiSecretStore, MemorySecretStore
from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerService, serve
from upcam_app.workflow import WorkflowBlocked


class FakeCatalog:
    def __init__(self):
        self.refresh_calls = 0
        self.cache = None

    def load(self):
        return self.cache

    def refresh(self, api_key):
        self.refresh_calls += 1
        self.cache = {"models": [{"id": "synthetic-model"}], "fetched_at_utc": "2026-11-04T00:00:00+00:00"}
        return self.cache


class WorkerApiTests(unittest.TestCase):
    def test_long_safe_error_has_an_explicit_truncation_notice(self):
        from upcam_app.worker_api import _safe_error_message
        from upcam_app.config import SettingsError
        message = _safe_error_message(SettingsError("Synthetic diagnostic line. " * 200))
        self.assertLessEqual(len(message), 2400)
        self.assertIn("截断", message)
        self.assertIn("只读诊断", message)

    def make_service(self, directory, secret_values=None):
        root = Path(directory)
        settings = SettingsStore(root / "settings.json")
        secrets = MemorySecretStore()
        if secret_values:
            secrets.save(secret_values)
        history = HistoryStore(root / "history.sqlite3")
        catalog = FakeCatalog()
        calls = []

        def startup_configurer(enabled, **kwargs):
            calls.append((enabled, kwargs))

        service = WorkerService(
            data_dir=root, settings_store=settings, secret_store=secrets,
            history=history, model_catalog=catalog,
            startup_configurer=startup_configurer,
            sample_path=Path(__file__).parents[1] / "examples" / "digest.json",
        )
        return service, secrets, catalog, calls

    def test_initialize_returns_credential_presence_without_values_and_never_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, catalog, _startup_calls = self.make_service(
                directory, {"openai_api_key": "synthetic-secret-must-not-return"})

            result = service.dispatch({"id": "init", "op": "initialize", "payload": {}})

            serialized = json.dumps(result, ensure_ascii=False)
            self.assertTrue(result["secret_presence"]["openai_api_key"])
            self.assertNotIn("synthetic-secret-must-not-return", serialized)
            self.assertEqual(catalog.refresh_calls, 0)
            self.assertIn("max", result["reasoning_efforts"]["gpt-6-luna"])

    def test_save_stores_secret_but_returns_presence_only(self):
        with tempfile.TemporaryDirectory() as directory:
            service, secrets, _catalog, _startup_calls = self.make_service(directory)
            settings = dict(DEFAULT_SETTINGS)

            result = service.dispatch({"id": "save", "op": "save", "payload": {
                "settings": settings,
                "secret_values": {"openai_api_key": "synthetic-secret-never-echo"},
                "clear_secrets": [],
            }})

            self.assertEqual(secrets.load()["openai_api_key"], "synthetic-secret-never-echo")
            self.assertTrue(result["secret_presence"]["openai_api_key"])
            self.assertNotIn("synthetic-secret-never-echo", json.dumps(result))

    def test_member_import_is_atomic_and_uses_existing_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, _catalog, _calls = self.make_service(directory)

            result = service.dispatch({"id": "import", "op": "import_owner_mappings", "payload": {
                "source": '{"synthetic-member-01":"虚构成员甲"}',
                "existing": {},
            }})

            self.assertEqual(result["mappings"], {"synthetic-member-01": "虚构成员甲"})
            with self.assertRaisesRegex(ValueError, "已有成员 ID"):
                service.dispatch({"id": "bad", "op": "import_owner_mappings", "payload": {
                    "source": '{"synthetic-member-01":"冲突姓名"}',
                    "existing": {"synthetic-member-01": "虚构成员甲"},
                }})

    def test_sample_preview_uses_repository_template_without_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, catalog, _calls = self.make_service(directory)

            result = service.dispatch({"id": "preview", "op": "preview_sample", "payload": {
                "start": "2026-11-03 16:50",
                "end": "2026-11-04 08:50",
            }})

            self.assertIn("虚构示例", result["markdown"])
            self.assertEqual(catalog.refresh_calls, 0)

    def test_json_lines_protocol_preserves_request_id_and_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, _catalog, _calls = self.make_service(
                directory, {"openai_api_key": "protocol-secret"})
            source = io.StringIO('{"id":"r-1","op":"initialize","payload":{}}\n')
            output = io.StringIO()

            serve(source, output, service=service)

            response = json.loads(output.getvalue())
            self.assertEqual(response["id"], "r-1")
            self.assertTrue(response["ok"])
            self.assertNotIn("protocol-secret", output.getvalue())

    def test_ui_cannot_forge_scope_verification_in_save_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, _catalog, _calls = self.make_service(directory)
            settings = deepcopy(DEFAULT_SETTINGS)
            settings.update({"tenant_id": "demo-org", "project_id": "demo-project",
                             "api_contract_verified": True,
                             "view": {"verified": True, "evidence": "client forged", "task_equals": {}}})
            result = service.dispatch({"op": "save", "payload": {
                "settings": settings, "scope_confirmed": True}})
            self.assertFalse(result["settings"]["api_contract_verified"])
            self.assertFalse(result["settings"]["view"]["verified"])
            self.assertEqual(result["settings"]["view"]["evidence"], "")

    def test_synthetic_service_exercises_probe_save_diagnose_and_guarded_workflow(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden in synthetic test")):
            service = make_smoke_service(Path(directory))
            initial = service.dispatch({"op": "initialize"})
            end = datetime.now().replace(second=0, microsecond=0)
            window = {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                      "end": end.strftime("%Y-%m-%d %H:%M")}
            probe = service.dispatch({"op": "probe_project", "payload": window})
            self.assertEqual(probe["task_count"], 1)
            saved = service.dispatch({"op": "save", "payload": {
                "settings": initial["settings"], "verification_window": window}})
            self.assertTrue(saved["settings"]["view"]["verified"])
            diagnostic = service.dispatch({"op": "diagnose", "payload": window})
            self.assertTrue(diagnostic["ok"], diagnostic["message"])
            export_path = Path(directory) / "安全结构.json"
            service.dispatch({"op": "export_diagnostic_to_file", "payload": {**window, "path": str(export_path)}})
            exported = export_path.read_text(encoding="utf-8")
            parsed_export = json.loads(exported)
            self.assertEqual(parsed_export["format"], "upcam-teambition-safe-shape-v5")
            self.assertTrue(parsed_export["tasks"][0]["activities"][0]["in_report_window"])
            self.assertIn("ci-synthetic-task", exported)
            for excluded in ("CI 虚构：验证原生界面布局", "synthetic-app-secret", "虚构成员甲"):
                self.assertNotIn(excluded, exported)
            service.dispatch({"op": "refresh_models"})
            run = service.dispatch({"op": "run_manual", "payload": window})
            self.assertEqual(run["state"], "sent")
            history = service.dispatch({"op": "history"})["runs"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["state"], "sent")
            repeated = service.dispatch({"op": "run_manual", "payload": window})
            self.assertEqual(repeated["state"], "sent")
            self.assertNotEqual(run["manual_request_id"], repeated["manual_request_id"])
            self.assertEqual(len(service.dispatch({"op": "history"})["runs"]), 2)

    def test_missing_ledger_blocks_before_constructing_any_network_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _secrets, _catalog, _calls = self.make_service(directory)
            service.dispatch({"op": "initialize"})
            (Path(directory) / "delivery_ledger.sqlite3").unlink()
            calls = []

            def forbidden_workflow(*args, **kwargs):
                calls.append(True)
                raise AssertionError("A missing ledger must block before any collection or summary")

            service.workflow_factory = forbidden_workflow
            with self.assertRaisesRegex(ValueError, "防重账本"):
                service.dispatch({"op": "run_manual", "payload": {
                    "start": "2026-11-03 16:50", "end": "2026-11-04 08:50"}})
            self.assertEqual(calls, [])

    def test_scheduled_recovery_processes_one_slot_and_never_retries_persisted_block(self):
        with tempfile.TemporaryDirectory() as directory:
            service, secrets, catalog, _calls = self.make_service(directory)
            service.dispatch({"op": "initialize"})
            settings = service.settings_store.load()
            settings["auto_send_enabled"] = True
            settings["auto_send_enabled_since_local"] = (datetime.now() - timedelta(days=1)).isoformat()
            service.settings_store.save(settings)
            calls = []

            def factory(_settings, _credentials, _data_dir, *, history, **_kwargs):
                class SyntheticBlockedWorkflow:
                    def run(self, start, end, *, schedule_key):
                        calls.append(schedule_key)
                        history.create_blocked_attempt(start, end, "Synthetic scheduled block", schedule_key)
                        raise WorkflowBlocked("Synthetic scheduled block")
                return SyntheticBlockedWorkflow()

            service.workflow_factory = factory
            results = [service.dispatch({"op": "run_due"}) for _ in range(3)]
            self.assertEqual([result["started"] for result in results], [1, 1, 0])
            self.assertEqual(len(set(calls)), 2)
            restarted = WorkerService(Path(directory), secret_store=secrets, model_catalog=catalog,
                                      workflow_factory=factory, startup_configurer=lambda *_args, **_kwargs: None)
            self.assertEqual(restarted.dispatch({"op": "run_due"})["started"], 0)
            self.assertEqual(len(calls), 2)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI requires the Windows CI runner")
    def test_windows_dpapi_legacy_store_round_trip_uses_current_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.dpapi"
            store = DpapiSecretStore(path)
            values = {"openai_api_key": "ci-synthetic-secret-only", "delivery_ledger_id": "ci-synthetic-identity"}
            store.save(values)
            self.assertEqual(store.load(), values)
            self.assertNotIn(b"ci-synthetic-secret-only", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
