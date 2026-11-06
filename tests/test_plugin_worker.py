"""Synthetic-only contract tests for the separate Workbench worker boundary."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from upcam_app import RULESET_REVISION
from upcam_app.config import DEFAULT_SETTINGS, SettingsStore
from upcam_app.preferences import PREFERENCE_DEFAULTS, preference_values
from upcam_app.secrets import SecretStoreError
from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerApiError, WorkerService
from upcam_app.workflow import DigestWorkflow
from report_contract import updated_selection


REPOSITORY = Path(__file__).resolve().parents[1]
HANDSHAKE = {
    "protocol_version": 1,
    "backend_version": "0.4.3",
    "nonce": "0123456789abcdef0123456789abcdef",
}
WINDOW = {"start": "2026-11-03 16:50", "end": "2026-11-04 08:50"}


class PluginWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="upcam-plugin-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        try:
            self.adapter = importlib.import_module("upcam_app.plugin_worker")
        except ModuleNotFoundError:
            self.fail("The Workbench plugin adapter has not been implemented")

    def plugin(self, directory=None, **kwargs):
        return self.adapter.PluginWorkerService(directory or self.root / "profile", **kwargs)

    def ready(self, plugin):
        result = plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        self.assertEqual(result["mode"], "project-workbench")
        return plugin.dispatch({"op": "initialize", "payload": {}})

    def lines(self, requests, *, profile=None, factory=None, synthetic=True):
        source = io.BytesIO(b"".join(
            item if isinstance(item, bytes) else (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
            for item in requests))
        target = io.BytesIO()
        self.adapter.serve(profile or self.root / "profile", synthetic=synthetic,
                           service_factory=factory, input_stream=source, output_stream=target)
        return [json.loads(line) for line in target.getvalue().decode("utf-8").splitlines()]

    def test_profile_must_be_explicit_absolute_local_and_not_a_root(self):
        for candidate in (None, "", "relative/profile", str(self.root.anchor), "//server/share/profile",
                          r"\\server\share\profile", r"\\?\C:\profile", str(self.root / ".." / "profile")):
            with self.subTest(candidate=candidate), self.assertRaises(WorkerApiError):
                self.adapter.PluginWorkerService(candidate)

    def test_profile_rejects_link_components_before_constructing_service(self):
        actual = self.root / "actual"
        actual.mkdir()
        alias = self.root / "linked"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        for path in (alias, alias / "nested"):
            with self.subTest(path=path), self.assertRaises(WorkerApiError):
                self.plugin(path)
        self.assertEqual(list(actual.iterdir()), [])

    def test_profile_rejects_non_profile_adoption_and_linked_owned_files(self):
        directory = self.root / "unrelated"
        directory.mkdir()
        (directory / "private.txt").write_text("synthetic unrelated data", encoding="utf-8")
        with self.assertRaises(WorkerApiError):
            self.plugin(directory)
        (directory / "settings.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(WorkerApiError):
            self.plugin(directory)
        SettingsStore(directory / "settings.json").save(deepcopy(DEFAULT_SETTINGS))
        target = self.root / "unrelated.sqlite3"
        target.touch()
        try:
            (directory / "history.sqlite3").symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(WorkerApiError):
            self.plugin(directory)
        self.assertEqual(target.read_bytes(), b"")

    def test_handshake_is_required_before_any_profile_creation(self):
        plugin = self.plugin()
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "initialize", "payload": {}})
        self.assertFalse((self.root / "profile").exists())

    def test_handshake_echoes_exact_contract_and_keeps_business_initialization_separate(self):
        directory = self.root / "space in profile"
        plugin = self.plugin(directory, synthetic=True)
        self.assertFalse(directory.exists())
        response = plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        self.assertEqual(response, {
            **HANDSHAKE, "mode": "project-workbench", "data_directory": str(directory.resolve()),
            "synthetic": True, "ruleset_revision": RULESET_REVISION,
        })
        self.assertFalse((directory / "delivery_ledger.sqlite3").exists())
        self.assertFalse((directory / "language.json").exists())
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})

    def test_bad_handshake_locks_session_without_profile_or_factory_side_effects(self):
        invalid = [
            {**HANDSHAKE, "protocol_version": 2}, {**HANDSHAKE, "protocol_version": True},
            {**HANDSHAKE, "backend_version": "0.4.2"}, {**HANDSHAKE, "nonce": "a" * 31},
            {**HANDSHAKE, "nonce": "A" * 32}, {**HANDSHAKE, "nonce": "g" * 32},
            {**HANDSHAKE, "extra": True}, {},
        ]
        for index, payload in enumerate(invalid):
            directory = self.root / f"invalid-{index}"
            plugin = self.plugin(directory, service_factory=lambda _path: self.fail("Factory ran before validation"))
            with self.subTest(payload=payload):
                with self.assertRaises(WorkerApiError):
                    plugin.dispatch({"op": "plugin_handshake", "payload": payload})
                with self.assertRaises(WorkerApiError):
                    plugin.dispatch({"op": "initialize", "payload": {}})
                self.assertFalse(directory.exists())

    def test_profile_is_rechecked_when_opened_after_handshake(self):
        directory = self.root / "profile"
        plugin = self.plugin(directory, synthetic=True)
        directory.mkdir()
        (directory / "unrelated.txt").write_text("synthetic", encoding="utf-8")
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["unrelated.txt"])

    def test_synthetic_flag_cannot_open_or_seed_an_unmarked_existing_profile(self):
        directory = self.root / "standalone"
        SettingsStore(directory / "settings.json").save(deepcopy(DEFAULT_SETTINGS))
        secret_path = directory / "credentials.dpapi"
        secret_path.write_bytes(b"synthetic opaque bytes must not be read")
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with self.assertRaises(WorkerApiError), patch(
                "upcam_app.secrets.DpapiSecretStore.load", side_effect=AssertionError("No normal-profile secrets")):
            self.ready(self.plugin(directory, synthetic=True))
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_standalone_preferences_are_preserved_even_when_save_omits_or_changes_them(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        original = standalone.settings_store.load()
        original.update({"ui_language": "en-US", "ui_theme": "light", "start_with_windows": True,
                         "tray_icon_enabled": True, "close_to_tray": False,
                         "start_in_tray": True, "auto_check_updates": True})
        standalone.settings_store.save(original)
        with patch("upcam_app.worker_api.configure_user_startup", side_effect=AssertionError("Registry forbidden")):
            plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
            self.ready(plugin)
            for replacement in (PREFERENCE_DEFAULTS, {}):
                settings = {key: value for key, value in original.items() if key not in PREFERENCE_DEFAULTS}
                settings.update(replacement)
                settings["max_tasks_per_run"] = 456
                saved = plugin.dispatch({"op": "save", "payload": {"settings": settings}})
                self.assertEqual(preference_values(saved["settings"]), preference_values(original))
                self.assertEqual(saved["settings"]["max_tasks_per_run"], 456)
                self.assertEqual(preference_values(standalone.settings_store.load()), preference_values(original))

    def test_standalone_preference_and_update_operations_are_rejected(self):
        plugin = self.plugin(synthetic=True)
        self.ready(plugin)
        for operation in ("save_preferences", "check_updates", "download_update", "prepare_update", "finish_update"):
            with self.subTest(operation=operation), self.assertRaises(WorkerApiError):
                plugin.dispatch({"op": operation, "payload": {}})
        self.assertFalse((self.root / "profile" / "updates").exists())

    def test_business_operation_named_update_is_allowed(self):
        plugin = self.plugin(synthetic=True)
        self.ready(plugin)
        result = plugin.dispatch({"op": "update_view_filter", "payload": {
            "action": "put", "filters": {}, "field": "stageId", "value": "synthetic-stage"}})
        self.assertEqual(result["filters"], {"stageId": "synthetic-stage"})

    def test_protocol_uses_correlated_json_envelopes_and_never_echoes_entered_secret(self):
        secret = "synthetic-private-value-never-echo"
        responses = self.lines([
            {"id": "h_1", "op": "plugin_handshake", "payload": HANDSHAKE},
            {"id": "i_1", "op": "initialize", "payload": {}},
            {"id": "s_1", "op": "save", "payload": {"settings": deepcopy(DEFAULT_SETTINGS),
             "secret_values": {"openai_api_key": secret}}},
            {"id": "x_1", "op": "shutdown", "payload": {}},
            {"id": "ignored", "op": "initialize", "payload": {}},
        ])
        self.assertEqual([item["id"] for item in responses], ["h_1", "i_1", "s_1", "x_1"])
        self.assertTrue(all(item["ok"] for item in responses), responses)
        self.assertNotIn(secret, json.dumps(responses))
        self.assertTrue(responses[2]["result"]["secret_presence"]["openai_api_key"])

    def test_protocol_rejects_duplicate_keys_nan_invalid_utf8_and_invalid_ids(self):
        responses = self.lines([
            b'{"id":"duplicate","id":"other","op":"plugin_handshake","payload":{}}\n',
            b'{"id":"nan","op":"plugin_handshake","payload":{"protocol_version":NaN}}\n',
            b'{"id":"utf","op":"\xff"}\n',
            {"id": "invalid id with spaces", "op": "plugin_handshake", "payload": HANDSHAKE},
            {"id": "valid", "op": "plugin_handshake", "payload": HANDSHAKE},
        ])
        self.assertEqual(len(responses), 5)
        self.assertTrue(all(not response["ok"] for response in responses[:4]))
        self.assertTrue(responses[-1]["ok"])

    def test_protocol_counts_request_bytes_and_drains_oversized_line(self):
        oversized = (json.dumps({"id": "large", "op": "initialize", "payload": {
            "value": "虚" * (750 * 1024)}}, ensure_ascii=False) + "\n").encode("utf-8")
        responses = self.lines([oversized, {"id": "valid", "op": "plugin_handshake", "payload": HANDSHAKE}])
        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["ok"])
        self.assertIsNone(responses[0]["id"])
        self.assertTrue(responses[1]["ok"])

    def test_entry_requires_exact_cli_and_never_echoes_rejected_arguments(self):
        marker = "synthetic-secret-argument-never-echo"
        for args in ([], ["--data-directory", "relative"], ["--unknown", marker],
                     ["--data-directory", str(self.root / "profile"), "--synthetic", "--synthetic"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(REPOSITORY / "plugin_worker_entry.py"), *args],
                                        input=b"", capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(marker.encode(), result.stderr)

    def test_entry_supports_utf8_and_explicit_synthetic_profile_with_spaces(self):
        directory = self.root / "explicit profile with spaces"
        requests = [{"id": "hello", "op": "plugin_handshake", "payload": HANDSHAKE},
                    {"id": "init", "op": "initialize", "payload": {}},
                    {"id": "stop", "op": "shutdown", "payload": {}}]
        result = subprocess.run([sys.executable, str(REPOSITORY / "plugin_worker_entry.py"),
                                 "--data-directory", str(directory), "--synthetic"],
                                input=("\n".join(json.dumps(row) for row in requests) + "\n").encode(),
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        responses = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
        self.assertEqual([response["id"] for response in responses], ["hello", "init", "stop"])
        self.assertTrue(all(response["ok"] for response in responses), responses)
        self.assertEqual(responses[0]["result"]["data_directory"], str(directory.resolve()))
        self.assertFalse(responses[1]["result"]["settings"]["auto_send_enabled"])

    def test_secret_in_business_settings_is_rejected_before_any_save(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        original = self.ready(plugin)["settings"]
        injected = dict(original, tenant_id="synthetic-entered-key-never-save")
        with self.assertRaises(WorkerApiError) as raised:
            plugin.dispatch({"op": "save", "payload": {"settings": injected,
                "secret_values": {"openai_api_key": "synthetic-entered-key-never-save"}}})
        self.assertNotIn("synthetic-entered-key-never-save", str(raised.exception))
        self.assertEqual(standalone.settings_store.load(), original)

    def test_unexpected_core_error_never_exposes_saved_secret(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        secret = standalone.secret_store.load()["openai_api_key"]
        standalone._history = lambda _payload: (_ for _ in ()).throw(WorkerApiError("Request failed: " + secret))
        responses = self.lines([
            {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
            {"id": "i", "op": "initialize", "payload": {}},
            {"id": "e", "op": "history", "payload": {}},
        ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
        self.assertFalse(responses[-1]["ok"])
        self.assertEqual(responses[-1]["id"], "e")
        self.assertNotIn(secret, json.dumps(responses))

    def test_existing_non_secret_field_cannot_leak_saved_secret_on_initialize(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        secret = standalone.secret_store.load()["teambition_app_secret"]
        settings = standalone.settings_store.load()
        settings["tenant_id"] = secret
        standalone.settings_store.save(settings)
        responses = self.lines([
            {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
            {"id": "i", "op": "initialize", "payload": {}},
        ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
        self.assertFalse(responses[-1]["ok"])
        self.assertNotIn(secret, json.dumps(responses))

    def test_response_byte_limit_returns_safe_correlated_failure(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        standalone._preview_sample = lambda _payload: {"markdown": "x" * (32 * 1024 * 1024)}
        responses = self.lines([
            {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
            {"id": "big", "op": "preview_sample", "payload": WINDOW},
            {"id": "after", "op": "history", "payload": {}},
        ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
        self.assertEqual([response["id"] for response in responses], ["h", "big", "after"])
        self.assertFalse(responses[1]["ok"])
        self.assertTrue(responses[2]["ok"])
        self.assertLess(len(json.dumps(responses)), 4096)

    def test_sample_preview_is_identical_to_standalone_core(self):
        standalone = make_smoke_service(self.root / "standalone")
        plugin = self.plugin(synthetic=True)
        self.ready(plugin)
        expected = standalone.dispatch({"op": "preview_sample", "payload": WINDOW})
        result = plugin.dispatch({"op": "preview_sample", "payload": WINDOW})
        self.assertEqual(result, expected)
        self.assertIn("Teambition｜UPCAM", result["markdown"])

    def test_adopting_same_canonical_profile_keeps_ledger_identity_and_unknown_delivery(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        standalone.dispatch({"op": "initialize"})
        identity = standalone.secret_store.load()["delivery_ledger_id"]
        run_id = standalone.history.create_draft(datetime(2026, 11, 3, 16, 50),
            datetime(2026, 11, 4, 8, 50), "teambition", "synthetic interrupted report", "2026-11-04:morning")
        standalone.history.start_delivery(run_id)
        with closing(sqlite3.connect(directory / "delivery_ledger.sqlite3")) as db, db:
            db.execute("INSERT INTO deliveries VALUES ('synthetic-scope', 'synthetic-hash', 'in_flight')")
        ledger_before = (directory / "delivery_ledger.sqlite3").read_bytes()
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        initialized = self.ready(plugin)
        self.assertEqual(initialized["data_directory"], str(directory))
        self.assertEqual(standalone.secret_store.load()["delivery_ledger_id"], identity)
        self.assertEqual(initialized["history"][0]["run_id"], run_id)
        self.assertEqual(initialized["history"][0]["state"], "delivery_unknown")
        self.assertEqual((directory / "delivery_ledger.sqlite3").read_bytes(), ledger_before)
        self.assertFalse(initialized["settings"]["auto_send_enabled"])

    def test_synthetic_restart_preserves_settings_history_and_independent_ledger_identity(self):
        directory = self.root / "profile"
        first = self.plugin(directory, synthetic=True)
        original = self.ready(first)
        saved_settings = dict(original["settings"], max_tasks_per_run=456, morning_run_at="09:15")
        first.dispatch({"op": "save", "payload": {"settings": saved_settings}})
        first.dispatch({"op": "preview_sample", "payload": WINDOW})
        ledger_before = (directory / "delivery_ledger.sqlite3").read_bytes()
        marker_before = (directory / ".plugin-synthetic.json").read_bytes()
        first.dispatch({"op": "shutdown", "payload": {}})
        restarted = self.plugin(directory, synthetic=True)
        initialized = self.ready(restarted)
        self.assertEqual(initialized["settings"]["max_tasks_per_run"], 456)
        self.assertEqual(initialized["settings"]["morning_run_at"], "09:15")
        self.assertFalse(initialized["settings"]["auto_send_enabled"])
        self.assertTrue(initialized["ledger_ready"])
        self.assertEqual((directory / "delivery_ledger.sqlite3").read_bytes(), ledger_before)
        self.assertEqual((directory / ".plugin-synthetic.json").read_bytes(), marker_before)
        self.assertFalse((directory / "credentials.dpapi").exists())
        self.assertNotIn(b"synthetic-app-secret", marker_before)
        self.assertNotIn(b"synthetic-key-cannot-call-openai", marker_before)

    def test_synthetic_missing_ledger_is_never_recreated_on_restart(self):
        directory = self.root / "profile"
        self.ready(self.plugin(directory, synthetic=True))
        (directory / "delivery_ledger.sqlite3").unlink()
        initialized = self.ready(self.plugin(directory, synthetic=True))
        self.assertFalse(initialized["ledger_ready"])
        self.assertFalse((directory / "delivery_ledger.sqlite3").exists())
        self.assertFalse(initialized["settings"]["auto_send_enabled"])

    def test_ordinary_profile_uses_dpapi_store_without_plaintext_fallback(self):
        directory = self.root / "profile"
        plugin = self.plugin(directory)
        with patch("upcam_app.secrets.DpapiSecretStore.load", return_value={}), patch(
                "upcam_app.secrets.DpapiSecretStore.save", side_effect=SecretStoreError("synthetic DPAPI unavailable")):
            initialized = self.ready(plugin)
        self.assertFalse(initialized["ledger_ready"])
        self.assertFalse((directory / ".plugin-synthetic.json").exists())
        self.assertFalse((directory / "delivery_ledger.sqlite3").exists())

    def test_fresh_production_profile_can_reopen_without_an_explicit_business_save(self):
        directory = self.root / "profile"
        with patch("upcam_app.secrets.DpapiSecretStore.load", return_value={}), patch(
                "upcam_app.secrets.DpapiSecretStore.save", side_effect=SecretStoreError("Synthetic unavailable DPAPI")):
            first = self.ready(self.plugin(directory))
            try:
                restarted_plugin = self.plugin(directory)
            except WorkerApiError:
                self.fail("A fresh initialized profile must reopen before its first business save")
            restarted = self.ready(restarted_plugin)
        self.assertEqual(restarted["settings"], first["settings"])
        self.assertFalse(restarted["settings"]["auto_send_enabled"])

    def test_profile_owned_file_link_swap_after_handshake_is_rejected(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        (directory / "history.sqlite3").unlink()
        target = self.root / "unrelated.sqlite3"
        target.touch()
        try:
            (directory / "history.sqlite3").symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "initialize", "payload": {}})
        self.assertEqual(target.read_bytes(), b"")
        self.assertFalse((directory / "language.json").exists())

    def test_unverified_scope_cannot_enable_auto_send_during_plugin_save(self):
        plugin = self.plugin(synthetic=True)
        initial = self.ready(plugin)
        settings = dict(initial["settings"], auto_send_enabled=True, api_contract_verified=True,
                        view={"verified": True, "evidence": "synthetic client cannot grant authorization", "task_equals": {}})
        saved = plugin.dispatch({"op": "save", "payload": {"settings": settings}})
        self.assertFalse(saved["settings"]["auto_send_enabled"])
        self.assertFalse(saved["settings"]["view"]["verified"])
        self.assertEqual(plugin.dispatch({"op": "run_due", "payload": {}})["started"], 0)

    def test_full_synthetic_probe_save_diagnose_and_send_parity(self):
        plugin = self.plugin(synthetic=True)
        initialized = self.ready(plugin)
        probe = plugin.dispatch({"op": "probe_project", "payload": WINDOW})
        self.assertEqual(probe["task_count"], 1)
        saved = plugin.dispatch({"op": "save", "payload": {
            "settings": initialized["settings"], "verification_window": WINDOW}})
        self.assertTrue(saved["settings"]["view"]["verified"])
        self.assertFalse(saved["settings"]["auto_send_enabled"])
        plugin.dispatch({"op": "refresh_models", "payload": {}})
        diagnostic = plugin.dispatch({"op": "diagnose", "payload": WINDOW})
        self.assertTrue(diagnostic["ok"], diagnostic["message"])
        sent = plugin.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(sent["state"], "sent")
        history = plugin.dispatch({"op": "history", "payload": {}})["runs"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "sent")
        self.assertEqual(history[0]["run_id"], sent["run_id"])
        with closing(sqlite3.connect(self.root / "profile" / "delivery_ledger.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT state FROM deliveries").fetchall(), [("confirmed",)])

    def test_no_dynamic_activity_sends_identical_template_with_zero_openai_calls(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        initial = standalone.dispatch({"op": "initialize"})
        standalone.dispatch({"op": "probe_project", "payload": WINDOW})
        standalone.dispatch({"op": "save", "payload": {
            "settings": initial["settings"], "verification_window": WINDOW}})
        secrets = standalone.secret_store.load()
        secrets.pop("openai_api_key")
        standalone.secret_store.save(secrets)
        settings = standalone.settings_store.load()
        settings["openai_model"] = "synthetic-unavailable-model"
        standalone.settings_store.save(settings)
        summary_calls, sent_bodies = [], []

        class EmptyClient:
            def __init__(self, tenant_id, **_kwargs):
                self.tenant_id = tenant_id

            def collect(self, project_id, *, day, window, **_kwargs):
                return {"tenant_id": self.tenant_id, "project_id": project_id,
                        "pagination_complete": True, "include_archived": False,
                        "selection": updated_selection(day, *window),
                        "batch_id": "00000000-0000-4000-8000-000000000001",
                        "collected_at": "2026-11-04T01:00:00+00:00", "records": []}

        class ForbiddenSummarizer:
            def summarize(self, *_args, **_kwargs):
                summary_calls.append(True)
                raise AssertionError("No OpenAI call for a verified empty collection")

        class CapturedSender:
            def send_markdown(self, _webhook, _secret, markdown, **_kwargs):
                sent_bodies.append(markdown)
                return True

        def workflow(settings, credentials, data_dir, **kwargs):
            return DigestWorkflow(settings, credentials, data_dir, client_factory=EmptyClient,
                summarizer=ForbiddenSummarizer(), ding_sender=CapturedSender(), **kwargs)

        standalone.workflow_factory = workflow
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        self.ready(plugin)
        run = plugin.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(run["state"], "sent")
        self.assertEqual(summary_calls, [])
        self.assertEqual(len(sent_bodies), 1)
        self.assertEqual(run["markdown"], sent_bodies[0])
        self.assertIn("更新 0 项", run["markdown"])

    def test_scheduled_history_continues_without_replaying_blocked_slots(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        standalone.dispatch({"op": "initialize"})
        now = datetime.now().replace(microsecond=0)
        settings = standalone.settings_store.load()
        settings["auto_send_enabled"] = True
        settings["auto_send_enabled_since_local"] = (now - timedelta(days=1)).isoformat()
        standalone.settings_store.save(settings)
        calls = []

        def blocked_workflow(_settings, _credentials, _data_dir, *, history, **_kwargs):
            class Blocked:
                def run(self, start, end, *, schedule_key):
                    calls.append(schedule_key)
                    history.create_blocked_attempt(start, end, "Synthetic permanent scheduled block", schedule_key)
                    from upcam_app.workflow import WorkflowBlocked
                    raise WorkflowBlocked("Synthetic permanent scheduled block")
            return Blocked()

        standalone.workflow_factory = blocked_workflow
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        initialized = self.ready(plugin)
        self.assertTrue(initialized["settings"]["auto_send_enabled"])
        results = [plugin.dispatch({"op": "run_due", "payload": {}}) for _ in range(3)]
        self.assertEqual([row["started"] for row in results], [1, 1, 0])
        restarted_service = WorkerService(directory, secret_store=standalone.secret_store,
            model_catalog=standalone.model_catalog, workflow_factory=blocked_workflow)
        restarted = self.plugin(directory, service_factory=lambda _directory: restarted_service)
        self.ready(restarted)
        self.assertEqual(restarted.dispatch({"op": "run_due", "payload": {}})["started"], 0)
        self.assertEqual(len(calls), 2)

    def test_unknown_send_remains_blocked_after_plugin_to_standalone_rollback(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        initial = self.ready(plugin)
        plugin.dispatch({"op": "probe_project", "payload": WINDOW})
        plugin.dispatch({"op": "save", "payload": {"settings": initial["settings"], "verification_window": WINDOW}})
        plugin.dispatch({"op": "refresh_models", "payload": {}})
        from upcam_app.smoke_data import SyntheticClient, SyntheticSummarizer
        send_calls = []

        class UnknownSender:
            def send_markdown(self, *_args, **_kwargs):
                send_calls.append(True)
                raise TimeoutError("synthetic private service response")

        def workflow(settings, credentials, data_dir, **kwargs):
            return DigestWorkflow(settings, credentials, data_dir, client_factory=SyntheticClient,
                summarizer=SyntheticSummarizer(), ding_sender=UnknownSender(), **kwargs)

        standalone.workflow_factory = workflow
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "run_manual", "payload": WINDOW})
        plugin.dispatch({"op": "shutdown", "payload": {}})
        rollback = WorkerService(directory, secret_store=standalone.secret_store,
            model_catalog=standalone.model_catalog, workflow_factory=workflow)
        rollback.dispatch({"op": "initialize"})
        with self.assertRaises(WorkerApiError):
            rollback.dispatch({"op": "run_manual", "payload": WINDOW})
        self.assertEqual(send_calls, [True])
        with closing(sqlite3.connect(directory / "delivery_ledger.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT state FROM deliveries").fetchall(), [("unknown",)])

    @unittest.skipUnless(os.name == "nt", "Windows junction validation requires Windows")
    def test_windows_junction_component_is_rejected(self):
        actual = self.root / "actual"
        actual.mkdir()
        junction = self.root / "junction"
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(actual)], capture_output=True)
        if created.returncode:
            self.skipTest("This Windows runner cannot create a junction")
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        for path in (junction, junction / "profile"):
            with self.subTest(path=path), self.assertRaises(WorkerApiError):
                self.plugin(path)

    @unittest.skipUnless(os.name == "nt", "Windows short-path aliases require Windows")
    def test_windows_short_profile_alias_reports_the_same_canonical_directory(self):
        import ctypes
        directory = self.root / "synthetic profile with long directory name"
        directory.mkdir()
        get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short_path.restype = ctypes.c_uint32
        required = get_short_path(str(directory), None, 0)
        if not required:
            self.skipTest("This Windows volume does not provide short path aliases")
        buffer = ctypes.create_unicode_buffer(required)
        written = get_short_path(str(directory), buffer, required)
        self.assertGreater(written, 0)
        self.assertLess(written, required)
        alias = Path(buffer.value)
        canonical = directory.resolve()
        if str(alias).casefold() == str(canonical).casefold():
            self.skipTest("No distinct short alias is available for this profile")
        plugin = self.plugin(alias, synthetic=True)
        response = plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        self.assertEqual(response["data_directory"], str(canonical))
        initialized = plugin.dispatch({"op": "initialize", "payload": {}})
        self.assertEqual(initialized["data_directory"], str(canonical))
        self.assertTrue((canonical / ".plugin-synthetic.json").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI round trip requires Windows")
    def test_windows_dpapi_profile_adoption_preserves_ciphertext_and_identity(self):
        from upcam_app.secrets import DpapiSecretStore
        directory = self.root / "standalone"
        standalone = WorkerService(directory)
        standalone.settings_store.save(deepcopy(DEFAULT_SETTINGS))
        standalone.secret_store.save({"openai_api_key": "synthetic-windows-dpapi-key"})
        initialized = standalone.dispatch({"op": "initialize"})
        self.assertTrue(initialized["ledger_ready"])
        ciphertext = (directory / "credentials.dpapi").read_bytes()
        identity = DpapiSecretStore(directory / "credentials.dpapi").load()["delivery_ledger_id"]
        with patch("upcam_app.worker_api.configure_user_startup", side_effect=AssertionError("Registry forbidden")):
            adopted = self.ready(self.plugin(directory))
        self.assertTrue(adopted["secret_presence"]["openai_api_key"])
        self.assertEqual((directory / "credentials.dpapi").read_bytes(), ciphertext)
        self.assertEqual(DpapiSecretStore(directory / "credentials.dpapi").load()["delivery_ledger_id"], identity)

    def test_webhook_query_components_cannot_be_saved_as_business_settings(self):
        for key in ("access_token", "token", "sign", "signature"):
            with self.subTest(key=key):
                directory = self.root / key
                standalone = make_smoke_service(directory)
                plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
                original = self.ready(plugin)["settings"]
                before = (directory / "settings.json").read_bytes()
                old_secrets = standalone.secret_store.load()
                component = "fakeWebhookcomponent-" + key + "+decoded"
                webhook = "https://oapi.dingtalk.com/robot/send?" + key.upper() + "=" + quote(component, safe="")
                proposed = dict(original, tenant_id=component, project_id="", auto_send_enabled=False)
                with self.assertRaises(WorkerApiError) as raised:
                    plugin.dispatch({"op": "save", "payload": {"settings": proposed,
                        "secret_values": {"dingtalk_webhook": webhook,
                                          "dingtalk_signing_secret": "synthetic-distinct-signing-secret"}}})
                self.assertNotIn(component, str(raised.exception))
                self.assertEqual((directory / "settings.json").read_bytes(), before)
                self.assertEqual(standalone.secret_store.load(), old_secrets)

    def test_saved_webhook_query_component_cannot_escape_in_results_or_core_errors(self):
        for operation in ("initialize", "history"):
            with self.subTest(operation=operation):
                directory = self.root / operation
                standalone = make_smoke_service(directory)
                component = "fakeWebhookcomponent+decoded"
                credentials = standalone.secret_store.load()
                credentials["dingtalk_webhook"] = (
                    "https://oapi.dingtalk.com/robot/send?access_token=" + quote(component, safe=""))
                credentials["dingtalk_signing_secret"] = "synthetic-distinct-signing-secret"
                standalone.secret_store.save(credentials)
                if operation == "initialize":
                    settings = standalone.settings_store.load()
                    settings.update(tenant_id=component, project_id="")
                    standalone.settings_store.save(settings)
                else:
                    standalone._history = lambda _payload: (_ for _ in ()).throw(
                        WorkerApiError("Request failed: " + component))
                responses = self.lines([
                    {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
                    {"id": "business", "op": operation, "payload": {}},
                ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
                self.assertFalse(responses[-1]["ok"])
                self.assertNotIn(component, json.dumps(responses))

    def test_owned_profile_files_cannot_alias_another_file_through_a_hardlink(self):
        for name in ("settings.json", "credentials.dpapi", "history.sqlite3", "delivery_ledger.sqlite3"):
            with self.subTest(name=name):
                directory = self.root / name.replace(".", "-")
                SettingsStore(directory / "settings.json").save(deepcopy(DEFAULT_SETTINGS))
                owned = directory / name
                if not owned.exists():
                    owned.write_bytes(b"")
                outside = self.root / (name + ".outside")
                try:
                    os.link(owned, outside)
                except OSError:
                    self.skipTest("This filesystem cannot create hardlinks")
                before = outside.read_bytes()
                with self.assertRaises(WorkerApiError):
                    self.plugin(directory, service_factory=lambda _path: self.fail("Hardlink validation must precede construction"))
                self.assertEqual(outside.read_bytes(), before)

    def test_hardlink_added_after_handshake_is_rejected_before_business_initialization(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
        plugin.dispatch({"op": "plugin_handshake", "payload": HANDSHAKE})
        outside = self.root / "outside-history.sqlite3"
        try:
            os.link(directory / "history.sqlite3", outside)
        except OSError:
            self.skipTest("This filesystem cannot create hardlinks")
        before = outside.read_bytes()
        with self.assertRaises(WorkerApiError):
            plugin.dispatch({"op": "initialize", "payload": {}})
        self.assertEqual(outside.read_bytes(), before)
        self.assertFalse((directory / "language.json").exists())

    def test_unrelated_original_profile_hardlinks_are_preserved(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        unrelated = directory / "original-unrelated-notes.txt"
        unrelated.write_text("Synthetic original data", encoding="utf-8")
        outside = self.root / "outside-notes.txt"
        try:
            os.link(unrelated, outside)
        except OSError:
            self.skipTest("This filesystem cannot create hardlinks")
        initialized = self.ready(self.plugin(directory, service_factory=lambda _directory: standalone))
        self.assertEqual(initialized["settings"]["project_id"], "demo-project")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "Synthetic original data")
        self.assertEqual(outside.read_bytes(), unrelated.read_bytes())

    def test_compact_mapping_that_exceeds_disk_budget_is_rejected_without_profile_or_secret_mutation(self):
        directory = self.root / "standalone"
        standalone = make_smoke_service(directory)
        standalone.dispatch({"op": "initialize", "payload": {}})
        original_settings = (directory / "settings.json").read_bytes()
        original_secrets = standalone.secret_store.load()
        proposed = dict(deepcopy(DEFAULT_SETTINGS), owner_names={f"u{index:06d}": "N" for index in range(110000)})
        request = {"id": "oversized-save", "op": "save", "payload": {"settings": proposed,
                   "secret_values": {"openai_api_key": "synthetic-rejected-profile-save-key"}}}
        compact = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.assertLess(len(compact), 2 * 1024 * 1024)
        self.assertGreater(len((json.dumps(proposed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")), 2 * 1024 * 1024)
        responses = self.lines([
            {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE}, compact,
        ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
        self.assertFalse(responses[-1]["ok"])
        self.assertNotIn("synthetic-rejected-profile-save-key", json.dumps(responses))
        self.assertEqual((directory / "settings.json").read_bytes(), original_settings)
        self.assertEqual(standalone.secret_store.load(), original_secrets)
        reopened = WorkerService(directory, secret_store=standalone.secret_store, model_catalog=standalone.model_catalog)
        self.assertEqual(self.ready(self.plugin(directory, service_factory=lambda _directory: reopened))["settings"],
                         standalone.settings_store.load())

    def test_short_stored_auth_secrets_embedded_in_settings_are_rejected_before_save(self):
        for secret in ("q", "qz", "qzx"):
            with self.subTest(secret_length=len(secret)):
                directory = self.root / ("short-save-" + str(len(secret)))
                standalone = make_smoke_service(directory)
                plugin = self.plugin(directory, service_factory=lambda _directory: standalone)
                original = self.ready(plugin)["settings"]
                credentials = standalone.secret_store.load()
                credentials["openai_api_key"] = secret
                standalone.secret_store.save(credentials)
                before = (directory / "settings.json").read_bytes()
                embedded = "synthetic-prefix-" + secret + "-suffix"
                proposed = dict(original, tenant_id=embedded, project_id="", auto_send_enabled=False)
                with self.assertRaises(WorkerApiError) as raised:
                    plugin.dispatch({"op": "save", "payload": {"settings": proposed}})
                self.assertNotIn(embedded, str(raised.exception))
                self.assertEqual((directory / "settings.json").read_bytes(), before)
                self.assertEqual(standalone.secret_store.load(), credentials)

    def test_short_stored_auth_secrets_cannot_escape_inside_initialize_results(self):
        for secret in ("q", "qz", "qzx"):
            with self.subTest(secret_length=len(secret)):
                directory = self.root / ("short-result-" + str(len(secret)))
                standalone = make_smoke_service(directory)
                credentials = standalone.secret_store.load()
                credentials["openai_api_key"] = secret
                standalone.secret_store.save(credentials)
                embedded = "synthetic-prefix-" + secret + "-suffix"
                settings = standalone.settings_store.load()
                settings.update(tenant_id=embedded, project_id="")
                standalone.settings_store.save(settings)
                responses = self.lines([
                    {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
                    {"id": "business", "op": "initialize", "payload": {}},
                ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
                self.assertFalse(responses[-1]["ok"])
                self.assertNotIn(embedded, json.dumps(responses[-1]))

    def test_short_stored_auth_secrets_cannot_escape_inside_core_errors(self):
        for secret in ("q", "qz", "qzx"):
            with self.subTest(secret_length=len(secret)):
                directory = self.root / ("short-error-" + str(len(secret)))
                standalone = make_smoke_service(directory)
                credentials = standalone.secret_store.load()
                credentials["openai_api_key"] = secret
                standalone.secret_store.save(credentials)
                embedded = "synthetic-prefix-" + secret + "-suffix"
                standalone._history = lambda _payload: (_ for _ in ()).throw(
                    WorkerApiError("Request failed: " + embedded))
                responses = self.lines([
                    {"id": "h", "op": "plugin_handshake", "payload": HANDSHAKE},
                    {"id": "business", "op": "history", "payload": {}},
                ], profile=directory, factory=lambda _directory: standalone, synthetic=False)
                self.assertFalse(responses[-1]["ok"])
                self.assertNotIn(embedded, json.dumps(responses[-1]))


if __name__ == "__main__":
    unittest.main()
