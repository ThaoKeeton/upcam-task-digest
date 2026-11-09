import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from delivery_guard import initialize_ledger
from report_contract import SHANGHAI, updated_selection
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.secrets import teambition_mcp_identity, MCP_AUTHORIZATION_FIELD
from upcam_app.openai_models import OpenAIModelCatalog
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked


class FakeSummarizer:
    def __init__(self):
        self.calls = 0
        self.inputs = []

    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        self.calls += 1
        self.inputs.append(deepcopy((rows, _rules)))
        return {row["task_key"]: "虚构：已定位导出异常，仍待修复。" for row in rows}


class FakeDingTalk:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def send_markdown(self, webhook, secret, markdown):
        self.calls.append((webhook, secret, markdown))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTeambition:
    def __init__(self, collection, credentials):
        self.collection = collection
        self.credentials = credentials
        self.calls = []

    def collect(self, project, **kwargs):
        self.calls.append((project, kwargs))
        collection = deepcopy(self.collection)
        collection["selection"] = updated_selection(kwargs["day"], *kwargs["window"])
        if collection["records"]:
            collection["records"][0]["task"]["updated"] = "2026-01-02T00:30:00Z"
        return collection


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        fixture_dir = Path(__file__).parent / "fixtures"
        self.collection = json.loads((fixture_dir / "collection.json").read_text(encoding="utf-8"))
        self.collection["selection"] = updated_selection("2026-01-02")
        self.collection["records"][0]["task"]["updated"] = "2026-01-02T00:30:00Z"
        self.collection["batch_id"] = "00000000-0000-4000-8000-000000000001"
        self.collection["collected_at"] = "2026-01-03T01:00:00+00:00"
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update({
            "tenant_id": "demo-org", "project_id": "demo-project",
            "api_contract_verified": True,
            "view": {"verified": True, "evidence": "fictional project view check", "task_equals": {}},
            "owner_names": {"demo-person": "虚构成员"},
            "status_names": {"demo-done": "已解决", "demo-open": "待处理"},
            "resolved_status_ids": ["demo-done"],
        })
        self.key = "synthetic-api-key-for-tests"
        self.secret = {
            "teambition_mcp_token": "synthetic-mcp-token",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=synthetic-webhook-access-token-for-tests",
            "dingtalk_signing_secret": "synthetic-signing-secret",
            "openai_api_key": self.key,
            "delivery_ledger_id": initialize_ledger(self.data_dir / "delivery_ledger.sqlite3"),
        }
        self.secret[MCP_AUTHORIZATION_FIELD] = teambition_mcp_identity(self.secret)
        OpenAIModelCatalog(self.data_dir / "openai_models.json",
                           transport=lambda *_: b'{"data":[{"id":"gpt-6-luna"}]}').refresh(self.key)
        self.fake_client = None

        def factory(tenant_id, *, credentials):
            self.fake_client = FakeTeambition(self.collection, credentials)
            return self.fake_client

        self.client_factory = factory
        self.summarizer = FakeSummarizer()
        self.ding = FakeDingTalk()

    def tearDown(self):
        self.temp.cleanup()

    def workflow(self):
        return DigestWorkflow(self.settings, self.secret, self.data_dir,
                              client_factory=self.client_factory, summarizer=self.summarizer,
                              ding_sender=self.ding)

    def test_run_collects_summarizes_and_sends_without_review(self):
        workflow = self.workflow()
        result = workflow.run(
            datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI),
            datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
            schedule_key="2026-01-02:morning",
        )
        self.assertEqual(result["state"], "sent")
        self.assertEqual(self.summarizer.calls, 1)
        self.assertEqual(len(self.ding.calls), 1)
        self.assertEqual(self.ding.calls[0][0], self.secret["dingtalk_webhook"])
        self.assertEqual(self.fake_client.calls[0][1]["day"], "2026-01-02")
        self.assertEqual(workflow.history.get(result["run_id"])["state"], "sent")
        model_content = json.dumps(self.summarizer.inputs, ensure_ascii=False)
        for credential in self.secret.values():
            self.assertNotIn(credential, model_content)

    def test_duplicate_schedule_key_is_stopped_before_second_collection(self):
        workflow = self.workflow()
        start = datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI)
        end = datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI)
        workflow.run(start, end, schedule_key="2026-01-02:morning")
        with self.assertRaises(WorkflowBlocked):
            workflow.run(start, end, schedule_key="2026-01-02:morning")
        self.assertEqual(len(self.fake_client.calls), 1)
        self.assertEqual(len(self.ding.calls), 1)

    def test_unknown_delivery_result_is_recorded_and_not_retried(self):
        self.ding.result = TimeoutError("synthetic timeout with credential")
        workflow = self.workflow()
        with self.assertRaises(WorkflowBlocked):
            workflow.run(datetime(2026, 1, 2, 0, 0, tzinfo=SHANGHAI),
                         datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
                         schedule_key="2026-01-02:morning")
        run = workflow.history.list_runs()[0]
        self.assertEqual(run["state"], "delivery_unknown")
        self.assertNotIn("credential", run["delivery_note"])
        with self.assertRaises(WorkflowBlocked):
            workflow.run(datetime(2026, 1, 2, 0, 0, tzinfo=SHANGHAI),
                         datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
                         schedule_key="2026-01-02:morning")
        self.assertEqual(len(self.ding.calls), 1)

    def test_invalid_project_verification_stops_before_teambition_and_openai(self):
        self.settings["api_contract_verified"] = False
        with self.assertRaises(WorkflowBlocked):
            self.workflow().run(datetime(2026, 1, 2, 0, 0, tzinfo=SHANGHAI),
                                datetime(2026, 1, 3, 0, 0, tzinfo=SHANGHAI))
        self.assertIsNone(self.fake_client)
        self.assertEqual(self.summarizer.calls, 0)
        self.assertEqual(self.ding.calls, [])

    def test_verified_no_activity_sends_template_report_without_openai_key_or_cache(self):
        self.collection["records"] = []
        self.secret.pop("openai_api_key")
        (self.data_dir / "openai_models.json").unlink()
        workflow = self.workflow()
        result = workflow.run(
            datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI),
            datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
            schedule_key="2026-01-02:morning",
        )
        self.assertEqual(result["report"]["markdown"],
            "**Teambition｜UPCAM 项目任务动态日报**  \n"
            "🕒 时间范围：2026-01-01 16:50 — 2026-01-02 08:50  \n📊 更新 0 项\n")
        self.assertEqual(self.summarizer.calls, 0)
        self.assertEqual(len(self.ding.calls), 1)
        self.assertEqual(self.ding.calls[0][2], result["report"]["markdown"])

    def test_incomplete_query_is_blocked_without_openai_or_dingtalk(self):
        original_factory = self.client_factory

        def incomplete_factory(tenant_id, *, credentials):
            client = original_factory(tenant_id, credentials=credentials)
            def collect(project, **kwargs):
                collection = client.collect(project, **kwargs)
                collection["records"] = []
                collection["pagination_complete"] = False
                return collection
            client.collect = collect
            return client

        self.client_factory = incomplete_factory
        with self.assertRaises(WorkflowBlocked):
            self.workflow().run(
                datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI),
                datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
            )
        self.assertEqual(self.summarizer.calls, 0)
        self.assertEqual(self.ding.calls, [])

    def test_source_validation_failure_is_diagnosed_and_saved_without_raw_content(self):
        self.collection["records"][0]["activities"].append({
            "id": "demo-unknown-event",
            "action": "future.unknown.action",
            "boundToObjectId": "demo-old-task",
            "boundToObjectType": "task",
            "createTime": "2026-01-01T18:00:00Z",
            "content": "synthetic-private-payload",
        })
        workflow = self.workflow()

        with self.assertRaises(WorkflowBlocked) as raised:
            workflow.run(datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI),
                         datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
                         schedule_key="2026-01-02:morning")

        message = str(raised.exception)
        self.assertIn("future.unknown.action", message)
        self.assertIn("demo-old-task", message)
        self.assertIn("未调用 OpenAI", message)
        self.assertNotIn("synthetic-private-payload", message)
        self.assertEqual(self.summarizer.calls, 0)
        self.assertEqual(self.ding.calls, [])
        runs = workflow.history.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], "blocked")
        self.assertIn("future.unknown.action", runs[0]["delivery_note"])
        self.assertNotIn("synthetic-private-payload", runs[0]["delivery_note"])

    def assert_credential_blocked(self, workflow, *, model_calls=0):
        with self.assertRaises(WorkflowBlocked) as raised:
            workflow.run(datetime(2026, 1, 1, 16, 50, tzinfo=SHANGHAI),
                         datetime(2026, 1, 2, 8, 50, tzinfo=SHANGHAI),
                         schedule_key="2026-01-02:morning")
        self.assertEqual(self.summarizer.calls, model_calls)
        self.assertEqual(self.ding.calls, [])
        runs = workflow.history.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], "blocked")
        persisted = json.dumps(workflow.history.get(runs[0]["run_id"]), ensure_ascii=False)
        for value in self.secret.values():
            self.assertNotIn(value, str(raised.exception))
            self.assertNotIn(value, persisted)
        self.assertNotIn("private-evidence-marker", persisted)

    def test_credential_in_task_title_blocks_before_model_and_preserves_evidence(self):
        self.collection["records"][0]["task"]["content"] = (
            "private-evidence-marker " + self.secret["teambition_mcp_token"])
        evidence = deepcopy(self.collection)
        self.assert_credential_blocked(self.workflow())
        self.assertEqual(self.collection, evidence)

    def test_credential_in_comment_blocks_before_model(self):
        comment = {"comment": "private-evidence-marker " + self.secret["openai_api_key"],
                   "attachments": [], "dingFiles": [], "files": [], "renderMode": "text",
                   "isOnlyNotifyMentions": False, "mentions": {}}
        self.collection["records"][0]["activities"][0]["content"] = json.dumps(comment)
        self.assert_credential_blocked(self.workflow())

    def test_encoded_credential_in_unrendered_link_blocks_before_model(self):
        token = self.secret["teambition_mcp_token"]
        escaped = "".join("%{:02x}".format(byte) for byte in token.encode())
        self.collection["records"][0]["task"]["url"] = "https://example.invalid/" + quote(escaped)
        self.assert_credential_blocked(self.workflow())

    def test_credential_in_discovered_owner_name_blocks_before_model(self):
        self.collection["metadata"] = {
            "owner_names": {"new-owner": self.secret["dingtalk_signing_secret"]}}
        self.assert_credential_blocked(self.workflow())

    def test_credential_in_saved_status_name_blocks_before_model(self):
        self.settings["status_names"]["demo-open"] = self.secret["teambition_mcp_token"]
        self.assert_credential_blocked(self.workflow())

    def test_dingtalk_access_token_component_is_also_blocked(self):
        self.secret["dingtalk_webhook"] = (
            "https://oapi.dingtalk.com/robot/send?access_token=synthetic-robot-credential")
        self.collection["records"][0]["task"]["content"] = "synthetic-robot-credential"
        self.assert_credential_blocked(self.workflow())

    def test_credential_in_model_editorial_rules_blocks_before_model(self):
        with patch("automated_digest.load_editorial_rules",
                   return_value=[self.secret["teambition_mcp_token"]]):
            self.assert_credential_blocked(self.workflow())

    def test_credential_in_model_output_blocks_before_history_draft_or_sender(self):
        token = self.secret["teambition_mcp_token"]

        class LeakingSummarizer(FakeSummarizer):
            def summarize(self, *args, **kwargs):
                result = super().summarize(*args, **kwargs)
                return {key: "private-evidence-marker " + token for key in result}

        self.summarizer = LeakingSummarizer()
        self.assert_credential_blocked(self.workflow(), model_calls=1)

    def test_credential_in_model_exception_is_not_returned_or_persisted(self):
        from openai_summarizer import SummaryError
        token = self.secret["teambition_mcp_token"]

        class LeakingSummarizer(FakeSummarizer):
            def summarize(self, *args, **kwargs):
                super().summarize(*args, **kwargs)
                raise SummaryError("private-evidence-marker " + token)

        self.summarizer = LeakingSummarizer()
        self.assert_credential_blocked(self.workflow(), model_calls=1)

    def test_uninspectable_source_blocks_before_model(self):
        self.collection["unrecognized_metadata"] = b"must-not-be-stringified"
        self.assert_credential_blocked(self.workflow())

    def test_credential_detection_failure_blocks_before_model(self):
        self.secret["github_update_token"] = "x" * (129 * 1024)
        self.assert_credential_blocked(self.workflow())

    def test_credential_in_final_markdown_blocks_even_without_model(self):
        from automated_digest import build_automated_digest
        self.collection["records"] = []

        def unsafe_render(*args, **kwargs):
            report = build_automated_digest(*args, **kwargs)
            report["markdown"] += self.secret["teambition_mcp_token"]
            return report

        with patch("upcam_app.workflow.build_automated_digest", side_effect=unsafe_render):
            self.assert_credential_blocked(self.workflow())

    def test_actual_send_callback_checks_markdown_again(self):
        self.collection["records"] = []
        token = self.secret["teambition_mcp_token"]

        class ChangedMarkdownGuard:
            def __init__(self, *args, **kwargs):
                pass

            def attempt(self, report, *, send, **kwargs):
                send(report["markdown"] + token)

            def close(self):
                pass

        workflow = self.workflow()
        workflow.guard_factory = ChangedMarkdownGuard
        self.assert_credential_blocked(workflow)


if __name__ == "__main__":
    unittest.main()
