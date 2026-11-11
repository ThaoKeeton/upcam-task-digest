"""Per-user DPAPI storage for Windows credentials."""

from __future__ import annotations

import ctypes
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .config import atomic_write


SECRET_FIELDS = (
    "teambition_mcp_token",
    "dingtalk_webhook",
    "dingtalk_signing_secret",
    "openai_api_key",
)
# Retained solely to preserve existing encrypted data, never used for authentication.
LEGACY_SECRET_FIELDS = ("teambition_app_id", "teambition_app_secret")
UPDATE_SECRET_FIELDS = ("github_update_token",)
INTERNAL_SECRET_FIELDS = ("delivery_ledger_id",)
MCP_AUTHORIZATION_FIELD = "teambition_mcp_authorized_identity"
ALL_SECRET_FIELDS = (SECRET_FIELDS + UPDATE_SECRET_FIELDS + INTERNAL_SECRET_FIELDS
                     + LEGACY_SECRET_FIELDS + (MCP_AUTHORIZATION_FIELD,))


class SecretStoreError(RuntimeError):
    """Credential data is unavailable or could not be protected."""


class DpapiSecretStore:
    """Encrypt secrets with Windows DPAPI for the current Windows user."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        clear = self._unprotect(self.path.read_bytes())
        try:
            values = json.loads(clear.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStoreError("Encrypted credentials are damaged") from exc
        return self._validate(values)

    def save(self, values: Mapping[str, str]) -> None:
        clean = self._validate(dict(values))
        payload = json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write(self.path, self._protect(payload))

    @staticmethod
    def _validate(values: object) -> dict[str, str]:
        if not isinstance(values, dict) or set(values) - set(ALL_SECRET_FIELDS):
            raise SecretStoreError("Credential data has an unsupported shape")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
            raise SecretStoreError("Credentials must be text values")
        return dict(values)

    @staticmethod
    def _dpapi(data: bytes, unprotect: bool) -> bytes:
        if os.name != "nt":
            raise SecretStoreError("Windows DPAPI is available only in the Windows application")

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        raw = ctypes.create_string_buffer(data, max(1, len(data)))
        input_blob = DataBlob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
        output_blob = DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if unprotect:
            function = crypt32.CryptUnprotectData
            function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.POINTER(DataBlob)]
            function.restype = ctypes.c_int
            succeeded = function(ctypes.byref(input_blob), None, None, None, None, 0x1,
                                 ctypes.byref(output_blob))
        else:
            function = crypt32.CryptProtectData
            function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.POINTER(DataBlob)]
            function.restype = ctypes.c_int
            succeeded = function(ctypes.byref(input_blob), "UPCAM Task Digest credentials",
                                 None, None, None, 0x1, ctypes.byref(output_blob))
        if not succeeded:
            raise SecretStoreError("Windows could not protect or unlock local credentials")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(output_blob.pbData)

    @classmethod
    def _protect(cls, data: bytes) -> bytes:
        return cls._dpapi(data, unprotect=False)

    @classmethod
    def _unprotect(cls, data: bytes) -> bytes:
        return cls._dpapi(data, unprotect=True)


class MemorySecretStore:
    """In-memory store for synthetic tests; it never writes credentials to disk."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        return dict(self.values)

    def save(self, values: Mapping[str, str]) -> None:
        self.values = DpapiSecretStore._validate(dict(values))


def revoke_missing_mcp_authorization(settings: dict, credentials: Mapping[str, str]) -> dict:
    """Old App credentials cannot retain an authorization or automatic-run claim."""
    if mcp_connection_authorized(credentials):
        return settings
    migrated = dict(settings)
    previous_view = settings.get("view", {})
    if ("mcp_migration_previous_authorization" not in migrated and
            (settings.get("auto_send_enabled") or settings.get("auto_send_enabled_since_local")
             or settings.get("api_contract_verified") or previous_view.get("verified")
             or previous_view.get("evidence"))):
        # Recovery reference only: never read by authorization, scope or scheduling.
        # Preserve the first pre-MCP preferences instead of replacing them on restart.
        migrated["mcp_migration_previous_authorization"] = {
            "auto_send_enabled": settings.get("auto_send_enabled", False),
            "auto_send_enabled_since_local": settings.get("auto_send_enabled_since_local", ""),
            "api_contract_verified": settings.get("api_contract_verified", False),
            "view": deepcopy(previous_view),
        }
    return dict(migrated, api_contract_verified=False, auto_send_enabled=False,
                auto_send_enabled_since_local="",
                view=dict(previous_view, verified=False, evidence=""))


def teambition_mcp_identity(credentials: Mapping[str, str]) -> str | None:
    """Bind proofs to a single MCP Token without exposing the credential."""
    token = credentials.get("teambition_mcp_token")
    if not isinstance(token, str) or not token.strip():
        return None
    return hashlib.sha256(("teambition-mcp/v1\0" + token).encode("utf-8")).hexdigest()


def mcp_connection_authorized(credentials: Mapping[str, str]) -> bool:
    """Only a deliberate local grant for this Token enables official MCP use."""
    identity = teambition_mcp_identity(credentials)
    return bool(identity and credentials.get(MCP_AUTHORIZATION_FIELD) == identity)
