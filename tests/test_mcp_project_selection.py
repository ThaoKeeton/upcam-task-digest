"""Offline project selection and authorization boundaries; no production calls."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from teambition_readonly import CollectionError
from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerApiError
from upcam_app.secrets import MCP_AUTHORIZATION_FIELD, teambition_mcp_identity

PROJECTS = [{"id": "fictional-project-one", "name": "同名项目", "organizationId": "fictional-org-one"},
            {"id": "fictional-project-two", "name": "同名项目", "organizationId": "fictional-org-two"}]


class McpProjectSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.service = make_smoke_service(self.directory)
        self.calls = []
        self.rows = deepcopy(PROJECTS)
        self.failure = None
        self.after_read = lambda: None
        parent = self

        class ProjectClient:
            def __init__(self, tenant_id, *, credentials, project_discovery):
                parent.calls.append((tenant_id, credentials(), project_discovery))
            def list_projects(self):
                if parent.failure is not None:
                    raise parent.failure
                parent.after_read()
                return deepcopy(parent.rows)
        self.service.client_factory = ProjectClient
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def list_projects(self):
        return self.service.dispatch({"op": "list_projects"})

    def test_initialize_does_not_discover_projects_or_return_credentials(self):
        result = self.service.dispatch({"op": "initialize"})
        self.assertEqual(self.calls, [])
        self.assertNotIn("synthetic-mcp-token", json.dumps(result))

    def test_project_list_uses_authorized_token_without_tenant_and_does_not_save(self):
        before = self.service.settings_store.load()
        result = self.list_projects()
        self.assertEqual(result["projects"], PROJECTS)
        self.assertEqual(self.calls, [(None, "synthetic-mcp-token", True)])
        self.assertEqual(self.service.settings_store.load(), before)
        self.assertNotIn("synthetic-mcp-token", json.dumps(result))

    def test_project_success_explains_visible_membership_and_possible_omissions(self):
        result = self.list_projects()
        for text in ("当前账号参与的可见项目", "列表可能不完整", "手动填 ID", "归档"):
            self.assertIn(text, result["status"])
        self.assertNotIn("已完整读取", result["status"])
        self.assertNotIn("完整加载", result["status"])

    def test_tk_project_success_and_empty_list_disclose_possible_omissions(self):
        from desktop_app import DigestDesktopApp
        messages = []
        app = SimpleNamespace(secret_store=self.service.secret_store,
                              load_projects_button=SimpleNamespace(configure=lambda **_: None),
                              project_combo=SimpleNamespace(configure=lambda **_: None),
                              _set_status=messages.append)
        for projects in (deepcopy(PROJECTS), []):
            with self.subTest(empty=not projects):
                DigestDesktopApp._projects_loaded(app, projects, None,
                    teambition_mcp_identity(self.service.secret_store.load()))
                for text in ("当前账号参与的可见项目", "列表可能不完整", "手动填 ID", "归档"):
                    self.assertIn(text, messages[-1])
                self.assertNotIn("已完整读取", messages[-1])

    def test_winui_project_hint_does_not_claim_complete_discovery(self):
        page = (Path(__file__).parents[1] / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text()
        picker = page[page.index("private FrameworkElement BuildProjectPicker()"):page.index("private FrameworkElement BuildDingTalk()")]
        for text in ("当前账号参与的可见项目", "列表可能不完整", "手动", "归档"):
            self.assertIn(text, picker)

    def test_unapproved_token_cannot_construct_discovery_client(self):
        values = self.service.secret_store.load()
        values.pop(MCP_AUTHORIZATION_FIELD)
        self.service.secret_store.save(values)
        with self.assertRaisesRegex(WorkerApiError, "授权"):
            self.list_projects()
        self.assertEqual(self.calls, [])

    def test_selection_uses_project_identity_not_duplicate_display_name_and_only_returns_draft(self):
        before = self.service.settings_store.load()
        self.list_projects()
        selected = self.service.dispatch({"op": "select_project", "payload": {"project_id": "fictional-project-two"}})
        self.assertEqual(selected["project_id"], "fictional-project-two")
        self.assertEqual(selected["tenant_id"], "fictional-org-two")
        self.assertEqual(self.service.settings_store.load(), before)
        self.assertEqual(self.service.settings_store.load()["owner_names"], before["owner_names"])

    def test_verified_optional_prefix_is_project_scoped_and_draft_only(self):
        before = self.service.settings_store.load()
        self.rows[1]["uniqueIdPrefix"] = "NEW_2"
        result = self.list_projects()
        self.assertEqual(result["projects"][1].get("uniqueIdPrefix"), "NEW_2")
        selected = self.service.dispatch({"op": "select_project", "payload": {"project_id": "fictional-project-two"}})
        self.assertEqual(selected.get("task_number_prefix"), "NEW_2")
        self.assertEqual(selected.get("task_display_scope"), {
            "tenant_id": "fictional-org-two", "project_id": "fictional-project-two"})
        self.assertNotIn("task_link_view_id", selected)
        self.assertEqual(self.service.settings_store.load(), before)

    def test_missing_prefix_never_fills_or_changes_existing_display_settings(self):
        before = self.service.settings_store.load()
        before.update(task_number_prefix="ORIGINAL", task_link_view_id="a" * 24,
                      task_display_scope={"tenant_id": before["tenant_id"], "project_id": before["project_id"]})
        self.service.settings_store.save(before)
        for value in (None, ""):
            with self.subTest(value=value):
                self.rows[0]["uniqueIdPrefix"] = value
                projects = self.list_projects()["projects"]
                self.assertNotIn("uniqueIdPrefix", projects[0])
                selected = self.service.dispatch({"op": "select_project", "payload": {"project_id": "fictional-project-one"}})
                self.assertNotIn("task_number_prefix", selected)
                self.assertNotIn("task_display_scope", selected)
                self.assertEqual(self.service.settings_store.load(), before)

    def test_invalid_or_secret_prefix_does_not_offer_partial_or_guessed_results(self):
        before = self.service.settings_store.load()
        for prefix in (True, 123, [], {}, "1BAD", "BAD-PREFIX", " HAS_SPACE", "X" * 33, "BAD\n"):
            with self.subTest(prefix=prefix):
                self.rows[1]["uniqueIdPrefix"] = prefix
                with self.assertRaises(WorkerApiError):
                    self.list_projects()
                self.assertEqual(self.service._project_choices, {})
                self.assertEqual(self.service.settings_store.load(), before)
        credentials = self.service.secret_store.load()
        credentials["openai_api_key"] = "SYNTHETIC_SECRET"
        self.service.secret_store.save(credentials)
        self.rows[1]["uniqueIdPrefix"] = "SYNTHETIC_SECRET"
        with self.assertRaises(WorkerApiError) as raised:
            self.list_projects()
        self.assertNotIn("SYNTHETIC_SECRET", str(raised.exception))

    def test_conflicting_prefixes_for_one_project_are_rejected(self):
        self.rows = [dict(PROJECTS[0], uniqueIdPrefix="ONE"), dict(PROJECTS[0], uniqueIdPrefix="TWO")]
        with self.assertRaises(WorkerApiError):
            self.list_projects()
        self.assertEqual(self.service._project_choices, {})

    def test_tk_selected_prefix_stays_in_current_project_draft_until_saved(self):
        from desktop_app import DigestDesktopApp
        from test_legacy_mention_privacy import Value
        from upcam_app.secrets import SECRET_FIELDS
        before = self.service.settings_store.load()
        before.update(task_number_prefix="ORIGINAL", task_link_view_id="a" * 24,
                      task_display_scope={"tenant_id": before["tenant_id"], "project_id": before["project_id"]})
        self.service.settings_store.save(before)
        app = SimpleNamespace(project_choices=[dict(PROJECTS[0], uniqueIdPrefix="DISCOVERED")],
                              _project_choices_identity=teambition_mcp_identity(self.service.secret_store.load()),
                              secret_store=self.service.secret_store, settings=deepcopy(before),
                              project_combo=SimpleNamespace(current=lambda: 0),
                              values={"project_id": Value(before["project_id"]), "tenant_id": Value(before["tenant_id"])},
                              _set_status=lambda _: None)
        DigestDesktopApp._select_discovered_project(app)
        draft = dict(before, project_id=PROJECTS[0]["id"], tenant_id=PROJECTS[0]["organizationId"])
        DigestDesktopApp._apply_project_display_draft(app, draft)
        self.assertEqual(draft["task_number_prefix"], "DISCOVERED")
        self.assertEqual(draft["task_link_view_id"], "")
        self.assertEqual(draft["task_display_scope"], {
            "tenant_id": PROJECTS[0]["organizationId"], "project_id": PROJECTS[0]["id"]})
        different = dict(before, project_id="different-project")
        DigestDesktopApp._apply_project_display_draft(app, different)
        self.assertEqual(different["task_number_prefix"], before["task_number_prefix"])
        self.assertEqual(app.settings, before)
        self.assertEqual(self.service.settings_store.load(), before)
        app.values.update({key: Value("") for key in SECRET_FIELDS})
        app.saved_secrets = self.service.secret_store.load()
        app.settings_store = self.service.settings_store
        app.owner_map, app.status_map = before["owner_names"], before["status_names"]
        app.resolved_status_ids = set(before["resolved_status_ids"])
        app.view_filters, app._setup_probe = {}, None
        app.scope_confirm_var, app.root = Value(False), None
        app._load_model_cache = lambda: None
        for key in SECRET_FIELDS:
            setattr(app, "_secret_state_" + key, SimpleNamespace(configure=lambda **_: None))
        with patch("desktop_app.messagebox.showerror") as error, patch("desktop_app.messagebox.showinfo"):
            DigestDesktopApp._save(app)
        self.assertFalse(error.called)
        saved = app.settings_store.load()
        self.assertEqual(saved["task_number_prefix"], "DISCOVERED")
        self.assertEqual(saved["task_display_scope"], draft["task_display_scope"])
        self.assertEqual(saved["task_link_view_id"], "")

    def test_tk_without_reselection_or_same_project_without_prefix_preserves_display(self):
        from desktop_app import DigestDesktopApp
        from test_legacy_mention_privacy import Value
        before = self.service.settings_store.load()
        before.update(task_number_prefix="ORIGINAL", task_link_view_id="a" * 24,
                      task_display_scope={"tenant_id": before["tenant_id"], "project_id": before["project_id"]})
        app = SimpleNamespace(settings=deepcopy(before))
        draft = deepcopy(before)
        DigestDesktopApp._apply_project_display_draft(app, draft)
        self.assertEqual(draft, before)
        app.secret_store = self.service.secret_store
        app._project_choices_identity = teambition_mcp_identity(self.service.secret_store.load())
        app.project_choices = [{"id": before["project_id"], "organizationId": before["tenant_id"], "name": "current"}]
        app.project_combo = SimpleNamespace(current=lambda: 0)
        app.values = {key: Value(before[key]) for key in ("project_id", "tenant_id")}
        app._set_status = lambda _: None
        DigestDesktopApp._select_discovered_project(app)
        DigestDesktopApp._apply_project_display_draft(app, draft)
        self.assertEqual(draft, before)

    def test_winui_selection_applies_prefix_after_identity_and_scope_only_when_present(self):
        page = (Path(__file__).parents[1] / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text()
        picker = page[page.index("private FrameworkElement BuildProjectPicker()"):page.index("private FrameworkElement BuildDingTalk()")]
        self.assertIn('result["task_number_prefix"] is JsonValue', picker)
        self.assertIn('_state.Set("task_number_prefix"', picker)
        self.assertIn('result["task_display_scope"]', picker)
        self.assertLess(picker.index('_state.Set("project_id"'), picker.index('_state.Set("task_number_prefix"'))

    def test_incomplete_pagination_is_explicit_discards_choices_and_preserves_original_project(self):
        before = self.service.settings_store.load()
        self.list_projects()
        self.failure = CollectionError("Invalid or repeated pagination token private-server-body")
        with self.assertRaises(WorkerApiError) as raised:
            self.list_projects()
        self.assertIn("分页", str(raised.exception))
        self.assertIn("手动", str(raised.exception))
        self.assertNotIn("private-server-body", str(raised.exception))
        with self.assertRaises(WorkerApiError):
            self.service.dispatch({"op": "select_project", "payload": {"project_id": "fictional-project-one"}})
        self.assertEqual(self.service.settings_store.load(), before)

    def test_changed_or_revoked_authorization_during_discovery_cannot_return_stale_projects(self):
        def revoke():
            values = self.service.secret_store.load()
            values.pop(MCP_AUTHORIZATION_FIELD)
            self.service.secret_store.save(values)
        self.after_read = revoke
        with self.assertRaisesRegex(WorkerApiError, "授权"):
            self.list_projects()
        with self.assertRaises(WorkerApiError):
            self.service.dispatch({"op": "select_project", "payload": {"project_id": "fictional-project-one"}})

    def test_project_results_cannot_echo_configured_credentials(self):
        self.rows[0]["name"] = "contains synthetic-mcp-token here"
        with self.assertRaises(WorkerApiError) as raised:
            self.list_projects()
        self.assertNotIn("synthetic-mcp-token", str(raised.exception))

    def test_unknown_or_unlisted_selection_cannot_change_scope(self):
        self.list_projects()
        before = self.service.settings_store.load()
        for payload in ({"project_id": "unlisted-project"},
                        {"project_id": "fictional-project-one", "tenant_id": "injected-org"}):
            with self.subTest(payload=payload), self.assertRaises(WorkerApiError):
                self.service.dispatch({"op": "select_project", "payload": payload})
        self.assertEqual(self.service.settings_store.load(), before)

    def test_tk_selection_fills_both_ids_but_does_not_save(self):
        from desktop_app import DigestDesktopApp
        from test_legacy_mention_privacy import Value
        app = SimpleNamespace(project_choices=deepcopy(PROJECTS),
                              _project_choices_identity=teambition_mcp_identity(self.service.secret_store.load()),
                              secret_store=self.service.secret_store,
                              project_combo=SimpleNamespace(current=lambda: 1),
                              values={"project_id": Value("old-project"), "tenant_id": Value("old-org")},
                              _set_status=lambda _: None)
        before = self.service.settings_store.load()
        DigestDesktopApp._select_discovered_project(app)
        self.assertEqual(app.values["project_id"].get(), "fictional-project-two")
        self.assertEqual(app.values["tenant_id"].get(), "fictional-org-two")
        self.assertEqual(self.service.settings_store.load(), before)

    def test_empty_complete_list_is_explained_without_clearing_current_project(self):
        before = self.service.settings_store.load()
        self.rows = []
        result = self.list_projects()
        self.assertEqual(result["projects"], [])
        self.assertIn("未找到", result["status"])
        self.assertIn("列表可能不完整", result["status"])
        self.assertEqual(self.service.settings_store.load(), before)

    def test_conflicting_or_malformed_rows_never_return_partial_choices(self):
        for rows in ([PROJECTS[0], dict(PROJECTS[0], organizationId="different-org")],
                     [PROJECTS[0], {"id": "invalid"}],
                     [dict(PROJECTS[0], name="hidden\ncontrol")]):
            with self.subTest(rows=rows):
                self.rows = deepcopy(rows)
                with self.assertRaises(WorkerApiError):
                    self.list_projects()
                self.assertEqual(self.service._project_choices, {})

    def test_synthetic_ui_service_supports_project_picker_without_a_transport(self):
        service = make_smoke_service(self.directory / "synthetic-ui")
        result = service.dispatch({"op": "list_projects"})
        self.assertEqual(result["projects"], [{"id": "demo-project", "name": "CI 虚构项目", "organizationId": "demo-org"}])

    def test_malformed_client_project_response_cannot_return_candidates_or_change_settings(self):
        from teambition_readonly import Client
        before = self.service.settings_store.load()
        self.list_projects()
        invalid_rows = [dict(PROJECTS[0], isArchived=False),
                        {"id": "invalid-project", "name": "private-malformed-response"}]
        self.service.client_factory = lambda tenant, **kwargs: Client(
            tenant, **kwargs, mcp_adapter_factory=lambda _: SimpleNamespace(projects=lambda: invalid_rows))
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden")) as network:
            with self.assertRaises(WorkerApiError) as raised:
                self.list_projects()
        self.assertIn("加载失败", str(raised.exception))
        self.assertNotIn("private-malformed-response", str(raised.exception))
        self.assertEqual(network.call_count, 0)
        self.assertEqual(self.service._project_choices, {})
        self.assertIsNone(self.service._project_choices_identity)
        self.assertEqual(self.service.settings_store.load(), before)
        with self.assertRaises(WorkerApiError):
            self.service.dispatch({"op": "select_project", "payload": {"project_id": PROJECTS[0]["id"]}})

    def test_loading_projects_cannot_save_unrelated_ui_drafts(self):
        page = (Path(__file__).parents[1] / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text()
        picker = page[page.index("private FrameworkElement BuildProjectPicker()"):page.index("private FrameworkElement BuildDingTalk()")]
        self.assertNotIn("SaveCoreAsync", picker)

    def test_tk_revoked_selection_keeps_both_original_ids(self):
        from desktop_app import DigestDesktopApp
        from test_legacy_mention_privacy import Value
        values = self.service.secret_store.load()
        identity = teambition_mcp_identity(values)
        values.pop(MCP_AUTHORIZATION_FIELD)
        self.service.secret_store.save(values)
        app = SimpleNamespace(project_choices=deepcopy(PROJECTS), _project_choices_identity=identity,
                              secret_store=self.service.secret_store,
                              project_combo=SimpleNamespace(current=lambda: 1),
                              values={"project_id": Value("old-project"), "tenant_id": Value("old-org")},
                              _set_status=lambda _: None)
        DigestDesktopApp._select_discovered_project(app)
        self.assertEqual(app.values["project_id"].get(), "old-project")
        self.assertEqual(app.values["tenant_id"].get(), "old-org")

    def test_winui_has_explicit_load_and_selection_and_keeps_manual_id_fields(self):
        page = (Path(__file__).parents[1] / "winui/UPCAMTaskDigest/MainWindow.Pages.cs").read_text()
        for text in ("加载我的项目", '"list_projects"', '"select_project"', '"tenant-id"', '"project-id"'):
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
