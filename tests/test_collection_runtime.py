"""Synthetic desktop collection routing and progress-clear safety checks."""

from datetime import datetime, timedelta
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from report_contract import SHANGHAI, updated_selection
from teambition_readonly import CollectionError
from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.smoke_data import make_smoke_service, SyntheticClient
from upcam_app.worker_api import WorkerApiError, WorkerService
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked


SUCCESS = "未完成的采集进度已清除；配置、凭据、模型缓存、运行记录和发送账本保持不变。"
BUSY = "采集正在进行，暂不能清除进度；请等待完成或退出应用后重试。"
FAILURE = "本地采集进度无法安全清除；请退出应用后重试。"
EXPLICIT_CREDENTIALS = {
    "teambition_app_id": "fictional-desktop-app",
    "teambition_app_secret": "fictional-desktop-secret",
}


def synthetic_checkpoint(path):
    def transform(data):
        return bytes(value ^ 0x65 for value in data)
    return DpapiCollectionCheckpoint(path, protect=transform, unprotect=transform)


class ClosingCheckpoint:
    def __init__(self, path):
        self.path = Path(path)
        self.closed = False

    def clear(self):
        pass

    def close(self):
        self.closed = True


class CollectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        forbidden_network = patch("urllib.request.OpenerDirector.open",
                                  side_effect=AssertionError("Synthetic test forbids network"))
        forbidden_network.start()
        self.addCleanup(forbidden_network.stop)

    def runtime(self):
        self.assertIsNotNone(importlib.util.find_spec("upcam_app.collection_runtime"),
                             "The common desktop collection builder is missing")
        return importlib.import_module("upcam_app.collection_runtime")

    def service(self, checkpoint_factory=synthetic_checkpoint):
        synthetic = make_smoke_service(self.directory)
        return WorkerService(
            self.directory, settings_store=synthetic.settings_store,
            secret_store=synthetic.secret_store, history=synthetic.history,
            model_catalog=synthetic.model_catalog, client_factory=synthetic.client_factory,
            workflow_factory=synthetic.workflow_factory,
            startup_configurer=synthetic.startup_configurer,
            update_manager=synthetic.update_manager, checkpoint_factory=checkpoint_factory,
        )

    def window(self):
        end = datetime.now().replace(second=0, microsecond=0)
        return {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M")}

    def start_checkpoint(self):
        checkpoint = synthetic_checkpoint(self.directory / "collection-progress.sqlite3")
        context = {
            "tenant_id": "demo-org", "project_id": "demo-project",
            "include_archived": False, "selection": updated_selection("2026-01-02"),
            "policy": {"synthetic": True},
            "credential_identity": hashlib.sha256(b"fictional-credential-pair").hexdigest(),
        }
        checkpoint.start(context, now=100)
        candidate = {"id": "fictional-task", "projectId": "demo-project", "isArchived": False,
                     "updated": "2026-01-02T01:00:00Z"}
        checkpoint.save_task("fictional-task", candidate, {"task": candidate, "activities": []})
        return checkpoint

    def test_default_client_gets_batch_limits_checkpoint_and_frozen_explicit_credentials(self):
        runtime = self.runtime()
        credentials = dict(EXPLICIT_CREDENTIALS)
        checkpoints = []

        def checkpoint_factory(path):
            checkpoint = ClosingCheckpoint(path)
            checkpoints.append(checkpoint)
            return checkpoint

        class RecordingClient:
            def __init__(self, tenant_id, **kwargs):
                self.tenant_id = tenant_id
                self.kwargs = kwargs

        settings = dict(DEFAULT_SETTINGS, tenant_id="demo-org", max_tasks_per_run=123)
        with patch.object(runtime, "Client", RecordingClient):
            client = runtime.make_collection_client(
                settings, credentials, self.directory, client_factory=RecordingClient,
                checkpoint_factory=checkpoint_factory)
        credentials["teambition_app_secret"] = "fictional-rotated-secret"
        self.assertEqual(client.tenant_id, "demo-org")
        self.assertEqual(client.kwargs["credentials"](),
                         ("fictional-desktop-app", "fictional-desktop-secret"))
        self.assertTrue(client.kwargs["batch_mode"])
        self.assertEqual(client.kwargs["max_tasks"], 123)
        self.assertEqual(client.kwargs["max_project_tasks"], 10000)
        self.assertIs(client.kwargs["checkpoint"], checkpoints[0])
        self.assertEqual(checkpoints[0].path, self.directory / "collection-progress.sqlite3")

    def test_injected_client_keeps_narrow_signature_without_opening_checkpoint(self):
        runtime = self.runtime()

        class NarrowClient:
            def __init__(self, tenant_id, *, credentials):
                self.tenant_id, self.credentials = tenant_id, credentials

        def forbidden_checkpoint(_path):
            self.fail("Injected clients must not create a DPAPI checkpoint")

        settings = dict(DEFAULT_SETTINGS, tenant_id="demo-org")
        client = runtime.make_collection_client(
            settings, EXPLICIT_CREDENTIALS, self.directory,
            client_factory=NarrowClient, checkpoint_factory=forbidden_checkpoint)
        self.assertEqual(client.credentials(),
                         ("fictional-desktop-app", "fictional-desktop-secret"))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_constructor_failure_closes_checkpoint_and_hides_exception_content(self):
        runtime = self.runtime()
        checkpoint = ClosingCheckpoint(self.directory / "collection-progress.sqlite3")

        class BrokenClient:
            def __init__(self, *_args, **_kwargs):
                raise OSError("fictional-private-path-and-secret")

        settings = dict(DEFAULT_SETTINGS, tenant_id="demo-org")
        with patch.object(runtime, "Client", BrokenClient), self.assertRaises(CollectionError) as raised:
            runtime.make_collection_client(settings, EXPLICIT_CREDENTIALS, self.directory,
                                           client_factory=BrokenClient,
                                           checkpoint_factory=lambda _path: checkpoint)
        self.assertTrue(checkpoint.closed)
        self.assertNotIn("fictional-private", str(raised.exception))

    def test_missing_explicit_credentials_cannot_fall_back_to_environment(self):
        runtime = self.runtime()
        with self.assertRaises(CollectionError):
            runtime.make_collection_client(dict(DEFAULT_SETTINGS, tenant_id="demo-org"), {},
                                           self.directory, client_factory=SyntheticClient)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_real_default_client_is_lazy_and_uses_the_encrypted_store(self):
        runtime = self.runtime()
        client = runtime.make_collection_client(
            dict(DEFAULT_SETTINGS, tenant_id="demo-org"), EXPLICIT_CREDENTIALS, self.directory,
            checkpoint_factory=synthetic_checkpoint)
        try:
            self.assertTrue(client.batch_mode)
            self.assertIsInstance(client.checkpoint, DpapiCollectionCheckpoint)
            self.assertEqual(client.checkpoint.path, self.directory / "collection-progress.sqlite3")
            self.assertEqual(list(self.directory.iterdir()), [])
        finally:
            client.checkpoint.close()

    def test_real_default_clients_resume_complete_records_using_the_same_builder_context(self):
        runtime = self.runtime()
        tasks = [{"id": name, "projectId": "demo-project", "isArchived": False,
                  "updated": "2026-01-02T01:00:00Z"}
                 for name in ("fictional-task-a", "fictional-task-b")]
        details = []
        interrupted = []

        class Response(io.BytesIO):
            status = 200

        def transport(request, timeout):
            url = urlsplit(request.full_url)
            if url.path == "/api/v3/project/demo-project/task/query":
                result = {"code": 200, "result": tasks, "nextPageToken": ""}
            elif url.path == "/api/v3/task/query":
                task_id = parse_qs(url.query)["taskId"][0]
                details.append(task_id)
                if task_id == "fictional-task-b" and not interrupted:
                    interrupted.append(True)
                    raise CollectionError("Synthetic interrupted collection")
                result = {"code": 200, "result": next(task for task in tasks if task["id"] == task_id)}
            else:
                result = {"code": 200, "result": [], "nextPageToken": ""}
            return Response(json.dumps(result).encode("utf-8"))

        settings = dict(DEFAULT_SETTINGS, tenant_id="demo-org", project_id="demo-project")
        with patch("teambition_readonly.build_opener", return_value=SimpleNamespace(open=transport)):
            first = runtime.make_collection_client(settings, EXPLICIT_CREDENTIALS, self.directory,
                                                   checkpoint_factory=synthetic_checkpoint)
            with self.assertRaises(CollectionError):
                first.collect("demo-project", day="2026-01-02")
            self.assertEqual(details, ["fictional-task-a", "fictional-task-b"])
            resumed = runtime.make_collection_client(settings, EXPLICIT_CREDENTIALS, self.directory,
                                                     checkpoint_factory=synthetic_checkpoint)
            source = resumed.collect("demo-project", day="2026-01-02")
        self.assertEqual(details, ["fictional-task-a", "fictional-task-b", "fictional-task-b"])
        self.assertTrue(source["pagination_complete"])
        self.assertEqual([record["task"]["id"] for record in source["records"]],
                         ["fictional-task-a", "fictional-task-b"])
        self.assertEqual(source["collection_stats"]["resumed_tasks"], 1)

    def test_four_desktop_entry_points_use_common_builder_with_same_context(self):
        runtime = self.runtime()
        service = self.service()
        initial = service.dispatch({"op": "initialize"})
        window = self.window()
        with patch("upcam_app.worker_api.make_collection_client", wraps=runtime.make_collection_client) as worker_builder, patch(
                "upcam_app.workflow.make_collection_client", wraps=runtime.make_collection_client) as workflow_builder:
            self.assertEqual(service.dispatch({"op": "probe_project", "payload": window})["task_count"], 1)
            service.dispatch({"op": "save", "payload": {
                "settings": initial["settings"], "verification_window": window}})
            self.assertTrue(service.dispatch({"op": "diagnose", "payload": window})["ok"])
            self.assertEqual(service.dispatch({"op": "test_teambition"})["records"], 1)
            service.dispatch({"op": "refresh_models"})
            self.assertEqual(service.dispatch({"op": "run_manual", "payload": window})["state"], "sent")
        self.assertEqual(worker_builder.call_count, 3)
        self.assertEqual(workflow_builder.call_count, 1)
        for call in (*worker_builder.call_args_list, *workflow_builder.call_args_list):
            self.assertEqual(call.args[0]["project_id"], "demo-project")
            self.assertEqual(call.args[1]["teambition_app_id"], "synthetic-app-id")
            self.assertEqual(call.args[2], self.directory)
            self.assertIs(call.kwargs["checkpoint_factory"], synthetic_checkpoint)

    def test_save_scope_probe_uses_builder_and_holds_the_clear_lock(self):
        service = self.service()
        initial = service.dispatch({"op": "initialize"})
        attempts = []

        class CollectingClient(SyntheticClient):
            def collect(inner, *args, **kwargs):
                with self.assertRaisesRegex(WorkerApiError, BUSY):
                    service.dispatch({"op": "clear_collection_progress"})
                attempts.append(True)
                return super().collect(*args, **kwargs)

        service.client_factory = CollectingClient
        result = service.dispatch({"op": "save", "payload": {
            "settings": initial["settings"], "verification_window": self.window()}})
        self.assertTrue(result["settings"]["view"]["verified"])
        self.assertEqual(attempts, [True])
        self.assertFalse(service._job_lock.locked())

    def test_every_worker_collection_holds_clear_lock_until_source_returns(self):
        service = self.service()
        service.dispatch({"op": "initialize"})
        attempts = []

        class CollectingClient(SyntheticClient):
            def collect(inner, *args, **kwargs):
                with self.assertRaisesRegex(WorkerApiError, BUSY):
                    service.dispatch({"op": "clear_collection_progress"})
                attempts.append(True)
                return super().collect(*args, **kwargs)

        service.client_factory = CollectingClient
        for operation, payload in (("probe_project", self.window()), ("diagnose", self.window()),
                                   ("test_teambition", {})):
            service.dispatch({"op": operation, "payload": payload})
        self.assertEqual(attempts, [True, True, True])
        self.assertFalse(service._job_lock.locked())

    def test_clear_removes_only_progress_without_loading_sensitive_stores_or_services(self):
        service = self.service()
        service.dispatch({"op": "initialize"})
        checkpoint = self.start_checkpoint()
        checkpoint.close()
        (self.directory / "openai_models.json").write_text("fictional-model-cache", encoding="utf-8")
        protected = {path.name: path.read_bytes() for path in self.directory.iterdir()
                     if path.name in {"settings.json", "history.sqlite3", "delivery_ledger.sqlite3", "openai_models.json"}}
        secrets = service.secret_store.load()
        with patch.object(service.settings_store, "load", side_effect=AssertionError("Settings accessed")), patch.object(
                service.secret_store, "load", side_effect=AssertionError("Credentials accessed")), patch.object(
                service.history, "list_runs", side_effect=AssertionError("History accessed")), patch.object(
                service.model_catalog, "refresh", side_effect=AssertionError("Model service called")), patch.object(
                service, "workflow_factory", side_effect=AssertionError("Workflow started")):
            result = service.dispatch({"op": "clear_collection_progress"})
        self.assertEqual(result, {"status": SUCCESS})
        self.assertEqual(service.secret_store.load(), secrets)
        for name, content in protected.items():
            self.assertEqual((self.directory / name).read_bytes(), content, name)
        reopened = synthetic_checkpoint(self.directory / "collection-progress.sqlite3")
        try:
            info = reopened.start({"tenant_id": "demo-org", "project_id": "demo-project",
                                   "include_archived": False, "selection": updated_selection("2026-01-02"),
                                   "policy": {"synthetic": True},
                                   "credential_identity": hashlib.sha256(b"fictional-credential-pair").hexdigest()}, now=100)
            self.assertFalse(info["resumed"])
            self.assertIsNone(reopened.load_task("fictional-task"))
        finally:
            reopened.close()

    def test_clear_rejects_payload_and_busy_worker_without_opening_checkpoint(self):
        calls = []

        def checkpoint_factory(path):
            calls.append(path)
            return ClosingCheckpoint(path)

        service = self.service(checkpoint_factory)
        with self.assertRaises(WorkerApiError):
            service.dispatch({"op": "clear_collection_progress", "payload": {"path": "fictional-other-db"}})
        service._job_lock.acquire()
        try:
            with self.assertRaisesRegex(WorkerApiError, BUSY):
                service.dispatch({"op": "clear_collection_progress"})
        finally:
            service._job_lock.release()
        self.assertEqual(calls, [])

    def test_clear_rejects_other_session_lease_and_preserves_checkpoint(self):
        service = self.service()
        checkpoint = self.start_checkpoint()
        try:
            with self.assertRaisesRegex(WorkerApiError, BUSY):
                service.dispatch({"op": "clear_collection_progress"})
            self.assertIsNotNone(checkpoint.load_task("fictional-task"))
        finally:
            checkpoint.close()
        self.assertEqual(service.dispatch({"op": "clear_collection_progress"}), {"status": SUCCESS})

    def test_clear_failure_closes_store_releases_job_lock_and_hides_path_or_codec_errors(self):
        created = []

        class BrokenCheckpoint(ClosingCheckpoint):
            def clear(self):
                raise OSError("fictional-private-path-and-credential")

        def checkpoint_factory(path):
            checkpoint = BrokenCheckpoint(path)
            created.append(checkpoint)
            return checkpoint

        service = self.service(checkpoint_factory)
        with self.assertRaises(WorkerApiError) as raised:
            service.dispatch({"op": "clear_collection_progress"})
        self.assertEqual(str(raised.exception), FAILURE)
        self.assertTrue(created[0].closed)
        self.assertFalse(service._job_lock.locked())

    def test_incomplete_source_blocks_workflow_before_model_or_sender(self):
        service = self.service()
        initial = service.dispatch({"op": "initialize"})
        service.dispatch({"op": "save", "payload": {
            "settings": initial["settings"], "verification_window": self.window()}})
        settings = service.settings_store.load()
        calls = []

        class IncompleteClient(SyntheticClient):
            def collect(self, *args, **kwargs):
                result = super().collect(*args, **kwargs)
                result["records"] = []
                result["pagination_complete"] = False
                return result

        class ForbiddenModel:
            def summarize(self, *_args, **_kwargs):
                calls.append("model")
                raise AssertionError("Incomplete source reached model")

        class ForbiddenSender:
            def send_markdown(self, *_args, **_kwargs):
                calls.append("sender")
                raise AssertionError("Incomplete source reached sender")

        workflow = DigestWorkflow(settings, service.secret_store.load(), self.directory,
                                  client_factory=IncompleteClient, summarizer=ForbiddenModel(),
                                  ding_sender=ForbiddenSender())
        with self.assertRaises(WorkflowBlocked):
            workflow.run(datetime(2026, 1, 2, 0, 0, tzinfo=SHANGHAI),
                         datetime(2026, 1, 2, 1, 0, tzinfo=SHANGHAI))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
