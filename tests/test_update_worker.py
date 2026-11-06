"""Worker update integration uses a fixed synthetic release and no services."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upcam_app.smoke_data import make_smoke_service
from upcam_app import APP_VERSION
from upcam_app.worker_api import WorkerApiError


class UpdateWorkerTests(unittest.TestCase):
    def test_optional_update_access_does_not_use_other_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = make_smoke_service(Path(directory))
            before = worker.settings_store.load()
            result = worker.dispatch({"op": "check_updates"})
            self.assertEqual(result["status"], "access_required")
            self.assertEqual(worker.settings_store.load(), before)
            self.assertEqual(worker.history.list_runs(), [])

    def test_verified_synthetic_update_stages_without_executing_or_altering_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = make_smoke_service(root)
            worker.dispatch({"op": "save_preferences", "payload": {
                "preferences": {}, "github_token": "github_pat_ci_synthetic-not-real"}})
            checked = worker.dispatch({"op": "check_updates"})
            self.assertEqual(checked["status"], "available")
            self.assertGreater(tuple(map(int, checked["version"].split("."))),
                               tuple(map(int, APP_VERSION.split("."))))
            downloaded = worker.dispatch({"op": "download_update"})
            self.assertEqual(downloaded["status"], "downloaded")
            previous = root / "previous" / "UPCAMTaskDigest.exe"
            previous.parent.mkdir()
            previous.write_bytes(b"CI old app")
            with patch("upcam_app.worker_api._current_application_executable", return_value=str(previous)):
                prepared = worker.dispatch({"op": "prepare_update"})
            self.assertEqual(prepared["status"], "ready")
            self.assertTrue(Path(prepared["executable"]).is_file())
            self.assertEqual(previous.read_bytes(), b"CI old app")
            self.assertNotIn("github_pat_ci_synthetic-not-real", json.dumps(prepared))
            self.assertEqual(worker.history.list_runs(), [])

    def test_updates_do_not_run_during_a_collection_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = make_smoke_service(Path(directory))
            worker._job_lock.acquire()
            try:
                for operation in ("check_updates", "download_update", "prepare_update"):
                    with self.subTest(operation=operation), self.assertRaisesRegex(WorkerApiError, "采集或发送"):
                        worker.dispatch({"op": operation})
            finally:
                worker._job_lock.release()

    def test_update_commands_reject_arbitrary_sources_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = make_smoke_service(Path(directory))
            for operation in ("check_updates", "download_update", "prepare_update", "finish_update"):
                with self.subTest(operation=operation), self.assertRaises(WorkerApiError):
                    worker.dispatch({"op": operation, "payload": {"url": "https://example.invalid/untrusted.zip"}})
