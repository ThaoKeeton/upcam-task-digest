import tempfile
from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()
