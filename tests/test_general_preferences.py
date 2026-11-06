"""General settings never probe a project or expose credentials."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from upcam_app.config import DEFAULT_SETTINGS, SettingsError, SettingsStore, validate_settings
from upcam_app.secrets import MemorySecretStore
from upcam_app.worker_api import WorkerApiError, WorkerService


class GeneralPreferenceTests(unittest.TestCase):
    def test_old_settings_gain_supported_general_defaults(self):
        settings = validate_settings({"schema_version": 1})
        self.assertEqual(settings.get("ui_language"), "system")
        self.assertEqual(settings.get("ui_theme"), "system")
        self.assertIs(settings.get("tray_icon_enabled"), True)
        self.assertIs(settings.get("close_to_tray"), True)
        self.assertIs(settings.get("start_in_tray"), False)
        self.assertIs(settings.get("auto_check_updates"), False)

    def test_invalid_preferences_and_hidden_without_tray_are_rejected(self):
        for change in ({"ui_language": "made-up"}, {"ui_theme": "purple"},
                       {"tray_icon_enabled": 1}, {"auto_check_updates": "yes"},
                       {"tray_icon_enabled": False},
                       {"tray_icon_enabled": False, "close_to_tray": False, "start_in_tray": True}):
            with self.subTest(change=change), self.assertRaises(SettingsError):
                validate_settings(dict(DEFAULT_SETTINGS, **change))

    def service(self, directory, *, startup=None):
        store = SettingsStore(Path(directory) / "settings.json")
        secret = MemorySecretStore()
        calls = []
        def no_enterprise(*args, **kwargs):
            raise AssertionError("General settings must not access enterprise services")
        def configure(enabled, **kwargs):
            calls.append(enabled)
        worker = WorkerService(Path(directory), settings_store=store, secret_store=secret,
                               client_factory=no_enterprise,
                               startup_configurer=startup or configure)
        return worker, store, secret, calls

    def test_general_save_works_for_blank_project_without_other_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, store, secret, calls = self.service(directory)
            before = store.load()
            result = worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {"ui_language": "en-US", "ui_theme": "light"},
            }})
            after = store.load()
            self.assertEqual(after, dict(before, ui_language="en-US", ui_theme="light"))
            self.assertEqual(result["preferences"]["ui_language"], "en-US")
            self.assertEqual(secret.load(), {})
            self.assertEqual(calls, [])

    def test_general_save_preserves_verified_scope_and_auto_run(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, store, secret, calls = self.service(directory)
            before = deepcopy(DEFAULT_SETTINGS)
            before.update(tenant_id="synthetic-org", project_id="synthetic-project",
                          api_contract_verified=True, auto_send_enabled=True,
                          owner_names={"synthetic-owner": "虚构成员"},
                          view={"verified": True, "evidence": "Synthetic verified scope", "task_equals": {}})
            store.save(before)
            worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {"ui_theme": "dark"}}})
            self.assertEqual(store.load(), dict(before, ui_theme="dark"))
            self.assertEqual(calls, [])

    def test_private_update_token_is_secret_presence_only_and_separately_clearable(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, store, secret, calls = self.service(directory)
            result = worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {}, "github_token": "github_pat_synthetic-only"}})
            self.assertEqual(secret.load()["github_update_token"], "github_pat_synthetic-only")
            self.assertTrue(result["secret_presence"]["github_update_token"])
            self.assertNotIn("github_pat_synthetic-only", json.dumps(result))
            self.assertNotIn("github_pat_synthetic-only", store.path.read_text())
            worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {}, "clear_github_token": True}})
            self.assertNotIn("github_update_token", secret.load())

    def test_general_payload_cannot_change_business_fields_or_inject_headers(self):
        for payload in ({"preferences": {"project_id": "forged"}},
                        {"preferences": {}, "secret_values": {"openai_api_key": "forged"}},
                        {"preferences": {}, "github_token": "synthetic\r\nHeader: bad"}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                worker, store, secret, calls = self.service(directory)
                before = store.load()
                with self.assertRaises(WorkerApiError):
                    worker.dispatch({"op": "save_preferences", "payload": payload})
                self.assertEqual(store.load(), before)
                self.assertEqual(secret.load(), {})
                self.assertEqual(calls, [])

    def test_startup_failure_restores_preferences_and_update_token(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = []
            def fail_once(enabled, **kwargs):
                attempts.append(enabled)
                if len(attempts) == 1:
                    raise OSError("synthetic startup failure")
            worker, store, secret, calls = self.service(directory, startup=fail_once)
            before = store.load()
            with self.assertRaises(WorkerApiError):
                worker.dispatch({"op": "save_preferences", "payload": {
                    "preferences": {"start_with_windows": True, "ui_theme": "light"},
                    "github_token": "github_pat_synthetic-only"}})
            self.assertEqual(store.load(), before)
            self.assertEqual(secret.load(), {})
            self.assertEqual(attempts, [True, False])

    def test_resaving_enabled_startup_reapplies_current_application_path(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, store, secret, calls = self.service(directory)
            store.save(dict(DEFAULT_SETTINGS, start_with_windows=True))
            worker.dispatch({"op": "save_preferences", "payload": {"preferences": {}}})
            self.assertEqual(calls, [True])
            self.assertTrue(store.load()["start_with_windows"])

    def test_language_bootstrap_contains_only_saved_interface_language(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, store, secret, calls = self.service(directory)
            worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {"ui_language": "en-US"}, "github_token": "github_pat_synthetic-only"}})
            self.assertEqual(json.loads((Path(directory) / "language.json").read_text()), {"language": "en-US"})


if __name__ == "__main__":
    unittest.main()
