"""Synthetic mention settings, envelope, privacy, and delivery regressions."""

from copy import deepcopy
from datetime import datetime
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from delivery_guard import DeliveryGuard, initialize_ledger
from report_contract import SHANGHAI, updated_selection
from upcam_app.config import DEFAULT_SETTINGS, SettingsError, SettingsStore, runtime_config, validate_settings
from upcam_app.diagnostic_export import safe_structure_export
from upcam_app.dingtalk import DingTalkSender
from upcam_app.openai_models import OpenAIModelCatalog
from upcam_app.secrets import MemorySecretStore
from upcam_app.worker_api import WorkerService
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked


WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=synthetic-only"


class MentionSettingsTests(unittest.TestCase):
    def test_legacy_settings_default_to_disabled_mobile_mentions(self):
        settings = validate_settings({})
        self.assertEqual(settings["dingtalk_mention_mode"], "mobile")
        self.assertEqual(settings["dingtalk_mentions"], "")

    def test_normalizes_all_supported_separators_and_deduplicates_in_first_seen_order(self):
        settings = validate_settings(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id",
            dingtalk_mentions=" ci-one,ci-Two；ci-one;ci-two\tci_3\r\nci-four，ci-Two "))
        self.assertEqual(settings["dingtalk_mentions"], "ci-one\nci-Two\nci-two\nci_3\nci-four")

    def test_mobile_supported_form_preserves_plus_prefix_and_exact_identifier(self):
        settings = validate_settings(dict(DEFAULT_SETTINGS,
            dingtalk_mentions="+10000001,10000002 +10000001"))
        self.assertEqual(settings["dingtalk_mentions"], "+10000001\n10000002")

    def test_distinct_target_limit_is_fifty_and_duplicates_do_not_consume_it(self):
        targets = [f"ci-{index}" for index in range(50)]
        settings = validate_settings(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id",
                                           dingtalk_mentions=" ".join(targets + targets)))
        self.assertEqual(settings["dingtalk_mentions"].splitlines(), targets)
        with self.assertRaises(SettingsError):
            validate_settings(dict(settings, dingtalk_mentions="\n".join(targets + ["ci-extra"])))

    def test_rejects_invalid_modes_and_nontext_targets_with_fixed_safe_errors(self):
        for mode in (None, {}, [], "all", "userId", "mobile "):
            with self.subTest(mode=mode), self.assertRaises(SettingsError):
                validate_settings(dict(DEFAULT_SETTINGS, dingtalk_mention_mode=mode))
        for targets in (None, [], {}, True, 123):
            with self.subTest(targets=targets), self.assertRaises(SettingsError):
                validate_settings(dict(DEFAULT_SETTINGS, dingtalk_mentions=targets))

    def test_rejects_prefixed_ids_markdown_urls_labels_unicode_and_unsafe_controls(self):
        for mode, targets in (
            ("mobile", "12345"), ("mobile", "1" * 21), ("mobile", "@10000001"),
            ("mobile", "100-000-01"), ("mobile", "１２３４５６７８"),
            ("user_id", "@ci-one"), ("user_id", "[ci-one](https://example.invalid)"),
            ("user_id", "https://example.invalid"), ("user_id", "姓名:ci-one"),
            ("user_id", "ci-姓名"), ("user_id", "ci-one\x00ci-two"),
            ("user_id", "ci-one\x1bci-two"), ("user_id", "ci-one\vci-two"),
            ("user_id", "ci-one\u202eci-two"), ("user_id", "ci-one\u00a0ci-two"),
            ("user_id", "all"), ("user_id", "ALL"), ("user_id", "a" * 129),
        ):
            with self.subTest(mode=mode, targets=targets), self.assertRaises(SettingsError) as raised:
                validate_settings(dict(DEFAULT_SETTINGS, dingtalk_mention_mode=mode, dingtalk_mentions=targets))
            self.assertNotIn(targets, str(raised.exception))

    def test_invalid_settings_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SettingsStore(Path(temporary) / "settings.json")
            store.save(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id", dingtalk_mentions="ci-one"))
            before = store.path.read_bytes()
            with self.assertRaises(SettingsError):
                store.save(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id", dingtalk_mentions="@ci-two"))
            self.assertEqual(store.path.read_bytes(), before)

    def test_workflow_options_are_immutable_and_empty_config_provides_no_kwargs(self):
        helpers = importlib.import_module("upcam_app.dingtalk_mentions")
        self.assertEqual(dict(helpers.mention_options(DEFAULT_SETTINGS)), {})
        options = helpers.mention_options({"dingtalk_mention_mode": "user_id", "dingtalk_mentions": "ci-one ci-two"})
        self.assertEqual(options["at_user_ids"], ("ci-one", "ci-two"))
        with self.assertRaises(TypeError):
            options["at_user_ids"] = ("changed",)

    def test_known_credentials_and_decoded_webhook_parts_are_not_valid_mention_targets(self):
        helpers = importlib.import_module("upcam_app.dingtalk_mentions")
        credentials = {"dingtalk_signing_secret": "SEC_ci_known_secret", "openai_api_key": "ci-model-key",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=ci%2Dprivate%2Dtoken&sign=ci%2Dprivate%2Dsign"}
        for value in ("SEC_ci_known_secret", "ci-model-key", "ci-private-token", "ci-private-sign",
                      "prefix-SEC_ci_known_secret-suffix"):
            settings = {"dingtalk_mention_mode": "user_id", "dingtalk_mentions": "ci-normal；" + value}
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                helpers.reject_credential_mentions(settings, credentials)
            self.assertNotIn(value, str(raised.exception))

    def test_unknown_secret_like_id_is_still_supported_and_empty_mentions_need_no_credentials(self):
        helpers = importlib.import_module("upcam_app.dingtalk_mentions")
        self.assertIsNone(helpers.reject_credential_mentions(
            {"dingtalk_mention_mode": "user_id", "dingtalk_mentions": "SEC_ci_legitimate_user"},
            {"dingtalk_signing_secret": "SEC_ci_different_secret"}))
        self.assertIsNone(helpers.reject_credential_mentions(DEFAULT_SETTINGS, {}))


class MentionSenderTests(unittest.TestCase):
    def setUp(self):
        self.bodies = []
        self.sender = DingTalkSender(transport=self.transport, clock_ms=lambda: 123456789)

    def transport(self, _url, body, _timeout):
        self.bodies.append(body)
        return {"errcode": 0}

    def test_empty_options_preserve_original_outgoing_bytes_exactly(self):
        expected = '{"msgtype":"markdown","markdown":{"title":"UPCAM 站会日报","text":"# 虚构日报\\n正文"}}'.encode("utf-8")
        self.assertTrue(self.sender.send_markdown(WEBHOOK, "synthetic-secret", "# 虚构日报\n正文"))
        self.assertTrue(self.sender.send_markdown(WEBHOOK, "synthetic-secret", "# 虚构日报\n正文",
                                                  at_mobiles=[], at_user_ids=()))
        self.assertEqual(self.bodies, [expected, expected])

    def test_mobile_targets_have_one_matching_footer_and_top_level_at_object(self):
        body = "# 原始正文\n\n任务布局保持\n"
        self.assertTrue(self.sender.send_markdown(WEBHOOK, "synthetic-secret", body,
                                                  at_mobiles=["+10000001", "10000002", "+10000001"]))
        parsed = json.loads(self.bodies[0])
        self.assertEqual(parsed["at"], {"atMobiles": ["+10000001", "10000002"], "isAtAll": False})
        self.assertEqual(parsed["markdown"]["text"], "# 原始正文\n\n任务布局保持\n\n\u3000\n\n@+10000001 @10000002\n")
        self.assertEqual(set(parsed), {"msgtype", "markdown", "at"})

    def test_user_ids_preserve_case_order_and_use_atUserIds(self):
        self.sender.send_markdown(WEBHOOK, "synthetic-secret", '虚构 "正文"',
                                  at_user_ids=("ci-Two", "ci-one", "ci-Two", "ci_two.3"))
        parsed = json.loads(self.bodies[0])
        self.assertEqual(parsed["at"], {"atUserIds": ["ci-Two", "ci-one", "ci_two.3"], "isAtAll": False})
        self.assertEqual(parsed["markdown"]["text"], '虚构 "正文"\n\n\u3000\n\n@ci-Two @ci-one @ci_two.3\n')

    def test_footer_uses_existing_blank_block_preserves_internal_markdown_and_has_no_divider(self):
        self.sender.send_markdown(WEBHOOK, "synthetic-secret", "首段  \n\n\u3000\n\n末段  \r\n\r\n", at_user_ids=("ci-one",))
        text = json.loads(self.bodies[0])["markdown"]["text"]
        self.assertEqual(text, "首段  \n\n\u3000\n\n末段  \n\n\u3000\n\n@ci-one\n")
        self.assertFalse(any(line in ("---", "___", "***") for line in text.splitlines()))

    def test_empty_mentions_do_not_normalize_terminal_crlf(self):
        self.sender.send_markdown(WEBHOOK, "synthetic-secret", "正文  \r\n\r\n")
        self.assertEqual(json.loads(self.bodies[0])["markdown"]["text"], "正文  \r\n\r\n")

    def test_direct_sender_blocks_supplied_signing_secret_and_decoded_webhook_tokens(self):
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=ci%2Dprivate%2Dtoken&sign=ci%2Dprivate%2Dsign"
        for value in ("SEC_ci_known_secret", "ci-private-token", "ci-private-sign"):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                self.sender.send_markdown(webhook, "SEC_ci_known_secret", "虚构正文", at_user_ids=(value,))
            self.assertNotIn(value, str(raised.exception))
        self.assertEqual(self.bodies, [])

    def test_sender_revalidates_options_before_any_transport(self):
        for options in (
            {"at_mobiles": ["10000001"], "at_user_ids": ["ci-one"]},
            {"at_user_ids": "ci-one"}, {"at_mobiles": {"10000001"}},
            {"at_user_ids": ["ci-one ci-two"]}, {"at_user_ids": ["@ci-one"]},
            {"at_user_ids": ["all"]}, {"at_mobiles": ["+bad-phone"]},
            {"at_user_ids": ["ci-one", None]}, {"at_mobiles": None},
            {"at_user_ids": [f"ci-{index}" for index in range(51)]},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.sender.send_markdown(WEBHOOK, "synthetic-secret", "虚构正文", **options)
        self.assertEqual(self.bodies, [])


class MentionWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        fixture_dir = Path(__file__).parent / "fixtures"
        self.collection = json.loads((fixture_dir / "collection.json").read_text(encoding="utf-8"))
        self.settings = deepcopy(DEFAULT_SETTINGS)
        self.settings.update(tenant_id="demo-org", project_id="demo-project", api_contract_verified=True,
            view={"verified": True, "evidence": "synthetic-only", "task_equals": {}},
            owner_names={"demo-person": "虚构成员"}, status_names={"demo-done": "已解决", "demo-open": "待处理"},
            resolved_status_ids=["demo-done"])
        self.secrets = {"teambition_app_id": "synthetic-app", "teambition_app_secret": "synthetic-secret",
            "dingtalk_webhook": WEBHOOK, "dingtalk_signing_secret": "synthetic-signing-secret",
            "openai_api_key": "synthetic-key", "delivery_ledger_id": initialize_ledger(self.directory / "delivery_ledger.sqlite3")}
        self.catalog = OpenAIModelCatalog(self.directory / "models.json",
            transport=lambda *_: b'{"data":[{"id":"gpt-6-luna"}]}')
        self.catalog.refresh(self.secrets["openai_api_key"])
        self.start = datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI)
        self.end = datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI)
        self.bodies, self.model_requests = [], []
        self.on_collect = lambda: None
        parent = self

        class Client:
            def collect(self, project_id, **query):
                result = deepcopy(parent.collection)
                result.update(selection=updated_selection(query["day"], *query["window"]), batch_id="synthetic-batch",
                               collected_at="2026-01-03T01:00:00+00:00")
                for record in result["records"]:
                    record["task"]["updated"] = "2026-01-02T00:30:00Z"
                parent.on_collect()
                return result

        class Summarizer:
            def summarize(self, key, model, effort, rows, rules, service_tier="default"):
                parent.model_requests.append((key, model, effort, rows, rules, service_tier))
                return {row["task_key"]: "虚构：已定位导出异常，仍待修复。" for row in rows}

        self.workflow = DigestWorkflow(self.settings, self.secrets, self.directory,
            client_factory=lambda *_args, **_kwargs: Client(), summarizer=Summarizer(), model_catalog=self.catalog,
            ding_sender=DingTalkSender(transport=lambda _url, body, _timeout: self.bodies.append(body) or {"errcode": 0}))
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def test_workflow_selects_user_id_mentions_and_keeps_model_report_history_and_diagnostics_private(self):
        self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions="ci-private-one,ci-private-two")
        result = self.workflow.run(self.start, self.end)
        self.assertEqual(json.loads(self.bodies[0])["at"]["atUserIds"], ["ci-private-one", "ci-private-two"])
        self.assertEqual(len(self.model_requests), 1)
        exported = safe_structure_export(self.collection, runtime_config(self.settings), "2026-01-02", (self.start, self.end))
        for target in ("ci-private-one", "ci-private-two"):
            for artifact in (result, self.model_requests, self.workflow.history.list_runs(), exported):
                self.assertNotIn(target, str(artifact))

    def test_workflow_selects_mobile_mentions(self):
        self.settings.update(dingtalk_mention_mode="mobile", dingtalk_mentions="+10000001\n10000002")
        self.workflow.run(self.start, self.end)
        self.assertEqual(json.loads(self.bodies[0])["at"]["atMobiles"], ["+10000001", "10000002"])

    def test_empty_mentions_keep_legacy_three_argument_sender_compatible(self):
        calls = []

        class LegacySender:
            def send_markdown(self, webhook, secret, markdown):
                calls.append((webhook, secret, markdown))
                return True

        self.workflow.ding_sender = LegacySender()
        result = self.workflow.run(self.start, self.end)
        self.assertEqual(calls[0][2], result["report"]["markdown"])

    def test_sender_options_are_frozen_before_collection_mutates_settings(self):
        self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions="ci-original")
        self.on_collect = lambda: self.settings.update(dingtalk_mention_mode="mobile", dingtalk_mentions="10000001")
        self.workflow.run(self.start, self.end)
        self.assertEqual(json.loads(self.bodies[0])["at"], {"atUserIds": ["ci-original"], "isAtAll": False})

    def test_no_dynamic_tasks_still_send_mentions_with_zero_model_calls(self):
        self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions="ci-no-dynamic")
        self.collection["records"] = []
        self.workflow.secrets.pop("openai_api_key")
        with patch.object(self.catalog, "load", side_effect=AssertionError("No model cache required")):
            result = self.workflow.run(self.start, self.end)
        self.assertEqual(self.model_requests, [])
        self.assertEqual(json.loads(self.bodies[0])["markdown"]["text"],
                         result["report"]["markdown"].removesuffix("\n") + "\n\n\u3000\n\n@ci-no-dynamic\n")

    def test_workflow_rejects_known_credentials_before_collection_or_model_calls(self):
        for target in (self.workflow.secrets["dingtalk_signing_secret"], "synthetic-only",
                       self.workflow.secrets["openai_api_key"]):
            self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions=target)
            with self.subTest(target=target), patch.object(self.workflow, "client_factory") as client:
                with self.assertRaises(WorkflowBlocked) as raised:
                    self.workflow.run(self.start, self.end)
                self.assertNotIn(target, str(raised.exception))
                client.assert_not_called()
        self.assertEqual(self.model_requests, [])
        self.assertEqual(self.bodies, [])
        self.assertEqual(self.workflow.history.list_runs(), [])

    def test_changed_mentions_do_not_bypass_ordinary_duplicate_guard(self):
        self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions="ci-one")
        self.workflow.run(self.start, self.end)
        self.settings["dingtalk_mentions"] = "ci-two"
        with self.assertRaises(WorkflowBlocked):
            self.workflow.run(self.start, self.end)
        self.assertEqual(len(self.bodies), 1)

    def test_guard_binds_mention_options_to_current_config_without_exposing_targets(self):
        self.settings.update(dingtalk_mention_mode="user_id", dingtalk_mentions="ci-one")

        class AlteredConfigGuard(DeliveryGuard):
            def attempt(self, report, **kwargs):
                kwargs["current_config"]["dingtalk_mentions"] = "ci-tampered-target"
                return super().attempt(report, **kwargs)

        self.workflow.guard_factory = AlteredConfigGuard
        with self.assertRaises(WorkflowBlocked) as raised:
            self.workflow.run(self.start, self.end)
        self.assertNotIn("ci-one", str(raised.exception))
        self.assertNotIn("ci-tampered-target", str(raised.exception))
        self.assertEqual(self.bodies, [])


class MentionWorkerTests(unittest.TestCase):
    def test_saved_new_rotated_and_cleared_credentials_cannot_persist_as_plaintext_targets(self):
        previous = {"dingtalk_signing_secret": "SEC_ci_previous_secret", "teambition_app_id": "ci-app-id",
                    "teambition_app_secret": "ci-app-secret", "dingtalk_webhook": WEBHOOK}
        cases = [
            ("SEC_ci_previous_secret", {}, []),
            ("SEC_ci_new_secret", {"dingtalk_signing_secret": "SEC_ci_new_secret"}, []),
            ("SEC_ci_previous_secret", {"dingtalk_signing_secret": "SEC_ci_new_secret"}, []),
            ("SEC_ci_previous_secret", {}, ["dingtalk_signing_secret"]),
            ("synthetic-only", {}, ["dingtalk_webhook"]),
            ("ci-new-token", {"dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=ci%2Dnew%2Dtoken"}, []),
        ]
        for target, replacements, cleared in cases:
            with self.subTest(target=target, cleared=cleared), tempfile.TemporaryDirectory() as temporary:
                secrets = MemorySecretStore()
                secrets.save(previous)
                service = WorkerService(Path(temporary), secret_store=secrets)
                saved = service.settings_store.save(dict(DEFAULT_SETTINGS, tenant_id="demo-org", project_id="demo-project"))
                before = service.settings_store.path.read_bytes()
                proposed = dict(saved, dingtalk_mention_mode="user_id", dingtalk_mentions="ci-normal；" + target)
                with patch.object(secrets, "save", wraps=secrets.save) as secret_writes, \
                        patch.object(service.settings_store, "save", wraps=service.settings_store.save) as settings_writes, \
                        patch.object(service, "client_factory") as client:
                    with self.assertRaises(ValueError) as raised:
                        service.dispatch({"op": "save", "payload": {"settings": proposed,
                            "secret_values": replacements, "clear_secrets": cleared,
                            "verification_window": {"start": "2026-01-01 16:50", "end": "2026-01-02 08:50"}}})
                    self.assertNotIn(target, str(raised.exception))
                    secret_writes.assert_not_called()
                    settings_writes.assert_not_called()
                    client.assert_not_called()
                self.assertEqual(secrets.load(), previous)
                self.assertEqual(service.settings_store.path.read_bytes(), before)

    def test_local_check_and_initialize_do_not_echo_preexisting_credential_targets(self):
        for operation in ("initialize", "validate_dingtalk_config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                secrets = MemorySecretStore()
                secrets.save({"dingtalk_webhook": WEBHOOK, "dingtalk_signing_secret": "SEC_ci_existing_secret"})
                service = WorkerService(Path(temporary), secret_store=secrets)
                service.settings_store.save(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id",
                                                  dingtalk_mentions="SEC_ci_existing_secret"))
                with self.assertRaises(ValueError) as raised:
                    service.dispatch({"op": operation})
                self.assertNotIn("SEC_ci_existing_secret", str(raised.exception))

    def test_local_config_check_returns_count_and_mode_without_ids_or_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SettingsStore(Path(temporary) / "settings.json")
            store.save(dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id", dingtalk_mentions="ci-private-one;ci-private-two"))
            secrets = MemorySecretStore()
            secrets.save({"dingtalk_webhook": WEBHOOK, "dingtalk_signing_secret": "synthetic-secret"})
            service = WorkerService(Path(temporary), settings_store=store, secret_store=secrets)
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Local check only")):
                result = service.dispatch({"op": "validate_dingtalk_config"})
            self.assertEqual(result["mention_count"], 2)
            self.assertEqual(result["mention_mode"], "user_id")
            self.assertNotIn("ci-private", str(result))
            self.assertIn("群成员", result["status"])
            self.assertIn("没有发送", result["status"])

    def test_invalid_mention_save_preserves_settings_and_existing_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            secrets = MemorySecretStore()
            secrets.save({"dingtalk_signing_secret": "existing-synthetic-secret"})
            service = WorkerService(Path(temporary), secret_store=secrets)
            old = service.settings_store.save(deepcopy(DEFAULT_SETTINGS))
            with self.assertRaises(ValueError):
                service.dispatch({"op": "save", "payload": {
                    "settings": dict(DEFAULT_SETTINGS, dingtalk_mention_mode="user_id", dingtalk_mentions="@invalid-target"),
                    "secret_values": {"dingtalk_signing_secret": "replacement-secret"}}})
            self.assertEqual(service.settings_store.load(), old)
            self.assertEqual(secrets.load(), {"dingtalk_signing_secret": "existing-synthetic-secret"})


if __name__ == "__main__":
    unittest.main()
