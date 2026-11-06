"""Short-lived, current-user encrypted progress for complete task reads only.

Only this cache's owned SQLite format is writable. API cursors, requests, send
state and credentials are never part of the checkpoint interface. The caller
supplies a credential identity digest, rather than a credential getter.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

from .secrets import DpapiSecretStore


_APPLICATION_ID = 0x55434350
_SCHEMA_VERSION = 1
_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
_LEASE_MARKER = b"UPCAM collection checkpoint lease v1\n"
_STATE_KEY = hashlib.sha256(b"UPCAM collection checkpoint state v1").digest()
_COUNTERS = ("requests", "records", "response_bytes", "retained_bytes", "batches", "passes")
_CONTEXT_FIELDS = {"tenant_id", "project_id", "include_archived", "selection", "policy", "credential_identity"}
_SCHEMA = {
    name: f"CREATE TABLE {name} (row_key BLOB PRIMARY KEY NOT NULL, payload BLOB NOT NULL, "
          "logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0)) WITHOUT ROWID"
    for name in ("checkpoint_state", "checkpoint_tasks")
}


class CollectionCheckpointError(RuntimeError):
    """Deliberately excludes paths, SQLite errors and decrypted source content."""

    def __init__(self):
        super().__init__("Local collection checkpoint is unavailable")


class CollectionCheckpointBusy(CollectionCheckpointError):
    """Another process owns the unfinished collection session."""


def _safe_operation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CollectionCheckpointBusy:
            raise CollectionCheckpointBusy() from None
        except CollectionCheckpointError:
            raise CollectionCheckpointError() from None
        except Exception:
            raise CollectionCheckpointError() from None
    return guarded


def _publish_no_replace(source, destination):
    """Atomically expose a complete file without replacing any existing path."""
    if os.name == "nt":
        # Windows rename fails when destination exists, including a concurrent
        # creator. POSIX rename would silently replace it.
        os.rename(source, destination)
        return
    if not sys.platform.startswith("linux"):
        raise CollectionCheckpointError()
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise CollectionCheckpointError()
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "Checkpoint initialization target exists")
        raise OSError(error, "Checkpoint initialization could not be published")


def _json_bytes(value):
    """Accept strict JSON without Python's tuple/key/bool-number coercions."""
    def visit(item, ancestors, depth):
        if depth > 64:
            raise CollectionCheckpointError()
        kind = type(item)
        if kind in (str, int, bool, type(None)):
            return
        if kind is float:
            if not math.isfinite(item):
                raise CollectionCheckpointError()
            return
        if kind not in (dict, list) or id(item) in ancestors:
            raise CollectionCheckpointError()
        ancestors = ancestors | {id(item)}
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise CollectionCheckpointError()
            children = item.values()
        else:
            children = item
        for child in children:
            visit(child, ancestors, depth + 1)
    visit(value, set(), 0)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise CollectionCheckpointError()
    return value


def _task_identity(value):
    if type(value) is not dict:
        raise CollectionCheckpointError()
    identities = [value[key] for key in ("id", "_id") if value.get(key)]
    if not identities and value.get("taskId"):
        identities = [value["taskId"]]
    if not identities or any(identity != identities[0] for identity in identities):
        raise CollectionCheckpointError()
    identity = _identifier(identities[0])
    if "taskId" in value and value["taskId"] != identity:
        raise CollectionCheckpointError()
    return identity


def _validate_context(value):
    if type(value) is not dict or set(value) != _CONTEXT_FIELDS:
        raise CollectionCheckpointError()
    _identifier(value["tenant_id"])
    _identifier(value["project_id"])
    if (type(value["include_archived"]) is not bool or type(value["selection"]) is not dict
            or type(value["policy"]) is not dict or type(value["credential_identity"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["credential_identity"]) is None):
        raise CollectionCheckpointError()
    return _json_bytes(value)


def _validate_counters(value):
    if type(value) is not dict or set(value) not in (set(_COUNTERS), set(_COUNTERS) | {"elapsed_seconds"}):
        raise CollectionCheckpointError()
    if any(type(value[key]) is not int or value[key] < 0 for key in _COUNTERS):
        raise CollectionCheckpointError()
    if "elapsed_seconds" in value and not _nonnegative_number(value["elapsed_seconds"]):
        raise CollectionCheckpointError()
    return dict(value)


def _nonnegative_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _validate_task(task_id, candidate, record):
    _identifier(task_id)
    if (type(record) is not dict or set(record) != {"task", "activities"}
            or type(record["activities"]) is not list
            or any(type(activity) is not dict for activity in record["activities"])
            or _task_identity(candidate) != task_id or _task_identity(record["task"]) != task_id):
        raise CollectionCheckpointError()
    _json_bytes({"candidate": candidate, "record": record})


class DpapiCollectionCheckpoint:
    """An owned SQLite cache containing only hashes, numbers and DPAPI blobs.

    ``now`` and returned ``created_at`` are Unix seconds. ``close`` releases the
    connection and preserves progress; ``clear`` deletes only this cache's rows.
    Injected byte codecs are for synthetic tests, never a plaintext fallback.
    """

    @_safe_operation
    def __init__(self, path, *, protect=None, unprotect=None, max_bytes=160 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 1:
            raise CollectionCheckpointError()
        if (protect is None) != (unprotect is None):
            raise CollectionCheckpointError()
        self.path = Path(path).absolute()
        if ".." in Path(path).parts or self.path.name in ("", ".", ".."):
            raise CollectionCheckpointError()
        self.max_bytes = max_bytes
        self._protect = protect if protect is not None else DpapiSecretStore._protect
        self._unprotect = unprotect if unprotect is not None else DpapiSecretStore._unprotect
        if not callable(self._protect) or not callable(self._unprotect):
            raise CollectionCheckpointError()
        self._connection = None
        self._identity = None
        self._state = None
        self._lease_fd = None
        self._lease_identity = None
        self._lease_path = self.path.with_name(self.path.name + ".lease")

    def _inspect_paths(self, *, create_parent=False):
        if self.path.drive.startswith("\\"):
            raise CollectionCheckpointError()
        for part in self.path.parts:
            if part == self.path.anchor:
                continue
            if (":" in part or part != part.rstrip(" .")
                    or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE)):
                raise CollectionCheckpointError()
        components = list(reversed(self.path.parents)) + [self.path]
        main_info = None
        for component in components:
            try:
                info = component.lstat()
            except FileNotFoundError:
                if component == self.path:
                    break
                if not create_parent:
                    return None
                component.mkdir(mode=0o700)
                info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CollectionCheckpointError()
            if component == self.path:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.max_bytes:
                    raise CollectionCheckpointError()
                main_info = info
            elif not stat.S_ISDIR(info.st_mode):
                raise CollectionCheckpointError()
        for suffix in ("-journal", "-wal", "-shm", ".lease"):
            sidecar = self.path.with_name(self.path.name + suffix)
            try:
                info = sidecar.lstat()
            except FileNotFoundError:
                if suffix == ".lease" and self._lease_fd is not None:
                    raise CollectionCheckpointError()
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or info.st_size > self.max_bytes or suffix in ("-wal", "-shm")
                    or (suffix == "-journal" and main_info is None)):
                raise CollectionCheckpointError()
            if suffix == ".lease" and self._lease_fd is not None and (info.st_dev, info.st_ino) != self._lease_identity:
                raise CollectionCheckpointError()
        return main_info

    def _acquire_lease(self, *, create=True):
        info = self._inspect_paths(create_parent=create)
        if self._lease_fd is not None:
            if not self._lease_path.exists():
                raise CollectionCheckpointError()
            return
        if info is None and not create and not self._lease_path.exists():
            return
        try:
            descriptor = os.open(self._lease_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            try:
                self._stage_initial_file(self._lease_path, self._initialize_lease)
            except FileExistsError:
                # Another initializer published its complete marker first.
                # Lock and validate that file; never replace its inode.
                pass
            descriptor = os.open(self._lease_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            lease_info = os.fstat(descriptor)
            if (not stat.S_ISREG(lease_info.st_mode) or lease_info.st_nlink != 1
                    or getattr(lease_info, "st_file_attributes", 0) & 0x400
                    or lease_info.st_size != len(_LEASE_MARKER)):
                raise CollectionCheckpointError()
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise CollectionCheckpointBusy() from None
                raise
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, len(_LEASE_MARKER) + 1) != _LEASE_MARKER:
                raise CollectionCheckpointError()
            current = self._lease_path.lstat()
            if (stat.S_ISLNK(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400
                    or (current.st_dev, current.st_ino) != (lease_info.st_dev, lease_info.st_ino)):
                raise CollectionCheckpointError()
        except BaseException:
            os.close(descriptor)
            raise
        self._lease_fd = descriptor
        self._lease_identity = (lease_info.st_dev, lease_info.st_ino)

    def _stage_initial_file(self, target, initialize):
        temporary = target.with_name(target.name + ".init-" + uuid.uuid4().hex + ".tmp")
        identity = None
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            os.close(descriptor)
            descriptor = None
            initialize(temporary)
            current = temporary.lstat()
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or getattr(current, "st_file_attributes", 0) & 0x400
                    or (current.st_dev, current.st_ino) != identity or current.st_size > self.max_bytes):
                raise CollectionCheckpointError()
            descriptor = os.open(temporary, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            _publish_no_replace(temporary, target)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if identity is not None:
                try:
                    current = temporary.lstat()
                except FileNotFoundError:
                    current = None
                if current is not None:
                    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                            or getattr(current, "st_file_attributes", 0) & 0x400
                            or (current.st_dev, current.st_ino) != identity):
                        raise CollectionCheckpointError()
                    temporary.unlink()

    @staticmethod
    def _initialize_lease(path):
        descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            if os.write(descriptor, _LEASE_MARKER) != len(_LEASE_MARKER):
                raise CollectionCheckpointError()
        finally:
            os.close(descriptor)

    def _release_lease(self):
        descriptor = self._lease_fd
        if descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._lease_fd = None
            self._lease_identity = None

    def _verify_schema(self, connection):
        if (connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION):
            raise CollectionCheckpointError()
        rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
        if set(rows) != {("table", name, name, sql) for name, sql in _SCHEMA.items()}:
            raise CollectionCheckpointError()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CollectionCheckpointError()

    def _configure_connection(self, connection, *, initialize=False):
        connection.execute("PRAGMA trusted_schema=OFF")
        if not initialize:
            self._verify_schema(connection)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        if initialize:
            connection.execute("PRAGMA auto_vacuum=FULL")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        if self.max_bytes // page_size < 4:
            raise CollectionCheckpointError()
        connection.execute(f"PRAGMA max_page_count={self.max_bytes // page_size}")
        if initialize:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                for sql in _SCHEMA.values():
                    connection.execute(sql)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._verify_schema(connection)

    def _initialize_database(self, path):
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            self._configure_connection(connection, initialize=True)
        finally:
            connection.close()

    def _open(self, *, create=True):
        info = self._inspect_paths(create_parent=create)
        if self._connection is not None:
            if info is None or (info.st_dev, info.st_ino) != self._identity:
                raise CollectionCheckpointError()
            return self._connection
        if info is None and not create:
            return None
        if info is None:
            self._stage_initial_file(self.path, self._initialize_database)
            info = self._inspect_paths()
        descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
                or int.from_bytes(header[68:72], "big") != _APPLICATION_ID
                or int.from_bytes(header[60:64], "big") != _SCHEMA_VERSION):
            raise CollectionCheckpointError()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            current = self._inspect_paths()
            if current is None or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise CollectionCheckpointError()
            self._configure_connection(connection)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._identity = (info.st_dev, info.st_ino)
        return connection

    def _decode(self, payload, logical_bytes):
        limit = min(self.max_bytes, _MAX_PAYLOAD_BYTES)
        if (type(payload) is not bytes or not payload or len(payload) > self.max_bytes
                or type(logical_bytes) is not int or not 1 <= logical_bytes <= limit):
            raise CollectionCheckpointError()
        clear = self._unprotect(payload)
        if type(clear) is not bytes or len(clear) > limit or len(clear) != logical_bytes:
            raise CollectionCheckpointError()

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise CollectionCheckpointError()
                result[key] = value
            return result
        value = json.loads(clear.decode("utf-8"), object_pairs_hook=object_pairs)
        if _json_bytes(value) != clear:
            raise CollectionCheckpointError()
        return value

    def _encode(self, value):
        clear = _json_bytes(value)
        if len(clear) > min(self.max_bytes, _MAX_PAYLOAD_BYTES):
            raise CollectionCheckpointError()
        encrypted = self._protect(clear)
        if type(encrypted) is not bytes or not encrypted or len(encrypted) > self.max_bytes:
            raise CollectionCheckpointError()
        return encrypted, len(clear)

    def _check_storage(self, connection):
        logical_total = 0
        for name in _SCHEMA:
            invalid = connection.execute(
                f"SELECT COUNT(*) FROM {name} WHERE typeof(row_key) != 'blob' OR length(row_key) != 32 "
                "OR typeof(payload) != 'blob' OR length(payload) < 1 OR length(payload) > ? "
                "OR typeof(logical_bytes) != 'integer' OR logical_bytes < 1 OR logical_bytes > ?",
                (self.max_bytes, min(self.max_bytes, _MAX_PAYLOAD_BYTES)),
            ).fetchone()[0]
            if invalid:
                raise CollectionCheckpointError()
            logical_total += connection.execute(f"SELECT COALESCE(SUM(logical_bytes),0) FROM {name}").fetchone()[0]
        if logical_total > self.max_bytes:
            raise CollectionCheckpointError()

    def _decode_task(self, row_key, payload, logical_bytes, state, *, task_id=None):
        value = self._decode(payload, logical_bytes)
        if (type(value) is not dict
                or set(value) != {"schema_version", "complete", "session_nonce", "context_digest", "task_id", "candidate", "record"}
                or type(value["schema_version"]) is not int or value["schema_version"] != _SCHEMA_VERSION
                or value["complete"] is not True or value["session_nonce"] != state["session_nonce"]
                or value["context_digest"] != state["context_digest"]
                or (task_id is not None and value["task_id"] != task_id)):
            raise CollectionCheckpointError()
        _validate_task(value["task_id"], value["candidate"], value["record"])
        if hashlib.sha256(value["task_id"].encode("ascii")).digest() != row_key:
            raise CollectionCheckpointError()
        return {"candidate": value["candidate"], "record": value["record"]}

    @contextmanager
    def _transaction(self, connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._inspect_paths()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _write_row(self, connection, table, key, encoded):
        payload, logical_bytes = encoded
        total = sum(connection.execute(f"SELECT COALESCE(SUM(logical_bytes), 0) FROM {name}").fetchone()[0]
                    for name in _SCHEMA)
        previous = connection.execute(f"SELECT logical_bytes FROM {table} WHERE row_key=?", (key,)).fetchone()
        if total - (previous[0] if previous else 0) + logical_bytes > self.max_bytes:
            raise CollectionCheckpointError()
        connection.execute(f"INSERT INTO {table}(row_key,payload,logical_bytes) VALUES(?,?,?) "
                           "ON CONFLICT(row_key) DO UPDATE SET payload=excluded.payload,logical_bytes=excluded.logical_bytes",
                           (key, payload, logical_bytes))

    def _load_state(self, connection):
        rows = connection.execute("SELECT row_key,payload,logical_bytes FROM checkpoint_state").fetchall()
        if not rows:
            if connection.execute("SELECT COUNT(*) FROM checkpoint_tasks").fetchone()[0]:
                raise CollectionCheckpointError()
            return None
        if len(rows) != 1 or rows[0][0] != _STATE_KEY:
            raise CollectionCheckpointError()
        state = self._decode(*rows[0][1:])
        if (type(state) is not dict
                or set(state) != {"schema_version", "kind", "session_nonce", "context", "context_digest", "created_at", "counters"}
                or type(state["schema_version"]) is not int or state["schema_version"] != _SCHEMA_VERSION
                or state["kind"] != "state" or type(state["session_nonce"]) is not str
                or re.fullmatch(r"[0-9a-f]{32}", state["session_nonce"]) is None
                or not _nonnegative_number(state["created_at"])):
            raise CollectionCheckpointError()
        if hashlib.sha256(_validate_context(state["context"])).hexdigest() != state["context_digest"]:
            raise CollectionCheckpointError()
        _validate_counters(state["counters"])
        return state

    @_safe_operation
    def start(self, context: dict, *, now: float, ttl_seconds: int = 7200) -> dict:
        context_bytes = _validate_context(context)
        if not _nonnegative_number(now) or type(ttl_seconds) is not int or ttl_seconds < 1:
            raise CollectionCheckpointError()
        self._state = None
        self._acquire_lease()
        connection = self._open()
        with self._transaction(connection):
            self._check_storage(connection)
            state = self._load_state(connection)
            if state is not None and state["created_at"] > now:
                raise CollectionCheckpointError()
            if state is not None:
                for row in connection.execute("SELECT row_key,payload,logical_bytes FROM checkpoint_tasks"):
                    self._decode_task(*row, state)
            resumed = (state is not None and _json_bytes(state["context"]) == context_bytes
                       and now - state["created_at"] < ttl_seconds)
            if not resumed:
                state = {"schema_version": _SCHEMA_VERSION, "kind": "state", "session_nonce": uuid.uuid4().hex,
                         "context": json.loads(context_bytes), "context_digest": hashlib.sha256(context_bytes).hexdigest(),
                         "created_at": float(now), "counters": dict.fromkeys(_COUNTERS, 0)}
                encoded = self._encode(state)
                connection.execute("DELETE FROM checkpoint_tasks")
                connection.execute("DELETE FROM checkpoint_state")
                self._write_row(connection, "checkpoint_state", _STATE_KEY, encoded)
        self._state = state
        return {"created_at": state["created_at"], "counters": dict(state["counters"]), "resumed": resumed}

    def _active(self):
        if self._state is None:
            raise CollectionCheckpointError()
        return self._open(create=False)

    @_safe_operation
    def load_task(self, task_id):
        _identifier(task_id)
        connection = self._active()
        key = hashlib.sha256(task_id.encode("ascii")).digest()
        row = connection.execute("SELECT payload,logical_bytes FROM checkpoint_tasks WHERE row_key=?", (key,)).fetchone()
        if row is None:
            return None
        return self._decode_task(key, *row, self._state, task_id=task_id)

    @_safe_operation
    def save_task(self, task_id, candidate, record):
        _validate_task(task_id, candidate, record)
        connection = self._active()
        value = {"schema_version": _SCHEMA_VERSION, "complete": True, "session_nonce": self._state["session_nonce"],
                 "context_digest": self._state["context_digest"], "task_id": task_id,
                 "candidate": candidate, "record": record}
        encoded = self._encode(value)
        with self._transaction(connection):
            self._write_row(connection, "checkpoint_tasks", hashlib.sha256(task_id.encode("ascii")).digest(), encoded)

    @_safe_operation
    def save_counters(self, counters: dict):
        clean = _validate_counters(counters)
        connection = self._active()
        state = dict(self._state, counters=clean)
        encoded = self._encode(state)
        with self._transaction(connection):
            self._write_row(connection, "checkpoint_state", _STATE_KEY, encoded)
        self._state = state

    @_safe_operation
    def prune(self, task_ids):
        keys = {hashlib.sha256(_identifier(task_id).encode("ascii")).digest() for task_id in task_ids}
        connection = self._active()
        with self._transaction(connection):
            for (key,) in connection.execute("SELECT row_key FROM checkpoint_tasks").fetchall():
                if key not in keys:
                    connection.execute("DELETE FROM checkpoint_tasks WHERE row_key=?", (key,))

    @_safe_operation
    def clear(self):
        self._acquire_lease(create=False)
        connection = self._open(create=False)
        if connection is not None:
            with self._transaction(connection):
                connection.execute("DELETE FROM checkpoint_tasks")
                connection.execute("DELETE FROM checkpoint_state")
        self.close()

    @_safe_operation
    def close(self):
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            self._connection = None
            self._identity = None
            self._state = None
            self._release_lease()
