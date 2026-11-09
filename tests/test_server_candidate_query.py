"""Fictional TQL service tests; no real Teambition calls or credentials."""

import copy
import re
import unittest
from datetime import datetime

from report_contract import task_is_candidate, updated_selection, validate_selection
from collection_batches import MemoryCheckpoint
from teambition_readonly import CollectionError
from test_batched_collection import FakeClock, ProjectTransport, collector, event, task
from test_collection import FakeTransport, arguments, tool_name, client, page
from unittest.mock import patch


DAY = "2026-01-02"
LOWER_BOUND = "2026-01-02T00:00:00+08:00"
EXPECTED_Q = f"updated >= '{LOWER_BOUND}'"


class FictionalTqlService(FakeTransport):
    """Explicit simulated MCP business semantics, not a live contract claim."""

    def __init__(self, tasks):
        super().__init__()
        self.tasks = copy.deepcopy(tasks)

    def business_response(self, name, params):
        if name == 'teambition.project.task-query':
            rows = self.tasks
            if not params['includeArchived']:
                rows = [row for row in rows if not row['isArchived']]
            if 'tql' in params:
                match = re.fullmatch(r"updated >= '([^']+)'", params['tql'])
                if not match:
                    return {'code': 400, 'result': []}
                cutoff = datetime.fromisoformat(match[1].replace('Z', '+00:00'))
                rows = [row for row in rows
                        if datetime.fromisoformat(row['updated'].replace('Z', '+00:00')) >= cutoff]
            offset = int(params.get('pageToken', '0'))
            size = params['pageSize']
            token = str(offset + size) if offset + size < len(rows) else ''
            return page(rows[offset:offset + size], token)
        if name == 'teambition.task.query':
            return {'code': 200, 'result': next(row for row in self.tasks if row['id'] == params['taskId'])}
        if name == 'teambition.task.activity':
            return page([event(params['taskId'])])
        raise AssertionError('Unexpected fictional MCP tool')


class ServerCandidateQueryTests(unittest.TestCase):
    def test_server_tql_matches_local_candidate_semantics_and_paginates_in_both_modes(self):
        rows = [
            task(0, updated="2020-01-01T00:00:00Z"),
            task(1, updated="2026-01-01T15:59:59.999999Z"),
            task(2, updated=LOWER_BOUND),
            task(3, updated="2026-01-01T16:00:00Z"),
            task(4, updated="2026-01-02T10:00:00+08:00"),
            task(5, updated="2026-01-05T10:00:00+08:00"),
            task(6, updated="2026-01-03T10:00:00+08:00", isArchived=True),
        ]
        expected_selection = updated_selection(DAY)
        for batched in (False, True):
            for include_archived in (False, True):
                with self.subTest(batched=batched, archived=include_archived):
                    service = FictionalTqlService(rows)
                    clock = FakeClock()
                    instance = client(service, clock=clock, sleep=clock.sleep, batch_mode=batched)
                    collection = instance.collect("fictional-project", day=DAY, page_size=2,
                                                  include_archived=include_archived)
                    expected = [row["id"] for row in rows
                                if (include_archived or not row["isArchived"])
                                and task_is_candidate(row["updated"], expected_selection)]
                    self.assertEqual(expected, [rows[i]["id"] for i in
                                               ([2, 3, 4, 5, 6] if include_archived else [2, 3, 4, 5])])
                    self.assertEqual([record["task"]["id"] for record in collection["records"]], expected)
                    self.assertEqual(collection["selection"]["q"], EXPECTED_Q)
                    listing = [arguments(request) for request in service.requests
                               if tool_name(request) == "teambition.project.task-query"]
                    self.assertGreaterEqual(len(listing), 4)  # both scans and multiple pages
                    self.assertTrue(all(params.get("tql") == EXPECTED_Q for params in listing))
                    self.assertTrue(all(params.get("includeArchived") is include_archived
                                        for params in listing))
                    self.assertTrue(all("tql" not in arguments(request) for request in service.requests
                                        if tool_name(request) != "teambition.project.task-query"))

    def test_fictional_large_project_avoids_paging_unselected_rows(self):
        rows = [task(i, updated="2020-01-01T00:00:00Z") for i in range(1200)]
        for row in rows[-12:]:
            row["updated"] = "2026-01-05T00:00:00+08:00"
        service = FictionalTqlService(rows)
        clock = FakeClock()
        instance = client(service, clock=clock, sleep=clock.sleep, batch_mode=True)
        result = instance.collect("fictional-project", day=DAY)
        self.assertEqual(len(result["records"]), 12)
        self.assertEqual(result["collection_stats"]["project_tasks_listed"], 12)
        self.assertEqual(result["collection_stats"]["requests"], 29)

    def test_full_audit_still_queries_unfiltered_and_finds_unadvanced_task(self):
        old = task(0, updated="2020-01-01T00:00:00Z")
        service = FictionalTqlService([old])
        clock = FakeClock()
        instance = client(service, clock=clock, sleep=clock.sleep, batch_mode=True)
        result = instance.collect("fictional-project", day=DAY, candidate_mode="full_project")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["activities"], [event(old["id"])])
        self.assertTrue(all("tql" not in arguments(request) for request in service.requests))

    def test_rejected_query_fails_without_unfiltered_fallback(self):
        transport = FakeTransport({"code": 400, "errorMessage": "fictional query rejection"})
        with self.assertRaises(CollectionError):
            client(transport).collect("demo-project", day=DAY)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(arguments(transport.requests[0]).get("tql"), EXPECTED_Q)

    def test_server_ignoring_query_fails_instead_of_silently_scanning_old_rows(self):
        for batched in (False, True):
            with self.subTest(batched=batched):
                project = "fictional-project" if batched else "demo-project"
                old = task(0, updated="2020-01-01T00:00:00Z", projectId=project)
                transport = FakeTransport(page([old]), page([old]))
                if batched:
                    clock = FakeClock()
                    instance = client(transport, clock=clock, sleep=clock.sleep, batch_mode=True)
                else:
                    instance = client(transport)
                with self.assertRaisesRegex(CollectionError, "MCP request failed"):
                    instance.collect(project, day=DAY)
                self.assertEqual(len(transport.requests), 1)

    def test_existing_local_candidate_contract_remains_readable_but_not_new_contract(self):
        previous = updated_selection(DAY)
        previous.update(q=None, candidate_updated_since="2026-01-01T00:00:00+08:00",
                        candidate_overlap_seconds=86400)
        self.assertEqual(validate_selection(previous, DAY), previous)
        self.assertNotEqual(previous, updated_selection(DAY))

    def test_previous_local_only_checkpoint_is_not_reused_for_server_query(self):
        rows = [task(0), task(1)]
        clock, store = FakeClock(), MemoryCheckpoint()
        previous = updated_selection(DAY)
        previous.update(q=None, candidate_updated_since="2026-01-01T00:00:00+08:00",
                        candidate_overlap_seconds=86400)
        first = ProjectTransport(rows, clock)
        first.interrupt_on_detail = rows[1]["id"]
        with patch("teambition_readonly.updated_selection", return_value=previous):
            with self.assertRaises(CollectionError):
                collector(first, clock, checkpoint=store).collect("fictional-project", day=DAY)
        self.assertIn(rows[0]["id"], store.tasks)
        second = ProjectTransport(rows, clock)
        result = collector(second, clock, checkpoint=store).collect("fictional-project", day=DAY)
        self.assertEqual(second.detail_ids, [row["id"] for row in rows])
        self.assertEqual(result["collection_stats"]["resumed_tasks"], 0)

    def test_fractional_window_start_rounds_candidate_cutoff_down_without_widening_report(self):
        start = "2026-01-02T00:00:00.333333+08:00"
        end = "2026-01-02T08:50:00+08:00"
        selection = updated_selection(DAY, start, end)
        self.assertEqual(selection["candidate_updated_since"], LOWER_BOUND)
        self.assertEqual(selection["q"], EXPECTED_Q)
        self.assertEqual(selection["start"], start)
        self.assertEqual(selection["candidate_overlap_seconds"], 0.333333)
        self.assertTrue(task_is_candidate("2026-01-02T00:00:00.100000+08:00", selection))
        self.assertFalse(task_is_candidate("2026-01-01T23:59:59.999999+08:00", selection))
        previous = dict(selection, q=None, candidate_updated_since="2026-01-01T00:00:00.333333+08:00",
                        candidate_overlap_seconds=86400)
        self.assertEqual(validate_selection(previous, DAY, start, end), previous)


if __name__ == "__main__":
    unittest.main()
