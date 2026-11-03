"""Local run history; human approval is not part of the delivery state machine."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import uuid


class HistoryError(RuntimeError):
    """A run cannot be moved to the requested state."""


_STATES = ("preview", "prepared", "sending", "sent", "blocked", "delivery_unknown")


class HistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self):
        with self._connect() as db:
            existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='digest_runs'").fetchone()
            if existing:
                columns = {row[1] for row in db.execute("PRAGMA table_info(digest_runs)")}
                if "approved_at_utc" in columns:
                    self._migrate_approval_schema(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS digest_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at_utc TEXT NOT NULL,
                    window_start_local TEXT NOT NULL,
                    window_end_local TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('teambition', 'sample')),
                    state TEXT NOT NULL CHECK(state IN ('preview','prepared','sending','sent','blocked','delivery_unknown')),
                    digest_markdown TEXT NOT NULL,
                    digest_sha256 TEXT NOT NULL,
                    schedule_key TEXT UNIQUE,
                    delivery_note TEXT
                );
                CREATE INDEX IF NOT EXISTS digest_runs_created_idx ON digest_runs(created_at_utc DESC);
            """)

    @staticmethod
    def _migrate_approval_schema(db: sqlite3.Connection):
        db.executescript("""
            ALTER TABLE digest_runs RENAME TO digest_runs_with_approval;
            CREATE TABLE digest_runs (
                run_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                window_start_local TEXT NOT NULL,
                window_end_local TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('teambition', 'sample')),
                state TEXT NOT NULL CHECK(state IN ('preview','prepared','sending','sent','blocked','delivery_unknown')),
                digest_markdown TEXT NOT NULL,
                digest_sha256 TEXT NOT NULL,
                schedule_key TEXT UNIQUE,
                delivery_note TEXT
            );
            INSERT INTO digest_runs
                (run_id, created_at_utc, window_start_local, window_end_local, source,
                 state, digest_markdown, digest_sha256, schedule_key, delivery_note)
            SELECT run_id, created_at_utc, window_start_local, window_end_local, source,
                CASE state
                    WHEN 'draft' THEN CASE source WHEN 'sample' THEN 'preview' ELSE 'prepared' END
                    WHEN 'approved' THEN 'prepared'
                    WHEN 'sending' THEN 'delivery_unknown'
                    WHEN 'sent' THEN 'sent'
                    WHEN 'failed' THEN 'blocked'
                    WHEN 'delivery_unknown' THEN 'delivery_unknown'
                    ELSE 'blocked'
                END,
                digest_markdown, digest_sha256, schedule_key, delivery_note
            FROM digest_runs_with_approval;
            DROP TABLE digest_runs_with_approval;
        """)

    def create_draft(self, start: datetime, end: datetime, source: str, markdown: str,
                     schedule_key: str | None = None) -> str:
        """Persist a preview or a fully locally validated automatic report."""
        if source not in ("teambition", "sample"):
            raise HistoryError("Unknown digest source")
        if end <= start or not markdown.strip():
            raise HistoryError("A digest needs a valid collection range and content")
        run_id = str(uuid.uuid4())
        digest_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        state = "prepared" if source == "teambition" else "preview"
        with self._connect() as db:
            try:
                db.execute("""INSERT INTO digest_runs
                    (run_id, created_at_utc, window_start_local, window_end_local, source,
                     state, digest_markdown, digest_sha256, schedule_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, _utc_now(), start.isoformat(timespec="minutes"),
                     end.isoformat(timespec="minutes"), source, state, markdown, digest_hash, schedule_key))
            except sqlite3.IntegrityError as exc:
                raise HistoryError("This scheduled slot already has a run record") from exc
        return run_id

    def start_delivery(self, run_id: str) -> dict:
        """Move a validated run directly to sending; no approval transition exists."""
        with self._connect() as db:
            cur = db.execute("UPDATE digest_runs SET state='sending', delivery_note=NULL "
                             "WHERE run_id=? AND state='prepared' AND source='teambition'", (run_id,))
            if cur.rowcount != 1:
                raise HistoryError("Only a validated Teambition report can start delivery once")
            row = db.execute("SELECT * FROM digest_runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row)

    def finish_delivery(self, run_id: str, state: str, note: str) -> None:
        if state not in ("sent", "blocked", "delivery_unknown"):
            raise HistoryError("Unsupported delivery result")
        with self._connect() as db:
            cur = db.execute("UPDATE digest_runs SET state=?, delivery_note=? WHERE run_id=? AND state='sending'",
                             (state, note[:240], run_id))
        if cur.rowcount != 1:
            raise HistoryError("The run is no longer waiting for a delivery result")

    def recover_interrupted_sends(self) -> int:
        """A crash during a send is ambiguous; never auto-send it again."""
        with self._connect() as db:
            cur = db.execute("UPDATE digest_runs SET state='delivery_unknown', delivery_note=? WHERE state='sending'",
                             ("The app closed while delivery was in progress; verify DingTalk before action.",))
            return cur.rowcount

    def get(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM digest_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 200) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM digest_runs ORDER BY created_at_utc DESC LIMIT ?",
                              (max(1, min(limit, 1000)),)).fetchall()
        return [dict(row) for row in rows]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
