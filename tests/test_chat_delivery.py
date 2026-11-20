"""Offline, synthetic-only tests for explicit standalone-chat delivery claims."""

import concurrent.futures
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from delivery_guard import DeliveryBlocked, initialize_ledger


class ChatDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'delivery_ledger.sqlite3'
        self.identity = initialize_ledger(self.path)
        self.unexpected_dispatches = []
        self.addCleanup(self.assertEqual, self.unexpected_dispatches, [])
        try:
            self.module = importlib.import_module('upcam_app.chat_delivery')
        except ModuleNotFoundError as error:
            if error.name != 'upcam_app.chat_delivery':
                raise
            self.fail('The guarded standalone-chat delivery API is not implemented')
        self.destination = 'https://synthetic.invalid/hook?token=fictional-destination'
        self.content = '  Fictional exact answer\n第二行, no mentions.  '
        self.request_id = str(uuid.uuid4())

    def guard(self, **kwargs):
        guard = self.module.ChatDelivery(
            self.path, self.identity, persistent_storage_verified=True, **kwargs)
        self.addCleanup(guard.close)
        return guard

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                'SELECT request_id, destination_hash, content_hash, state '
                'FROM chat_attempts ORDER BY request_id').fetchall()

    def attempt(self, guard, send, *, request_id=None, destination=None, content=None):
        return guard.attempt(
            request_id or self.request_id,
            self.destination if destination is None else destination,
            self.content if content is None else content, send)

    def never_send(self, content):
        # Sender exceptions are intentionally swallowed, so record unexpected
        # dispatch and check it independently during cleanup.
        self.unexpected_dispatches.append(content)
        return False

    def test_success_passes_exact_text_after_durable_claim_and_persists_only_hashes(self):
        guard = self.guard()
        calls = []

        def send(content):
            self.assertEqual(self.rows(), [(
                self.request_id, hashlib.sha256(self.destination.encode()).hexdigest(),
                hashlib.sha256(self.content.encode()).hexdigest(), 'in_flight')])
            calls.append(content)
            return True

        self.assertEqual(self.attempt(guard, send), 'confirmed')
        self.assertEqual(calls, [self.content])
        self.assertEqual(self.rows()[0][3], 'confirmed')
        for file in self.path.parent.iterdir():
            if file.is_file():
                contents = file.read_bytes()
                self.assertNotIn(self.destination.encode(), contents)
                self.assertNotIn(self.content.encode(), contents)

    def test_confirmed_uuid_never_dispatches_twice_even_with_changed_arguments(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: True), 'confirmed')
        guard.close()
        reopened = self.guard()
        self.assertEqual(self.attempt(reopened, self.never_send,
                                     destination='fictional-other-destination',
                                     content='Completely different answer'), 'not_sent')
        self.assertEqual(len(self.rows()), 1)

    def test_confirmed_destination_can_repeat_only_using_a_new_uuid(self):
        guard = self.guard()
        calls = []
        self.assertEqual(self.attempt(guard, lambda body: calls.append(body) or True), 'confirmed')
        self.assertEqual(self.attempt(guard, lambda body: calls.append(body) or True,
                                     request_id=str(uuid.uuid4())), 'confirmed')
        self.assertEqual(calls, [self.content, self.content])
        self.assertEqual(len(self.rows()), 2)

    def test_prior_unresolved_destination_blocks_fresh_uuid_and_changed_text_after_reopen(self):
        for state in ('in_flight', 'unknown'):
            with self.subTest(state=state):
                guard = self.guard()
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('DELETE FROM chat_attempts')
                    db.execute('INSERT INTO chat_attempts VALUES (?, ?, ?, ?)', (
                        self.request_id, hashlib.sha256(self.destination.encode()).hexdigest(),
                        hashlib.sha256(self.content.encode()).hexdigest(), state))
                guard.close()
                reopened = self.guard()
                self.assertEqual(self.attempt(reopened, self.never_send,
                                             request_id=str(uuid.uuid4()),
                                             content='New text cannot bypass unresolved scope'), 'unknown')
                self.assertEqual(len(self.rows()), 1)

    def test_unresolved_uuid_blocks_a_changed_destination(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.attempt(guard, self.never_send,
                                     destination='fictional-new-destination'), 'unknown')
        self.assertEqual(len(self.rows()), 1)

    def test_distinct_destinations_do_not_share_unresolved_scope(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.attempt(guard, lambda _: True,
                                     request_id=str(uuid.uuid4()),
                                     destination='fictional-other-destination'), 'confirmed')

    def test_only_exact_true_acknowledges_and_other_results_never_retry(self):
        for acknowledgement in (False, None, 1, 'true', {'status': 200}, [True]):
            with self.subTest(acknowledgement=acknowledgement):
                guard = self.guard()
                destination = 'fictional-' + str(uuid.uuid4())
                request_id = str(uuid.uuid4())
                self.assertEqual(guard.attempt(request_id, destination, self.content,
                                              lambda _: acknowledgement), 'unknown')
                self.assertEqual(guard.attempt(str(uuid.uuid4()), destination, 'New answer',
                                              self.never_send), 'unknown')
                self.assertEqual(next(row[3] for row in self.rows()
                                      if row[0] == request_id), 'unknown')

    def test_sender_errors_and_interruptions_are_unknown_without_exposing_details(self):
        for error in (TimeoutError, RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                guard = self.guard()
                request_id = str(uuid.uuid4())
                destination = 'fictional-' + str(uuid.uuid4())

                def send(_):
                    raise error(self.destination + self.content)

                self.assertEqual(guard.attempt(request_id, destination, self.content, send), 'unknown')
                self.assertEqual(guard.attempt(request_id, 'different-destination', 'different-body',
                                              self.never_send), 'unknown')
                self.assertEqual(next(row[3] for row in self.rows()
                                      if row[0] == request_id), 'unknown')

    def test_invalid_requests_are_not_sent_or_claimed(self):
        guard = self.guard()
        invalid_ids = ('not-a-uuid', str(uuid.uuid1()), str(uuid.UUID(int=0)),
                       self.request_id.upper(), uuid.uuid4(), None, '')
        for request_id in invalid_ids:
            with self.subTest(request_id=request_id):
                self.assertEqual(guard.attempt(request_id, self.destination, self.content,
                                              self.never_send), 'not_sent')
        for destination, content, send in (('', self.content, self.never_send),
                                           (None, self.content, self.never_send),
                                           (self.destination, '', self.never_send),
                                           (self.destination, None, self.never_send),
                                           (self.destination, self.content, None)):
            self.assertEqual(guard.attempt(str(uuid.uuid4()), destination, content, send), 'not_sent')
        self.assertEqual(self.rows(), [])

    def test_unvalidated_missing_wrong_identity_or_corrupt_ledger_refuses_construction(self):
        for verified in (False, None, 1):
            with self.subTest(verified=verified):
                with self.assertRaises(DeliveryBlocked):
                    self.module.ChatDelivery(self.path, self.identity,
                                             persistent_storage_verified=verified)
        with self.assertRaises(DeliveryBlocked):
            self.module.ChatDelivery(self.path, 'fictional-wrong-identity',
                                     persistent_storage_verified=True)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='chat_attempts'").fetchone())
        missing = self.path.parent / 'missing.sqlite3'
        with self.assertRaises(DeliveryBlocked):
            self.module.ChatDelivery(missing, self.identity, persistent_storage_verified=True)
        self.assertFalse(missing.exists())
        corrupt = self.path.parent / 'corrupt.sqlite3'
        corrupt.write_text('fictional non-database data')
        with self.assertRaises(DeliveryBlocked):
            self.module.ChatDelivery(corrupt, self.identity, persistent_storage_verified=True)

    def test_schema_addition_preserves_existing_schema_identity_and_delivery_rows(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO deliveries VALUES ('fictional-key', 'fictional-hash', 'unknown')")
            original_schema = db.execute(
                "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: True), 'confirmed')
        with closing(sqlite3.connect(self.path)) as db:
            schema = db.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall()
            for row in original_schema:
                self.assertIn(row, schema)
            self.assertEqual(db.execute('SELECT identity FROM metadata').fetchall(), [(self.identity,)])
            self.assertEqual(db.execute('SELECT * FROM deliveries').fetchall(),
                             [('fictional-key', 'fictional-hash', 'unknown')])

    def test_unique_index_enforces_unresolved_destination_even_outside_api(self):
        self.guard()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO chat_attempts VALUES ('first', 'same-hash', 'one', 'in_flight')")
        with closing(sqlite3.connect(self.path)) as db, db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO chat_attempts VALUES ('second', 'same-hash', 'two', 'unknown')")
            db.execute("UPDATE chat_attempts SET state='confirmed' WHERE request_id='first'")
            db.execute("INSERT INTO chat_attempts VALUES ('second', 'same-hash', 'two', 'unknown')")

    def test_concurrent_instances_with_same_uuid_dispatch_once_even_for_different_destinations(self):
        barrier = threading.Barrier(8)
        release_sender = threading.Event()
        sender_started = threading.Event()
        calls = []

        def worker(index):
            guard = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                barrier.wait(timeout=10)

                def send(content):
                    calls.append(content)
                    sender_started.set()
                    self.assertTrue(release_sender.wait(timeout=10))
                    return True

                return guard.attempt(self.request_id, f'fictional-destination-{index}',
                                     self.content, send)
            finally:
                guard.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, index) for index in range(8)]
            try:
                self.assertTrue(sender_started.wait(timeout=10))
                completed, _ = concurrent.futures.wait(
                    futures, timeout=10, return_when=concurrent.futures.FIRST_COMPLETED)
                self.assertTrue(completed)
            finally:
                release_sender.set()
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(outcomes.count('confirmed'), 1)
        self.assertTrue(all(outcome in ('confirmed', 'not_sent', 'unknown') for outcome in outcomes))
        self.assertEqual(calls, [self.content])
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_fresh_requests_same_destination_cannot_bypass_in_flight(self):
        sender_started = threading.Event()
        release_sender = threading.Event()
        calls = []

        def first():
            guard = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                def send(content):
                    calls.append(content)
                    sender_started.set()
                    self.assertTrue(release_sender.wait(timeout=10))
                    return True
                return self.attempt(guard, send)
            finally:
                guard.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(sender_started.wait(timeout=10))
                self.assertEqual(self.attempt(self.guard(), self.never_send,
                                             request_id=str(uuid.uuid4()), content='Changed answer'), 'unknown')
            finally:
                release_sender.set()
            self.assertEqual(future.result(timeout=10), 'confirmed')
        self.assertEqual(calls, [self.content])

    def test_sqlite_write_failure_before_dispatch_is_not_sent(self):
        guard = self.guard()
        guard.db.execute('PRAGMA query_only=ON')
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')
        self.assertEqual(self.rows(), [])

    def test_closed_database_before_dispatch_is_not_sent(self):
        guard = self.guard()
        guard.close()
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')

    def test_claim_commit_failure_rolls_back_and_never_dispatches(self):
        real_connect = sqlite3.connect

        class ClaimFailure(sqlite3.Connection):
            fail_commit = False

            def commit(db):
                if db.fail_commit:
                    raise sqlite3.OperationalError('fictional private commit error')
                return super().commit()

        def connect(*args, **kwargs):
            return real_connect(*args, **dict(kwargs, factory=ClaimFailure))

        with patch.object(self.module.sqlite3, 'connect', side_effect=connect):
            guard = self.guard()
        guard.db.fail_commit = True
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')
        self.assertEqual(self.rows(), [])

    def test_interrupted_claim_commit_that_was_durable_never_dispatches_or_retries(self):
        real_connect = sqlite3.connect

        class InterruptedClaim(sqlite3.Connection):
            interrupt_commit = False

            def commit(db):
                super().commit()
                if db.interrupt_commit:
                    raise KeyboardInterrupt('fictional sensitive interruption')

        def connect(*args, **kwargs):
            return real_connect(*args, **dict(kwargs, factory=InterruptedClaim))

        with patch.object(self.module.sqlite3, 'connect', side_effect=connect):
            guard = self.guard()
        guard.db.interrupt_commit = True
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')
        self.assertEqual(self.rows()[0][3], 'in_flight')
        self.assertEqual(self.attempt(self.guard(), self.never_send,
                                     request_id=str(uuid.uuid4()), content='Changed answer'), 'unknown')

    def test_post_dispatch_database_failure_retains_in_flight_and_blocks_new_uuid(self):
        guard = self.guard()

        def send(_):
            guard.db.execute('PRAGMA query_only=ON')
            return True

        self.assertEqual(self.attempt(guard, send), 'unknown')
        self.assertEqual(self.rows()[0][3], 'in_flight')
        guard.close()
        self.assertEqual(self.attempt(self.guard(), self.never_send,
                                     request_id=str(uuid.uuid4()), content='Different answer'), 'unknown')

    def test_result_commit_interruption_returns_unknown_even_after_durable_confirmation(self):
        real_connect = sqlite3.connect

        class InterruptedResult(sqlite3.Connection):
            interrupt_commit = False

            def commit(db):
                super().commit()
                if db.interrupt_commit:
                    raise KeyboardInterrupt('fictional private interruption')

        def connect(*args, **kwargs):
            return real_connect(*args, **dict(kwargs, factory=InterruptedResult))

        with patch.object(self.module.sqlite3, 'connect', side_effect=connect):
            guard = self.guard()

        def send(_):
            guard.db.interrupt_commit = True
            return True

        self.assertEqual(self.attempt(guard, send), 'unknown')
        self.assertEqual(self.attempt(self.guard(), self.never_send), 'not_sent')

    def test_synchronous_full_is_enabled_on_the_delivery_connection(self):
        guard = self.guard()
        self.assertEqual(guard.db.execute('PRAGMA synchronous').fetchone(), (2,))


    def tombstones(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                'SELECT request_id, destination_hash, content_hash, original_state, user_reviewed '
                'FROM chat_attempt_tombstones ORDER BY request_id').fetchall()

    def resolve(self, guard, destination=None):
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        destination = self.destination if destination is None else destination
        snapshot = self.peek(guard, destination)
        return guard.resolve_unknown(destination, self.request_id, **self.reviewed_fields(snapshot))

    def test_unknown_resolution_preserves_original_state_and_user_reviewed_audit_without_plaintext(self):
        guard = self.guard()
        with closing(sqlite3.connect(self.path)) as db:
            original_schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='chat_attempts'").fetchone()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.resolve(guard), 'resolved')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.tombstones(), [(
            self.request_id, hashlib.sha256(self.destination.encode()).hexdigest(),
            hashlib.sha256(self.content.encode()).hexdigest(), 'unknown', 1)])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT sql FROM sqlite_master WHERE name='chat_attempts'").fetchone(), original_schema)
        for file in self.path.parent.iterdir():
            if file.is_file():
                contents = file.read_bytes()
                self.assertNotIn(self.destination.encode(), contents)
                self.assertNotIn(self.content.encode(), contents)

    def test_resolved_uuid_never_dispatches_again_even_with_changed_destination_after_reopen(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.resolve(guard), 'resolved')
        guard.close()
        self.assertEqual(self.attempt(self.guard(), self.never_send,
                                     destination='fictional-different-destination',
                                     content='A changed answer cannot replay a resolved UUID'), 'not_sent')
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.tombstones()), 1)

    def test_fresh_uuid_can_send_only_after_unknown_resolution_and_old_uuid_stays_blocked(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.attempt(guard, self.never_send,
                                     request_id=str(uuid.uuid4()), content='New answer'), 'unknown')
        self.assertEqual(self.resolve(guard), 'resolved')
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')
        calls = []
        self.assertEqual(self.attempt(guard, lambda body: calls.append(body) or True,
                                     request_id=str(uuid.uuid4()), content='New answer'), 'confirmed')
        self.assertEqual(calls, ['New answer'])
        self.assertEqual(self.rows()[0][3], 'confirmed')
        self.assertEqual(len(self.tombstones()), 1)

    def test_resolving_persisted_in_flight_is_refused_and_does_not_release_destination(self):
        guard = self.guard()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO chat_attempts VALUES (?, ?, ?, ?)', (
                self.request_id, hashlib.sha256(self.destination.encode()).hexdigest(),
                hashlib.sha256(self.content.encode()).hexdigest(), 'in_flight'))
        self.assertEqual(self.resolve(guard), 'in_flight')
        self.assertEqual(self.rows()[0][3], 'in_flight')
        self.assertEqual(self.tombstones(), [])
        self.assertEqual(self.attempt(guard, self.never_send,
                                     request_id=str(uuid.uuid4()), content='Changed answer'), 'unknown')

    def test_resolution_does_not_cross_destination_or_remove_confirmed_attempts(self):
        guard = self.guard()
        other_destination = 'fictional-other-destination'
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.attempt(guard, lambda _: False, request_id=str(uuid.uuid4()),
                                     destination=other_destination), 'unknown')
        self.assertEqual(self.resolve(guard, 'fictional-missing-destination'), 'not_found')
        self.assertEqual(self.resolve(guard), 'resolved')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][1], hashlib.sha256(other_destination.encode()).hexdigest())
        self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4()),
                                     destination=other_destination), 'unknown')
        self.assertEqual(self.attempt(guard, lambda _: True, request_id=str(uuid.uuid4()),
                                     destination='fictional-confirmed-destination'), 'confirmed')
        self.assertEqual(self.resolve(guard, 'fictional-confirmed-destination'), 'not_found')
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.tombstones()), 1)

    def test_repeated_resolution_is_not_found_without_rewriting_original_audit(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertEqual(self.resolve(guard), 'resolved')
        original_audit = self.tombstones()
        self.assertEqual(self.resolve(guard), 'not_found')
        self.assertEqual(self.resolve(self.guard()), 'not_found')
        self.assertEqual(self.tombstones(), original_audit)

    def test_invalid_resolution_destination_does_not_mutate_unknown_attempt(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        for invalid in ('', None, 1, [], '\ud800'):
            with self.subTest(destination=repr(invalid)):
                self.assertEqual(guard.resolve_unknown(invalid, self.request_id, **self.reviewed_fields(self.peek(guard))), 'not_found')
        self.assertEqual(self.rows()[0][3], 'unknown')
        self.assertEqual(self.tombstones(), [])

    def test_concurrent_resolution_claims_create_one_tombstone_and_resolve_only_once(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        barrier = threading.Barrier(8)

        def worker(_):
            other = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                barrier.wait(timeout=10)
                return self.resolve(other)
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(worker, range(8)))
        self.assertEqual(outcomes.count('resolved'), 1)
        self.assertEqual(outcomes.count('not_found'), 7)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.tombstones()), 1)

    def test_resolution_cannot_release_destination_while_another_instance_is_sending(self):
        guard = self.guard()
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        sender_started = threading.Event()
        release_sender = threading.Event()
        calls = []

        def first():
            other = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                def send(content):
                    calls.append(content)
                    sender_started.set()
                    self.assertTrue(release_sender.wait(timeout=10))
                    return True
                return self.attempt(other, send)
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(sender_started.wait(timeout=10))
                self.assertEqual(self.resolve(guard), 'in_flight')
                self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4())), 'unknown')
                self.assertEqual(self.tombstones(), [])
            finally:
                release_sender.set()
            self.assertEqual(future.result(timeout=10), 'confirmed')
        self.assertEqual(calls, [self.content])

    def test_uuid_replay_racing_resolution_cannot_dispatch(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        barrier = threading.Barrier(2)
        calls = []

        def worker(resolve):
            other = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                barrier.wait(timeout=10)
                if resolve:
                    return self.resolve(other)
                return self.attempt(other, lambda body: calls.append(body) or True,
                                    destination='fictional-changed-destination', content='Changed answer')
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            resolution, replay = list(pool.map(worker, (True, False)))
        self.assertEqual(resolution, 'resolved')
        self.assertIn(replay, ('unknown', 'not_sent'))
        self.assertEqual(calls, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.tombstones()), 1)

    def test_resolution_database_failure_rolls_back_audit_and_preserves_unknown_gate(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.assertTrue(callable(getattr(guard, 'resolve_unknown', None)),
                        'Explicit user-reviewed unknown recovery is not implemented')
        guard.db.execute("""CREATE TRIGGER fail_chat_resolution BEFORE DELETE ON chat_attempts
                            BEGIN SELECT RAISE(ABORT, 'fictional-private-recovery-error'); END""")
        with self.assertRaises(DeliveryBlocked) as error:
            self.resolve(guard)
        self.assertNotIn('fictional-private-recovery-error', str(error.exception))
        self.assertEqual(self.rows()[0][3], 'unknown')
        self.assertEqual(self.tombstones(), [])
        self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4())), 'unknown')
        guard.db.execute('DROP TRIGGER fail_chat_resolution')
        self.assertEqual(self.resolve(guard), 'resolved')


    def peek(self, guard, destination=None):
        self.assertTrue(callable(getattr(guard, 'peek_unresolved', None)),
                        'Read-only record-bound recovery inspection is not implemented')
        return guard.peek_unresolved(self.destination if destination is None else destination)

    def test_equivalent_dingtalk_urls_share_unknown_scope_and_record_bound_recovery(self):
        guard = self.guard()
        first_url = 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-alpha&timestamp=100&sign=first'
        alias_url = 'https://oapi.dingtalk.com:443/robot/send?sign=second&access_token=%73ynthetic-alpha&timestamp=200'
        self.assertEqual(self.attempt(guard, lambda _: False, destination=first_url), 'unknown')
        self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4()),
                                     destination=alias_url, content='Changed answer'), 'unknown')
        first = self.peek(guard, first_url)
        alias = self.peek(guard, alias_url)
        self.assertEqual(first, alias)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(first['request_id'], self.request_id)
        expected_hash = hashlib.sha256(
            b'dingtalk-robot-v1\x00https://oapi.dingtalk.com/robot/send\x00synthetic-alpha').hexdigest()
        self.assertEqual(first['destination_hash'], expected_hash)
        self.assertEqual(guard.resolve_unknown(alias_url, first['request_id'], **self.reviewed_fields(first)), 'resolved')
        self.assertEqual(self.attempt(guard, self.never_send, destination=first_url), 'not_sent')
        self.assertEqual(self.attempt(guard, lambda _: True, request_id=str(uuid.uuid4()),
                                     destination=alias_url, content='New explicit message'), 'confirmed')
        for file in self.path.parent.iterdir():
            if file.is_file():
                raw = file.read_bytes()
                for secret in (first_url, alias_url, 'synthetic-alpha', self.content):
                    self.assertNotIn(secret.encode(), raw)

    def test_different_dingtalk_tokens_have_separate_unknown_scopes(self):
        guard = self.guard()
        first = 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-alpha'
        second = 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-beta'
        self.assertEqual(self.attempt(guard, lambda _: False, destination=first), 'unknown')
        self.assertEqual(self.attempt(guard, lambda _: True, request_id=str(uuid.uuid4()),
                                     destination=second), 'confirmed')
        self.assertNotEqual(self.rows()[0][1], self.rows()[1][1])

    def test_forged_or_ambiguous_dingtalk_destinations_are_rejected_without_claims(self):
        guard = self.guard()
        base = 'https://oapi.dingtalk.com/robot/send?'
        invalid_urls = (
            base + 'access_token=one&access_token=two',
            base + 'access_token=one&%61ccess_token=one',
            base + 'access_token=', base + 'timestamp=1&sign=signature',
            base + 'access_token=synthetic%20token', base + 'access_token=synthetic%26token',
            base + 'access_token=synthetic%0Atoken', base + 'access_token=synthetic%ZZtoken',
            base + 'access_token=synthetic&ACCESS_TOKEN=other',
            base + 'access_token=synthetic#access_token=other',
            base + 'access_token=synthetic&unexpected=value',
            'http://oapi.dingtalk.com/robot/send?access_token=synthetic',
            'https://oapi.dingtalk.com:8443/robot/send?access_token=synthetic',
            'https://oapi.dingtalk.com/robot/other?access_token=synthetic',
            'https://oapi.dingtalk.com.evil.invalid/robot/send?access_token=synthetic',
            'https://oapi.dingtalk.com@evil.invalid/robot/send?access_token=synthetic',
            'https://user:password@oapi.dingtalk.com/robot/send?access_token=synthetic',
            'https://evil.invalid/robot/send?access_token=synthetic',
            'https://oapi.dingtalk.com/robot/send?access_token=synthetic\n',
        )
        for destination in invalid_urls:
            with self.subTest(destination=destination):
                self.assertEqual(self.attempt(guard, self.never_send,
                                             request_id=str(uuid.uuid4()), destination=destination), 'not_sent')
        self.assertEqual(self.rows(), [])

    def test_peek_is_read_only_and_returns_exact_hashes_uuid_and_utc_claim_time(self):
        guard = self.guard()
        before = datetime.now(timezone.utc)
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        after = datetime.now(timezone.utc)
        changes = guard.db.total_changes
        snapshot = self.peek(guard)
        self.assertEqual(set(snapshot), {'state', 'request_id', 'destination_hash',
                                         'content_hash', 'attempted_at_utc'})
        self.assertEqual(snapshot['state'], 'unknown')
        self.assertEqual(snapshot['request_id'], self.request_id)
        self.assertEqual(snapshot['destination_hash'], hashlib.sha256(self.destination.encode()).hexdigest())
        self.assertEqual(snapshot['content_hash'], hashlib.sha256(self.content.encode()).hexdigest())
        when = datetime.fromisoformat(snapshot['attempted_at_utc'])
        self.assertEqual(when.utcoffset().total_seconds(), 0)
        self.assertLessEqual(before, when)
        self.assertLessEqual(when, after)
        self.assertEqual(guard.db.total_changes, changes)
        self.assertEqual(snapshot, self.peek(self.guard()))
        missing = self.peek(guard, 'fictional-not-found')
        self.assertEqual(missing['state'], 'not_found')
        for key in ('request_id', 'content_hash', 'attempted_at_utc'):
            self.assertIsNone(missing[key])

    def test_in_flight_snapshot_has_durable_metadata_before_sender_runs(self):
        guard = self.guard()
        snapshots = []

        def send(_):
            other = self.guard()
            snapshots.append(self.peek(other))
            return True

        self.assertEqual(self.attempt(guard, send), 'confirmed')
        self.assertEqual(snapshots[0]['state'], 'in_flight')
        self.assertEqual(snapshots[0]['request_id'], self.request_id)
        self.assertIsNotNone(snapshots[0]['attempted_at_utc'])

    def test_missing_optional_attempt_timestamp_is_reported_as_null(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        self.peek(guard)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM chat_attempt_metadata WHERE request_id=?', (self.request_id,))
        self.assertIsNone(self.peek(guard)['attempted_at_utc'])

    def test_attempt_and_timestamp_metadata_are_atomic_on_database_failure(self):
        guard = self.guard()
        self.peek(guard)
        guard.db.execute("""CREATE TRIGGER fail_chat_metadata BEFORE INSERT ON chat_attempt_metadata
                            BEGIN SELECT RAISE(ABORT, 'fictional-private-time-error'); END""")
        self.assertEqual(self.attempt(guard, self.never_send), 'not_sent')
        self.assertEqual(self.rows(), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM chat_attempt_metadata').fetchall(), [])

    def test_stale_snapshot_for_old_unknown_cannot_resolve_a_new_unknown(self):
        guard = self.guard()
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        old = self.peek(guard)
        self.assertEqual(guard.resolve_unknown(self.destination, old['request_id'], **self.reviewed_fields(old)), 'resolved')
        next_id = str(uuid.uuid4())
        self.assertEqual(self.attempt(guard, lambda _: False, request_id=next_id, content='New unknown'), 'unknown')
        self.assertEqual(guard.resolve_unknown(self.destination, old['request_id'], **self.reviewed_fields(old)), 'not_found')
        current = self.peek(guard)
        self.assertEqual(current['request_id'], next_id)
        self.assertEqual(current['state'], 'unknown')
        self.assertEqual(len(self.tombstones()), 1)
        self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4())), 'unknown')

    def test_snapshot_cannot_resolve_changed_robot_or_different_request_uuid(self):
        guard = self.guard()
        original = 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-alpha'
        changed = 'https://oapi.dingtalk.com/robot/send?access_token=synthetic-beta'
        self.assertEqual(self.attempt(guard, lambda _: False, destination=original), 'unknown')
        snapshot = self.peek(guard, original)
        self.assertEqual(guard.resolve_unknown(changed, snapshot['request_id'], **self.reviewed_fields(snapshot)), 'not_found')
        self.assertEqual(guard.resolve_unknown(original, str(uuid.uuid4()), **self.reviewed_fields(snapshot)), 'not_found')
        self.assertEqual(self.peek(guard, original), snapshot)
        self.assertEqual(self.tombstones(), [])

    def test_concurrent_equivalent_robot_urls_dispatch_once_and_share_snapshot(self):
        guard = self.guard()
        self.peek(guard)
        urls = ('https://oapi.dingtalk.com/robot/send?access_token=synthetic-alpha&sign=first&timestamp=1',
                'https://oapi.dingtalk.com/robot/send?timestamp=2&sign=second&access_token=%73ynthetic-alpha')
        started = threading.Event()
        release = threading.Event()
        calls = []

        def worker():
            other = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                def send(content):
                    calls.append(content)
                    started.set()
                    self.assertTrue(release.wait(timeout=10))
                    return True
                return self.attempt(other, send, destination=urls[0])
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(started.wait(timeout=10))
                snapshot = self.peek(guard, urls[1])
                self.assertEqual(snapshot['state'], 'in_flight')
                self.assertEqual(guard.resolve_unknown(urls[1], snapshot['request_id'], **self.reviewed_fields(snapshot)), 'in_flight')
                self.assertEqual(self.attempt(guard, self.never_send, request_id=str(uuid.uuid4()),
                                             destination=urls[1]), 'unknown')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), 'confirmed')
        self.assertEqual(calls, [self.content])

    def test_legacy_active_or_tombstone_rows_without_identity_version_refuse_ledger(self):
        for tombstone in (False, True):
            with self.subTest(tombstone=tombstone):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'delivery_ledger.sqlite3'
                    identity = initialize_ledger(path)
                    with closing(sqlite3.connect(path)) as db, db:
                        table = 'chat_attempt_tombstones' if tombstone else 'chat_attempts'
                        schema = ('request_id TEXT PRIMARY KEY, destination_hash TEXT, content_hash TEXT, '
                                  'original_state TEXT, user_reviewed INTEGER' if tombstone else
                                  'request_id TEXT PRIMARY KEY, destination_hash TEXT, content_hash TEXT, state TEXT')
                        db.execute(f'CREATE TABLE {table} ({schema})')
                        values = (self.request_id, 'fictional-legacy-destination-hash',
                                  'fictional-legacy-content-hash', 'unknown')
                        if tombstone:
                            values += (1,)
                        db.execute(f'INSERT INTO {table} VALUES ({", ".join("?" for _ in values)})', values)
                    with self.assertRaises(DeliveryBlocked):
                        self.module.ChatDelivery(path, identity, persistent_storage_verified=True)
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute(f'SELECT * FROM {table}').fetchall(), [values])


    def reviewed_fields(self, snapshot):
        return {'expected_content_hash': snapshot['content_hash'],
                'expected_attempted_at_utc': snapshot['attempted_at_utc']}

    def require_full_recovery_snapshot(self, guard):
        parameters = inspect.signature(guard.resolve_unknown).parameters
        self.assertIn('expected_content_hash', parameters, 'Recovery does not bind displayed content hash')
        self.assertIn('expected_attempted_at_utc', parameters, 'Recovery does not bind displayed UTC time')

    def test_modified_content_or_time_invalidates_confirmed_recovery_snapshot(self):
        guard = self.guard()
        self.require_full_recovery_snapshot(guard)
        for field in ('content_hash', 'attempted_at_utc'):
            with self.subTest(field=field):
                request_id = str(uuid.uuid4())
                destination = 'fictional-mutable-record-' + field
                self.assertEqual(guard.attempt(request_id, destination, self.content, lambda _: False), 'unknown')
                snapshot = self.peek(guard, destination)
                with closing(sqlite3.connect(self.path)) as db, db:
                    if field == 'content_hash':
                        db.execute('UPDATE chat_attempts SET content_hash=? WHERE request_id=?',
                                   ('0' * 64, request_id))
                    else:
                        db.execute('UPDATE chat_attempt_metadata SET attempted_at_utc=? WHERE request_id=?',
                                   ('2026-01-01T00:00:00+00:00', request_id))
                self.assertEqual(guard.resolve_unknown(destination, request_id,
                                                       **self.reviewed_fields(snapshot)), 'not_found')
                self.assertEqual(self.peek(guard, destination)['state'], 'unknown')
                self.assertEqual(self.tombstones(), [])

    def test_missing_timestamp_cannot_be_resolved_using_a_previous_snapshot(self):
        guard = self.guard()
        self.require_full_recovery_snapshot(guard)
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        snapshot = self.peek(guard)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM chat_attempt_metadata WHERE request_id=?', (self.request_id,))
        self.assertEqual(guard.resolve_unknown(self.destination, self.request_id,
                                               **self.reviewed_fields(snapshot)), 'not_found')
        self.assertEqual(self.peek(guard)['state'], 'unknown')
        self.assertEqual(self.tombstones(), [])

    def test_concurrent_metadata_writer_cannot_release_a_stale_confirmation(self):
        guard = self.guard()
        self.require_full_recovery_snapshot(guard)
        self.assertEqual(self.attempt(guard, lambda _: False), 'unknown')
        snapshot = self.peek(guard)
        ready = threading.Event()
        start = threading.Event()

        def worker():
            other = self.module.ChatDelivery(self.path, self.identity, persistent_storage_verified=True)
            try:
                ready.set()
                self.assertTrue(start.wait(timeout=10))
                return other.resolve_unknown(self.destination, self.request_id,
                                             **self.reviewed_fields(snapshot))
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            self.assertTrue(ready.wait(timeout=10))
            with closing(sqlite3.connect(self.path)) as db:
                try:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE chat_attempt_metadata SET attempted_at_utc=? WHERE request_id=?',
                               ('2026-01-01T00:00:00+00:00', self.request_id))
                    start.set()
                    db.commit()
                finally:
                    start.set()
            self.assertEqual(future.result(timeout=10), 'not_found')
        self.assertEqual(self.peek(guard)['state'], 'unknown')
        self.assertEqual(self.tombstones(), [])


if __name__ == '__main__':
    unittest.main()
