"""Durable delivery claims, including deliberate manual repeat requests.

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


_MANUAL_ATTEMPT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS manual_attempts (
        request_id TEXT PRIMARY KEY,
        delivery_key TEXT NOT NULL REFERENCES deliveries(key),
        digest_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_flight','confirmed','unknown'))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS manual_attempts_unresolved_scope
        ON manual_attempts(delivery_key) WHERE state IN ('in_flight','unknown');
"""


class DeliveryBlocked(RuntimeError):
    def __init__(self, message, *, outcome="blocked"):
        super().__init__(message)
        self.outcome = outcome


def validate_manual_delivery_options(allow_confirmed_repeat, manual_request_id):
    """Only explicit local requests with canonical UUID v4 IDs can repeat."""
    if type(allow_confirmed_repeat) is not bool:
        raise DeliveryBlocked("手动发送选项无效；未发送消息。")
    if not allow_confirmed_repeat:
        if manual_request_id is not None:
            raise DeliveryBlocked("手动请求标识只能用于明确的手动发送；未发送消息。")
        return
    try:
        parsed = uuid.UUID(manual_request_id) if isinstance(manual_request_id, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != manual_request_id:
        raise DeliveryBlocked("手动发送请求标识无效；请重新点击发送。")


def _outside_repository(path):
    path = Path(path).resolve()
    if path == Path(":memory:").resolve() or Path(__file__).resolve().parent in path.parents:
        raise DeliveryBlocked("Ledger must live outside the repository")
    return path


def initialize_ledger(path):
    """Explicit provisioning only. Save returned identity independently of DB."""
    path = _outside_repository(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
        """ + _MANUAL_ATTEMPT_SCHEMA)
        db.execute("INSERT INTO metadata VALUES (?)", (ledger_id,))
    # Database commits sync file data; persist the newly created directory entry.
    # O_DIRECTORY is Unix-only. SQLite synchronous=FULL remains active on
    # Windows; where directory handles are supported, also persist the entry.
    if hasattr(os, "O_DIRECTORY"):
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
            self.db.execute("PRAGMA foreign_keys=ON")
            identities = self.db.execute("SELECT identity FROM metadata").fetchall()
            if identities != [(expected_identity,)]:
                self.db.close()
                raise DeliveryBlocked("Ledger identity mismatch")
            # Additive migration preserves the original identity and every
            # original delivery row, including unresolved claims.
            self.db.executescript(_MANUAL_ATTEMPT_SCHEMA)
        except sqlite3.Error:
            if hasattr(self, "db"):
                self.db.close()
            raise DeliveryBlocked("Ledger cannot be opened or validated") from None

    def close(self):
        self.db.close()

    def attempt(self, report, *, tenant, project, destination, send,
                current_collection=None, current_config=None, current_model=None,
                current_effort=None, current_service_tier="default",
                allow_confirmed_repeat=False, manual_request_id=None):
        """send(markdown) must return exactly True ONLY after provider success.

        current_* inputs must come independently from the trusted local workflow,
        not be extracted from the report or external task/comment payloads. Only
        an automatically summarized report whose provenance can be independently
        reproduced is deliverable; no human approval token is accepted or needed.

        A timeout, rejection, unexpected response, crash or failed local commit
        leaves unknown/in_flight and blocks all further attempts for the scope.
        Only a fresh manual request can repeat an acknowledged delivery; the
        default and scheduled paths still deduplicate. No reset API exists.
        """
        validate_manual_delivery_options(allow_confirmed_repeat, manual_request_id)
        if not isinstance(report, dict) or report.get("ready_to_send") is not True or report.get("issues"):
            raise DeliveryBlocked("Automatic report validation did not pass")
        # No legacy/plain-report fallback: generation and content are reproduced
        # from independent current inputs, never trusted from the report itself.
        from automated_digest import AutomatedDigestBlocked, validate_automated_report
        try:
            if (current_collection is None or current_config is None
                    or not isinstance(current_model, str) or not current_model
                    or not isinstance(current_effort, str) or not current_effort):
                raise ValueError("Current evidence and model settings are required")
            validate_automated_report(report, current_collection, current_config,
                                      current_model, current_effort, current_service_tier)
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, AutomatedDigestBlocked):
            raise DeliveryBlocked("Invalid, stale or unverified report provenance") from None
        if any(not isinstance(v, str) or not v for v in (tenant, project, destination)):
            raise DeliveryBlocked("Missing delivery scope")
        try:
            source_tenant, source_project, report_day = validated_report_source(report)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DeliveryBlocked("Invalid report source, canonical date or window") from None
        if (tenant, project) != (source_tenant, source_project):
            raise DeliveryBlocked("Delivery scope differs from report source")
        # Content is NOT part of the key: regenerated text cannot bypass the gate.
        # The half-open range distinguishes the two configured daily runs.
        scope = report["scope"]
        key = hashlib.sha256(json.dumps([
            source_tenant, source_project, report_day, scope["start"],
            scope["end_exclusive"], destination,
        ],
                                       separators=(",", ":")).encode()).hexdigest()
        digest_hash = hashlib.sha256(report["markdown"].encode()).hexdigest()
        primary = self._claim(key, digest_hash, manual_request_id)
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
                state = "confirmed" if confirmed else "unknown"
                if primary:
                    self.db.execute("UPDATE deliveries SET state=? WHERE key=? AND state='in_flight'",
                                    (state, key))
                if manual_request_id is not None:
                    self.db.execute("UPDATE manual_attempts SET state=? WHERE request_id=? AND state='in_flight'",
                                    (state, manual_request_id))
        except sqlite3.Error:
            raise DeliveryBlocked("发送结果不确定；请先核实钉钉记录，勿重复发送。", outcome="unknown") from None
        if not confirmed:
            raise DeliveryBlocked("发送结果不确定或未获确认；请先核实钉钉记录，勿重复发送。", outcome="unknown")
        return "confirmed"

    def _claim(self, key, digest_hash, manual_request_id):
        """Serialize scope checks and commit the claim before invoking a sender."""
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if manual_request_id is not None:
                    replay = self.db.execute(
                        "SELECT delivery_key, state FROM manual_attempts WHERE request_id=?",
                        (manual_request_id,)).fetchone()
                    if replay is not None:
                        if replay[1] != "confirmed":
                            raise DeliveryBlocked("该手动请求仍在发送或结果不确定；请先核实钉钉记录，勿重复发送。",
                                                  outcome="unknown")
                        raise DeliveryBlocked("该手动发送请求已完成；再次点击发送会创建新的请求。")
                prior = self.db.execute("SELECT state FROM deliveries WHERE key=?", (key,)).fetchone()
                unresolved = self.db.execute(
                    "SELECT 1 FROM manual_attempts WHERE delivery_key=? AND state IN ('in_flight','unknown') LIMIT 1",
                    (key,)).fetchone()
                if unresolved is not None or prior is not None and prior != ("confirmed",):
                    raise DeliveryBlocked("该时间范围仍在发送或结果不确定；请先核实钉钉记录，勿重复发送。",
                                          outcome="unknown")
                primary = prior is None
                if not primary and manual_request_id is None:
                    raise DeliveryBlocked("该时间范围已确认发送；定时或自动重复发送已阻止。需要重发时请手动点击发送。")
                if primary:
                    self.db.execute("INSERT INTO deliveries VALUES (?, ?, 'in_flight')", (key, digest_hash))
                if manual_request_id is not None:
                    self.db.execute("INSERT INTO manual_attempts VALUES (?, ?, ?, 'in_flight')",
                                    (manual_request_id, key, digest_hash))
            return primary
        except sqlite3.Error:
            raise DeliveryBlocked("发送占用记录未能安全保存；未发送消息。") from None
