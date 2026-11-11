"""Legacy standalone diagnostics accept raw MCP semantics, without network."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from desktop_app import DigestDesktopApp
from upcam_app.smoke_data import SyntheticClient, make_smoke_service
from test_legacy_mention_privacy import Value


class McpTkDiagnosticTests(unittest.TestCase):
    def diagnose(self, change_source=lambda _: None, change_settings=lambda _: None,
                 export_failure=False):
        results = []
        with tempfile.TemporaryDirectory() as directory:
            service = make_smoke_service(Path(directory))
            settings = service.settings_store.load()
            settings["api_contract_verified"] = True
            settings["view"].update(verified=True, evidence="Synthetic scope verification")
            change_settings(settings)

            class Client(SyntheticClient):
                def collect(self, *args, **kwargs):
                    collection = super().collect(*args, **kwargs)
                    change_source(collection)
                    return collection

            app = SimpleNamespace(
                settings=settings, saved_secrets=service.secret_store.load(),
                manual_start=Value("2026-01-02 00:00"), manual_end=Value("2026-01-03 00:00"),
                _job_lock=threading.Lock(), _set_status=lambda _: None,
                source_diagnostic_button=SimpleNamespace(configure=lambda **_: None),
                diagnostic_export_button=SimpleNamespace(configure=lambda **_: None),
                root=SimpleNamespace(after=lambda _, action: action()),
                _diagnostic_finished=lambda value: results.append(deepcopy(value)),
            )
            with patch("desktop_app._new_teambition_client", return_value=Client(settings["tenant_id"])), \
                    patch("desktop_app.threading.Thread", side_effect=lambda *, target, **_: SimpleNamespace(start=target)), \
                    patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
                if export_failure:
                    with patch("desktop_app.safe_structure_export", side_effect=ValueError("synthetic export failure")):
                        DigestDesktopApp._diagnose_manual(app)
                else:
                    DigestDesktopApp._diagnose_manual(app)
            self.assertFalse(app._job_lock.locked())
        self.assertEqual(len(results), 1)
        return results[0]

    def test_unknown_rich_activity_is_ready_for_model_without_legacy_semantics(self):
        def unknown(collection):
            event = collection["records"][0]["activities"][0]
            event["action"] = "fictional.future.rich.action"
            event["content"] = {"rich": "<p>虚构的新格式</p>", "future": [1, 2]}
        result = self.diagnose(unknown)
        self.assertTrue(result["ok"], result["message"])
        self.assertIn("未调用 OpenAI", result["message"])

    def test_auxiliary_export_failure_does_not_block_valid_raw_source(self):
        result = self.diagnose(export_failure=True)
        self.assertTrue(result["ok"], result["message"])
        self.assertIsNone(result["safe_shape"])

    def test_invalid_time_or_incomplete_collection_still_blocks(self):
        for mutate in (lambda source: source["records"][0]["activities"][0].pop("createTime"),
                       lambda source: source.update(pagination_complete=False)):
            with self.subTest(mutate=mutate):
                result = self.diagnose(mutate)
                self.assertFalse(result["ok"])

    def test_known_credentials_anywhere_in_collection_or_config_block_without_echo(self):
        secret = "synthetic-mcp-token"
        mutations = [
            {"change_source": lambda source: source.update(unused={"nested": secret})},
            {"change_settings": lambda settings: settings["owner_names"].update(unused=secret)},
        ]
        for kwargs in mutations:
            with self.subTest(location=next(iter(kwargs))):
                result = self.diagnose(**kwargs)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["safe_shape"])
                self.assertNotIn(secret, str(result))


if __name__ == "__main__":
    unittest.main()
