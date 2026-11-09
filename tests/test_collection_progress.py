"""Offline progress contract: counts only, request-scoped and opt-in."""
import io
import json
import unittest
from unittest.mock import patch

from test_batched_collection import FakeClock, ProjectTransport, collector, task
from upcam_app.worker_api import serve


class CollectionProgressTests(unittest.TestCase):
    def test_slow_valid_activity_pages_keep_progress_alive_before_task_completion(self):
        from collection_progress import observe_collection
        clock = FakeClock()
        row = task(0)
        source = ProjectTransport([row], clock, histories={row["id"]: [[] for _ in range(7)]})
        observed = []

        def transport(request, timeout):
            clock.now += 20
            return source(request, timeout)

        with observe_collection(lambda event: observed.append((clock(), event))):
            result = collector(transport, clock).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(result["records"]), 1)
        intervals = [right[0] - left[0] for left, right in zip(observed, observed[1:])]
        self.assertLessEqual(max(intervals), 30, "A valid slow task would trip the native silence watchdog")

    def test_batched_collection_emits_counts_without_project_content(self):
        from collection_progress import observe_collection
        observed = []
        clock = FakeClock()
        source = ProjectTransport([task(index) for index in range(3)], clock)
        with observe_collection(observed.append):
            result = collector(source, clock).collect("fictional-project", day="2026-01-02")
        self.assertEqual(len(result["records"]), 3)
        phases = {event["phase"] for event in observed}
        self.assertTrue({"starting", "listing", "collecting", "reconciling"}.issubset(phases))
        self.assertTrue(any(event["completed"] == 3 and event["total"] == 3 for event in observed))
        for event in observed:
            self.assertEqual(set(event), {"phase", "completed", "total", "requests", "batches", "passes"})
            for key, value in event.items():
                if key != "phase":
                    self.assertIs(type(value), int)
                    self.assertGreaterEqual(value, 0)
        self.assertNotIn("fictional", json.dumps(observed))
        self.assertNotIn("Task 0", json.dumps(observed))

    def test_observer_is_removed_after_an_exception(self):
        from collection_progress import observe_collection, report_collection
        observed = []
        with self.assertRaises(ValueError):
            with observe_collection(observed.append):
                raise ValueError("synthetic")
        report_collection("starting")
        self.assertEqual(observed, [])

    def test_progress_frames_are_opt_in_and_correlated(self):
        class Service:
            def dispatch(self, request):
                from collection_progress import report_collection
                report_collection("starting")
                report_collection("collecting", completed=2, total=3, requests=4, batches=1)
                return {"done": True}
        for enabled in (False, True):
            output = io.StringIO()
            serve(io.StringIO(json.dumps({"id": "safe-id", "op": "diagnose", "payload": {},
                                          "progress": enabled}) + "\n"), output, service=Service())
            frames = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(frames[-1], {"id": "safe-id", "ok": True, "result": {"done": True}})
            if enabled:
                self.assertEqual([frame["progress"]["phase"] for frame in frames[:-1]],
                                 ["starting", "collecting"])
                self.assertTrue(all(frame["id"] == "safe-id" and frame["kind"] == "progress" for frame in frames[:-1]))
            else:
                self.assertEqual(len(frames), 1)

    def test_progress_cannot_be_enabled_by_non_boolean_or_for_delivery(self):
        class Service:
            def dispatch(self, request):
                from collection_progress import report_collection
                report_collection("starting")
                return {}
        for op, value in (("diagnose", "true"), ("run_manual", True), ("run_due", True)):
            output = io.StringIO()
            serve(io.StringIO(json.dumps({"id": "safe-id", "op": op, "payload": {}, "progress": value}) + "\n"),
                  output, service=Service())
            self.assertEqual(len(output.getvalue().splitlines()), 1)
