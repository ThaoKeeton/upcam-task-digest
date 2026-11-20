"""Durable claims for explicitly confirmed, standalone-chat webhook sends.

The caller must bind a fresh user confirmation to the exact current answer and
frozen destination before calling this API. This module does not obtain consent,
read credentials, access a network, add mentions, or provision ledgers. Unknown
attempts can be resolved only through a separate explicit user-reviewed action.
Its guarantees require the same intact SQLite ledger on verified persistent
local storage, shared by all chat senders. They do not imply
exactly-once delivery or cover independent, replaced, or recreated databases.
"""

from datetime import datetime, timezone
import hashlib
import re
import sqlite3
import uuid
from urllib.parse import parse_qsl, urlsplit

from delivery_guard import DeliveryBlocked, _outside_repository


_CHAT_TABLE = """
    CREATE TABLE IF NOT EXISTS chat_attempts (
        request_id TEXT PRIMARY KEY,
        destination_hash TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_flight','confirmed','unknown'))
    )
"""
_CHAT_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS chat_attempts_unresolved_destination
        ON chat_attempts(destination_hash) WHERE state IN ('in_flight','unknown')
"""

_CHAT_TOMBSTONES = """
    CREATE TABLE IF NOT EXISTS chat_attempt_tombstones (
        request_id TEXT PRIMARY KEY,
        destination_hash TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        original_state TEXT NOT NULL CHECK (original_state = 'unknown'),
        user_reviewed INTEGER NOT NULL CHECK (user_reviewed = 1)
    )
"""

_CHAT_IDENTITY_VERSION = 'dingtalk-robot-v1+opaque-sha256-v1'
_CHAT_IDENTITY = """
    CREATE TABLE IF NOT EXISTS chat_delivery_identity (version TEXT NOT NULL)
"""
_CHAT_ATTEMPT_METADATA = """
    CREATE TABLE IF NOT EXISTS chat_attempt_metadata (
        request_id TEXT PRIMARY KEY,
        attempted_at_utc TEXT NOT NULL
    )
"""


def _canonical_request_id(request_id):
    try:
        parsed = uuid.UUID(request_id) if isinstance(request_id, str) else None
    except (ValueError, AttributeError):
        return False
    return parsed is not None and parsed.version == 4 and str(parsed) == request_id


def _destination_hash(destination):
    """Normalize real robot identity, never mutable signatures or timestamps.

    Non-DingTalk synthetic strings retain their opaque SHA-256 identity. A URL
    purporting to be a DingTalk robot must satisfy the supported official HTTPS
    endpoint and contain exactly one unambiguous decoded access token.
    """
    if not isinstance(destination, str) or not destination:
        raise ValueError('Invalid destination')
    try:
        parsed = urlsplit(destination)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict')
        robot_url = ('dingtalk' in parsed.netloc.lower()
                     or 'access_token' in parsed.query.lower()
                     or any(key.lower() == 'access_token' for key, _ in pairs))
        if not robot_url:
            return hashlib.sha256(destination.encode('utf-8')).hexdigest()
        if (any(ord(character) <= 32 or ord(character) == 127 for character in destination)
                or parsed.scheme != 'https' or parsed.hostname != 'oapi.dingtalk.com'
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.path != '/robot/send'
                or re.search(r'%(?![0-9a-fA-F]{2})', parsed.query)):
            raise ValueError('Invalid robot destination')
        keys = [key for key, _ in pairs]
        if (keys.count('access_token') != 1 or len(keys) != len(set(keys))
                or any(key not in {'access_token', 'sign', 'timestamp'} for key in keys)):
            raise ValueError('Ambiguous robot destination')
        token = next(value for key, value in pairs if key == 'access_token')
        if re.fullmatch(r'[A-Za-z0-9_-]{1,256}', token) is None:
            raise ValueError('Invalid robot token')
        identity = 'dingtalk-robot-v1\x00https://oapi.dingtalk.com/robot/send\x00' + token
        return hashlib.sha256(identity.encode('utf-8')).hexdigest()
    except (ValueError, UnicodeError):
        raise ValueError('Invalid destination') from None


class ChatDelivery:
    """Use only an existing, independently identified persistent shared ledger."""

    def __init__(self, path, expected_identity, *, persistent_storage_verified=False):
        if persistent_storage_verified is not True:
            raise DeliveryBlocked('Persistent shared storage has not been verified')
        self.path = _outside_repository(path)
        if not self.path.is_file():
            raise DeliveryBlocked('Ledger missing; refusing to recreate send history')
        try:
            # mode=rw cannot silently create missing send history. Durability and
            # timeout match DeliveryGuard; this schema does not touch its rows.
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('BEGIN IMMEDIATE')
            identities = self.db.execute('SELECT identity FROM metadata').fetchall()
            if not isinstance(expected_identity, str) or not expected_identity or identities != [(expected_identity,)]:
                raise DeliveryBlocked('Ledger identity mismatch')
            self.db.execute(_CHAT_TABLE)
            self.db.execute(_CHAT_INDEX)
            self.db.execute(_CHAT_TOMBSTONES)
            self.db.execute(_CHAT_IDENTITY)
            versions = self.db.execute('SELECT version FROM chat_delivery_identity').fetchall()
            if not versions:
                # Old raw-URL hashes cannot be attributed to a normalized robot.
                # Never invent that mapping or silently treat old history as empty.
                active = self.db.execute('SELECT 1 FROM chat_attempts LIMIT 1').fetchone()
                resolved = self.db.execute('SELECT 1 FROM chat_attempt_tombstones LIMIT 1').fetchone()
                if active is not None or resolved is not None:
                    raise DeliveryBlocked('Legacy chat delivery identity cannot be safely validated')
                self.db.execute('INSERT INTO chat_delivery_identity VALUES (?)', (_CHAT_IDENTITY_VERSION,))
            elif versions != [(_CHAT_IDENTITY_VERSION,)]:
                raise DeliveryBlocked('Chat delivery identity version mismatch')
            self.db.execute(_CHAT_ATTEMPT_METADATA)
            self.db.commit()
        except BaseException as error:
            if hasattr(self, 'db'):
                self.db.close()
            if isinstance(error, DeliveryBlocked):
                raise
            if isinstance(error, sqlite3.Error):
                raise DeliveryBlocked('Ledger cannot be opened or validated') from None
            raise

    def close(self):
        self.db.close()

    def attempt(self, request_id, destination, content, send):
        """Return confirmed, not_sent, or unknown without retrying a sender.

        send(content) must return exactly True only on provider acknowledgement.
        The callback receives the original, unmodified text. A confirmed UUID
        or user-resolved UUID replay is not_sent; an unresolved UUID or
        destination is unknown. Only a new explicit UUID can repeat an
        already-confirmed destination. The claim
        and its destination-wide unresolved gate are durable before dispatch.

        not_sent means this invocation did not call send. A failed or interrupted
        claim commit can nevertheless leave a durable in_flight row, which still
        blocks future attempts. Any error after admission returns unknown, even
        if the remote side effect or final state commit may have succeeded.
        """
        if (not _canonical_request_id(request_id)
                or not isinstance(content, str) or not content or not callable(send)):
            return 'not_sent'
        try:
            destination_hash = _destination_hash(destination)
            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        except (ValueError, UnicodeError):
            return 'not_sent'
        claim = self._claim(request_id, destination_hash, content_hash)
        if claim != 'claimed':
            return claim

        confirmed = False
        try:
            confirmed = send(content) is True
        except BaseException:
            # Provider errors and interruption can contain raw webhook URLs,
            # credentials, or bodies. Never expose or persist exception details.
            pass
        try:
            self.db.execute('BEGIN IMMEDIATE')
            updated = self.db.execute(
                "UPDATE chat_attempts SET state=? WHERE request_id=? AND state='in_flight'",
                ('confirmed' if confirmed else 'unknown', request_id))
            if updated.rowcount != 1:
                raise sqlite3.DatabaseError('Delivery claim is no longer intact')
            self.db.commit()
        except BaseException:
            self._rollback()
            return 'unknown'
        return 'confirmed' if confirmed else 'unknown'

    def peek_unresolved(self, destination):
        """Read one exact destination's claim without modifying the ledger.

        Return only state, request UUID, destination/content hashes, and the UTC
        claim time (None for a record without optional time metadata). The caller
        must retain this snapshot and bind confirmation to its exact request ID.
        """
        snapshot = dict(state='not_found', request_id=None, destination_hash=None,
                        content_hash=None, attempted_at_utc=None)
        try:
            destination_hash = _destination_hash(destination)
        except ValueError:
            return snapshot
        snapshot['destination_hash'] = destination_hash
        try:
            prior = self.db.execute(
                'SELECT a.state, a.request_id, a.destination_hash, a.content_hash, m.attempted_at_utc '
                'FROM chat_attempts a LEFT JOIN chat_attempt_metadata m ON m.request_id=a.request_id '
                "WHERE a.destination_hash=? AND a.state IN ('in_flight','unknown') LIMIT 1",
                (destination_hash,)).fetchone()
        except sqlite3.Error:
            raise DeliveryBlocked('Chat delivery record could not be safely inspected', outcome='unknown') from None
        if prior is not None:
            snapshot.update(zip(('state', 'request_id', 'destination_hash', 'content_hash',
                                 'attempted_at_utc'), prior))
        return snapshot

    def resolve_unknown(self, destination, request_id, *, expected_content_hash, expected_attempted_at_utc):
        """Release only the exact unknown request explicitly reviewed by a user.

        The caller must first show peek_unresolved's record, ask the user to check
        the provider, and obtain confirmation allowing new messages for that
        exact request, destination, full content hash, and original UTC time.
        All fields are matched inside the same writer transaction. A stale
        confirmation for A cannot release a later B. This never sends or retries a message, resolves in_flight work,
        or turns an unknown result into success. Hash-only tombstones retain the
        original unknown state and user-reviewed marker, preventing UUID replay.

        Returns resolved, not_found, or in_flight. Storage errors raise a generic
        DeliveryBlocked rather than claiming a resolution was safely persisted.
        """
        if not _canonical_request_id(request_id):
            return 'not_found'
        try:
            destination_hash = _destination_hash(destination)
        except ValueError:
            return 'not_found'
        try:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute(
                'SELECT a.request_id, a.destination_hash, a.content_hash, a.state, m.attempted_at_utc '
                'FROM chat_attempts a LEFT JOIN chat_attempt_metadata m ON m.request_id=a.request_id '
                "WHERE a.request_id=? AND a.destination_hash=? AND a.state IN ('in_flight','unknown')",
                (request_id, destination_hash)).fetchone()
            if prior is None:
                self.db.rollback()
                return 'not_found'
            if prior[3] == 'in_flight':
                self.db.rollback()
                return 'in_flight'
            if (not isinstance(expected_content_hash, str)
                    or re.fullmatch(r'[0-9a-f]{64}', expected_content_hash) is None
                    or not isinstance(expected_attempted_at_utc, str) or not expected_attempted_at_utc
                    or prior[2] != expected_content_hash or prior[4] != expected_attempted_at_utc):
                self.db.rollback()
                return 'not_found'
            self.db.execute(
                'INSERT INTO chat_attempt_tombstones VALUES (?, ?, ?, ?, 1)', prior[:4])
            removed = self.db.execute(
                "DELETE FROM chat_attempts WHERE request_id=? AND destination_hash=? AND state='unknown' "
                'AND content_hash=? AND EXISTS (SELECT 1 FROM chat_attempt_metadata m '
                'WHERE m.request_id=chat_attempts.request_id AND m.attempted_at_utc=?)',
                (request_id, destination_hash, expected_content_hash, expected_attempted_at_utc))
            if removed.rowcount != 1:
                raise sqlite3.DatabaseError('Unknown delivery claim is no longer intact')
            self.db.commit()
            return 'resolved'
        except BaseException:
            self._rollback()
            raise DeliveryBlocked('Chat recovery could not be safely confirmed', outcome='unknown') from None

    def _claim(self, request_id, destination_hash, content_hash):
        try:
            # The writer lock serializes the replay check, destination-wide gate,
            # and insertion across separate connections/processes. The partial
            # unique index is a second destination-level race barrier.
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute(
                'SELECT state FROM chat_attempts WHERE request_id=?', (request_id,)).fetchone()
            if prior is not None:
                self.db.rollback()
                return 'not_sent' if prior == ('confirmed',) else 'unknown'
            resolved = self.db.execute(
                'SELECT 1 FROM chat_attempt_tombstones WHERE request_id=?', (request_id,)).fetchone()
            if resolved is not None:
                self.db.rollback()
                return 'not_sent'
            unresolved = self.db.execute(
                "SELECT 1 FROM chat_attempts WHERE destination_hash=? "
                "AND state IN ('in_flight','unknown') LIMIT 1", (destination_hash,)).fetchone()
            if unresolved is not None:
                self.db.rollback()
                return 'unknown'
            self.db.execute(
                "INSERT INTO chat_attempts VALUES (?, ?, ?, 'in_flight')",
                (request_id, destination_hash, content_hash))
            self.db.execute('INSERT INTO chat_attempt_metadata VALUES (?, ?)',
                            (request_id, datetime.now(timezone.utc).isoformat()))
            self.db.commit()
            return 'claimed'
        except BaseException:
            # No dispatch has occurred, even if a commit interrupted after it
            # persisted. Such an in_flight record must never be auto-cleared.
            self._rollback()
            return 'not_sent'

    def _rollback(self):
        try:
            self.db.rollback()
        except BaseException:
            pass
