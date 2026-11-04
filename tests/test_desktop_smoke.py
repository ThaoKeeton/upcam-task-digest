import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from desktop_app import DigestDesktopApp, verify_desktop_smoke


_EXPECTED_TABS = (
    "首次配置 / 自动运行",
    "项目映射与范围",
    "自选时间 / 运行",
    "运行记录",
)


class _FakeNotebook:
    def __init__(self, labels):
        self.labels = labels

    def index(self, _index):
        return len(self.labels)

    def tab(self, index, option):
        if option != "text":
            raise AssertionError(option)
        return self.labels[index]


class _FakeApp:
    def __init__(self, labels):
        self.tabs = _FakeNotebook(labels)
        self.project_probe_button = object()
        self.owner_tree = object()
        self.status_tree = object()
        self.startup_check = object()
        self.mapping_canvas = object()
        self.owner_bulk_text = object()
        self.owner_import_button = object()
        self.source_diagnostic_button = object()


class _FakeSecretStore:
    def __init__(self):
        self.values = None

    def save(self, values):
        self.values = dict(values)


class DesktopSmokeTests(unittest.TestCase):
    def test_first_run_prepares_a_validated_local_delivery_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DigestDesktopApp.__new__(DigestDesktopApp)
            app.data_dir = Path(temporary)
            app.saved_secrets = {}
            app.secret_store = _FakeSecretStore()

            ready, _message = app._prepare_ledger()

            self.assertTrue(ready)
            self.assertIn("delivery_ledger_id", app.secret_store.values)
            self.assertTrue((Path(temporary) / "delivery_ledger.sqlite3").is_file())

    def test_smoke_check_accepts_expected_tabs_and_bundled_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "daily_digest.json"
            template.write_text("{}", encoding="utf-8")
            self.assertTrue(verify_desktop_smoke(_FakeApp(_EXPECTED_TABS), template))

    def test_smoke_check_rejects_a_missing_tab(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "daily_digest.json"
            template.write_text("{}", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_desktop_smoke(_FakeApp(_EXPECTED_TABS[:-1]), template)

    def test_smoke_check_rejects_a_missing_bundled_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                verify_desktop_smoke(_FakeApp(_EXPECTED_TABS), Path(temporary) / "missing.json")

    def test_smoke_check_rejects_a_missing_member_import_button(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "daily_digest.json"
            template.write_text("{}", encoding="utf-8")
            app = _FakeApp(_EXPECTED_TABS)
            del app.owner_import_button

            with self.assertRaises(RuntimeError):
                verify_desktop_smoke(app, template)

    def test_smoke_check_rejects_a_missing_read_only_diagnostic_button(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "daily_digest.json"
            template.write_text("{}", encoding="utf-8")
            app = _FakeApp(_EXPECTED_TABS)
            del app.source_diagnostic_button

            with self.assertRaises(RuntimeError):
                verify_desktop_smoke(app, template)


class _FakeBulkText:
    def __init__(self, value):
        self.value = value

    def get(self, _start, _end):
        return self.value

    def delete(self, _start, _end):
        self.value = ""


class DesktopMemberImportTests(unittest.TestCase):
    def app_for_import(self, source, existing=None):
        app = DigestDesktopApp.__new__(DigestDesktopApp)
        app.owner_bulk_text = _FakeBulkText(source)
        app.owner_map = dict(existing or {})
        app.owner_candidate_ids = set(app.owner_map)
        app.root = object()
        app.rendered = 0
        app.status = ""
        app._render_setup_tables = lambda: setattr(app, "rendered", app.rendered + 1)
        app._set_status = lambda value: setattr(app, "status", value)
        return app

    def test_json_paste_uses_the_real_import_handler_and_clears_after_success(self):
        app = self.app_for_import('{"demo-person":"虚构成员","demo-person-2":"虚构乙"}',
                                  {"existing-person": "已有虚构成员"})

        app._import_owner_mappings()

        self.assertEqual(app.owner_map, {
            "existing-person": "已有虚构成员",
            "demo-person": "虚构成员",
            "demo-person-2": "虚构乙",
        })
        self.assertEqual(app.owner_candidate_ids, set(app.owner_map))
        self.assertEqual(app.owner_bulk_text.value, "")
        self.assertEqual(app.rendered, 1)
        self.assertIn("已导入 2 条", app.status)

    def test_conflicting_json_paste_keeps_all_existing_mappings_and_explains_formats(self):
        original = {"demo-person": "原有虚构姓名"}
        app = self.app_for_import('{"demo-person":"不同姓名","other-person":"虚构乙"}', original)

        with patch("desktop_app.messagebox.showerror") as showerror:
            app._import_owner_mappings()

        self.assertEqual(app.owner_map, original)
        self.assertEqual(app.owner_candidate_ids, {"demo-person"})
        self.assertEqual(app.owner_bulk_text.value, '{"demo-person":"不同姓名","other-person":"虚构乙"}')
        self.assertEqual(app.rendered, 0)
        self.assertIn("JSON", showerror.call_args.args[1])
        self.assertIn("ID<TAB>", showerror.call_args.args[1])
        self.assertIn("未导入任何", showerror.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
