import json
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from upcam_app.config import (
    DEFAULT_SETTINGS, SettingsError, SettingsStore, collection_client_limits, parse_manual_window,
    recoverable_schedule_occurrences, scheduled_occurrences, scheduled_window, validate_settings,
)
from upcam_app.dingtalk import DingTalkConfigError, DingTalkSender, signed_webhook
from upcam_app.history import HistoryError, HistoryStore
from upcam_app.secrets import DpapiSecretStore, MemorySecretStore, SecretStoreError
from upcam_app.openai_models import ModelCatalogError, OpenAIModelCatalog, cache_matches_key, reasoning_efforts


class ConfigTests(unittest.TestCase):
    def test_legacy_task_setting_enables_bounded_batches_without_limiting_project_to_one_batch(self):
        self.assertEqual(DEFAULT_SETTINGS["max_tasks_per_run"], 300)
        for batch_size, requests, seconds in ((1, 1000, 310), (300, 1000, 310), (700, 1800, 510)):
            with self.subTest(batch_size=batch_size):
                settings = validate_settings(dict(DEFAULT_SETTINGS, max_tasks_per_run=batch_size))
                self.assertEqual(collection_client_limits(settings), {
                    "max_tasks": batch_size,
                    "max_requests": requests,
                    "max_pages": 50,
                    "max_records": 50000,
                    "max_seconds": seconds,
                    "max_response_bytes": 128 * 1024 * 1024,
                    "batch_mode": True,
                    "max_project_tasks": 10000,
                })
        for value in (0, 701, True, "500", 1.5):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                validate_settings(dict(DEFAULT_SETTINGS, max_tasks_per_run=value))

    def test_schema_one_batch_setting_round_trips_without_new_persisted_budget_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            legacy = {"schema_version": 1, "max_tasks_per_run": 700}
            path.write_text(json.dumps(legacy), encoding="utf-8")
            store = SettingsStore(path)
            settings = store.load()
            store.save(settings)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 1)
            self.assertEqual(saved["max_tasks_per_run"], 700)
            self.assertNotIn("batch_mode", saved)
            self.assertNotIn("max_project_tasks", saved)
            self.assertEqual(collection_client_limits(store.load()).get("max_project_tasks"), 10000)

    def test_default_windows_preserve_the_documented_morning_and_afternoon_periods(self):
        morning = scheduled_window(DEFAULT_SETTINGS, "morning", date(2026, 11, 3))
        afternoon = scheduled_window(DEFAULT_SETTINGS, "afternoon", date(2026, 11, 3))
        self.assertEqual(morning, (datetime(2026, 11, 2, 16, 50), datetime(2026, 11, 3, 8, 50)))
        self.assertEqual(afternoon, (datetime(2026, 11, 3, 0, 0), datetime(2026, 11, 3, 16, 50)))

    def test_manual_range_requires_local_datetime_and_order(self):
        start, end = parse_manual_window("2026-11-02 16:50", "2026-11-03 08:50")
        self.assertEqual((end - start).total_seconds(), 16 * 3600)
        with self.assertRaises(SettingsError):
            parse_manual_window("2026-11-03 08:50", "2026-11-03 08:50")

    def test_invalid_times_are_rejected(self):
        data = dict(DEFAULT_SETTINGS, afternoon_end_at="00:00")
        with self.assertRaises(SettingsError):
            validate_settings(data)
        with self.assertRaises(SettingsError):
            parse_manual_window("yesterday", "tomorrow")

    def test_fast_service_tier_is_opt_in_and_validated(self):
        self.assertEqual(DEFAULT_SETTINGS["openai_service_tier"], "default")
        self.assertEqual(validate_settings(dict(DEFAULT_SETTINGS, openai_service_tier="fast"))["openai_service_tier"],
                         "fast")
        with self.assertRaises(SettingsError):
            validate_settings(dict(DEFAULT_SETTINGS, openai_service_tier="ultrafast"))

    def test_windows_startup_is_explicit_and_defaults_off(self):
        self.assertFalse(DEFAULT_SETTINGS["start_with_windows"])
        self.assertTrue(validate_settings(dict(DEFAULT_SETTINGS, start_with_windows=True))["start_with_windows"])
        with self.assertRaises(SettingsError):
            validate_settings(dict(DEFAULT_SETTINGS, start_with_windows="yes"))

    def test_scheduled_slots_are_ordered_and_use_unique_keys(self):
        slots = scheduled_occurrences(DEFAULT_SETTINGS, datetime(2026, 11, 3, 8, 49),
                                      datetime(2026, 11, 4, 8, 51))
        self.assertEqual([item[1] for item in slots], ["morning", "afternoon", "morning"])
        self.assertEqual(len({item[2] for item in slots}), 3)

    def test_restart_recovery_catches_recent_slots_but_not_slots_older_than_24_hours(self):
        recent = recoverable_schedule_occurrences(DEFAULT_SETTINGS, datetime(2026, 11, 3, 9, 30))
        self.assertEqual([item[2] for item in recent], [
            "2026-11-02:afternoon", "2026-11-03:morning",
        ])
        later = recoverable_schedule_occurrences(DEFAULT_SETTINGS, datetime(2026, 11, 4, 17, 0))
        self.assertEqual([item[2] for item in later], [
            "2026-11-04:morning", "2026-11-04:afternoon",
        ])

    def test_restart_recovery_requires_local_naive_time(self):
        with self.assertRaises(SettingsError):
            recoverable_schedule_occurrences(
                DEFAULT_SETTINGS, datetime.fromisoformat("2026-11-03T09:30:00+08:00"))

    def test_recovery_does_not_replay_slots_from_before_auto_run_was_enabled(self):
        slots = recoverable_schedule_occurrences(
            DEFAULT_SETTINGS, datetime(2026, 11, 3, 9, 30),
            enabled_since=datetime(2026, 11, 3, 8, 0))
        self.assertEqual([item[2] for item in slots], ["2026-11-03:morning"])

    def test_saved_auto_run_enable_time_must_be_local_naive_time(self):
        settings = dict(DEFAULT_SETTINGS, auto_send_enabled=True,
                        auto_send_enabled_since_local="2026-11-03T08:00:00+08:00")
        with self.assertRaises(SettingsError):
            validate_settings(settings)

    def test_settings_round_trip_does_not_include_secret_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path)
            store.save(dict(DEFAULT_SETTINGS, project_id="synthetic-project"))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(store.load()["project_id"], "synthetic-project")
            self.assertNotIn("openai_api_key", saved)


class SecretStoreTests(unittest.TestCase):
    def test_memory_store_handles_only_known_synthetic_fields(self):
        store = MemorySecretStore()
        store.save({"openai_api_key": "fake-key-for-test"})
        self.assertEqual(store.load(), {"openai_api_key": "fake-key-for-test"})
        with self.assertRaises(SecretStoreError):
            store.save({"unexpected": "value"})

    def test_dpapi_store_refuses_plaintext_fallback_outside_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DpapiSecretStore(Path(tmp) / "secrets.dpapi")
            if __import__("os").name != "nt":
                with self.assertRaises(SecretStoreError):
                    store.save({"openai_api_key": "fake-key-for-test"})
                self.assertFalse(store.path.exists())


class OpenAIModelCatalogTests(unittest.TestCase):
    def test_refresh_caches_key_scoped_model_ids_without_the_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            received = []

            def fake_transport(api_key, timeout):
                received.append((api_key, timeout))
                return json.dumps({"object": "list", "data": [
                    {"id": "gpt-6-luna", "owned_by": "openai", "shutdown_date": None},
                    {"id": "synthetic-text-model", "owned_by": "test-org"},
                ]}).encode()

            store = OpenAIModelCatalog(Path(tmp) / "models.json", transport=fake_transport,
                                       clock=lambda: datetime(2026, 11, 3, tzinfo=__import__("datetime").timezone.utc))
            cache = store.refresh("fake-api-key")
            self.assertEqual(received, [("fake-api-key", 20)])
            self.assertEqual([item["id"] for item in cache["models"]], ["gpt-6-luna", "synthetic-text-model"])
            on_disk = (Path(tmp) / "models.json").read_text(encoding="utf-8")
            self.assertNotIn("fake-api-key", on_disk)
            self.assertTrue(cache_matches_key(cache, "fake-api-key"))
            self.assertFalse(cache_matches_key(cache, "different-synthetic-key"))
            self.assertEqual(store.load(), cache)

    def test_failed_refresh_preserves_existing_cache_and_sanitizes_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            first = OpenAIModelCatalog(path, transport=lambda *_: b'{"data":[{"id":"gpt-6-luna"}]}')
            original = first.refresh("fake-api-key")
            original_bytes = path.read_bytes()

            def fail_with_sensitive_error(*_):
                raise RuntimeError("fake-api-key appeared in a synthetic exception")

            refreshed = OpenAIModelCatalog(path, transport=fail_with_sensitive_error)
            with self.assertRaises(ModelCatalogError) as raised:
                refreshed.refresh("fake-api-key")
            self.assertNotIn("fake-api-key", str(raised.exception))
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(refreshed.load(), original)

    def test_reasoning_efforts_are_only_exposed_for_documented_models(self):
        self.assertIn("max", reasoning_efforts("gpt-6-luna"))
        self.assertEqual(reasoning_efforts("new-model-not-in-app-catalog"), ())

    def test_invalid_or_empty_model_responses_do_not_replace_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            store = OpenAIModelCatalog(path, transport=lambda *_: b'{"data":[]}')
            with self.assertRaises(ModelCatalogError):
                store.refresh("fake-api-key")
            self.assertFalse(path.exists())


class HistoryAndTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history = HistoryStore(Path(self.temp.name) / "history.sqlite3")
        self.markdown = "**虚构日报**\n"
        self.run_id = self.history.create_draft(datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 8, 50),
                                                "teambition", self.markdown)

    def test_samples_remain_preview_only(self):
        sample = self.history.create_draft(datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 8, 50),
                                           "sample", self.markdown)
        with self.assertRaises(HistoryError):
            self.history.start_delivery(sample)
        self.assertEqual(self.history.get(sample)["state"], "preview")

    def test_blocked_preflight_is_saved_with_a_bounded_diagnostic_and_no_digest(self):
        note = "自动校验未通过；缺少成员映射：demo-member。" + "原因" * 1200

        run_id = self.history.create_blocked_attempt(
            datetime(2026, 11, 3, 0, 0), datetime(2026, 11, 3, 8, 50), note,
            schedule_key="2026-11-03:morning")
        row = self.history.get(run_id)

        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["delivery_note"], note[:2000])
        self.assertIn("demo-member", row["delivery_note"])
        self.assertIn("未生成日报", row["digest_markdown"])
        with self.assertRaises(HistoryError):
            self.history.create_blocked_attempt(
                datetime(2026, 11, 3, 0, 0), datetime(2026, 11, 3, 8, 50), note,
                schedule_key="2026-11-03:morning")

    def test_validated_run_starts_sending_without_approval_state(self):
        run = self.history.start_delivery(self.run_id)
        self.assertEqual(run["state"], "sending")
        self.assertNotIn("approved_at_utc", run)
        self.history.finish_delivery(self.run_id, "sent", "Synthetic DingTalk success")
        self.assertEqual(self.history.get(self.run_id)["state"], "sent")
        with self.assertRaises(HistoryError):
            self.history.start_delivery(self.run_id)

    def test_dingtalk_transport_reports_success_only_for_acknowledged_envelope(self):
        seen = {}

        def transport(url, body, timeout):
            seen["url"] = url
            seen["body"] = json.loads(body.decode("utf-8"))
            return {"errcode": 0, "errmsg": "ok"}

        sender = DingTalkSender(transport=transport, clock_ms=lambda: 123456789)
        result = sender.send_markdown("https://oapi.dingtalk.com/robot/send?access_token=fake-token",
                                      "fake-secret", self.markdown)
        self.assertTrue(result)
        self.assertIn("sign=", seen["url"])
        self.assertEqual(seen["body"]["msgtype"], "markdown")

    def test_dingtalk_rejection_is_not_success(self):
        sender = DingTalkSender(transport=lambda *_: {"errcode": 310000}, clock_ms=lambda: 123456789)
        self.assertFalse(sender.send_markdown(
            "https://oapi.dingtalk.com/robot/send?access_token=fake-token", "fake-secret", self.markdown))

    def test_uncertain_transport_error_is_left_for_delivery_guard_to_record(self):

        def uncertain(*_args):
            raise TimeoutError("synthetic timeout with fake token")

        sender = DingTalkSender(transport=uncertain, clock_ms=lambda: 123456789)
        with self.assertRaises(TimeoutError):
            sender.send_markdown("https://oapi.dingtalk.com/robot/send?access_token=fake-token",
                                 "fake-secret", self.markdown)

    def test_interrupted_in_flight_send_is_marked_unknown(self):
        self.history.start_delivery(self.run_id)
        self.assertEqual(self.history.recover_interrupted_sends(), 1)
        self.assertEqual(self.history.get(self.run_id)["state"], "delivery_unknown")

    def test_history_can_skip_a_schedule_slot_already_started_before_restart(self):
        slot = self.history.create_draft(
            datetime(2026, 11, 3, 0, 0), datetime(2026, 11, 3, 8, 50),
            "teambition", self.markdown, "2026-11-03:morning")
        self.assertTrue(self.history.has_schedule_key("2026-11-03:morning"))
        self.assertFalse(self.history.has_schedule_key("2026-11-03:afternoon"))
        self.assertEqual(self.history.get(slot)["schedule_key"], "2026-11-03:morning")

    def test_signed_webhook_rejects_non_dingtalk_host(self):
        with self.assertRaises(DingTalkConfigError):
            signed_webhook("https://example.com/?access_token=fake", "fake-secret", 123)


if __name__ == "__main__":
    unittest.main()
