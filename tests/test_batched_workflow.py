"""End-to-end large-project workflow with synthetic GET/model/sender boundaries."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from collection_batches import BatchPolicy, MemoryCheckpoint
from delivery_guard import initialize_ledger
from report_contract import SHANGHAI
from teambition_readonly import Client
from upcam_app.config import DEFAULT_SETTINGS
from upcam_app.workflow import DigestWorkflow, WorkflowBlocked
import test_batched_collection as samples


class ModelBoundary:
    def __init__(self):
        self.calls = 0

    def summarize(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("No comment text needs a model")


class SenderBoundary:
    def __init__(self):
        self.calls = []

    def send_markdown(self, webhook, secret, markdown, **options):
        self.calls.append(markdown)
        return True  # Synthetic provider acknowledgement, without a transport.


class BatchedWorkflowTests(unittest.TestCase):
    def setup_workflow(self, directory, *, policy=None):
        tasks = [samples.task(i, updated="2020-01-01T00:00:00Z", uniqueId=i + 1,
                    executorId="fictional-owner", tfsId="fictional-open", isDone=False,
                    project={"_id": "fictional-project", "uniqueIdPrefix": "SYNTH"})
                 for i in range(1201)]
        histories = {task["id"]: [[]] for task in tasks}
        histories[tasks[-1]["id"]] = [[samples.event(tasks[-1]["id"], action="create", content="")]]
        clock = samples.FakeClock()
        transport = samples.ProjectTransport(tasks, clock, histories=histories)
        progress = MemoryCheckpoint()

        def factory(tenant_id, *, credentials):
            return Client(tenant_id, credentials=credentials, transport=transport,
                          sleep=clock.sleep, clock=clock, batch_mode=True,
                          checkpoint=progress, batch_policy=policy or BatchPolicy())

        settings = dict(DEFAULT_SETTINGS, tenant_id="fictional-tenant", project_id="fictional-project",
                        api_contract_verified=True,
                        view={"verified": True, "evidence": "synthetic test scope", "task_equals": {}},
                        owner_names={"fictional-owner": "虚构负责人"},
                        status_names={"fictional-open": "待处理"}, resolved_status_ids=[])
        credentials = {"teambition_app_id": "fictional-app", "teambition_app_secret": "fictional-secret",
                       "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=fictional-only",
                       "dingtalk_signing_secret": "fictional-signing",
                       "delivery_ledger_id": initialize_ledger(Path(directory) / "delivery_ledger.sqlite3")}
        model, sender = ModelBoundary(), SenderBoundary()
        workflow = DigestWorkflow(settings, credentials, Path(directory), client_factory=factory,
                                  summarizer=model, ding_sender=sender)
        return workflow, model, sender, transport

    def run_workflow(self, workflow):
        return workflow.run(datetime(2026, 1, 2, tzinfo=SHANGHAI),
                            datetime(2026, 1, 3, tzinfo=SHANGHAI))

    def test_1201_complete_task_scan_reaches_source_revalidation_and_fake_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, model, sender, transport = self.setup_workflow(directory)
            result = self.run_workflow(workflow)
            self.assertEqual(result["state"], "sent")
            self.assertEqual(len(transport.detail_ids), 1201)
            self.assertEqual(model.calls, 0)
            self.assertEqual(len(sender.calls), 1)
            self.assertEqual(len(result["report"]["digest"]["tasks"]), 1)
            self.assertEqual(result["report"]["digest"]["tasks"][0]["category"], "new")
            self.assertIn("📊 更新 1 项", sender.calls[0])

    def test_incomplete_large_scan_never_calls_model_or_sender_or_creates_zero_report(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow, model, sender, _transport = self.setup_workflow(
                directory, policy=replace(BatchPolicy(), max_requests=1))
            with self.assertRaises(WorkflowBlocked):
                self.run_workflow(workflow)
            self.assertEqual(model.calls, 0)
            self.assertEqual(sender.calls, [])
            history = workflow.history.list_runs(10)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["state"], "blocked")
            self.assertNotIn("📊 更新 0 项", history[0]["digest_markdown"])


if __name__ == "__main__":
    unittest.main()
