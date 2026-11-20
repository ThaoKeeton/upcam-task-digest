"""Named DingTalk robots for the standalone app. Synthetic values only."""
import json
from pathlib import Path
import tempfile
import unittest

from upcam_app.config import SettingsStore
from upcam_app.history import HistoryStore
from upcam_app.secrets import MemorySecretStore
from upcam_app.worker_api import WorkerApiError, WorkerService
from upcam_app.workflow import WorkflowBlocked


def _hook(token):
    return "https://oapi.dingtalk.com/robot/send?access_token=" + token


TEST_HOOK = _hook("synthetic-test-token")
FORMAL_HOOK = _hook("synthetic-formal-token")
TEST_SECRET = "synthetic-test-signing-secret"
FORMAL_SECRET = "synthetic-formal-signing-secret"


class CaptureWorkflow:
    instances = []

    def __init__(self, settings, secrets, data_dir, **kwargs):
        self.secrets = dict(secrets)
        CaptureWorkflow.instances.append(self)

    def run(self, *args, **kwargs):
        raise WorkflowBlocked("合成测试在调用发送前停止")


class DingTalkDestinationTests(unittest.TestCase):
    def setUp(self):
        CaptureWorkflow.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = SettingsStore(root / "settings.json")
        self.secrets = MemorySecretStore()
        self.service = WorkerService(
            root,
            settings_store=self.store,
            secret_store=self.secrets,
            history=HistoryStore(root / "history.sqlite3"),
            startup_configurer=lambda *args, **kwargs: None,
            workflow_factory=CaptureWorkflow,
            update_manager=object(),
        )

    def _op(self, op, payload=None):
        return self.service.dispatch({"op": op, "payload": payload or {}})

    def test_legacy_single_robot_migrates_and_stays_selected(self):
        self.secrets.save({"dingtalk_webhook": TEST_HOOK, "dingtalk_signing_secret": TEST_SECRET})
        listed = self._op("list_dingtalk_destinations")
        view = listed["dingtalk_destinations"]
        self.assertEqual(view["current_role"], "unspecified")
        self.assertEqual(view["current_name"], "原有配置")
        self.assertTrue(view["current_ready"])
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("synthetic-test-token", rendered)
        self.assertNotIn(TEST_SECRET, rendered)
        self.assertNotIn("access_token", rendered)
        again = self._op("initialize")
        self.assertEqual(again["dingtalk_destinations"]["selected_id"], view["selected_id"])
        self.assertEqual(self.secrets.load()["dingtalk_webhook"], TEST_HOOK)
        self.assertEqual(self.secrets.load()["dingtalk_signing_secret"], TEST_SECRET)
        self.assertFalse(self.store.load()["auto_send_enabled"])
        self.assertEqual(CaptureWorkflow.instances, [])

    def test_switch_edit_and_restart_use_only_the_selected_pair(self):
        self._op("save_dingtalk_destination", {
            "name": "测试群配置", "role": "test", "webhook": TEST_HOOK, "signing_secret": TEST_SECRET})
        formal = self._op("save_dingtalk_destination", {
            "name": "正式群配置", "role": "formal", "webhook": FORMAL_HOOK, "signing_secret": FORMAL_SECRET})
        formal_id = next(row["id"] for row in formal["dingtalk_destinations"]["profiles"]
                         if row["role"] == "formal")
        self.assertEqual(formal["dingtalk_destinations"]["current_role"], "test")
        self.assertEqual(self.secrets.load()["dingtalk_webhook"], TEST_HOOK)
        selected = self._op("select_dingtalk_destination", {"id": formal_id})
        self.assertEqual(selected["dingtalk_destinations"]["current_role"], "formal")
        self.assertEqual(self.secrets.load()["dingtalk_webhook"], FORMAL_HOOK)
        self.assertEqual(self.secrets.load()["dingtalk_signing_secret"], FORMAL_SECRET)
        self.assertNotIn(TEST_HOOK, json.dumps(selected["dingtalk_destinations"], ensure_ascii=False))
        self.assertFalse(selected["settings"]["auto_send_enabled"])
        self._op("save_dingtalk_destination", {
            "id": formal_id, "name": "正式群配置", "role": "formal",
            "webhook": FORMAL_HOOK, "signing_secret": "synthetic-formal-signing-secret-v2"})
        reloaded = WorkerService(
            self.service.data_dir,
            settings_store=self.store,
            secret_store=self.secrets,
            history=self.service.history,
            startup_configurer=lambda *args, **kwargs: None,
            workflow_factory=CaptureWorkflow,
            update_manager=object(),
        )
        view = reloaded.dispatch({"op": "list_dingtalk_destinations", "payload": {}})
        self.assertEqual(view["dingtalk_destinations"]["current_name"], "正式群配置")
        self.assertEqual(self.secrets.load()["dingtalk_signing_secret"], "synthetic-formal-signing-secret-v2")
        self.assertEqual(self.secrets.load()["dingtalk_webhook"], FORMAL_HOOK)
        self.assertEqual(CaptureWorkflow.instances, [])

    def test_incomplete_selection_cannot_send_and_snapshot_ignores_a_later_switch(self):
        self.secrets.save({"dingtalk_webhook": TEST_HOOK})
        migrated = self._op("list_dingtalk_destinations")
        self.assertFalse(migrated["dingtalk_destinations"]["current_ready"])
        self.assertEqual(self.secrets.load().get("dingtalk_webhook", ""), "")
        self.assertEqual(self.secrets.load().get("dingtalk_signing_secret", ""), "")
        with self.assertRaises(WorkerApiError):
            self._op("validate_dingtalk_config")
        saved = self._op("save_dingtalk_destination", {
            "name": "测试群配置", "role": "test", "webhook": TEST_HOOK, "signing_secret": TEST_SECRET})
        test_id = next(row["id"] for row in saved["dingtalk_destinations"]["profiles"] if row["role"] == "test")
        self._op("select_dingtalk_destination", {"id": test_id})
        with self.assertRaises(WorkerApiError) as caught:
            self._op("run_manual", {"start": "2026-11-20 09:00", "end": "2026-11-20 10:00"})
        self.assertIn("发送前停止", str(caught.exception))
        self.assertEqual(CaptureWorkflow.instances[-1].secrets["dingtalk_webhook"], TEST_HOOK)
        self.assertEqual(CaptureWorkflow.instances[-1].secrets["dingtalk_signing_secret"], TEST_SECRET)
        legacy_id = migrated["dingtalk_destinations"]["selected_id"]
        self._op("select_dingtalk_destination", {"id": legacy_id})
        self.assertEqual(CaptureWorkflow.instances[-1].secrets["dingtalk_webhook"], TEST_HOOK)
        self.assertEqual(self.secrets.load().get("dingtalk_webhook", ""), "")

    def test_ordinary_secret_save_updates_the_selected_robot_without_sending(self):
        self.secrets.save({"dingtalk_webhook": TEST_HOOK, "dingtalk_signing_secret": TEST_SECRET})
        self._op("list_dingtalk_destinations")
        settings = self.store.load()
        self._op("save", {"settings": settings, "secret_values": {
            "dingtalk_webhook": FORMAL_HOOK, "dingtalk_signing_secret": FORMAL_SECRET}})
        loaded = self.secrets.load()
        self.assertEqual(loaded["dingtalk_webhook"], FORMAL_HOOK)
        self.assertEqual(loaded["dingtalk_signing_secret"], FORMAL_SECRET)
        self.assertNotIn(TEST_SECRET, loaded["dingtalk_destinations"])
        self.assertFalse(self.store.load()["auto_send_enabled"])
        self.assertEqual(CaptureWorkflow.instances, [])


if __name__ == "__main__":
    unittest.main()
