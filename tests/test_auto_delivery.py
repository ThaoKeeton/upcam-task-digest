import json
from copy import deepcopy
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from automated_digest import build_automated_digest
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from report_contract import SHANGHAI, updated_selection


class FakeSummarizer:
    def summarize(self, api_key, model, effort, rows, editorial_rules, service_tier="default"):
        return {row["task_key"]: "虚构：已定位导出异常，仍待修复。" for row in rows}


class AutomatedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp.name) / "delivery.sqlite3"
        self.identity = initialize_ledger(self.ledger)
        fixtures = Path(__file__).parent / "fixtures"
        self.collection = json.loads((fixtures / "collection.json").read_text(encoding="utf-8"))
        self.config = json.loads((fixtures / "config.json").read_text(encoding="utf-8"))
        self.collection.update(
            batch_id="00000000-0000-4000-8000-000000000001",
            collected_at="2026-01-03T01:00:00+00:00",
            selection=updated_selection("2026-01-02"),
        )
        self.config.update(
            api_contract_verified=True,
            view={"verified": True, "evidence": "fictional verification", "task_equals": {}},
        )
        self.model = "gpt-6-luna"
        self.effort = "max"
        self.report = build_automated_digest(
            self.collection, self.config, "2026-01-02", "synthetic-key", self.model, self.effort,
            client=FakeSummarizer(),
        )
        self.scope = {
            "tenant": "demo-org",
            "project": "demo-project",
            "destination": "fictional-destination",
            "current_collection": self.collection,
            "current_config": self.config,
            "current_model": self.model,
            "current_effort": self.effort,
        }

    def tearDown(self):
        self.temp.cleanup()

    def guard(self):
        return DeliveryGuard(self.ledger, self.identity, persistent_storage_verified=True)

    def states(self):
        with closing(sqlite3.connect(self.ledger)) as db:
            return db.execute("SELECT state FROM deliveries ORDER BY key").fetchall()

    def test_automated_report_sends_without_human_review_and_deduplicates(self):
        calls = []
        guard = self.guard()
        self.assertEqual(
            guard.attempt(self.report, **self.scope, send=lambda body: calls.append(body) or True),
            "confirmed",
        )
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report, **self.scope, send=lambda body: calls.append(body) or True)
        guard.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.states(), [("confirmed",)])

    def test_unconfirmed_send_is_unknown_and_never_retried(self):
        calls = []
        guard = self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report, **self.scope,
                          send=lambda body: calls.append(body) or (_ for _ in ()).throw(TimeoutError("synthetic")))
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report, **self.scope, send=lambda body: calls.append(body) or True)
        guard.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.states(), [("unknown",)])

    def test_same_report_day_morning_and_afternoon_windows_have_independent_claims(self):
        guard = self.guard()
        for label, start_hour, end_hour, event_hour, update_hour in (
                ("morning", 8, 10, 9, 9), ("afternoon", 14, 16, 15, 15)):
            collection = deepcopy(self.collection)
            start = datetime(2026, 1, 2, start_hour, 0, tzinfo=SHANGHAI)
            end = datetime(2026, 1, 2, end_hour, 0, tzinfo=SHANGHAI)
            collection["selection"] = updated_selection("2026-01-02", start, end)
            task = collection["records"][0]["task"]
            task["updated"] = f"2026-01-02T{update_hour:02d}:30:00+08:00"
            event = deepcopy(collection["records"][0]["activities"][0])
            event.update(
                id=f"demo-{label}-event",
                createTime=f"2026-01-02T{event_hour:02d}:00:00+08:00",
                content=json.dumps({"comment": f"fictional {label} change", "attachments": [],
                                    "dingFiles": [], "files": [], "renderMode": "text",
                                    "isOnlyNotifyMentions": False, "mentions": {}}, ensure_ascii=False),
            )
            collection["records"][0]["activities"] = [event]
            report = build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                            self.model, self.effort, client=FakeSummarizer())
            scope = dict(self.scope, current_collection=collection)
            self.assertEqual(guard.attempt(report, **scope, send=lambda _: True), "confirmed")
        guard.close()
        self.assertEqual(self.states(), [("confirmed",), ("confirmed",)])


if __name__ == "__main__":
    unittest.main()
