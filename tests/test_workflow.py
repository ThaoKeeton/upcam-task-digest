import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from delivery_guard import initialize_ledger
from report_contract import SHANGHAI, updated_selection
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.openai_models import OpenAIModelCatalog
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        self.calls += 1
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
            "teambition_app_id": "synthetic-app-id",
            "teambition_app_secret": "synthetic-app-secret",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=synthetic",
            "dingtalk_signing_secret": "synthetic-signing-secret",
            "openai_api_key": self.key,
            "delivery_ledger_id": initialize_ledger(self.data_dir / "delivery_ledger.sqlite3"),
        }
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


if __name__ == "__main__":
    unittest.main()
