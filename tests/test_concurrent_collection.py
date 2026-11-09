"""Fictional concurrent transports only; no real credentials or network."""
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from collection_batches import MemoryCheckpoint
from teambition_readonly import Client, CollectionError
from test_batched_collection import (FakeClock, task, event, ProjectTransport,
                                     PROJECT_TOOL, DETAIL_TOOL, ACTIVITY_TOOL,
                                     fictional_envelope, fictional_archive)


class OwnerCheckpoint(MemoryCheckpoint):
    def __init__(self):
        super().__init__()
        self.owner = threading.get_ident()
        self.thread_ids = []

    def save_counters(self, value):
        self.thread_ids.append(threading.get_ident())
        assert threading.get_ident() == self.owner, 'SQLite owner crossed threads'
        return super().save_counters(value)

    def save_task(self, *args):
        assert threading.get_ident() == self.owner, 'SQLite owner crossed threads'
        return super().save_task(*args)


class ConcurrentTransport(ProjectTransport):
    """Real MCP transport boundary with bounded concurrent fictional responses."""
    def __init__(self, rows, *, latency=.01, histories=None):
        super().__init__(rows, time.monotonic, histories=histories)
        self.latency = latency
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def __call__(self, request, timeout):
        started = time.monotonic()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            return super().__call__(request, timeout, _started=started)
        finally:
            with self.lock:
                self.active -= 1

    def business(self, operation, params):
        if operation != PROJECT_TOOL:
            time.sleep(self.latency)
        return super().business(operation, params)


def collect(transport, *, workers=4, checkpoint=None, **options):
    return Client('fictional-tenant', transport=transport,
                  credentials=lambda: 'fictional-token',
                  mcp_envelope_validator=fictional_envelope, archive_contract_validator=fictional_archive,
                  batch_mode=True, concurrent_tasks=workers, checkpoint=checkpoint,
                  **options).collect('fictional-project', day='2026-01-02')


class ConcurrentCollectionTests(unittest.TestCase):
    def fast_gate(self):
        from collection_concurrency import RequestGate
        return patch('collection_concurrency.RequestGate',
                     side_effect=lambda **kwargs: RequestGate(**dict(kwargs, interval=.002)))

    def test_reads_overlap_with_bounded_pool_and_owner_only_state(self):
        rows = [task(i) for i in range(9)]
        transport = ConcurrentTransport(rows, latency=.03)
        checkpoint = OwnerCheckpoint()
        with self.fast_gate():
            result = collect(transport, checkpoint=checkpoint)
        self.assertGreater(transport.peak, 1)
        self.assertLessEqual(transport.peak, 4)
        self.assertEqual([record['task']['id'] for record in result['records']],
                         [row['id'] for row in rows])
        self.assertEqual(len(transport.requests), 35)
        self.assertEqual(set(checkpoint.thread_ids), {checkpoint.owner})

    def test_updated_candidates_and_concurrency_match_serial_records(self):
        rows = [task(i, updated='2020-01-01T00:00:00Z') for i in range(40)]
        for row in rows[-4:]:
            row['updated'] = '2026-01-05T12:00:00+08:00'
        serial = collect(ConcurrentTransport(rows), workers=1)
        transport = ConcurrentTransport(rows)
        with self.fast_gate():
            parallel = collect(transport)
        self.assertEqual(parallel['records'], serial['records'])
        self.assertEqual(len(parallel['records']), 4)
        self.assertEqual(parallel['collection_stats']['project_tasks_listed'], 4)
        self.assertEqual(parallel['collection_stats']['requests'], 25)
        detail_ids = [params['taskId'] for path, params, _ in transport.requests
                      if path == DETAIL_TOOL]
        self.assertEqual(set(detail_ids), {row['id'] for row in rows[-4:]})

    def test_each_task_cursor_is_serial_and_identical_duplicates_are_removed(self):
        rows = [task(i) for i in range(4)]
        histories = {row['id']: [[event(row['id'], 0)], [event(row['id'], 0), event(row['id'], 1)]]
                     for row in rows}
        transport = ConcurrentTransport(rows, histories=histories)
        with self.fast_gate():
            result = collect(transport)
        self.assertTrue(result['pagination_complete'])
        for record in result['records']:
            self.assertEqual([row['id'] for row in record['activities']],
                             [event(record['task']['id'], n)['id'] for n in (0, 1)])
            calls = [params.get('pageToken', '0') for path, params, _ in transport.requests
                     if path == ACTIVITY_TOOL and params["taskId"] == record["task"]["id"]]
            self.assertEqual(calls, ['0', '1'])

    def test_conflicting_activity_aborts_without_partial_success(self):
        row = task(0)
        transport = ConcurrentTransport([row], histories={row['id']: [
            [event(row['id'], 0)], [event(row['id'], 0, rawActivity={'conflict': True})]]})
        with self.fast_gate(), self.assertRaisesRegex(CollectionError, 'Conflicting duplicate'):
            collect(transport)

    def test_external_cancellation_stops_new_admissions_and_preserves_reservations(self):
        cancelled = threading.Event()
        transport = ConcurrentTransport([task(i) for i in range(8)], latency=.01)
        original = transport.__call__
        def cancelling(request, timeout):
            result = original(request, timeout)
            if json.loads(request.data).get('params', {}).get('name') == DETAIL_TOOL:
                cancelled.set()
            return result
        with self.assertRaisesRegex(CollectionError, 'cancel'):
            collect(cancelling, cancel_event=cancelled)
        business = [(name, params) for name, params, _ in transport.requests
                    if name in (PROJECT_TOOL, DETAIL_TOOL, ACTIVITY_TOOL)]
        self.assertEqual(business[0][0], PROJECT_TOOL)
        self.assertLessEqual(sum(name == DETAIL_TOOL for name, _ in business), 4)
        self.assertFalse(any(name == ACTIVITY_TOOL for name, _ in business))
        self.assertLessEqual(len(transport.requests), 20)  # Initial session + four lane handshakes.

    def test_inflight_reservations_wait_instead_of_false_byte_exhaustion(self):
        from collection_batches import BatchPolicy
        transport = ConcurrentTransport([task(i) for i in range(4)], latency=.01)
        with self.fast_gate():
            source = collect(transport, batch_policy=BatchPolicy(max_response_bytes=8 * 1024 * 1024 + 1))
        self.assertEqual(len(source['records']), 4)
        self.assertEqual(source['collection_stats']['requests'], 25)

    def test_cancel_during_closing_scan_cannot_return_success(self):
        cancelled = threading.Event()
        transport = ConcurrentTransport([task(0)])
        scans = []
        def cancelling(request, timeout):
            result = transport(request, timeout)
            if json.loads(request.data).get('params', {}).get('name') == PROJECT_TOOL:
                scans.append(True)
                if len(scans) == 2:
                    cancelled.set()
            return result
        with self.fast_gate(), self.assertRaisesRegex(CollectionError, 'cancel'):
            collect(cancelling, cancel_event=cancelled)

    def test_inflight_reservations_wait_for_row_capacity(self):
        from collection_batches import BatchPolicy
        transport = ConcurrentTransport([task(i) for i in range(4)], latency=.01)
        with self.fast_gate():
            source = collect(transport, batch_policy=BatchPolicy(max_records=16))
        self.assertEqual(len(source['records']), 4)
        self.assertEqual(source['collection_stats']['records'], 16)

    def test_concurrent_sqlite_checkpoint_keeps_connection_and_codec_on_owner(self):
        import tempfile
        from pathlib import Path
        from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint
        owner = threading.get_ident()
        observed = set()
        def codec(data):
            observed.add(threading.get_ident())
            self.assertEqual(threading.get_ident(), owner)
            return bytes(value ^ 101 for value in data)
        with tempfile.TemporaryDirectory(prefix='synthetic-upcam-concurrent-') as directory:
            checkpoint = DpapiCollectionCheckpoint(Path(directory) / 'fictional.sqlite3',
                                                   protect=codec, unprotect=codec)
            with self.fast_gate():
                result = collect(ConcurrentTransport([task(i) for i in range(4)]), checkpoint=checkpoint)
        self.assertEqual(len(result['records']), 4)
        self.assertEqual(observed, {owner})

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI only')
    def test_concurrent_checkpoint_uses_real_windows_dpapi(self):
        import tempfile
        from pathlib import Path
        from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint
        with tempfile.TemporaryDirectory(prefix='synthetic-upcam-dpapi-concurrent-') as directory:
            checkpoint = DpapiCollectionCheckpoint(Path(directory) / 'fictional.sqlite3')
            with self.fast_gate():
                result = collect(ConcurrentTransport([task(i) for i in range(4)]), checkpoint=checkpoint)
        self.assertEqual(len(result['records']), 4)
        self.assertTrue(result['pagination_complete'])

    def test_shared_budget_exhaustion_never_returns_a_source(self):
        from collection_batches import BatchPolicy
        transport = ConcurrentTransport([task(i) for i in range(8)], latency=.02)
        with self.fast_gate(), self.assertRaisesRegex(CollectionError, 'budget'):
            collect(transport, batch_policy=BatchPolicy(max_requests=20))
        self.assertLessEqual(len(transport.requests), 20)
        self.assertTrue(any(name == DETAIL_TOOL for name, _, _ in transport.requests))

    def test_http_429_defers_all_new_worker_admissions(self):
        from urllib.error import HTTPError
        transport = ConcurrentTransport([task(i) for i in range(4)], latency=.02)
        original = transport.__call__
        rejected_at = []
        lock = threading.Lock()
        def limited(request, timeout):
            message = json.loads(request.data)
            if message.get('params', {}).get('name') == DETAIL_TOOL:
                with lock:
                    if not rejected_at:
                        rejected_at.append(time.monotonic())
                        raise HTTPError('https://fictional.invalid', 429, 'synthetic',
                                        {'Retry-After': '1'}, None)
            return original(request, timeout)
        result = collect(limited)
        later = [instant for _, _, instant in transport.requests if instant > rejected_at[0]]
        self.assertTrue(later)
        self.assertGreaterEqual(min(later) - rejected_at[0], .99)
        self.assertEqual(result['collection_stats']['requests'], 26)

    def test_progress_runs_on_owner_thread_and_never_exposes_content(self):
        from collection_progress import observe_collection
        owner = threading.get_ident()
        observed = []
        def observer(progress):
            self.assertEqual(threading.get_ident(), owner)
            observed.append(progress)
        with self.fast_gate(), observe_collection(observer):
            collect(ConcurrentTransport([task(i) for i in range(4)]))
        self.assertTrue(any(p['completed'] == 4 for p in observed))
        self.assertNotIn('fictional', json.dumps(observed))
        self.assertNotIn('Task 0', json.dumps(observed))

    def test_global_gate_spaces_all_attempts(self):
        transport = ConcurrentTransport([task(i) for i in range(3)], latency=.30)
        result = collect(transport)
        starts = [instant for _, _, instant in transport.requests]
        self.assertTrue(all(b - a >= .245 for a, b in zip(starts, starts[1:])), starts)
        self.assertGreater(transport.peak, 1)
        self.assertEqual(result['collection_stats']['requests'], 20)

    def test_desktop_concurrency_setting_is_persisted_validated_and_defaults_to_four(self):
        from upcam_app.config import DEFAULT_SETTINGS, SettingsError, SettingsStore, collection_client_limits
        import tempfile
        from pathlib import Path
        self.assertEqual(DEFAULT_SETTINGS['max_concurrent_tasks'], 4)
        for value in (1, 2, 3, 4):
            self.assertEqual(collection_client_limits(dict(DEFAULT_SETTINGS, max_concurrent_tasks=value))['concurrent_tasks'], value)
        for value in (0, 5, True, '4', 1.5):
            with self.assertRaises(SettingsError):
                collection_client_limits(dict(DEFAULT_SETTINGS, max_concurrent_tasks=value))
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / 'settings.json')
            store.save(dict(DEFAULT_SETTINGS, max_concurrent_tasks=2))
            self.assertEqual(store.load()['max_concurrent_tasks'], 2)

    def test_reusing_client_preserves_global_request_spacing_between_runs(self):
        transport = ConcurrentTransport([task(0)])
        client = Client('fictional-tenant', transport=transport,
                        credentials=lambda: 'fictional-token',
                        mcp_envelope_validator=fictional_envelope,
                        archive_contract_validator=fictional_archive,
                        batch_mode=True, concurrent_tasks=4)
        client.collect('fictional-project', day='2026-01-02')
        before = len(transport.requests)
        client.collect('fictional-project', day='2026-01-02')
        self.assertGreaterEqual(transport.requests[before][2] - transport.requests[before - 1][2], .245)

    def test_worker_bounds_are_validated(self):
        for bad in (0, 5, True, 1.5):
            with self.subTest(value=bad), self.assertRaises(CollectionError):
                Client('fictional-tenant', concurrent_tasks=bad)


class RequestGateTests(unittest.TestCase):
    def test_shared_cooldown_extends_rate_limit_and_cannot_be_shortened(self):
        from collection_concurrency import RequestGate
        clock = FakeClock()
        gate = RequestGate(interval=.25, clock=clock, sleep=clock.sleep)
        gate.acquire()
        gate.defer(4)
        gate.defer(1)
        gate.acquire()
        self.assertEqual(clock(), 4)
        gate.acquire()
        self.assertEqual(clock(), 4.25)

    def test_cancelled_gate_rejects_before_waiting(self):
        from collection_concurrency import RequestGate
        gate = RequestGate(interval=.25)
        gate.cancel()
        with self.assertRaisesRegex(CollectionError, 'cancel'):
            gate.acquire()
