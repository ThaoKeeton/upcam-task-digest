"""Separate at-most-one-attempt gate. No HTTP sender and no automatic retries.

Guarantees apply only to workers sharing this exact intact SQLite database on a
validated persistent local filesystem. They do NOT cover ephemeral/recreated
storage, independent databases, remote side effects, or exactly-once delivery.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from report_contract import validated_report_source


class DeliveryBlocked(RuntimeError):
    pass


def _outside_repository(path):
    path = Path(path).resolve()
    if path == Path(":memory:").resolve() or Path(__file__).resolve().parent in path.parents:
        raise DeliveryBlocked("Ledger must live outside the repository")
    return path


def initialize_ledger(path):
    """Explicit provisioning only. Save returned identity independently of DB."""
    path = _outside_repository(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    ledger_id = str(uuid.uuid4())
    # SQLite's context manager commits/rolls back but does not close the connection.
    # Exit the transaction first, then close on both success and failure.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE metadata (identity TEXT NOT NULL);
            CREATE TABLE deliveries (
                key TEXT PRIMARY KEY, digest_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('in_flight','confirmed','unknown'))
            );
        """)
        db.execute("INSERT INTO metadata VALUES (?)", (ledger_id,))
    # Database commits sync file data; persist the newly created directory entry.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return ledger_id


class DeliveryGuard:
    def __init__(self, path, expected_identity, *, persistent_storage_verified=False):
        if not persistent_storage_verified:
            raise DeliveryBlocked("Persistent shared storage has not been verified")
        self.path = _outside_repository(path)
        if not self.path.is_file():
            raise DeliveryBlocked("Ledger missing; refusing to recreate send history")
        # mode=rw prevents SQLite's default silent database creation.
        try:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            self.db.execute("PRAGMA synchronous=FULL")
            identities = self.db.execute("SELECT identity FROM metadata").fetchall()
            if identities != [(expected_identity,)]:
                self.db.close()
                raise DeliveryBlocked("Ledger identity mismatch")
        except sqlite3.Error:
            if hasattr(self, "db"):
                self.db.close()
            raise DeliveryBlocked("Ledger cannot be opened or validated") from None

    def close(self):
        self.db.close()

    def attempt(self, report, *, tenant, project, destination, send):
        """send(markdown) must return exactly True ONLY after provider success.

        A timeout, rejection, unexpected response, crash or failed local commit
        leaves unknown/in_flight and blocks all further attempts. Reconciliation
        is a separate manual operation; there is deliberately no retry/reset API.
        """
        if report.get("ready_to_send") is not True or report.get("issues"):
            raise DeliveryBlocked("Report requires review")
        if any(not isinstance(v, str) or not v for v in (tenant, project, destination)):
            raise DeliveryBlocked("Missing delivery scope")
        try:
            source_tenant, source_project, report_day = validated_report_source(report)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DeliveryBlocked("Invalid report source, canonical date or window") from None
        if (tenant, project) != (source_tenant, source_project):
            raise DeliveryBlocked("Delivery scope differs from report source")
        # Content is NOT part of the key: regenerated text cannot bypass the gate.
        key = hashlib.sha256(json.dumps([source_tenant, source_project, report_day, destination],
                                       separators=(",", ":")).encode()).hexdigest()
        digest_hash = hashlib.sha256(report["markdown"].encode()).hexdigest()
        try:
            with self.db:
                self.db.execute("INSERT INTO deliveries VALUES (?, ?, 'in_flight')", (key, digest_hash))
        except sqlite3.IntegrityError:
            raise DeliveryBlocked("Delivery already claimed; reconcile before any further action") from None
        except sqlite3.Error:
            raise DeliveryBlocked("Claim not safely committed; no send attempted") from None
        # No sender call can occur before the durable claim transaction commits.
        confirmed = False
        try:
            confirmed = send(report["markdown"]) is True
        except BaseException:
            # Includes interruption after provider acceptance. Never expose sender
            # exceptions: they can contain a webhook URL or response body.
            pass
        try:
            with self.db:
                self.db.execute("UPDATE deliveries SET state=? WHERE key=?",
                                ("confirmed" if confirmed else "unknown", key))
        except sqlite3.Error:
            raise DeliveryBlocked("Delivery outcome uncertain; do not retry") from None
        if not confirmed:
            raise DeliveryBlocked("Delivery outcome unknown or rejected; do not retry")
        return "confirmed"
