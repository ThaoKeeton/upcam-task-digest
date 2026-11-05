"""Deliberate manual repeats use fresh durable claims; schedules never repeat."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from automated_digest import build_automated_digest
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from report_contract import updated_selection
from upcam_app.smoke_data import make_smoke_service
from upcam_app.workflow import WorkflowBlocked


REQUEST_1 = "00000000-0000-4000-8000-000000000001"
REQUEST_2 = "00000000-0000-4000-8000-000000000002"
REQUEST_3 = "00000000-0000-4000-8000-000000000003"


class FakeSummarizer:
    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        return {row["task_key"]: "虚构：已定位导出异常，仍待修复。" for row in rows}


class ManualGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "delivery.sqlite3"
        self.identity = initialize_ledger(self.path)
        fixtures = Path(__file__).parent / "fixtures"
        self.collection = json.loads((fixtures / "collection.json").read_text(encoding="utf-8"))
        self.config = json.loads((fixtures / "config.json").read_text(encoding="utf-8"))
        self.collection.update(batch_id=REQUEST_1, collected_at="2026-01-03T01:00:00+00:00",
                               selection=updated_selection("2026-01-02"))
        self.config.update(api_contract_verified=True,
                           view={"verified": True, "evidence": "fictional-verification", "task_equals": {}})
        self.report = build_automated_digest(self.collection, self.config, "2026-01-02", "synthetic-key",
                                              "gpt-6-luna", "max", client=FakeSummarizer())
        self.scope = {"tenant": "demo-org", "project": "demo-project", "destination": "fictional-destination",
                      "current_collection": self.collection, "current_config": self.config,
                      "current_model": "gpt-6-luna", "current_effort": "max"}
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def guard(self, path=None, identity=None):
        guard = DeliveryGuard(path or self.path, identity or self.identity, persistent_storage_verified=True)
        self.addCleanup(guard.close)
        return guard

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def manual(self, guard, request_id=REQUEST_1, *, send=lambda _body: True, report=None, scope=None):
        return guard.attempt(report or self.report, **(scope or self.scope), send=send,
                             allow_confirmed_repeat=True, manual_request_id=request_id)

    def test_new_manual_requests_resend_confirmed_scope_and_leave_original_delivery_unchanged(self):
        guard = self.guard()
        calls = []
        sender = lambda body: calls.append(body) or True
        guard.attempt(self.report, **self.scope, send=sender)
        original = self.rows("deliveries")

        self.assertEqual(self.manual(guard, REQUEST_1, send=sender), "confirmed")
        self.assertEqual(self.manual(guard, REQUEST_2, send=sender), "confirmed")

        self.assertEqual(len(calls), 3)
        self.assertEqual(self.rows("deliveries"), original)
        attempts = self.rows("manual_attempts")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({row[0] for row in attempts}, {REQUEST_1, REQUEST_2})
        self.assertTrue(all(row[1] == original[0][0] and row[3] == "confirmed" for row in attempts))

    def test_first_manual_request_is_recorded_and_same_id_never_sends_twice(self):
        guard = self.guard()
        calls = []
        self.manual(guard, send=lambda body: calls.append(body) or True)
        original = self.rows("deliveries")
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, send=lambda body: calls.append(body) or True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.rows("manual_attempts")), 1)
        self.assertEqual(self.rows("deliveries"), original)
        self.assertEqual(self.manual(guard, REQUEST_2, send=lambda body: calls.append(body) or True), "confirmed")
        self.assertEqual(len(calls), 2)

    def test_ordinary_delivery_stays_deduplicated_after_manual_repeat(self):
        guard = self.guard()
        self.manual(guard)
        self.manual(guard, REQUEST_2)
        with self.assertRaises(DeliveryBlocked) as raised:
            guard.attempt(self.report, **self.scope, send=lambda _body: self.fail("No ordinary repeat"))
        self.assertEqual(raised.exception.outcome, "blocked")
        self.assertIn("定时", str(raised.exception))

    def test_unknown_original_blocks_all_new_manual_requests_and_keeps_safe_error(self):
        guard = self.guard()
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, send=lambda _body: (_ for _ in ()).throw(TimeoutError("sensitive-response")))
        for request_id in (REQUEST_1, REQUEST_2):
            with self.subTest(request_id=request_id), self.assertRaises(DeliveryBlocked) as raised:
                self.manual(guard, request_id, send=lambda _body: self.fail("Unknown original must block"))
            self.assertEqual(raised.exception.outcome, "unknown")
            self.assertNotIn("sensitive-response", str(raised.exception))
            self.assertIn("不确定", str(raised.exception))
        self.assertEqual(self.rows("deliveries")[0][2], "unknown")
        self.assertEqual(self.rows("manual_attempts")[0][3], "unknown")

    def test_unknown_ordinary_original_cannot_be_bypassed_by_manual_repeat(self):
        guard = self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report, **self.scope, send=lambda _body: False)
        with self.assertRaises(DeliveryBlocked) as raised:
            self.manual(guard, send=lambda _body: self.fail("Unknown original must block"))
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertEqual(self.rows("manual_attempts"), [])

    def test_unknown_repeat_blocks_manual_and_ordinary_while_original_stays_confirmed(self):
        guard = self.guard()
        guard.attempt(self.report, **self.scope, send=lambda _body: True)
        original = self.rows("deliveries")
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, send=lambda _body: False)
        reopened = self.guard()
        for options in ({}, {"allow_confirmed_repeat": True, "manual_request_id": REQUEST_2}):
            with self.subTest(options=options), self.assertRaises(DeliveryBlocked) as raised:
                reopened.attempt(self.report, **self.scope,
                                  send=lambda _body: self.fail("Unknown repeat must block"), **options)
            self.assertEqual(raised.exception.outcome, "unknown")
        self.assertEqual(self.rows("deliveries"), original)
        self.assertEqual(self.rows("manual_attempts")[0][3], "unknown")

    def test_every_sender_observes_committed_initial_and_repeat_claims(self):
        guard = self.guard()
        for request_id in (REQUEST_1, REQUEST_2):
            def sender(_body):
                attempts = self.rows("manual_attempts")
                claimed = next(row for row in attempts if row[0] == request_id)
                self.assertEqual(claimed[3], "in_flight")
                other = self.guard()
                with self.assertRaises(DeliveryBlocked) as raised:
                    self.manual(other, REQUEST_3, send=lambda _body: self.fail("No concurrent send"))
                self.assertEqual(raised.exception.outcome, "unknown")
                return True
            self.assertEqual(self.manual(guard, request_id, send=sender), "confirmed")

    def test_concurrent_connections_cannot_claim_repeat_while_sender_is_running(self):
        self.guard().attempt(self.report, **self.scope, send=lambda _body: True)
        entered, release = threading.Event(), threading.Event()
        sent = []

        def first():
            guard = DeliveryGuard(self.path, self.identity, persistent_storage_verified=True)
            try:
                def sender(body):
                    sent.append(body)
                    entered.set()
                    return release.wait(5)
                return self.manual(guard, REQUEST_1, send=sender)
            finally:
                guard.close()

        def second():
            guard = DeliveryGuard(self.path, self.identity, persistent_storage_verified=True)
            try:
                with self.assertRaises(DeliveryBlocked) as raised:
                    self.manual(guard, REQUEST_2, send=lambda body: sent.append(body) or True)
                return raised.exception.outcome
            finally:
                guard.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(first)
            try:
                self.assertTrue(entered.wait(5), "First sender must start")
                self.assertEqual(pool.submit(second).result(timeout=5), "unknown")
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5), "confirmed")
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.rows("manual_attempts")), 1)

    def test_manual_request_id_cannot_be_reused_for_another_destination(self):
        guard = self.guard()
        self.manual(guard)
        scope = dict(self.scope, destination="another-fictional-destination")
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, scope=scope, send=lambda _body: self.fail("Request ID cannot change scope"))
        self.assertEqual(len(self.rows("deliveries")), 1)

    def test_repeat_permission_does_not_bypass_independent_provenance_validation(self):
        guard = self.guard()
        guard.attempt(self.report, **self.scope, send=lambda _body: True)
        forged = deepcopy(self.report)
        forged["markdown"] = "forged-report"
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, report=forged, send=lambda _body: self.fail("Must revalidate provenance"))
        self.assertEqual(self.rows("manual_attempts"), [])

    def test_repeat_options_reject_malformed_flags_and_nonlocal_request_identifiers_before_claim(self):
        guard = self.guard()
        options = [(True, None), (False, REQUEST_1), (1, REQUEST_1), ("true", REQUEST_1),
                   (None, REQUEST_1), (True, ""), (True, "not-a-uuid"), (True, {}),
                   (True, REQUEST_1.replace("4000", "1000")), (True, str(uuid.UUID(int=0)))]
        for allowed, request_id in options:
            with self.subTest(allowed=allowed, request_id=request_id), self.assertRaises(DeliveryBlocked):
                guard.attempt(self.report, **self.scope, send=lambda _body: self.fail("Malformed repeat option"),
                              allow_confirmed_repeat=allowed, manual_request_id=request_id)
        self.assertEqual(self.rows("deliveries"), [])
        self.assertEqual(self.rows("manual_attempts"), [])

    def test_existing_ledger_migrates_additively_without_changing_original_rows_or_identity(self):
        self.guard().attempt(self.report, **self.scope, send=lambda _body: True)
        original = self.rows("deliveries")
        legacy = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript("CREATE TABLE metadata (identity TEXT NOT NULL);"
                             "CREATE TABLE deliveries (key TEXT PRIMARY KEY, digest_hash TEXT NOT NULL, state TEXT NOT NULL);")
            db.execute("INSERT INTO metadata VALUES (?)", (self.identity,))
            db.executemany("INSERT INTO deliveries VALUES (?, ?, ?)", original)

        guard = self.guard(legacy)
        self.assertEqual(self.manual(guard), "confirmed")

        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute("SELECT * FROM metadata").fetchall(), [(self.identity,)])
            self.assertEqual(db.execute("SELECT * FROM deliveries").fetchall(), original)
            self.assertEqual(db.execute("SELECT request_id, state FROM manual_attempts").fetchall(),
                             [(REQUEST_1, "confirmed")])

    def test_repeat_claim_commit_failure_prevents_sender(self):
        guard = self.guard()
        guard.attempt(self.report, **self.scope, send=lambda _body: True)
        guard.db.execute("PRAGMA query_only=ON")
        with self.assertRaises(DeliveryBlocked):
            self.manual(guard, send=lambda _body: self.fail("Uncommitted claim cannot send"))
        self.assertEqual(self.rows("manual_attempts"), [])

    def test_repeat_post_send_commit_failure_preserves_inflight_claim_and_original(self):
        guard = self.guard()
        guard.attempt(self.report, **self.scope, send=lambda _body: True)
        original = self.rows("deliveries")

        def sender(_body):
            guard.db.execute("PRAGMA query_only=ON")
            return True

        with self.assertRaises(DeliveryBlocked) as raised:
            self.manual(guard, send=sender)
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertEqual(self.rows("deliveries"), original)
        self.assertEqual(self.rows("manual_attempts")[0][3], "in_flight")
        with self.assertRaises(DeliveryBlocked):
            self.manual(self.guard(), REQUEST_2, send=lambda _body: self.fail("Inflight claim survives restart"))


class ManualWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = make_smoke_service(Path(temporary.name))
        end = datetime.now().replace(second=0, microsecond=0)
        self.start, self.end = end - timedelta(hours=2), end
        self.window = {"start": self.start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M")}
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        initial = self.service.dispatch({"op": "initialize"})
        self.service.dispatch({"op": "save", "payload": {
            "settings": initial["settings"], "verification_window": self.window}})
        self.service.dispatch({"op": "refresh_models"})

    def manual(self, **payload):
        return self.service.dispatch({"op": "run_manual", "payload": {**self.window, **payload}})

    def workflow(self):
        return self.service.workflow_factory(self.service.settings_store.load(), self.service.secret_store.load(),
                                             self.service.data_dir, history=self.service.history,
                                             model_catalog=self.service.model_catalog)

    def test_worker_repeated_manual_clicks_send_directly_and_retain_both_history_records(self):
        first = self.manual()
        original = self.service.history.get(first["run_id"])
        second = self.manual()

        self.assertEqual((first["state"], second["state"]), ("sent", "sent"))
        self.assertNotEqual(first["manual_request_id"], second["manual_request_id"])
        self.assertEqual(str(uuid.UUID(first["manual_request_id"])), first["manual_request_id"])
        self.assertEqual(self.service.history.get(first["run_id"]), original)
        self.assertEqual(len(self.service.history.list_runs()), 2)
        self.assertNotIn("allow_confirmed_repeat", self.service.settings_store.load())

    def test_worker_replay_of_same_manual_id_is_idempotent_and_keeps_original_sent_history(self):
        first = self.manual(manual_request_id=REQUEST_1)
        with self.assertRaises(ValueError):
            self.manual(manual_request_id=REQUEST_1)
        self.assertEqual(self.service.history.get(first["run_id"])["state"], "sent")
        self.assertEqual(sum(run["state"] == "sent" for run in self.service.history.list_runs()), 1)

    def test_worker_rejects_malformed_ids_or_unapproved_controls_before_workflow(self):
        with patch.object(self.service, "workflow_factory", side_effect=AssertionError("Invalid manual request cannot run")):
            cases = [{"manual_request_id": value} for value in (None, "", {}, 1, "invalid-request-id")]
            cases += [{"allow_confirmed_repeat": True}, {"schedule_key": "fake-slot"},
                      {"report_origin": "manual"}]
            for payload in cases:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.manual(**payload)
        self.assertEqual(self.service.history.list_runs(), [])

    def test_workflow_rejects_manual_repeat_options_for_schedule_before_collection(self):
        workflow = self.workflow()
        with patch.object(workflow, "client_factory", side_effect=AssertionError("No invalid scheduled collection")):
            with self.assertRaises(WorkflowBlocked):
                workflow.run(self.start, self.end, schedule_key="fictional-scheduled-slot",
                             allow_confirmed_repeat=True, manual_request_id=REQUEST_1)
        self.assertEqual(self.service.history.list_runs(), [])

    def test_direct_workflow_and_scheduled_runs_keep_ordinary_deduplication(self):
        workflow = self.workflow()
        workflow.run(self.start, self.end)
        for options in ({}, {"schedule_key": "fictional-scheduled-slot"}):
            with self.subTest(options=options), self.assertRaises(WorkflowBlocked):
                self.workflow().run(self.start, self.end, **options)
        self.assertEqual(sum(run["state"] == "sent" for run in self.service.history.list_runs()), 1)

    def test_manual_repeat_still_recollects_and_rejects_incomplete_source_without_sending(self):
        first = self.manual(manual_request_id=REQUEST_1)
        original_client = self.workflow().client_factory

        def incomplete_client(*args, **kwargs):
            client = original_client(*args, **kwargs)
            collect = client.collect

            def incomplete(*args, **kwargs):
                result = collect(*args, **kwargs)
                result["pagination_complete"] = False
                return result

            client.collect = incomplete
            return client

        workflow = self.workflow()
        workflow.client_factory = incomplete_client
        with patch.object(workflow.ding_sender, "send_markdown", side_effect=AssertionError("No incomplete repeat")):
            with self.assertRaises(WorkflowBlocked):
                workflow.run(self.start, self.end, allow_confirmed_repeat=True, manual_request_id=REQUEST_2)
        self.assertEqual(self.service.history.get(first["run_id"])["state"], "sent")
        self.assertEqual(sum(run["state"] == "sent" for run in self.service.history.list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
