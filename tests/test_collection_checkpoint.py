"""Checkpoint acceptance uses fictional records and an authenticated test codec."""

import copy
from contextlib import closing, contextmanager
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from report_contract import updated_selection


def protect(payload):
    """Synthetic-only reversible codec; production always uses Windows DPAPI."""
    packed = zlib.compress(payload)
    encrypted = bytes(value ^ 0xA6 for value in packed)
    return hmac.digest(b"fictional-checkpoint-codec", encrypted, "sha256") + encrypted


def unprotect(payload):
    signature, encrypted = payload[:32], payload[32:]
    if not hmac.compare_digest(signature, hmac.digest(b"fictional-checkpoint-codec", encrypted, "sha256")):
        raise ValueError("fictional-private-codec-error")
    return zlib.decompress(bytes(value ^ 0xA6 for value in encrypted))


@contextmanager
def fixture_database(path):
    """Preserve SQLite transaction semantics and release all fixture handles."""
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            with closing(connection.cursor()) as cursor:
                yield cursor


class CollectionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "collection.sqlite3"
        self.context = {
            "tenant_id": "fictional-org", "project_id": "demo-project",
            "include_archived": False, "selection": updated_selection("2026-01-02"),
            "policy": {"version": 1, "batch": {"max_tasks": 300}},
            "credential_identity": hashlib.sha256(b"fictional-pair").hexdigest(),
        }
        self.record = json.loads((Path(__file__).parent / "fixtures" / "collection.json").read_text(encoding="utf-8"))["records"][0]
        self.candidate = copy.deepcopy(self.record["task"])
        self.task_id = "demo-old-task"
        self.zero_counters = {"requests": 0, "records": 0, "response_bytes": 0,
                              "retained_bytes": 0, "batches": 0, "passes": 0}

    def module(self):
        try:
            return importlib.import_module("upcam_app.collection_checkpoint")
        except ModuleNotFoundError:
            self.fail("The collection checkpoint implementation is missing")

    def store(self, **kwargs):
        store = self.module().DpapiCollectionCheckpoint(self.path, protect=protect, unprotect=unprotect, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_fixture_database_releases_held_resources_after_commit_and_rollback(self):
        with fixture_database(self.path) as cursor:
            connection = cursor.connection
            cursor.execute("CREATE TABLE fixture_lifecycle(value TEXT)")
            cursor.execute("INSERT INTO fixture_lifecycle VALUES('fictional-committed')")
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.execute("SELECT value FROM fixture_lifecycle")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT value FROM fixture_lifecycle")
        with self.assertRaisesRegex(RuntimeError, "fictional-rollback"):
            with fixture_database(self.path) as failed_cursor:
                failed_connection = failed_cursor.connection
                failed_cursor.execute("INSERT INTO fixture_lifecycle VALUES('fictional-rolled-back')")
                raise RuntimeError("fictional-rollback")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed_cursor.execute("SELECT value FROM fixture_lifecycle")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed_connection.execute("SELECT value FROM fixture_lifecycle")
        with fixture_database(self.path) as verified:
            self.assertEqual(verified.execute("SELECT value FROM fixture_lifecycle").fetchall(), [("fictional-committed",)])

    def test_completed_record_and_counters_resume_after_new_object_and_close(self):
        store = self.store()
        self.assertEqual(store.start(self.context, now=100), {
            "created_at": 100.0, "counters": self.zero_counters, "resumed": False})
        store.save_task(self.task_id, self.candidate, self.record)
        counters = {"requests": 5, "records": 4, "response_bytes": 2500,
                    "retained_bytes": 1800, "batches": 1, "passes": 1}
        store.save_counters(counters)
        store.close()

        resumed = self.store()
        self.assertEqual(resumed.start(dict(reversed(list(self.context.items()))), now=200), {
            "created_at": 100.0, "counters": counters, "resumed": True})
        loaded = resumed.load_task(self.task_id)
        self.assertEqual(loaded, {"candidate": self.candidate, "record": self.record})
        loaded["record"]["task"]["content"] = "caller mutation"
        self.assertEqual(resumed.load_task(self.task_id)["record"], self.record)

    def test_different_scope_or_expired_age_clears_only_owned_cache(self):
        for change in ("project_id", "credential_identity", "selection", "include_archived", "policy", "expiry"):
            with self.subTest(change=change):
                store = self.store()
                store.start(self.context, now=100)
                store.save_task(self.task_id, self.candidate, self.record)
                other = copy.deepcopy(self.context)
                if change == "project_id":
                    other[change] = "fictional-other"
                elif change == "credential_identity":
                    other[change] = "b" * 64
                elif change == "selection":
                    other[change] = updated_selection("2026-01-03")
                elif change == "include_archived":
                    other[change] = True
                elif change == "policy":
                    other[change]["version"] = 2
                result = store.start(other, now=7301 if change == "expiry" else 101)
                self.assertFalse(result["resumed"])
                self.assertEqual(result["counters"], self.zero_counters)
                self.assertIsNone(store.load_task(self.task_id))
                store.clear()
                store.close()

    def test_prune_removes_obsolete_tasks_and_duplicate_save_replaces_atomically(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        replacement = copy.deepcopy(self.record)
        replacement["task"]["content"] = "fictional-replacement"
        store.save_task(self.task_id, self.candidate, replacement)
        other = copy.deepcopy(self.record)
        other["task"]["id"] = "fictional-other-task"
        store.save_task("fictional-other-task", other["task"], other)
        store.prune(iter([self.task_id]))
        self.assertIsNone(store.load_task("fictional-other-task"))
        self.assertEqual(store.load_task(self.task_id)["record"], replacement)

    def test_clear_can_run_without_start_and_preserves_neighbouring_files(self):
        neighbours = {name: Path(self.temp.name) / name for name in ("settings.json", "credentials.bin", "history.json", "ledger.sqlite3")}
        for path in neighbours.values():
            path.write_bytes(b"fictional-neighbour")
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        cleaner = self.store()
        cleaner.clear()
        cleaner.close()
        for path in neighbours.values():
            self.assertEqual(path.read_bytes(), b"fictional-neighbour")
        fresh = self.store()
        self.assertFalse(fresh.start(self.context, now=200)["resumed"])

    def test_disk_contains_no_task_context_or_counter_plaintext(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.save_counters(dict(self.zero_counters, requests=947))
        store.close()
        raw = self.path.read_bytes()
        for value in (self.task_id, "fictional-org", "demo-project", "candidate", "activities",
                      "response_bytes", "credential_identity", "created_at", "虚构旧任务"):
            self.assertNotIn(value.encode("utf-8"), raw)
        with fixture_database(self.path) as cursor:
            for table in ("checkpoint_state", "checkpoint_tasks"):
                self.assertEqual(cursor.execute(f"SELECT DISTINCT typeof(payload) FROM {table}").fetchall(), [("blob",)])

    def test_store_session_is_exclusive_and_close_releases_it(self):
        first, second = self.store(), self.store()
        first.start(self.context, now=100)
        first.save_task(self.task_id, self.candidate, self.record)
        for operation in (lambda: second.start(self.context, now=101), second.clear):
            with self.assertRaises(self.module().CollectionCheckpointBusy):
                operation()
        first.close()
        self.assertTrue(second.start(self.context, now=101)["resumed"])
        self.assertEqual(second.load_task(self.task_id)["record"], self.record)
        second.clear()
        self.assertFalse(first.start(self.context, now=102)["resumed"])

    def test_other_process_cannot_start_or_clear_an_active_store(self):
        store = self.store()
        store.start(self.context, now=100)
        program = """
import json, sys
from tests.test_collection_checkpoint import protect, unprotect
from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint, CollectionCheckpointBusy
path, context = json.loads(sys.stdin.read())
store = DpapiCollectionCheckpoint(path, protect=protect, unprotect=unprotect)
try:
    for operation in (lambda: store.start(context, now=101), store.clear):
        try:
            operation()
        except CollectionCheckpointBusy:
            continue
        raise AssertionError('Active store was writable by another process')
finally:
    store.close()
"""
        process = subprocess.run([sys.executable, "-c", program], input=json.dumps([str(self.path), self.context]),
                                 text=True, capture_output=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_process_death_releases_session_lock_and_rolls_back_partial_write(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        initial_size = self.path.stat().st_size
        program = """
import json, os, sqlite3, sys
from tests.test_collection_checkpoint import protect, unprotect
from upcam_app.collection_checkpoint import DpapiCollectionCheckpoint
path, context = json.loads(sys.stdin.read())
store = DpapiCollectionCheckpoint(path, protect=protect, unprotect=unprotect)
store.start(context, now=101)
connection = sqlite3.connect(path)
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE checkpoint_tasks SET payload=?', (b'fictional-incomplete-write' * 100000,))
os._exit(0)
"""
        process = subprocess.run([sys.executable, "-c", program], input=json.dumps([str(self.path), self.context]),
                                 text=True, capture_output=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertGreater(self.path.stat().st_size, initial_size)
        self.assertGreater(self.path.with_name(self.path.name + "-journal").stat().st_size, 0)
        resumed = self.store()
        self.assertTrue(resumed.start(self.context, now=102)["resumed"])
        self.assertEqual(resumed.load_task(self.task_id)["record"], self.record)

    def test_unsafe_sidecar_is_rejected_before_database_creation(self):
        victim = Path(self.temp.name) / "fictional-victim"
        victim.write_bytes(b"fictional-untouched")
        journal = self.path.with_name(self.path.name + "-journal")
        try:
            journal.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().start(self.context, now=100)
        self.assertFalse(self.path.exists())
        self.assertEqual(victim.read_bytes(), b"fictional-untouched")

    def test_orphan_journal_is_not_claimed_or_changed_when_no_owned_database_exists(self):
        journal = self.path.with_name(self.path.name + "-journal")
        journal.write_bytes(b"fictional-not-our-journal")
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().start(self.context, now=100)
        self.assertEqual(journal.read_bytes(), b"fictional-not-our-journal")
        self.assertFalse(self.path.exists())

    def test_unknown_database_and_non_database_remain_unmodified(self):
        for kind in ("text", "sqlite"):
            with self.subTest(kind=kind):
                if kind == "text":
                    self.path.write_bytes(b"fictional-private-non-cache")
                else:
                    with fixture_database(self.path) as cursor:
                        cursor.execute("CREATE TABLE unrelated(value TEXT)")
                        cursor.execute("INSERT INTO unrelated VALUES('fictional-private')")
                before = self.path.read_bytes()
                store = self.store()
                for operation in (lambda: store.start(self.context, now=100), store.clear):
                    with self.assertRaises(self.module().CollectionCheckpointError) as raised:
                        operation()
                    self.assertNotIn(str(self.path), str(raised.exception))
                    self.assertNotIn("private", str(raised.exception))
                    self.assertEqual(self.path.read_bytes(), before)
                store.close()
                self.path.unlink()

    def test_unknown_lease_file_is_never_overwritten(self):
        lease = self.path.with_name(self.path.name + ".lease")
        lease.write_bytes(b"fictional-not-our-file")
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().start(self.context, now=100)
        self.assertEqual(lease.read_bytes(), b"fictional-not-our-file")
        self.assertFalse(self.path.exists())

    def test_interruption_after_initial_file_creation_does_not_publish_an_empty_lease(self):
        store = self.store()
        actual_open = os.open
        interrupted = False
        def interrupt_open(path, flags, *args, **kwargs):
            nonlocal interrupted
            descriptor = actual_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT and not interrupted:
                interrupted = True
                os.close(descriptor)
                raise KeyboardInterrupt()
            return descriptor
        with patch.object(os, "open", side_effect=interrupt_open):
            with self.assertRaises(KeyboardInterrupt):
                store.start(self.context, now=100)
        store.close()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".lease").exists())
        fresh = self.store()
        self.assertFalse(fresh.start(self.context, now=101)["resumed"])
        fresh.clear()

    def test_interruption_before_schema_initialization_does_not_publish_an_empty_database(self):
        store = self.store()
        with patch("sqlite3.connect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                store.start(self.context, now=100)
        store.close()
        self.assertFalse(self.path.exists())
        fresh = self.store()
        self.assertFalse(fresh.start(self.context, now=101)["resumed"])
        fresh.clear()

    def test_clear_rejects_unknown_empty_lease_instead_of_claiming_success(self):
        lease = self.path.with_name(self.path.name + ".lease")
        lease.touch()
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().clear()
        self.assertEqual(lease.read_bytes(), b"")

    def test_task_and_context_must_be_strict_finite_json_with_matching_identities(self):
        store = self.store()
        store.start(self.context, now=100)
        cases = []
        for value in (float("nan"), float("inf"), float("-inf"), ("tuple",), object()):
            candidate = copy.deepcopy(self.candidate)
            candidate["unsupported"] = value
            cases.append((self.task_id, candidate, self.record))
        candidate = dict(self.candidate, _id="fictional-conflicting-id")
        cases.append((self.task_id, candidate, self.record))
        record = copy.deepcopy(self.record)
        record["task"]["id"] = "fictional-other"
        cases.append((self.task_id, self.candidate, record))
        cases.extend([("../unsafe", self.candidate, self.record),
                      (self.task_id, self.candidate, {"task": self.candidate, "activities": None})])
        for task_id, candidate, record in cases:
            with self.subTest(task_id=task_id, kind=type(candidate.get("unsupported")).__name__):
                with self.assertRaises(self.module().CollectionCheckpointError):
                    store.save_task(task_id, candidate, record)
        self.assertIsNone(store.load_task(self.task_id))

    def test_counter_validation_rejects_bool_negative_nonfinite_and_unknown_keys(self):
        store = self.store()
        store.start(self.context, now=100)
        for key in self.zero_counters:
            for invalid in (True, -1, 1.0, "1", float("nan"), float("inf")):
                with self.subTest(key=key, invalid=repr(invalid)), self.assertRaises(self.module().CollectionCheckpointError):
                    store.save_counters(dict(self.zero_counters, **{key: invalid}))
        for invalid in ({}, dict(self.zero_counters, secret="fictional"),
                        dict(self.zero_counters, elapsed_seconds=True),
                        dict(self.zero_counters, elapsed_seconds=float("nan"))):
            with self.assertRaises(self.module().CollectionCheckpointError):
                store.save_counters(invalid)
        store.save_counters(dict(self.zero_counters, elapsed_seconds=0.25))
        store.close()
        self.assertEqual(self.store().start(self.context, now=101)["counters"]["elapsed_seconds"], 0.25)

    def test_future_created_time_fails_instead_of_resetting_or_reusing(self):
        store = self.store()
        store.start(self.context, now=200)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        broken = self.store()
        with self.assertRaises(self.module().CollectionCheckpointError):
            broken.start(self.context, now=100)
        broken.close()
        good = self.store()
        self.assertTrue(good.start(self.context, now=201)["resumed"])
        self.assertEqual(good.load_task(self.task_id)["record"], self.record)

    def test_bad_codec_cannot_partially_replace_a_record_or_counters(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        def fail(clear):
            raise RuntimeError("fictional-private-body-and-credential")
        with patch.object(store, "_protect", side_effect=fail):
            for operation in (lambda: store.save_task(self.task_id, self.candidate, self.record),
                              lambda: store.save_counters(dict(self.zero_counters, requests=5))):
                with self.assertRaises(self.module().CollectionCheckpointError) as raised:
                    operation()
                self.assertNotIn("private", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(store.load_task(self.task_id)["record"], self.record)
        store.close()
        self.assertEqual(self.store().start(self.context, now=101)["counters"], self.zero_counters)

    def test_logical_limit_counts_plaintext_even_with_high_compression(self):
        store = self.store(max_bytes=32768)
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        oversized = copy.deepcopy(self.record)
        oversized["task"]["content"] = "fictional-repeated-content" * 3000
        with self.assertRaises(self.module().CollectionCheckpointError):
            store.save_task(self.task_id, self.candidate, oversized)
        self.assertEqual(store.load_task(self.task_id)["record"], self.record)
        self.assertLessEqual(self.path.stat().st_size, 32768)

    def test_physical_limit_rolls_back_a_failed_sqlite_replacement(self):
        store = self.store(max_bytes=32768)
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        with patch.object(store, "_protect", return_value=b"fictional-expanded-ciphertext" * 1100):
            with self.assertRaises(self.module().CollectionCheckpointError):
                store.save_task(self.task_id, self.candidate, self.record)
        self.assertEqual(store.load_task(self.task_id)["record"], self.record)
        self.assertLessEqual(self.path.stat().st_size, 32768)
        store.close()
        recovered = self.store(max_bytes=32768)
        self.assertTrue(recovered.start(self.context, now=101)["resumed"])
        self.assertEqual(recovered.load_task(self.task_id)["record"], self.record)

    def test_existing_oversized_file_is_rejected_before_sqlite_reads(self):
        self.path.write_bytes(b"fictional-overlarge" * 2000)
        before = self.path.read_bytes()
        with patch("sqlite3.connect", side_effect=AssertionError("must not open overlarge DB")):
            with self.assertRaises(self.module().CollectionCheckpointError):
                self.store(max_bytes=32768).start(self.context, now=100)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_context_cannot_expose_credentials_or_replace_valid_progress(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        cases = [dict(self.context, credential_identity="fictional-secret"),
                 dict(self.context, headers={"Authorization": "fictional-bearer"}),
                 dict(self.context, policy={"seconds": float("nan")}),
                 dict(self.context, selection={1: "non-string-key"})]
        for invalid in cases:
            with self.assertRaises(self.module().CollectionCheckpointError):
                store.start(invalid, now=101)
        self.assertTrue(store.start(self.context, now=102)["resumed"])
        self.assertEqual(store.load_task(self.task_id)["record"], self.record)

    def test_damaged_task_ciphertext_prevents_resume_before_any_row_reuse(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("UPDATE checkpoint_tasks SET payload=?", (b"fictional-damaged-ciphertext",))
        broken = self.store()
        with self.assertRaises(self.module().CollectionCheckpointError):
            broken.start(self.context, now=101)
        broken.clear()
        self.assertFalse(broken.start(self.context, now=102)["resumed"])

    def test_copied_valid_ciphertext_cannot_cross_row_or_session(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        with fixture_database(self.path) as cursor:
            payload, logical_bytes = cursor.execute("SELECT payload,logical_bytes FROM checkpoint_tasks").fetchone()
            cursor.execute("UPDATE checkpoint_tasks SET row_key=?", (hashlib.sha256(b"fictional-other-task").digest(),))
        broken = self.store()
        with self.assertRaises(self.module().CollectionCheckpointError):
            broken.start(self.context, now=101)
        broken.clear()
        broken.start(self.context, now=102)
        broken.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("INSERT INTO checkpoint_tasks(row_key,payload,logical_bytes) VALUES(?,?,?)",
                           (hashlib.sha256(self.task_id.encode()).digest(), payload, logical_bytes))
        with self.assertRaises(self.module().CollectionCheckpointError):
            broken.start(self.context, now=103)

    def test_damaged_metadata_can_be_cleared_without_unlocking_or_reading_credentials(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("UPDATE checkpoint_state SET payload=?", (b"fictional-damaged-metadata",))
        broken = self.store()
        with self.assertRaises(self.module().CollectionCheckpointError):
            broken.start(self.context, now=101)
        with patch.object(broken, "_unprotect", side_effect=AssertionError("clear must not decrypt")):
            broken.clear()
        self.assertFalse(broken.start(self.context, now=102)["resumed"])

    def test_malformed_owned_schema_is_rejected_without_dropping_unknown_tables(self):
        store = self.store()
        store.start(self.context, now=100)
        store.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("CREATE TABLE fictional_unrelated(value TEXT)")
            cursor.execute("INSERT INTO fictional_unrelated VALUES('fictional-preserve')")
        before = self.path.read_bytes()
        for operation in (lambda: store.start(self.context, now=101), store.clear):
            with self.assertRaises(self.module().CollectionCheckpointError):
                operation()
            self.assertEqual(self.path.read_bytes(), before)

    def test_unexpected_sqlite_internal_tables_are_not_exempt_from_ownership_validation(self):
        store = self.store()
        store.start(self.context, now=100)
        store.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("ANALYZE")
        before = self.path.read_bytes()
        with self.assertRaises(self.module().CollectionCheckpointError):
            store.clear()
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_parent_database_and_lease_are_rejected(self):
        real = Path(self.temp.name) / "real"
        real.mkdir()
        linked = Path(self.temp.name) / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        parent_store = self.module().DpapiCollectionCheckpoint(linked / "collection.sqlite3", protect=protect, unprotect=unprotect)
        self.addCleanup(parent_store.close)
        with self.assertRaises(self.module().CollectionCheckpointError):
            parent_store.start(self.context, now=100)
        self.assertEqual(list(real.iterdir()), [])
        victim = real / "fictional-victim"
        victim.write_bytes(b"fictional-untouched")
        for target in (self.path, self.path.with_name(self.path.name + ".lease")):
            with self.subTest(target=target.name):
                target.symlink_to(victim)
                with self.assertRaises(self.module().CollectionCheckpointError):
                    self.store().start(self.context, now=100)
                self.assertEqual(victim.read_bytes(), b"fictional-untouched")
                target.unlink()

    def test_directory_and_hardlink_paths_are_rejected(self):
        self.path.mkdir()
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().start(self.context, now=100)
        self.path.rmdir()
        victim = Path(self.temp.name) / "fictional-victim"
        victim.write_bytes(b"fictional-untouched")
        try:
            self.path.hardlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("Hardlinks unavailable")
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store().start(self.context, now=100)
        self.assertEqual(victim.read_bytes(), b"fictional-untouched")

    def test_metadata_logical_size_and_total_plaintext_limits_are_rechecked_on_resume(self):
        store = self.store(max_bytes=32768)
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        with fixture_database(self.path) as cursor:
            cursor.execute("UPDATE checkpoint_tasks SET logical_bytes=32768")
        with self.assertRaises(self.module().CollectionCheckpointError):
            self.store(max_bytes=32768).start(self.context, now=101)

    def test_now_ttl_and_constructor_limits_exclude_bool_and_nonfinite_values(self):
        for invalid in (True, -1, float("nan"), float("inf"), "100"):
            with self.subTest(now=repr(invalid)), self.assertRaises(self.module().CollectionCheckpointError):
                self.store().start(self.context, now=invalid)
        for invalid in (True, 0, -1, 1.0, "7200"):
            with self.subTest(ttl=repr(invalid)), self.assertRaises(self.module().CollectionCheckpointError):
                self.store().start(self.context, now=100, ttl_seconds=invalid)
        for invalid in (True, 0, -1, 1.0):
            with self.subTest(max_bytes=repr(invalid)), self.assertRaises(self.module().CollectionCheckpointError):
                self.store(max_bytes=invalid)
        self.assertFalse(self.path.exists())

    def test_valid_identity_aliases_round_trip_without_losing_fields(self):
        store = self.store()
        store.start(self.context, now=100)
        for aliases in ({"_id": self.task_id}, {"taskId": self.task_id},
                        {"id": self.task_id, "_id": self.task_id}):
            with self.subTest(aliases=aliases):
                candidate = {key: value for key, value in self.candidate.items() if key != "id"}
                candidate.update(aliases)
                record = dict(copy.deepcopy(self.record), task=candidate)
                store.save_task(self.task_id, candidate, record)
                self.assertEqual(store.load_task(self.task_id), {"candidate": candidate, "record": record})

    def test_invalid_prune_is_atomic_and_duplicate_saves_do_not_consume_extra_logical_space(self):
        store = self.store(max_bytes=32768)
        store.start(self.context, now=100)
        for _ in range(20):
            store.save_task(self.task_id, self.candidate, self.record)
        with self.assertRaises(self.module().CollectionCheckpointError):
            store.prune(iter([self.task_id, "../invalid"]))
        self.assertEqual(store.load_task(self.task_id)["record"], self.record)
        store.prune([])
        self.assertIsNone(store.load_task(self.task_id))

    def test_clear_removes_old_ciphertext_from_free_pages(self):
        store = self.store()
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        with fixture_database(self.path) as cursor:
            ciphertext = cursor.execute("SELECT payload FROM checkpoint_tasks").fetchone()[0]
        self.assertIn(ciphertext, self.path.read_bytes())
        store.clear()
        self.assertNotIn(ciphertext, self.path.read_bytes())

    def test_reparse_point_attributes_are_rejected_even_when_lstat_says_regular(self):
        self.path.write_bytes(b"fictional-untouched")
        original = Path.lstat
        def reparse_lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.path:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=info.st_size,
                                       st_dev=info.st_dev, st_ino=info.st_ino, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", reparse_lstat):
            with self.assertRaises(self.module().CollectionCheckpointError):
                self.store().start(self.context, now=100)
        self.assertEqual(self.path.read_bytes(), b"fictional-untouched")

    def test_lease_removed_during_session_prevents_further_writes(self):
        if os.name == "nt":
            self.skipTest("Windows open handles already prevent deleting the lease")
        store = self.store()
        store.start(self.context, now=100)
        self.path.with_name(self.path.name + ".lease").unlink()
        with self.assertRaises(self.module().CollectionCheckpointError):
            store.save_task(self.task_id, self.candidate, self.record)

    def test_windows_device_and_alternate_stream_path_syntax_is_rejected(self):
        for name in ("settings.json:checkpoint", "NUL", "CON.db", "aux", "COM1", "LPT9.cache"):
            with self.subTest(name=name):
                unsafe = self.module().DpapiCollectionCheckpoint(Path(self.temp.name) / name, protect=protect, unprotect=unprotect)
                self.addCleanup(unsafe.close)
                with self.assertRaises(self.module().CollectionCheckpointError):
                    unsafe.start(self.context, now=100)

    def test_file_descriptors_explicitly_use_binary_mode_for_windows_marker_and_header_reads(self):
        binary_flag = getattr(os, "O_BINARY", getattr(os, "O_CLOEXEC", 0x80000))
        actual_open = os.open
        opened_flags = []
        def binary_open(path, flags, *args, **kwargs):
            opened_flags.append(flags)
            return actual_open(path, flags, *args, **kwargs)
        with patch.object(os, "O_BINARY", binary_flag, create=True), patch.object(os, "open", side_effect=binary_open):
            store = self.store()
            store.start(self.context, now=100)
            store.close()
            store.start(self.context, now=101)
            store.close()
        self.assertGreaterEqual(len(opened_flags), 4)
        self.assertTrue(all(flags & binary_flag for flags in opened_flags))

    def test_real_dpapi_has_no_non_windows_fallback(self):
        if os.name == "nt":
            self.skipTest("This checks the non-Windows refusal")
        store = self.module().DpapiCollectionCheckpoint(self.path)
        self.addCleanup(store.close)
        with self.assertRaises(self.module().CollectionCheckpointError):
            store.start(self.context, now=100)
        self.assertNotIn(b"fictional-org", self.path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_windows_dpapi_roundtrip_reopens_and_releases_file_handles(self):
        store = self.module().DpapiCollectionCheckpoint(self.path)
        self.addCleanup(store.close)
        store.start(self.context, now=100)
        store.save_task(self.task_id, self.candidate, self.record)
        store.close()
        resumed = self.module().DpapiCollectionCheckpoint(self.path)
        self.addCleanup(resumed.close)
        self.assertTrue(resumed.start(self.context, now=101)["resumed"])
        self.assertEqual(resumed.load_task(self.task_id)["record"], self.record)
        resumed.clear()
        self.path.rename(self.path.with_suffix(".moved"))


if __name__ == "__main__":
    unittest.main()
