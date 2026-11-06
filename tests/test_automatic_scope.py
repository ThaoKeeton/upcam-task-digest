"""Save-time scope verification with isolated, read-only synthetic services."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teambition_readonly import CollectionError
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.smoke_data import SyntheticClient, make_smoke_service


class RecordingReadOnlyClient:
    def __init__(self):
        self.calls = []
        self.change_response = None
        self.failure = None

    def __call__(self, tenant_id, *, credentials, **limits):
        identity = credentials()
        parent = self

        class Client:
            def collect(self, project_id, **query):
                parent.calls.append({"tenant_id": tenant_id, "project_id": project_id,
                                     "credentials": identity, "limits": limits, **query})
                if parent.failure is not None:
                    raise parent.failure
                response = SyntheticClient(tenant_id).collect(project_id, **query)
                if parent.change_response is not None:
                    parent.change_response(response)
                return response

        return Client()


class AutomaticScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = make_smoke_service(Path(self.temporary.name))
        self.readonly = RecordingReadOnlyClient()
        self.service.client_factory = self.readonly
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.end = datetime.now().replace(second=0, microsecond=0)
        self.window = {"start": (self.end - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
                       "end": self.end.strftime("%Y-%m-%d %H:%M")}

    def save(self, settings=None, **payload):
        return self.service.dispatch({"op": "save", "payload": {
            "settings": settings if settings is not None else self.service.settings_store.load(),
            **payload,
        }})

    def legacy_verified(self, *, automatic=False):
        settings = self.service.settings_store.load()
        settings["api_contract_verified"] = True
        settings["view"].update({"verified": True,
                                 "evidence": "desktop-readonly-probe/v1; operator_confirmed=true"})
        settings["auto_send_enabled"] = automatic
        if automatic:
            settings["auto_send_enabled_since_local"] = (self.end - timedelta(days=1)).isoformat()
        self.service.settings_store.save(settings)
        return settings

    def assert_verified(self, result):
        self.assertTrue(result["settings"]["api_contract_verified"])
        self.assertTrue(result["settings"]["view"]["verified"])
        self.assertTrue(result["settings"]["view"]["evidence"])

    def assert_blocked(self, result):
        self.assertFalse(result["settings"]["api_contract_verified"])
        self.assertFalse(result["settings"]["view"]["verified"])
        self.assertEqual(result["settings"]["view"]["evidence"], "")
        self.assertFalse(result["settings"]["auto_send_enabled"])
        self.assertEqual(result["settings"]["auto_send_enabled_since_local"], "")
        self.assertIn("已保存", result["status"])
        self.assertIn("自动运行", result["status"])

    def test_first_configuration_save_verifies_using_new_unsaved_teambition_credentials(self):
        proposed = self.service.settings_store.load()
        self.service.settings_store.save(deepcopy(DEFAULT_SETTINGS))
        self.service.secret_store.save({})

        result = self.save(proposed, verification_window=self.window, secret_values={
            "teambition_app_id": "new-synthetic-app", "teambition_app_secret": "new-synthetic-secret"})

        self.assert_verified(result)
        self.assertEqual(len(self.readonly.calls), 1)
        self.assertEqual(self.readonly.calls[0]["credentials"], ("new-synthetic-app", "new-synthetic-secret"))
        self.assertNotIn("new-synthetic-secret", str(result))

    def test_automatic_save_returns_safe_discovery_ids_without_comment_text_or_credentials(self):
        result = self.save(verification_window=self.window)

        self.assert_verified(result)
        self.assertEqual(result["scope_probe"]["owner_ids"], ["demo-person", "demo-person-2"])
        self.assertEqual(result["scope_probe"]["status_ids"], ["demo-open"])
        self.assertEqual(result["scope_probe"]["status_name_suggestions"], {})
        for private in ("CI 虚构：验证原生界面布局", "synthetic-app-secret", "credentials", "credential_identity"):
            self.assertNotIn(private, str(result))

    def test_consumed_pending_probe_also_returns_discovery_without_another_query(self):
        self.service.dispatch({"op": "probe_project", "payload": self.window})

        result = self.save()

        self.assertEqual(result["scope_probe"]["owner_ids"], ["demo-person", "demo-person-2"])
        self.assertEqual(len(self.readonly.calls), 1)

    def test_initialize_and_unconfigured_save_perform_no_read_only_network_query(self):
        self.service.dispatch({"op": "initialize"})
        result = self.save(deepcopy(DEFAULT_SETTINGS))

        self.assert_blocked(result)
        self.assertEqual(self.readonly.calls, [])

    def test_complete_pending_probe_is_consumed_without_a_confirmation_field(self):
        self.service.dispatch({"op": "probe_project", "payload": self.window})

        result = self.save()

        self.assert_verified(result)
        self.assertEqual(len(self.readonly.calls), 1)

    def test_unchanged_legacy_verified_settings_survive_save_without_query_or_checkbox(self):
        settings = self.legacy_verified()
        settings["owner_names"]["new-synthetic-person"] = "新虚构成员"

        result = self.save(settings)

        self.assert_verified(result)
        self.assertEqual(result["settings"]["view"]["evidence"], settings["view"]["evidence"])
        self.assertEqual(self.readonly.calls, [])

    def test_new_scope_is_queried_and_verified_automatically_for_each_scope_field(self):
        for key, value in (("tenant_id", "other-org"), ("project_id", "other-project"),
                           ("include_archived", True), ("task_equals", {"tfsId": "demo-open"})):
            with self.subTest(key=key):
                settings = self.legacy_verified()
                if key == "task_equals":
                    settings["view"][key] = value
                else:
                    settings[key] = value
                self.readonly.calls.clear()

                result = self.save(settings, verification_window=self.window)

                self.assert_verified(result)
                self.assertNotEqual(result["settings"]["view"]["evidence"], settings["view"]["evidence"])
                self.assertEqual(len(self.readonly.calls), 1)
                query = self.readonly.calls[0]
                self.assertEqual(query["tenant_id"], settings["tenant_id"])
                self.assertEqual(query["project_id"], settings["project_id"])
                self.assertIs(query["include_archived"], settings["include_archived"])

    def test_changed_teambition_credential_revalidates_with_proposed_identity(self):
        for key in ("teambition_app_id", "teambition_app_secret"):
            with self.subTest(key=key):
                self.legacy_verified()
                self.readonly.calls.clear()

                result = self.save(verification_window=self.window, secret_values={key: "replacement-" + key})

                self.assert_verified(result)
                self.assertEqual(len(self.readonly.calls), 1)
                expected = self.service.secret_store.load()
                self.assertEqual(self.readonly.calls[0]["credentials"],
                                 (expected["teambition_app_id"], expected["teambition_app_secret"]))

    def test_reentering_same_teambition_credentials_preserves_verified_scope(self):
        self.legacy_verified()
        self.assert_verified(self.save(secret_values={"teambition_app_secret": "synthetic-app-secret"}))
        self.assertEqual(self.readonly.calls, [])

    def test_unrelated_secret_change_preserves_scope_verification(self):
        self.legacy_verified()
        self.assert_verified(self.save(secret_values={"openai_api_key": "different-synthetic-openai-key"}))
        self.assertEqual(self.readonly.calls, [])

    def test_pending_probe_for_different_credentials_cannot_verify_new_credentials(self):
        self.service.dispatch({"op": "probe_project", "payload": self.window})
        self.readonly.failure = CollectionError("synthetic authentication failure")

        result = self.save(verification_window=self.window,
                           secret_values={"teambition_app_secret": "replacement-secret"})

        self.assert_blocked(result)
        self.assertEqual(len(self.readonly.calls), 2)
        self.assertEqual(self.readonly.calls[-1]["credentials"][1], "replacement-secret")
        self.assertEqual(self.service.secret_store.load()["teambition_app_secret"], "replacement-secret")

    def test_pending_probe_for_different_scope_cannot_verify_changed_scope(self):
        self.service.dispatch({"op": "probe_project", "payload": self.window})
        proposed = self.service.settings_store.load()
        proposed["project_id"] = "another-synthetic-project"

        result = self.save(proposed, verification_window=self.window)

        self.assert_verified(result)
        self.assertEqual(len(self.readonly.calls), 2)
        self.assertEqual(self.readonly.calls[-1]["project_id"], "another-synthetic-project")

    def test_no_pending_probe_or_explicit_window_cannot_promote_client_verification(self):
        proposed = self.service.settings_store.load()
        proposed["api_contract_verified"] = True
        proposed["view"].update({"verified": True, "evidence": "forged-client-evidence"})

        result = self.save(proposed, scope_confirmed=True)

        self.assert_blocked(result)
        self.assertEqual(self.readonly.calls, [])

    def test_missing_configured_filter_field_cannot_receive_scope_verification(self):
        # The shared UI sample now includes a position event, so make the
        # missing-field failure explicit instead of relying on that fixture.
        self.readonly.change_response = lambda response: response["records"][0]["task"].pop("stageId", None)
        proposed = self.service.settings_store.load()
        proposed["view"]["task_equals"] = {"stageId": "synthetic-stage"}

        result = self.save(proposed, verification_window=self.window)

        self.assert_blocked(result)
        self.assertEqual(result["settings"]["view"]["task_equals"], {"stageId": "synthetic-stage"})
        self.assertEqual(len(self.readonly.calls), 1)

    def test_present_filter_field_with_no_matching_tasks_is_valid_selected_scope(self):
        proposed = self.service.settings_store.load()
        proposed["view"]["task_equals"] = {"tfsId": "another-synthetic-status"}

        result = self.save(proposed, verification_window=self.window)

        self.assert_verified(result)

    def test_incomplete_or_mismatched_source_never_produces_saved_proof(self):
        mutations = {
            "pagination": lambda data: data.update(pagination_complete=False),
            "tenant": lambda data: data.update(tenant_id="wrong-tenant"),
            "project": lambda data: data.update(project_id="wrong-project"),
            "archive": lambda data: data.update(include_archived=True),
            "window": lambda data: data["selection"].update(start="2020-01-01T00:00:00+08:00"),
            "task_binding": lambda data: data["records"][0]["task"].update(projectId="wrong-project"),
            "event_binding": lambda data: data["records"][0]["activities"][0].update(boundToObjectId="wrong-task"),
            "updated_without_timezone": lambda data: data["records"][0]["task"].update(updated="2020-01-01T00:00:00"),
            "invalid_event_time": lambda data: data["records"][0]["activities"][0].update(createTime="invalid-time"),
            "duplicate_task": lambda data: data["records"].append(deepcopy(data["records"][0])),
            "no_tasks": lambda data: data.update(records=[]),
            "no_activities": lambda data: data["records"][0].update(activities=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                proposed = self.service.settings_store.load()
                proposed["owner_names"]["edited-person"] = "保留的虚构成员"
                proposed["auto_send_enabled"] = True
                self.readonly.change_response = mutate
                self.readonly.calls.clear()

                result = self.save(proposed, verification_window=self.window)

                self.assert_blocked(result)
                self.assertEqual(len(self.readonly.calls), 1)
                self.assertEqual(self.service.settings_store.load()["owner_names"]["edited-person"], "保留的虚构成员")

    def test_failed_revalidation_saves_edits_and_stops_automatic_execution_without_model_or_send(self):
        proposed = self.legacy_verified(automatic=True)
        self.service.dispatch({"op": "initialize"})
        proposed["tenant_id"] = "changed-synthetic-tenant"
        proposed["owner_names"]["edited-person"] = "已保存的虚构成员"
        self.readonly.failure = OSError("sensitive synthetic server body")
        with patch.object(self.service, "workflow_factory", side_effect=AssertionError("No workflow during save")), \
                patch.object(self.service.model_catalog, "refresh", side_effect=AssertionError("No model calls during save")):
            result = self.save(proposed, verification_window=self.window,
                               secret_values={"teambition_app_secret": "new-local-secret"})
            due = self.service.dispatch({"op": "run_due"})

        self.assert_blocked(result)
        self.assertEqual(due["started"], 0)
        self.assertEqual(self.service.settings_store.load()["tenant_id"], "changed-synthetic-tenant")
        self.assertEqual(self.service.secret_store.load()["teambition_app_secret"], "new-local-secret")
        self.assertEqual(self.service.history.list_runs(), [])
        self.assertNotIn("sensitive synthetic server body", str(result))
        self.assertNotIn("new-local-secret", str(result))

    def test_unexpected_query_failure_does_not_lose_settings_or_expose_exception_text(self):
        self.readonly.failure = RuntimeError("unexpected synthetic credential body")

        result = self.save(verification_window=self.window)

        self.assert_blocked(result)
        self.assertNotIn("unexpected synthetic credential body", str(result))

    def test_invalid_verification_window_persists_unverified_settings_and_can_retry(self):
        result = self.save(verification_window={"start": self.window["end"], "end": self.window["start"]})

        self.assert_blocked(result)
        self.assertEqual(self.readonly.calls, [])
        self.assert_verified(self.save(verification_window=self.window))

    def test_removing_teambition_credential_invalidates_scope_and_does_not_query(self):
        self.legacy_verified(automatic=True)

        result = self.save(clear_secrets=["teambition_app_secret"], verification_window=self.window)

        self.assert_blocked(result)
        self.assertNotIn("teambition_app_secret", self.service.secret_store.load())
        self.assertEqual(self.readonly.calls, [])

    def test_clear_credentials_invalidates_scope_and_retains_internal_delivery_identity(self):
        self.service.dispatch({"op": "initialize"})
        self.legacy_verified()
        identity = self.service.secret_store.load()["delivery_ledger_id"]

        result = self.service.dispatch({"op": "clear_credentials"})

        self.assertFalse(result["settings"]["api_contract_verified"])
        self.assertFalse(result["settings"]["view"]["verified"])
        self.assertEqual(result["settings"]["view"]["evidence"], "")
        self.assertEqual(self.service.secret_store.load(), {"delivery_ledger_id": identity})

    def test_successful_scope_save_does_not_start_workflow_or_refresh_models(self):
        with patch.object(self.service, "workflow_factory", side_effect=AssertionError("No send during save")), \
                patch.object(self.service.model_catalog, "refresh", side_effect=AssertionError("No model calls during save")):
            result = self.save(verification_window=self.window)

        self.assert_verified(result)
        self.assertFalse(result["settings"]["auto_send_enabled"])
        self.assertEqual(self.service.history.list_runs(), [])

    def test_verified_scope_still_requires_credentials_current_model_cache_and_ledger_for_auto_enable(self):
        self.service.dispatch({"op": "initialize"})
        proposed = self.legacy_verified()
        proposed["auto_send_enabled"] = True
        with self.assertRaisesRegex(ValueError, "模型缓存"):
            self.save(proposed)
        self.assertFalse(self.service.settings_store.load()["auto_send_enabled"])

        self.service.dispatch({"op": "refresh_models"})
        credentials = self.service.secret_store.load()
        credentials.pop("dingtalk_signing_secret")
        self.service.secret_store.save(credentials)
        with self.assertRaisesRegex(ValueError, "凭据"):
            self.save(proposed)
        credentials["dingtalk_signing_secret"] = "synthetic-only"
        self.service.secret_store.save(credentials)
        (Path(self.temporary.name) / "delivery_ledger.sqlite3").unlink()
        with self.assertRaisesRegex(ValueError, "防重账本"):
            self.save(proposed)


if __name__ == "__main__":
    unittest.main()
