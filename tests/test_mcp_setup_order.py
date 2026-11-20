"""Offline setup ordering and first-use flow, with synthetic credentials only."""

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from desktop_app import DigestDesktopApp
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.secrets import mcp_connection_authorized
from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerApiError
from test_legacy_mention_privacy import Value


class FormValue(Value):
    def __init__(self, value=""):
        super().__init__(value)

    def trace_add(self, *_args):
        pass


class FormWidget:
    """Records the real Tk builder's layout without an OS display server."""

    def __init__(self, parent=None, **options):
        self.parent, self.options = parent, options
        self.children, self.layout = [], {}
        if parent is not None:
            parent.children.append(self)

    def grid(self, **layout):
        self.layout = layout

    def pack(self, **layout):
        self.layout = layout

    def columnconfigure(self, *_args, **_kwargs):
        pass

    def bind(self, *_args):
        pass

    def form_row(self, form):
        item = self
        while item.parent is not form:
            item = item.parent
        return item.layout["row"]

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class McpSetupOrderTests(unittest.TestCase):
    def build_form(self):
        app = DigestDesktopApp.__new__(DigestDesktopApp)
        app.settings_tab, app.values, app.secret_entries = FormWidget(), {}, {}
        app.wraplength = 900
        with ExitStack() as stack:
            for name in ("Frame", "Label", "Button", "Combobox", "Entry", "Separator"):
                stack.enter_context(patch("desktop_app.ttk." + name, FormWidget))
            stack.enter_context(patch("desktop_app.tk.StringVar", FormValue))
            app._build_settings()
        return app

    def test_run_status_discloses_raw_source_data_sent_to_openai(self):
        import threading
        app = DigestDesktopApp.__new__(DigestDesktopApp)
        app.ledger_ready, app._job_lock = True, threading.Lock()
        messages = []
        app._set_status = messages.append
        with patch('desktop_app.threading.Thread') as thread:
            app._launch_run(None, None, schedule_key=None, scheduled=False)
            thread.return_value.start.assert_called_once()
        self.assertIn('原始动态', messages[-1])
        self.assertIn('任务上下文', messages[-1])
        self.assertNotIn('只发送必要评论摘要', messages[-1])
        app._job_lock.release()

    def test_native_smoke_uses_current_scope_diagnostic_copy(self):
        source = (Path(__file__).resolve().parents[1] / 'winui' / 'UPCAMTaskDigest' / 'MainWindow.Smoke.cs').read_text(encoding='utf-8')
        self.assertNotIn('窗口内未支持动作：0 条', source)
        self.assertNotIn('时间归属无法判断的未支持动作：0 条', source)
        self.assertNotIn('copiedDiagnostic.Contains("只读校验通过")', source)
        self.assertIn('窗口内旧解析器未分类动作 0 条', source)
        self.assertIn('时间归属无法判断的动作：0 条', source)
        self.assertIn('copiedDiagnostic.Contains("只读范围检查通过")', source)

    def test_token_precedes_project_loading_selection_and_manual_ids(self):
        app = self.build_form()
        form = app.settings_tab
        token_row = app.secret_entries["teambition_mcp_token"].form_row(form)
        project_controls = [app.project_combo, app.load_projects_button]
        project_controls += [widget for widget in form.descendants()
                             if any(widget.options.get("textvariable") is app.values[key]
                                    for key in ("tenant_id", "project_id"))]
        self.assertEqual(len(project_controls), 4)
        for control in project_controls:
            self.assertLess(token_row, control.form_row(form))
        self.assertEqual(sum(widget.options.get("textvariable") is app.values["teambition_mcp_token"]
                             for widget in form.descendants()), 1)

    def test_save_and_authorize_is_available_before_project_picker(self):
        app = self.build_form()
        form = app.settings_tab
        save_buttons = [widget for widget in form.descendants()
                        if widget.options.get("command") == app._save]
        self.assertTrue(any(widget.form_row(form) < app.project_combo.form_row(form)
                            for widget in save_buttons),
                        "A fresh user must be able to save and authorize before choosing a project")

    def test_worker_saves_and_authorizes_token_before_any_project_is_chosen(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_smoke_service(Path(directory))
            service.settings_store.save(deepcopy(DEFAULT_SETTINGS))
            service.secret_store.save({})
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")), \
                    patch.object(service, "_collect_source", side_effect=AssertionError("No project yet")):
                for allow in (False, True):
                    result = service.dispatch({"op": "save", "payload": {
                        "settings": service.settings_store.load(),
                        "secret_values": {"teambition_mcp_token": "fictional-first-use-token"},
                        "authorize_teambition_mcp": allow,
                    }})
                    self.assertEqual(result["secret_presence"]["teambition_mcp_authorized"], allow)
                    self.assertEqual(result["settings"]["project_id"], "")
                    self.assertEqual(result["settings"]["tenant_id"], "")
                    self.assertFalse(result["settings"]["auto_send_enabled"])
                    if not allow:
                        with self.assertRaisesRegex(WorkerApiError, "授权"):
                            service.dispatch({"op": "list_projects"})
                listed = service.dispatch({"op": "list_projects"})
                selected = service.dispatch({"op": "select_project", "payload": {
                    "project_id": listed["projects"][0]["id"],
                }})
                self.assertEqual(selected["project_id"], "demo-project")
                self.assertEqual(service.settings_store.load()["project_id"], "")

    def test_tk_can_save_token_without_project_and_decline_or_grant_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_smoke_service(Path(directory))
            service.settings_store.save(deepcopy(DEFAULT_SETTINGS))
            service.secret_store.save({})
            app = self.build_form()
            app.settings, app.saved_secrets = service.settings_store.load(), {}
            app.settings_store, app.secret_store = service.settings_store, service.secret_store
            app.owner_map, app.status_map, app.resolved_status_ids = {}, {}, set()
            app.view_filters, app._setup_probe = {}, None
            app.scope_confirm_var, app.root = Value(False), None
            app._load_model_cache, app._set_status = lambda: None, lambda _: None
            for key in app.values:
                if key in DEFAULT_SETTINGS:
                    app.values[key].set(DEFAULT_SETTINGS[key])
            for key in app.secret_entries:
                setattr(app, "_secret_state_" + key, SimpleNamespace(configure=lambda **_: None))
            app.values["teambition_mcp_token"].set("fictional-first-use-token")
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")), \
                    patch("desktop_app.messagebox.showinfo"), patch("desktop_app.messagebox.showerror") as error:
                for allow in (False, True):
                    with patch("desktop_app.messagebox.askyesno", return_value=allow):
                        app._save()
                    self.assertFalse(error.called)
                    self.assertEqual(mcp_connection_authorized(service.secret_store.load()), allow)
                    self.assertEqual(service.settings_store.load()["project_id"], "")
                    self.assertEqual(app.values["teambition_mcp_token"].get(), "")


if __name__ == "__main__":
    unittest.main()
