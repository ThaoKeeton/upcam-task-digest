"""Synthetic opt-in detailed export: no production network, credentials or delivery."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app.smoke_data import make_smoke_service, SyntheticClient


class DetailedWorkerTests(unittest.TestCase):
    def test_detailed_export_uses_full_project_audit_instead_of_updated_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            modes = []
            previous = service.client_factory

            class RecordingMode(previous):
                def collect(self, *args, **kwargs):
                    modes.append(kwargs.get("candidate_mode", "updated_since_start"))
                    return super().collect(*args, **kwargs)

            service.client_factory = RecordingMode
            target = Path(directory) / "full-audit.json"
            result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                **window, "path": str(target), "include_project_content": True}})
            self.assertEqual(modes, ["full_project"])
            self.assertTrue(result["detailed_export"]["saved"])
            snapshot = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["scope"]["selection"]["candidate_scope"], "current-visible-project-tasks")

    def test_parent_replaced_at_writer_entry_is_not_followed(self):
        from upcam_app.diagnostic_files import write_private_diagnostic
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            chosen = Path(directory) / "chosen"
            chosen.mkdir()
            original = Path(directory) / "original"
            target = chosen / "snapshot.json"
            before = (service.data_dir / "settings.json").read_bytes()

            def replace_parent(*args, **kwargs):
                chosen.rename(original)
                chosen.mkdir()
                return write_private_diagnostic(*args, **kwargs)

            with patch("upcam_app.worker_api.write_private_diagnostic", side_effect=replace_parent):
                result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                    **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertFalse(target.exists())
            self.assertFalse((original / "snapshot.json").exists())
            self.assertTrue(result["export_available"])
            self.assertEqual((service.data_dir / "settings.json").read_bytes(), before)

    def test_windows_device_aliases_are_rejected_before_path_resolution(self):
        from upcam_app.worker_api import _is_windows_device_path
        for value in (r"\\?\C:\Synthetic\snapshot.json", r"\\.\C:\Synthetic\snapshot.json",
                      "//?/C:/Synthetic/snapshot.json", "//./C:/Synthetic/snapshot.json",
                      r"\??\C:\Synthetic\snapshot.json"):
            with self.subTest(value=value):
                self.assertTrue(_is_windows_device_path(value))
        for value in (r"C:\Synthetic\snapshot.json", "/tmp/synthetic.json", r"\\server\share\snapshot.json"):
            self.assertFalse(_is_windows_device_path(value))

    def test_credentials_changed_while_building_prevent_file_write(self):
        from upcam_app.detailed_diagnostic import build_detailed_snapshot
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            target = Path(directory) / "detailed.json"

            def change_credentials(*args, **kwargs):
                snapshot = build_detailed_snapshot(*args, **kwargs)
                credentials = service.secret_store.load()
                credentials["openai_api_key"] = "synthetic-changed-while-preparing"
                service.secret_store.save(credentials)
                return snapshot

            with patch("upcam_app.detailed_diagnostic.build_detailed_snapshot", side_effect=change_credentials):
                result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                    **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertFalse(target.exists())
            self.assertNotIn("synthetic-changed-while-preparing", json.dumps(result))

    def test_parent_directory_changed_during_collection_cannot_overwrite_app_state(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            chosen = Path(directory) / "chosen"
            chosen.mkdir()
            target = chosen / "snapshot.json"
            before = (service.data_dir / "settings.json").read_bytes()
            previous = service.client_factory
            probe = Path(directory) / "symlink-probe"
            try:
                probe.symlink_to(service.data_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlink creation is unavailable on this runner")
            else:
                probe.unlink()

            class RedirectedDirectory(previous):
                def collect(self, *args, **kwargs):
                    result = super().collect(*args, **kwargs)
                    chosen.rmdir()
                    chosen.symlink_to(service.data_dir, target_is_directory=True)
                    return result

            service.client_factory = RedirectedDirectory
            result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertFalse((service.data_dir / "snapshot.json").exists())
            self.assertEqual((service.data_dir / "settings.json").read_bytes(), before)

    def test_source_diagnostic_text_cannot_echo_configured_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            previous = service.client_factory

            class CredentialAction(previous):
                def collect(self, *args, **kwargs):
                    result = super().collect(*args, **kwargs)
                    result["records"][0]["activities"][-1]["action"] = "synthetic-mcp-token"
                    return result

            service.client_factory = CredentialAction
            result = service.dispatch({"op": "diagnose", "payload": window})
            self.assertFalse(result["ok"])
            self.assertNotIn("synthetic-mcp-token", json.dumps(result, ensure_ascii=False))
            self.assertIn("凭据", result["message"])

    def fixture(self, directory):
        service = make_smoke_service(Path(directory) / "data")
        service.dispatch({"op": "initialize"})
        end = datetime.now().replace(second=0, microsecond=0)
        window = {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                  "end": end.strftime("%Y-%m-%d %H:%M")}
        service.dispatch({"op": "save", "payload": {
            "settings": service.settings_store.load(), "verification_window": window}})
        calls = []

        class CountingClient(SyntheticClient):
            def collect(self, *args, **kwargs):
                calls.append(True)
                result = super().collect(*args, **kwargs)
                task = result["records"][0]
                task["activities"].append({
                    "id": "synthetic-unknown", "action": "future.synthetic.move",
                    "boundToObjectId": task["task"]["id"], "boundToObjectType": "task",
                    "createTime": task["task"]["updated"],
                    "content": json.dumps({"from": {"stage": {"_id": "a" * 24, "name": "虚构旧阶段"}},
                                           "to": {"stage": {"_id": "b" * 24, "name": "虚构新阶段"}},
                                           "secretLevel": "synthetic-business-level",
                                           "authorization": "Bearer synthetic-secret-cannot-export"}),
                })
                return result

        service.client_factory = CountingClient
        service.workflow_factory = lambda *_args, **_kwargs: self.fail("Detailed export cannot construct a workflow")
        return service, window, calls

    def test_detailed_export_requires_explicit_project_content_choice_before_collection(self):
        for consent in (None, False, 1, "true"):
            with self.subTest(consent_type=type(consent).__name__), tempfile.TemporaryDirectory() as directory:
                service, window, calls = self.fixture(directory)
                payload = {**window, "path": str(Path(directory) / "detailed.json")}
                if consent is not None:
                    payload["include_project_content"] = consent
                with self.assertRaisesRegex(ValueError, "项目内容"):
                    service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": payload})
                self.assertEqual(calls, [])

    def test_one_collection_exports_private_evidence_and_never_returns_body_in_protocol(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "urllib.request.OpenerDirector.open", side_effect=AssertionError("No network")):
            service, window, calls = self.fixture(directory)
            target = Path(directory) / "详细诊断.json"
            result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                **window, "path": str(target), "include_project_content": True}})
            self.assertEqual(len(calls), 1)
            self.assertTrue(result["ok"])  # Unknown activity semantics belong to the model.
            self.assertTrue(result["detailed_export"]["saved"])
            self.assertTrue(result["export_available"])
            raw = target.read_text(encoding="utf-8")
            snapshot = json.loads(raw)
            self.assertEqual(snapshot["format"], "upcam-teambition-detailed-diagnostic-v1")
            self.assertTrue(snapshot["contains_project_content"])
            self.assertFalse(snapshot["sendable"])
            self.assertEqual(snapshot["diagnostic"]["id"], result["diagnostic"]["id"])
            self.assertIn("虚构旧阶段", raw)
            self.assertIn("future.synthetic.move", raw)
            self.assertIn("synthetic-business-level", raw)
            for private in ("synthetic-mcp-token", "synthetic-app-id", "synthetic-key-cannot-call-openai",
                            "access_token=synthetic-only", "synthetic-secret-cannot-export"):
                self.assertNotIn(private, raw)
            self.assertNotIn("虚构旧阶段", json.dumps(result, ensure_ascii=False))
            self.assertEqual(service.dispatch({"op": "history"})["runs"], [])
            self.assertFalse(any("detailed" in name for name in vars(service)
                                 if isinstance(getattr(service, name), dict)))

    def test_regular_diagnosis_does_not_invoke_detailed_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, calls = self.fixture(directory)
            with patch("upcam_app.detailed_diagnostic.build_detailed_snapshot",
                       side_effect=AssertionError("Opt-in is required")):
                result = service.dispatch({"op": "diagnose", "payload": window})
            self.assertEqual(len(calls), 1)
            self.assertNotIn("detailed_export", result)
            self.assertIn("diagnostic_summary", result)
            self.assertEqual(result["diagnostic_summary"]["unknown_events_in_report_window"], 1)
            self.assertEqual(result["diagnostic_summary"]["unknown_events_with_unknown_time"], 0)

    def test_invalid_or_protected_output_paths_fail_before_collection_and_preserve_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, calls = self.fixture(directory)
            before = (service.data_dir / "settings.json").read_bytes()
            for target in ("relative.json", str(service.data_dir / "settings.json"),
                           str(service.data_dir / "credentials.dpapi"), str(service.data_dir / "history.sqlite3")):
                with self.subTest(target_kind=Path(target).name), self.assertRaises(ValueError):
                    service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                        **window, "path": target, "include_project_content": True}})
            if os.name == "nt":
                for prefix in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
                    for folder in (service.data_dir, Path(__file__).resolve().parents[1], Path(directory)):
                        with self.assertRaises(ValueError):
                            service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                                **window, "path": prefix + str(folder / "snapshot.json"),
                                "include_project_content": True}})
            self.assertEqual(calls, [])
            self.assertEqual((service.data_dir / "settings.json").read_bytes(), before)

    def test_collection_failure_does_not_write_or_claim_a_detailed_file(self):
        from teambition_readonly import CollectionError
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            target = Path(directory) / "detailed.json"
            with patch.object(service, "_client", side_effect=CollectionError("Synthetic incomplete collection")):
                result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                    **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertFalse(target.exists())
            self.assertFalse(result["export_available"])

    def test_write_failure_is_explicit_and_keeps_safe_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            target = Path(directory) / "detailed.json"
            with patch("upcam_app.worker_api.write_private_diagnostic", side_effect=OSError("PRIVATE_ERROR_BODY")):
                result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                    **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertTrue(result["export_available"])
            self.assertFalse(target.exists())
            self.assertNotIn("PRIVATE_ERROR_BODY", json.dumps(result))
            self.assertIn("文件可能已生成", result["detailed_export"]["message"])

    def test_credentials_changed_during_collection_prevent_unsafe_export(self):
        with tempfile.TemporaryDirectory() as directory:
            service, window, _calls = self.fixture(directory)
            previous = service.client_factory

            class ChangedCredentials(previous):
                def collect(self, *args, **kwargs):
                    result = super().collect(*args, **kwargs)
                    credentials = service.secret_store.load()
                    credentials["openai_api_key"] = "another-synthetic-private-key"
                    service.secret_store.save(credentials)
                    return result

            service.client_factory = ChangedCredentials
            target = Path(directory) / "detailed.json"
            result = service.dispatch({"op": "export_detailed_diagnostic_to_file", "payload": {
                **window, "path": str(target), "include_project_content": True}})
            self.assertFalse(result["detailed_export"]["saved"])
            self.assertFalse(target.exists())
            self.assertNotIn("another-synthetic-private-key", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
