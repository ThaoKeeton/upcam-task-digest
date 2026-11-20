"""Synthetic MCP budget, request gate and source binding regressions."""

import unittest

from collection_batches import BatchPolicy, SessionBudget, _counters
from teambition_readonly import BatchBudgetExceeded, Client, CollectionError


class FakeAdapter:
    def __init__(self): self.arguments = None
    def activity_page(self, task_id, **kwargs):
        self.arguments = (task_id, kwargs)
        return {'result': [], 'nextPageToken': ''}


class McpBudgetBridgeTests(unittest.TestCase):
    def test_activity_project_binding_is_forwarded(self):
        adapter = FakeAdapter()
        client = Client('org', credentials=lambda: 'fictional', mcp_adapter_factory=lambda owner: adapter)
        client.get('task.activities', {'taskId': 'task', 'projectId': 'project', 'pageSize': 5})
        self.assertEqual(adapter.arguments[1]['project_id'], 'project')

    def test_uncertain_read_bytes_count_against_batch_even_on_failure(self):
        client = Client('org', max_response_bytes=4096)
        client._after_mcp(None, {'completed': False, 'uncertain': True, 'response_bytes': 2000})
        self.assertEqual(client.response_bytes, 2000)
        with self.assertRaises(BatchBudgetExceeded):
            client._after_mcp(None, {'completed': False, 'uncertain': True, 'response_bytes': 3000})
        self.assertEqual(client.response_bytes, 5000)

    def test_no_usable_response_budget_does_not_charge_unsent_request(self):
        counters = _counters()
        budget = SessionBudget(BatchPolicy(max_response_bytes=1), counters,
                               created_at=0, now=0, clock=lambda: 0, save=lambda _: None)
        client = Client('org', shared_budget=budget, clock=lambda: 0)
        with self.assertRaises(CollectionError):
            client._before_mcp({'method': 'initialize', 'response_limit': 8192})
        self.assertEqual(budget.counters['requests'], 0)
        self.assertEqual(budget.counters['records'], 0)
        self.assertEqual(budget.counters['response_bytes'], 0)

    def test_progress_rpc_occurs_before_final_rate_gate_admission(self):
        events = []
        class Budget:
            def check_time(self): pass
            def plan_request(self, params, **kwargs):
                return {'params': params, 'body_limit': 10000, 'records': 1, 'response_bytes': 10001}
            def before_request(self, reservation): pass
            @property
            def counters(self):
                events.append('progress_rpc')
                return {'requests': 1}
        class Gate:
            def check_cancelled(self): pass
            def acquire(self, **kwargs): events.append('admitted')
        client = Client('org', shared_budget=Budget(), request_gate=Gate(), clock=lambda: 0)
        client._before_mcp({'method': 'initialize', 'response_limit': 8192})
        self.assertNotIn('admitted', events)
        client._dispatch_mcp({'timeout': 30})
        self.assertLess(events.index('progress_rpc'), events.index('admitted'))

    def test_deadline_is_rechecked_just_before_dispatch(self):
        now = [0]
        client = Client('org', max_seconds=1, clock=lambda: now[0])
        client._before_mcp({'method': 'initialize', 'response_limit': 1024})
        now[0] = 2
        with self.assertRaises(BatchBudgetExceeded):
            client._dispatch_mcp({'timeout': 30})

    def test_completed_response_after_deadline_is_not_success(self):
        client = Client('org', max_seconds=1, clock=lambda: 2)
        client.started = 0
        with self.assertRaises(BatchBudgetExceeded):
            client._after_mcp(None, {'completed': True, 'uncertain': False,
                                      'response_bytes': 15, 'result_count': 0})
        self.assertEqual(client.response_bytes, 15)

    def test_uncertain_response_after_deadline_reports_time_budget(self):
        client = Client('org', max_seconds=1, clock=lambda: 2)
        client.started = 0
        with self.assertRaises(BatchBudgetExceeded):
            client._after_mcp(None, {'dispatched': True, 'completed': False, 'uncertain': True,
                                      'response_bytes': 15})

    def test_remaining_row_budget_shrinks_the_next_page(self):
        budget = SessionBudget(BatchPolicy(max_records=2), _counters(), created_at=0,
                               now=0, clock=lambda: 0, save=lambda _: None)
        client = Client('org', shared_budget=budget, clock=lambda: 0)
        reservation = client._before_mcp({'method': 'tools/call', 'tool_name': 'teambition.task.activity',
                                          'pageSize': 3, 'response_limit': 1024})
        self.assertEqual(reservation['_mcp_page_size'], 2)
        self.assertEqual(budget.counters['requests'], 1)
        self.assertEqual(budget.counters['records'], 2)

    def test_remaining_byte_budget_shrinks_the_next_read(self):
        budget = SessionBudget(BatchPolicy(max_response_bytes=4096), _counters(), created_at=0,
                               now=0, clock=lambda: 0, save=lambda _: None)
        client = Client('org', shared_budget=budget, clock=lambda: 0)
        reservation = client._before_mcp({'method': 'initialize', 'response_limit': 8192})
        self.assertEqual(reservation['_mcp_response_limit'], 4095)
        self.assertEqual(budget.counters['response_bytes'], 4096)

    def test_session_reserves_only_smaller_per_batch_byte_limit(self):
        budget = SessionBudget(BatchPolicy(), _counters(), created_at=0,
                               now=0, clock=lambda: 0, save=lambda _: None)
        client = Client('org', max_response_bytes=4096, shared_budget=budget, clock=lambda: 0)
        reservation = client._before_mcp({'method': 'initialize', 'response_limit': 8192})
        self.assertEqual(reservation['_mcp_response_limit'], 4095)
        self.assertEqual(budget.counters['response_bytes'], 4096)

    def test_proven_unsent_request_releases_its_durable_reservation(self):
        budget = SessionBudget(BatchPolicy(), _counters(), created_at=0,
                               now=0, clock=lambda: 0, save=lambda _: None)
        client = Client('org', shared_budget=budget, clock=lambda: 0)
        reservation = client._before_mcp({'method': 'initialize', 'response_limit': 1024})
        client._after_mcp(reservation, {'dispatched': False, 'completed': False,
                                        'uncertain': True, 'response_bytes': 0})
        self.assertEqual(budget.counters['requests'], 0)
        self.assertEqual(budget.counters['records'], 0)
        self.assertEqual(budget.counters['response_bytes'], 0)

    def test_reservation_write_that_exceeds_deadline_prevents_admission(self):
        now = [0]
        def save(_): now[0] = 2
        budget = SessionBudget(BatchPolicy(max_seconds=1), _counters(), created_at=0,
                               now=0, clock=lambda: now[0], save=save)
        class Gate:
            admitted = False
            def check_cancelled(self): pass
            def acquire(self, **kwargs): self.admitted = True
        gate = Gate()
        client = Client('org', shared_budget=budget, request_gate=gate, clock=lambda: now[0])
        with self.assertRaises(CollectionError):
            client._before_mcp({'method': 'initialize', 'response_limit': 1024})
        self.assertFalse(gate.admitted)
        self.assertEqual(budget.counters['requests'], 0)
