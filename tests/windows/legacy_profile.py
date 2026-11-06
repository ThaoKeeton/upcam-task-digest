"""Test-owned schema-1/DPAPI/cache/history fixture; Windows Actions only.

Never reads a user profile. PowerShell creates the empty directory and random
ownership marker first; preparing refuses any existing persisted data. No
transport, provider client, credential environment or sender is used.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from delivery_guard import initialize_ledger
from upcam_app.config import user_data_dir
from upcam_app.history import HistoryStore
from upcam_app.openai_models import CACHE_SCHEMA, MODELS_URL, credential_fingerprint
from upcam_app.secrets import DpapiSecretStore


FILES = ("settings.json", "credentials.dpapi", "openai_models.json")
DUMMY_KEY = "ci-legacy-synthetic-not-real"


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ledger_identity(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT identity FROM metadata").fetchone()[0]


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This fixture is restricted to ephemeral Windows Actions")
    if len(sys.argv) != 4 or sys.argv[1] not in ("prepare", "verify"):
        raise RuntimeError("Invalid fixture invocation")
    root = Path(sys.argv[2]).resolve()
    if root != user_data_dir().resolve() or root.is_symlink():
        raise RuntimeError("Fixture path does not match the guarded CI profile")
    marker = root / ".ordinary-startup-test-owner"
    if marker.is_symlink() or marker.read_text() != sys.argv[3]:
        raise RuntimeError("Fixture directory ownership cannot be established")
    expected_path = root / ".legacy-fixture-expectations.json"
    if sys.argv[1] == "prepare":
        if any((root / name).exists() for name in (*FILES[1:], "history.sqlite3", "delivery_ledger.sqlite3", expected_path.name)):
            raise RuntimeError("Existing persisted files must never be used as fixtures")
        identity = initialize_ledger(root / "delivery_ledger.sqlite3")
        DpapiSecretStore(root / "credentials.dpapi").save(
            {"openai_api_key": DUMMY_KEY, "delivery_ledger_id": identity})
        cache = {"schema_version": CACHE_SCHEMA, "source": MODELS_URL,
                 "fetched_at_utc": "2026-11-01T00:00:00+00:00",
                 "credential_fingerprint": credential_fingerprint(DUMMY_KEY),
                 "models": [{"id": "ci-legacy-model", "owned_by": "synthetic", "shutdown_date": None}]}
        (root / "openai_models.json").write_text(json.dumps(cache), encoding="utf-8")
        history = HistoryStore(root / "history.sqlite3")
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        history.create_draft(start, start + timedelta(hours=1), "sample", "CI synthetic legacy preview")
        expected = {"hashes": {name: fingerprint(root / name) for name in FILES},
                    "history": history.list_runs(), "ledger_identity": identity}
        expected_path.write_text(json.dumps(expected), encoding="utf-8")
        print("Prepared test-owned legacy persistence; no provider credentials or requests")
    else:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        if any(fingerprint(root / name) != value for name, value in expected["hashes"].items()):
            raise AssertionError("Legacy settings, encrypted fixture or model cache changed")
        values = DpapiSecretStore(root / "credentials.dpapi").load()
        if values != {"openai_api_key": DUMMY_KEY, "delivery_ledger_id": expected["ledger_identity"]}:
            raise AssertionError("Synthetic DPAPI fixture could not be preserved and unlocked")
        if HistoryStore(root / "history.sqlite3").list_runs() != expected["history"]:
            raise AssertionError("Legacy history rows changed")
        if ledger_identity(root / "delivery_ledger.sqlite3") != expected["ledger_identity"]:
            raise AssertionError("Legacy delivery identity changed")
        print("Verified legacy settings, DPAPI, cache, history and ledger without exposing values")


if __name__ == "__main__":
    main()
