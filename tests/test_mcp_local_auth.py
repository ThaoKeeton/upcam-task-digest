"""Offline migration coverage. No real credentials or network are used."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teambition_readonly import CollectionError
from upcam_app.collection_runtime import make_collection_client
from upcam_app.secrets import DpapiSecretStore, SECRET_FIELDS, teambition_mcp_identity, MCP_AUTHORIZATION_FIELD
from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerApiError, _teambition_identity


class McpLocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = make_smoke_service(self.root)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        end = datetime.now().replace(second=0, microsecond=0)
        self.window = {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                       "end": end.strftime("%Y-%m-%d %H:%M")}

    def legacy_credentials(self):
        values = self.service.secret_store.load()
        values.pop("teambition_mcp_token", None)
        values.update(teambition_app_id="legacy-fictional-id", teambition_app_secret="legacy-fictional-secret")
        self.service.secret_store.save(values)
        return values

    def test_only_mcp_token_is_an_editable_teambition_credential(self):
        self.assertIn("teambition_mcp_token", SECRET_FIELDS)
        self.assertNotIn("teambition_app_id", SECRET_FIELDS)
        self.assertNotIn("teambition_app_secret", SECRET_FIELDS)

    def test_dpapi_round_trip_preserves_legacy_values_when_saving_mcp_token(self):
        expected = dict(self.legacy_credentials(), teambition_mcp_token="fictional-private-mcp-token")
        store = DpapiSecretStore(self.root / "credentials.dpapi")
        transform = lambda data: bytes(value ^ 0x69 for value in data)
        with patch.object(store, "_protect", side_effect=transform), patch.object(store, "_unprotect", side_effect=transform):
            store.save(expected)
            self.assertEqual(store.load(), expected)
        self.assertNotIn(b"fictional-private-mcp-token", store.path.read_bytes())

    def test_initialization_revokes_legacy_scope_without_deleting_other_settings_or_secrets(self):
        before_secrets = self.legacy_credentials()
        settings = self.service.settings_store.load()
        settings["api_contract_verified"] = True
        settings["view"].update(verified=True, evidence="desktop-readonly-probe/v1")
        settings.update(auto_send_enabled=True, auto_send_enabled_since_local="2026-01-01T08:00:00")
        self.service.settings_store.save(settings)
        result = self.service.dispatch({"op": "initialize"})
        self.assertFalse(result["secret_presence"]["teambition_mcp_token"])
        self.assertNotIn("teambition_app_id", result["secret_presence"])
        self.assertFalse(result["settings"]["api_contract_verified"])
        self.assertFalse(result["settings"]["view"]["verified"])
        self.assertFalse(result["settings"]["auto_send_enabled"])
        self.assertEqual(result["settings"]["owner_names"], settings["owner_names"])
        self.assertEqual(result["settings"]["status_names"], settings["status_names"])
        for key, value in before_secrets.items():
            self.assertEqual(self.service.secret_store.load()[key], value)
        self.assertEqual(self.service.settings_store.load(), result["settings"])
        self.assertNotIn("legacy-fictional-secret", json.dumps(result))

    def test_legacy_migration_keeps_recoverable_proof_without_restoring_automatic_sends(self):
        self.legacy_credentials()
        previous = self.service.settings_store.load()
        previous.update(auto_send_enabled=True, auto_send_enabled_since_local="2026-01-01T08:00:00",
                        api_contract_verified=True)
        previous["view"].update(verified=True, evidence="fictional-legacy-scope-proof")
        self.service.settings_store.save(previous)
        initialized = self.service.dispatch({"op": "initialize"})
        backup = initialized["settings"]["mcp_migration_previous_authorization"]
        self.assertTrue(backup["auto_send_enabled"])
        self.assertEqual(backup["auto_send_enabled_since_local"], previous["auto_send_enabled_since_local"])
        self.assertEqual(backup["view"], previous["view"])
        self.assertTrue(backup["api_contract_verified"])
        again = self.service.dispatch({"op": "initialize"})
        self.assertEqual(again["settings"]["mcp_migration_previous_authorization"], backup)
        self.assertEqual(self.service.dispatch({"op": "run_due"})["started"], 0)
        approved = self.service.dispatch({"op": "save", "payload": {
            "settings": again["settings"], "authorize_teambition_mcp": True,
            "secret_values": {"teambition_mcp_token": "new-fictional-approved-token"},
            "verification_window": self.window}})
        self.assertTrue(approved["secret_presence"]["teambition_mcp_authorized"])
        self.assertFalse(approved["settings"]["auto_send_enabled"])
        self.assertEqual(approved["settings"]["mcp_migration_previous_authorization"], backup)
        self.assertEqual(self.service.dispatch({"op": "run_due"})["started"], 0)

    def test_saving_token_keeps_existing_configuration_and_does_not_echo_credentials(self):
        before = self.legacy_credentials()
        settings = self.service.settings_store.load()
        result = self.service.dispatch({"op": "save", "payload": {"settings": settings,
            "secret_values": {"teambition_mcp_token": "fictional-new-mcp-token"},
            "verification_window": self.window}})
        self.assertTrue(result["secret_presence"]["teambition_mcp_token"])
        self.assertEqual(self.service.secret_store.load()["teambition_app_secret"], before["teambition_app_secret"])
        self.assertEqual(result["settings"]["owner_names"], settings["owner_names"])
        self.assertNotIn("fictional-new-mcp-token", json.dumps(result))

    def test_legacy_credentials_cannot_test_probe_diagnose_or_construct_a_client(self):
        self.legacy_credentials()
        for operation in ("test_teambition", "probe_project", "diagnose"):
            with self.subTest(operation=operation), self.assertRaisesRegex(WorkerApiError, "MCP"):
                self.service.dispatch({"op": operation, "payload": {} if operation == "test_teambition" else self.window})
        with self.assertRaises(CollectionError):
            make_collection_client(self.service.settings_store.load(), self.service.secret_store.load(), self.root,
                                   client_factory=lambda *_args, **_kwargs: self.fail("Legacy fallback"))

    def test_identity_uses_only_mcp_token_and_frozen_runtime_callback(self):
        values = self.legacy_credentials()
        self.assertIsNone(_teambition_identity(values))
        values["teambition_mcp_token"] = "fictional-stable-token"
        values[MCP_AUTHORIZATION_FIELD] = teambition_mcp_identity(values)
        identity = _teambition_identity(values)
        self.assertIsNotNone(identity)
        values["teambition_app_secret"] = "unrelated-old-value"
        self.assertEqual(_teambition_identity(values), identity)
        class OfflineClient:
            def __init__(self, tenant_id, *, credentials):
                self.credentials = credentials
        client = make_collection_client(self.service.settings_store.load(), values, self.root,
                                        client_factory=OfflineClient)
        values["teambition_mcp_token"] = "fictional-rotated-token"
        self.assertEqual(client.credentials(), "fictional-stable-token")
        self.assertNotEqual(_teambition_identity(values), identity)

    def test_expired_or_revoked_token_has_actionable_safe_connection_error(self):
        values = self.service.secret_store.load()
        values["teambition_mcp_token"] = "fictional-not-in-error"
        values[MCP_AUTHORIZATION_FIELD] = teambition_mcp_identity(values)
        self.service.secret_store.save(values)
        with patch.object(self.service, "_collect_source", side_effect=CollectionError("Teambition MCP authorization expired or denied")):
            with self.assertRaises(WorkerApiError) as raised:
                self.service.dispatch({"op": "test_teambition"})
        self.assertIn("Token", str(raised.exception))
        self.assertIn("权限", str(raised.exception))
        self.assertNotIn("fictional-not-in-error", str(raised.exception))

    def metadata_client(self):
        from upcam_app.smoke_data import SyntheticClient
        original = self.service.settings_store.load()
        metadata = {"owner_names": original["owner_names"], "status_names": original["status_names"],
                    "resolved_status_ids": original["resolved_status_ids"],
                    "status_resolution_known": list(original["status_names"])}
        class MetadataClient(SyntheticClient):
            def collect(self, *args, **kwargs):
                result = super().collect(*args, **kwargs)
                result["metadata"] = deepcopy(metadata)
                return result
        original.update(owner_names={}, status_names={}, resolved_status_ids=[])
        self.service.settings_store.save(original)
        return MetadataClient

    def test_diagnosis_resolves_names_from_mcp_without_saving_user_maps(self):
        self.service.client_factory = self.metadata_client()
        self.service.dispatch({"op": "save", "payload": {"settings": self.service.settings_store.load(),
                                                       "verification_window": self.window}})
        result = self.service.dispatch({"op": "diagnose", "payload": self.window})
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(self.service.settings_store.load()["owner_names"], {})
        self.assertEqual(self.service.settings_store.load()["status_names"], {})

    def test_fixed_digest_resolves_mcp_metadata_before_summary_and_delivery(self):
        from upcam_app.workflow import DigestWorkflow
        from upcam_app.smoke_data import SyntheticDingTalk, SyntheticSummarizer
        factory = self.metadata_client()
        self.service.dispatch({"op": "initialize"})
        self.service.dispatch({"op": "refresh_models"})
        self.service.client_factory = factory
        settings = self.service.dispatch({"op": "save", "payload": {
            "settings": self.service.settings_store.load(), "verification_window": self.window}})["settings"]
        workflow = DigestWorkflow(settings, self.service.secret_store.load(), self.root,
                                  client_factory=factory, summarizer=SyntheticSummarizer(),
                                  ding_sender=SyntheticDingTalk())
        result = workflow.run(datetime.strptime(self.window["start"], "%Y-%m-%d %H:%M"),
                              datetime.strptime(self.window["end"], "%Y-%m-%d %H:%M"))
        self.assertEqual(result["state"], "sent")
        self.assertIn("虚构成员甲", result["report"]["markdown"])
        self.assertEqual(self.service.settings_store.load()["owner_names"], {})
        self.assertEqual(self.service.settings_store.load()["status_names"], {})

    def test_saving_a_token_without_explicit_consent_never_connects(self):
        settings = self.service.settings_store.load()
        with patch.object(self.service, "_collect_source", side_effect=AssertionError("No consent")) as collect:
            result = self.service.dispatch({"op": "save", "payload": {
                "settings": settings, "secret_values": {"teambition_mcp_token": "unapproved-fictional-token"},
                "verification_window": self.window}})
        self.assertEqual(collect.call_count, 0)
        self.assertFalse(result["settings"]["api_contract_verified"])
        self.assertFalse(result["secret_presence"]["teambition_mcp_authorized"])
        with self.assertRaisesRegex(WorkerApiError, "授权"):
            self.service.dispatch({"op": "test_teambition"})

    def test_explicit_consent_is_bound_to_token_and_survives_restart(self):
        settings = self.service.settings_store.load()
        result = self.service.dispatch({"op": "save", "payload": {
            "settings": settings, "secret_values": {"teambition_mcp_token": "approved-fictional-token"},
            "authorize_teambition_mcp": True, "verification_window": self.window}})
        self.assertTrue(result["secret_presence"]["teambition_mcp_authorized"])
        self.assertTrue(result["settings"]["api_contract_verified"])
        self.assertTrue(self.service.dispatch({"op": "initialize"})["secret_presence"]["teambition_mcp_authorized"])
        credentials = self.service.secret_store.load()
        credentials["teambition_mcp_token"] = "rotated-without-consent"
        self.service.secret_store.save(credentials)
        with self.assertRaisesRegex(WorkerApiError, "授权"):
            self.service.dispatch({"op": "test_teambition"})
        result = self.service.dispatch({"op": "initialize"})
        self.assertFalse(result["settings"]["api_contract_verified"])
        self.assertFalse(result["secret_presence"]["teambition_mcp_authorized"])

    def test_worker_token_removal_or_rotation_cannot_revive_an_old_grant(self):
        for first_change in ({"clear_secrets": ["teambition_mcp_token"]},
                             {"secret_values": {"teambition_mcp_token": "fictional-token-b"}},
                             {"clear_secrets": ["teambition_mcp_token"],
                              "secret_values": {"teambition_mcp_token": "synthetic-mcp-token"}}):
            with self.subTest(first_change=first_change):
                service = make_smoke_service(self.root / str(len(str(first_change))))
                original = service.secret_store.load()
                token_a = original["teambition_mcp_token"]
                original["teambition_app_secret"] = "fictional-retained-legacy-secret"
                service.secret_store.save(original)
                with patch.object(service, "_collect_source", side_effect=AssertionError("No new consent")) as collect:
                    for change in (first_change, {"secret_values": {"teambition_mcp_token": token_a}}):
                        result = service.dispatch({"op": "save", "payload": {
                            "settings": service.settings_store.load(), "verification_window": self.window, **change}})
                        self.assertNotIn(MCP_AUTHORIZATION_FIELD, service.secret_store.load())
                        self.assertFalse(result["secret_presence"]["teambition_mcp_authorized"])
                        self.assertFalse(result["settings"]["api_contract_verified"])
                        self.assertFalse(result["settings"]["auto_send_enabled"])
                        self.assertEqual(service.secret_store.load()["teambition_app_secret"],
                                         "fictional-retained-legacy-secret")
                        with self.assertRaisesRegex(WorkerApiError, "授权"):
                            service.dispatch({"op": "list_projects"})
                    self.assertEqual(collect.call_count, 0)
                self.assertEqual(service.secret_store.load()["teambition_mcp_token"], token_a)

    def test_worker_clear_all_then_same_token_still_requires_new_explicit_consent(self):
        token_a = self.service.secret_store.load()["teambition_mcp_token"]
        self.service.dispatch({"op": "clear_credentials"})
        self.assertNotIn(MCP_AUTHORIZATION_FIELD, self.service.secret_store.load())
        with patch.object(self.service, "_collect_source", side_effect=AssertionError("No new consent")) as collect:
            result = self.service.dispatch({"op": "save", "payload": {
                "settings": self.service.settings_store.load(), "verification_window": self.window,
                "secret_values": {"teambition_mcp_token": token_a}}})
        self.assertEqual(collect.call_count, 0)
        self.assertNotIn(MCP_AUTHORIZATION_FIELD, self.service.secret_store.load())
        self.assertFalse(result["secret_presence"]["teambition_mcp_authorized"])
        granted = self.service.dispatch({"op": "save", "payload": {
            "settings": self.service.settings_store.load(), "authorize_teambition_mcp": True}})
        self.assertTrue(granted["secret_presence"]["teambition_mcp_authorized"])

    def tk_auth_app(self):
        from types import SimpleNamespace
        from test_legacy_mention_privacy import Value, LocalStore
        settings, secrets = self.service.settings_store.load(), self.service.secret_store.load()
        values = {key: Value("") for key in SECRET_FIELDS}
        values["auto_send_enabled"] = Value(False)
        app = SimpleNamespace(settings=settings, values=values, owner_map=settings["owner_names"],
                              status_map=settings["status_names"], resolved_status_ids=set(settings["resolved_status_ids"]),
                              view_filters={}, _setup_probe=None, scope_confirm_var=Value(False), root=None,
                              secret_store=LocalStore(secrets), settings_store=LocalStore(settings),
                              saved_secrets=secrets, _load_model_cache=lambda: None, _set_status=lambda _: None)
        for key in SECRET_FIELDS:
            setattr(app, "_secret_state_" + key, SimpleNamespace(configure=lambda **_: None))
        return app

    def test_tk_rotation_back_to_previous_token_requires_a_new_prompt_and_grant(self):
        from desktop_app import DigestDesktopApp
        app = self.tk_auth_app()
        token_a = app.saved_secrets["teambition_mcp_token"]
        with patch("desktop_app.messagebox.showerror") as error, patch("desktop_app.messagebox.showinfo"), \
                patch("desktop_app.messagebox.askyesno", return_value=False) as consent, \
                patch("desktop_app._new_teambition_client", side_effect=AssertionError("No new consent")) as client:
            for index, token in enumerate(("fictional-token-b", token_a), 1):
                app.values["teambition_mcp_token"].set(token)
                DigestDesktopApp._save(app)
                self.assertFalse(error.called)
                self.assertEqual(consent.call_count, index)
                self.assertEqual(app.secret_store.load()["teambition_mcp_token"], token)
                self.assertNotIn(MCP_AUTHORIZATION_FIELD, app.saved_secrets)
                self.assertNotIn(MCP_AUTHORIZATION_FIELD, app.secret_store.load())
                self.assertFalse(app.settings["api_contract_verified"])
                self.assertFalse(app.settings["auto_send_enabled"])
            self.assertEqual(client.call_count, 0)

    def test_tk_clear_services_then_reenter_previous_token_requires_new_consent(self):
        from desktop_app import DigestDesktopApp
        app = self.tk_auth_app()
        token_a = app.saved_secrets["teambition_mcp_token"]
        with patch("desktop_app.messagebox.askyesno", return_value=True), patch("desktop_app.messagebox.showerror") as error:
            DigestDesktopApp._clear_secrets(app)
        self.assertFalse(error.called)
        self.assertNotIn(MCP_AUTHORIZATION_FIELD, app.secret_store.load())
        app.values["teambition_mcp_token"].set(token_a)
        with patch("desktop_app.messagebox.showerror") as error, patch("desktop_app.messagebox.showinfo"), \
                patch("desktop_app.messagebox.askyesno", return_value=False) as consent:
            DigestDesktopApp._save(app)
        self.assertFalse(error.called)
        self.assertEqual(consent.call_count, 1)
        self.assertNotIn(MCP_AUTHORIZATION_FIELD, app.secret_store.load())
        self.assertFalse(app.settings["api_contract_verified"])

    def test_tk_declined_rotation_cannot_retain_a_grant_from_reloaded_store(self):
        from desktop_app import DigestDesktopApp
        app = self.tk_auth_app()
        token_a = app.saved_secrets["teambition_mcp_token"]
        app.saved_secrets = dict(app.saved_secrets, teambition_mcp_token="fictional-token-b")
        app.saved_secrets.pop(MCP_AUTHORIZATION_FIELD)
        app.values["teambition_mcp_token"].set(token_a)
        with patch("desktop_app.messagebox.showerror") as error, patch("desktop_app.messagebox.showinfo"), \
                patch("desktop_app.messagebox.askyesno", return_value=False) as consent:
            DigestDesktopApp._save(app)
        self.assertFalse(error.called)
        self.assertEqual(consent.call_count, 1)
        self.assertNotIn(MCP_AUTHORIZATION_FIELD, app.secret_store.load())
        self.assertFalse(app.settings["api_contract_verified"])

    def test_legacy_tk_token_rotation_invalidates_prior_scope_proof(self):
        from types import SimpleNamespace
        from desktop_app import DigestDesktopApp
        from test_legacy_mention_privacy import Value, LocalStore
        settings = self.service.settings_store.load()
        settings["api_contract_verified"] = True
        settings["view"].update(verified=True, evidence="previous-token-proof")
        values = {key: Value("") for key in SECRET_FIELDS}
        values["teambition_mcp_token"] = Value("fictional-rotated-token")
        secrets = self.service.secret_store.load()
        app = SimpleNamespace(settings=settings, values=values, owner_map=settings["owner_names"],
                              status_map=settings["status_names"], resolved_status_ids=set(settings["resolved_status_ids"]),
                              view_filters={}, _setup_probe=None, scope_confirm_var=Value(True), root=None,
                              secret_store=LocalStore(secrets), settings_store=LocalStore(settings),
                              saved_secrets=secrets, _load_model_cache=lambda: None, _set_status=lambda _: None)
        for key in SECRET_FIELDS:
            setattr(app, "_secret_state_" + key, SimpleNamespace(configure=lambda **_: None))
        with patch("desktop_app.messagebox.showerror"), patch("desktop_app.messagebox.showinfo"), \
                patch("desktop_app.messagebox.askyesno", return_value=False):
            DigestDesktopApp._save(app)
        self.assertFalse(app.settings["api_contract_verified"])
        self.assertFalse(app.settings["view"]["verified"])
        self.assertEqual(app.secret_store.load()["teambition_mcp_token"], "fictional-rotated-token")

    def test_mapping_overrides_leave_the_primary_winui_navigation(self):
        import xml.etree.ElementTree as ET
        repo = Path(__file__).parents[1]
        root = ET.parse(repo / "winui/UPCAMTaskDigest/MainWindow.xaml").getroot()
        primary_routes = [node.attrib.get("Tag") for node in root.iter() if node.tag.endswith("NavigationViewItem")]
        self.assertNotIn("members", primary_routes)
        self.assertNotIn("statuses", primary_routes)
        page = (repo / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text(encoding="utf-8")
        self.assertIn('NavigationButton("成员覆盖（可选）", "members")', page)
        self.assertIn('NavigationButton("状态覆盖（可选）", "statuses")', page)

    def test_frontends_only_render_personal_token_and_offer_optional_mapping_overrides(self):
        repo = Path(__file__).parents[1]
        tk_source = (repo / "desktop_app.py").read_text(encoding="utf-8")
        winui = (repo / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text(encoding="utf-8")
        for source in (tk_source, winui):
            self.assertIn("teambition_mcp_token", source)
            self.assertNotIn('"teambition_app_id"', source)
            self.assertNotIn('"teambition_app_secret"', source)
            self.assertIn("高级", source)
            self.assertIn("自动读取", source)


if __name__ == "__main__":
    unittest.main()
