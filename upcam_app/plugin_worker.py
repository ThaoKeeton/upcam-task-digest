"""Explicit-profile, bounded stdio adapter for the native Workbench plugin.

The standalone worker and its defaults are deliberately separate. The native
client owns the shared Windows singleton and child lifetime; this process owns
only the selected profile and the existing WorkerService business rules.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
from urllib.parse import parse_qsl, urlsplit

from . import APP_VERSION, RULESET_REVISION
from .config import DEFAULT_SETTINGS, SettingsError, SettingsStore, atomic_write, validate_settings
from .preferences import PREFERENCE_DEFAULTS, preference_values
from .secrets import INTERNAL_SECRET_FIELDS, SECRET_FIELDS, MemorySecretStore, SecretStoreError, teambition_mcp_identity, MCP_AUTHORIZATION_FIELD
from .windows_integration import StartupIntegrationError
from .worker_api import WorkerApiError, WorkerService, _reject_duplicate_json_keys


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SYNTHETIC_MARKER = ".plugin-synthetic.json"
_SYNTHETIC_FORMAT = "upcam-plugin-synthetic-v1"
_BLOCKED_OPERATIONS = frozenset({
    "save_preferences", "check_updates", "download_update", "prepare_update", "finish_update",
})
_PROFILE_FILES = (
    "settings.json", "credentials.dpapi", "language.json", "openai_models.json", SYNTHETIC_MARKER,
    *(name + suffix for name in ("history.sqlite3", "delivery_ledger.sqlite3", "collection-progress.sqlite3")
      for suffix in ("", "-journal", "-wal", "-shm")),
)
_PROFILE_ERROR = "Choose an explicit absolute local UPCAM profile without links."
_HANDSHAKE_ERROR = "The plugin worker handshake is invalid or incompatible."
_OPERATION_ERROR = "Operation failed; credentials and service responses were not logged."
_SETTINGS_SIZE_ERROR = "设置内容超出本机配置大小限制；未保存任何更改。"
_AUTO_AUTHORIZATION_ERROR = "尚未保存启用自动发送；请先在设置中明确开启并保存。"
_AUTHORIZED_RUN_PAYLOAD_ERROR = "自动运行授权请求格式无效；未采集或发送。"
_AUTHORIZATION_MISMATCH_ERROR = "自动运行授权已改变；请重新保存设置并明确启动。"
_SNAPSHOT_WRITE_ERROR = "自动运行期间不能修改已核验的设置或凭据；未保存任何更改。"


def _checked_stat(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkerApiError(_PROFILE_ERROR)
    return info


def validate_profile(data_directory, *, synthetic: bool = False) -> Path:
    """Validate before opening anything, without inferring a user directory."""
    try:
        if not isinstance(data_directory, (str, os.PathLike)):
            raise ValueError()
        text = os.fspath(data_directory)
        if not isinstance(text, str) or not text or "\x00" in text or text.startswith(("\\\\", "//")):
            raise ValueError()
        path = Path(text)
        if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
            raise ValueError()
        if os.name == "nt" and any(part.endswith((" ", ".")) or ":" in part for part in path.parts[1:]):
            raise ValueError()
        for component in (*reversed(path.parents), path):
            info = _checked_stat(component)
            if info is not None and not stat.S_ISDIR(info.st_mode):
                raise ValueError()
        canonical = path.resolve(strict=False)
        for name in _PROFILE_FILES:
            info = _checked_stat(canonical / name)
            if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
                raise ValueError()
        if not canonical.exists() or not any(canonical.iterdir()):
            return canonical
        marker = canonical / SYNTHETIC_MARKER
        if synthetic:
            if not marker.is_file() or marker.stat().st_size > 4096:
                raise ValueError()
            fixture = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
            if (not isinstance(fixture, dict) or fixture.get("format") != _SYNTHETIC_FORMAT
                    or set(fixture) - {"format", "delivery_ledger_id"}
                    or not isinstance(fixture.get("delivery_ledger_id", ""), str)
                    or (canonical / "credentials.dpapi").exists()):
                raise ValueError()
        elif marker.exists():
            # An explicit acceptance fixture must never become a live profile.
            raise ValueError()
        settings_path = canonical / "settings.json"
        if not settings_path.is_file() or settings_path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError()
        settings = json.loads(settings_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        if not isinstance(settings, dict) or not {"schema_version", "tenant_id", "project_id", "view"} <= set(settings):
            raise ValueError()
        validate_settings(settings)
        return canonical
    except WorkerApiError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise WorkerApiError(_PROFILE_ERROR) from None


class _PreferencePreservingSettingsStore:
    def __init__(self, store):
        self._store = store

    def load(self):
        return self._store.load()

    def validate_for_save(self, settings):
        previous = preference_values(self._store.load())
        clean = validate_settings(dict(settings, **previous))
        # Match SettingsStore's exact UTF-8 disk representation, including
        # defaults, indentation and the final newline. A compact protocol
        # request can be smaller than the resulting profile document.
        encoded = (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise SettingsError(_SETTINGS_SIZE_ERROR)
        return clean

    def save(self, settings):
        return self._store.save(self.validate_for_save(settings))


class _SnapshotStore:
    """Private immutable session values; never overwrite the real profile."""

    def __init__(self, values, error_type):
        self._values = deepcopy(values)
        self._error_type = error_type

    def load(self):
        return deepcopy(self._values)

    def save(self, values):
        if values != self._values:
            raise self._error_type(_SNAPSHOT_WRITE_ERROR)
        return self.load()


class PluginWorkerService:
    """Require a versioned handshake before constructing the business service."""

    def __init__(self, data_directory, *, synthetic=False, service_factory=None):
        if type(synthetic) is not bool:
            raise WorkerApiError(_PROFILE_ERROR)
        self.data_directory = validate_profile(data_directory, synthetic=synthetic)
        self.synthetic = synthetic
        self._service_factory = service_factory
        self._service = None
        self._handshake_attempted = False
        self._ready = False
        self._shutdown = False
        self._dispatch_lock = threading.RLock()

    def dispatch(self, request):
        # Stdio is sequential; also protect in-process callers from observing
        # another operation's temporary immutable core-store facades.
        with self._dispatch_lock:
            return self._dispatch(request)

    def _dispatch(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not isinstance(request.get("payload", {}), dict):
            raise WorkerApiError("Invalid plugin worker request.")
        operation = request["op"]
        payload = request.get("payload", {})
        if not self._ready:
            if self._handshake_attempted:
                raise WorkerApiError(_HANDSHAKE_ERROR)
            self._handshake_attempted = True
            if (operation != "plugin_handshake" or set(payload) != {"protocol_version", "backend_version", "nonce"}
                    or type(payload.get("protocol_version")) is not int
                    or payload["protocol_version"] != PROTOCOL_VERSION
                    or payload.get("backend_version") != APP_VERSION
                    or not isinstance(payload.get("nonce"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", payload["nonce"])):
                raise WorkerApiError(_HANDSHAKE_ERROR)
            self.data_directory = validate_profile(self.data_directory, synthetic=self.synthetic)
            try:
                factory = self._service_factory or (_make_synthetic_service if self.synthetic else _make_production_service)
                service = factory(self.data_directory)
                if Path(service.data_dir).resolve() != self.data_directory:
                    raise WorkerApiError(_PROFILE_ERROR)
                # Every construction route, including injected acceptance
                # factories, preserves the plugin's original comment-only
                # contract. This never writes into the shared profile.
                service._set_pipeline_mode("legacy")
                service.settings_store = _PreferencePreservingSettingsStore(service.settings_store)

                def preserve_startup(enabled, **_kwargs):
                    # WorkerService also repairs an enabled standalone Run value
                    # on save. In this adapter the already-preserved value is a
                    # no-op; a proposed change is an error, never a registry call.
                    if type(enabled) is not bool or enabled != service.settings_store.load()["start_with_windows"]:
                        raise StartupIntegrationError("Standalone startup preferences cannot change in the plugin.")

                service.startup_configurer = preserve_startup
                self._service = service
                self._ready = True
            except WorkerApiError:
                raise
            except Exception:
                raise WorkerApiError(_OPERATION_ERROR) from None
            return {
                **payload, "mode": "project-workbench", "data_directory": str(self.data_directory),
                "synthetic": self.synthetic, "ruleset_revision": RULESET_REVISION,
            }
        if operation == "plugin_handshake" or self._shutdown:
            raise WorkerApiError(_HANDSHAKE_ERROR)
        if operation in _BLOCKED_OPERATIONS:
            raise WorkerApiError("Standalone preferences and updates are managed outside the plugin.")
        validate_profile(self.data_directory, synthetic=self.synthetic)
        secrets = []
        try:
            secrets.extend(_credential_values(self._service.secret_store.load()))
            entered = payload.get("secret_values", {})
            if isinstance(entered, dict):
                secrets.extend(_credential_values(entered))
            if operation == "save" and isinstance(payload.get("settings"), dict):
                previous = preference_values(self._service.settings_store.load())
                payload = dict(payload, settings=dict(payload["settings"], **previous))
                _require_secret_free(payload["settings"], secrets)
                # Fail before WorkerService saves changed credentials. The
                # store repeats this validation on the final core settings.
                self._service.settings_store.validate_for_save(payload["settings"])
                request = dict(request, payload=payload)
            if operation == "plugin_auto_authorization":
                result = self._auto_authorization(payload)
            elif operation == "plugin_run_due_authorized":
                result = self._run_due_authorized(payload)
            else:
                result = self._service.dispatch(request)
            self._shutdown = bool(getattr(self._service, "_shutdown", False))
            secrets.extend(_credential_values(self._service.secret_store.load()))
            _require_secret_free(result, secrets)
            return result
        except WorkerApiError as exc:
            _require_secret_free(str(exc), secrets)
            raise
        except SettingsError as exc:
            _require_secret_free(str(exc), secrets)
            raise WorkerApiError(str(exc)) from None
        except Exception:
            raise WorkerApiError(_OPERATION_ERROR) from None

    def _auto_authorization(self, payload):
        """Validate saved authorization using only the existing local profile."""
        self._service._require_empty_payload(payload)
        settings, credentials = self._validated_authorization_snapshot()
        return self._authorization_result(settings, credentials)

    def _validated_authorization_snapshot(self):
        settings = deepcopy(self._service.settings_store.load())
        if settings["auto_send_enabled"] is not True:
            raise WorkerApiError(_AUTO_AUTHORIZATION_ERROR)
        credentials = deepcopy(self._service.secret_store.load())
        # This existing validator reads the scope proof, model cache, robot
        # configuration and ledger. It never refreshes models or calls services.
        self._service._validate_auto_ready(settings, credentials)
        return settings, credentials

    def _authorization_result(self, settings, credentials):
        relevant_credentials = {key: credentials.get(key, "") for key in SECRET_FIELDS + INTERNAL_SECRET_FIELDS}
        credential_fingerprint = hashlib.sha256(json.dumps(
            relevant_credentials, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")).hexdigest()
        authorization = {
            "schema_version": 1,
            "data_directory": str(self.data_directory),
            "application_version": APP_VERSION,
            "ruleset_revision": RULESET_REVISION,
            "settings": {key: value for key, value in settings.items() if key not in PREFERENCE_DEFAULTS},
            "credential_fingerprint": credential_fingerprint,
        }
        fingerprint = hashlib.sha256(json.dumps(
            authorization, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")).hexdigest()
        return {
            "authorized": True,
            "authorization_fingerprint": fingerprint,
            "data_directory": str(self.data_directory),
            "application_version": APP_VERSION,
            "ruleset_revision": RULESET_REVISION,
        }

    def _run_due_authorized(self, payload):
        expected = payload.get("authorization_fingerprint")
        if (set(payload) != {"authorization_fingerprint"} or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise WorkerApiError(_AUTHORIZED_RUN_PAYLOAD_ERROR)
        settings, credentials = self._validated_authorization_snapshot()
        actual = self._authorization_result(settings, credentials)["authorization_fingerprint"]
        if not hmac.compare_digest(expected, actual):
            raise WorkerApiError(_AUTHORIZATION_MISMATCH_ERROR)
        service = self._service
        live_settings, live_secrets = service.settings_store, service.secret_store
        try:
            # Both _run_due's schedule selection and _run's workflow inputs
            # must use the same values whose fingerprint was just validated.
            # History, delivery ledger and collection caches remain canonical.
            service.settings_store = _SnapshotStore(settings, SettingsError)
            service.secret_store = _SnapshotStore(credentials, SecretStoreError)
            return service.dispatch({"op": "run_due", "payload": {}})
        finally:
            service.settings_store, service.secret_store = live_settings, live_secrets


def _credential_values(credentials):
    """Include decoded credential-bearing URL fields as independent secrets."""
    values = set()
    for value in credentials.values():
        if not isinstance(value, str) or not value:
            continue
        values.add(value)
        try:
            query = parse_qsl(urlsplit(value).query, keep_blank_values=False)
        except (TypeError, ValueError):
            continue
        values.update(component for key, component in query
                      if key.casefold() in {"access_token", "token", "sign", "signature"} and component)
    return values


def _require_secret_free(value, secrets):
    """Keep credential material out of settings, responses and error messages."""
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret and secret in value:
                raise WorkerApiError(_OPERATION_ERROR)
    elif isinstance(value, dict):
        for key, item in value.items():
            _require_secret_free(key, secrets)
            _require_secret_free(item, secrets)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_secret_free(item, secrets)


def _make_production_service(directory):
    # Only a new empty profile reaches this branch without settings.json.
    # Persist defaults now so an unconfigured first session can reopen safely.
    store = SettingsStore(directory / "settings.json")
    if not store.path.exists():
        store.save(deepcopy(DEFAULT_SETTINGS))
    return WorkerService(directory, settings_store=store, pipeline_mode="legacy")


class _SyntheticSecretStore(MemorySecretStore):
    """Fixed fake service keys; persist only the separate fixture ledger ID.

    Never open DPAPI data or persist user-entered service keys. Acceptance keys
    are intentionally reset to the same fake values each process launch.
    """

    def __init__(self, marker):
        super().__init__()
        self.marker = marker
        fixture = json.loads(marker.read_text(encoding="utf-8"))
        self.values = {
            "teambition_mcp_token": "synthetic-mcp-token",
            MCP_AUTHORIZATION_FIELD: teambition_mcp_identity({"teambition_mcp_token": "synthetic-mcp-token"}),
            "openai_api_key": "synthetic-key-cannot-call-openai",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=synthetic-only",
            "dingtalk_signing_secret": "synthetic-only",
        }
        if fixture.get("delivery_ledger_id"):
            self.values["delivery_ledger_id"] = fixture["delivery_ledger_id"]

    def save(self, values):
        super().save(values)
        fixture = {"format": _SYNTHETIC_FORMAT}
        if self.values.get("delivery_ledger_id"):
            fixture["delivery_ledger_id"] = self.values["delivery_ledger_id"]
        atomic_write(self.marker, (json.dumps(fixture) + "\n").encode("utf-8"))


def _make_synthetic_service(directory):
    from .openai_models import OpenAIModelCatalog
    from .smoke_data import SyntheticClient, SyntheticDingTalk, SyntheticSummarizer
    from .workflow import DigestWorkflow

    marker = directory / SYNTHETIC_MARKER
    store = SettingsStore(directory / "settings.json")
    if not marker.exists():
        settings = deepcopy(DEFAULT_SETTINGS)
        settings.update({
            "tenant_id": "demo-org", "project_id": "demo-project",
            "ui_language": "zh-CN", "ui_theme": "dark",
            "owner_names": {"demo-person": "虚构成员甲", "demo-person-2": "虚构成员乙"},
            "status_names": {"demo-open": "待处理", "demo-progress": "进行中", "demo-review": "待验收", "demo-done": "已解决"},
            "resolved_status_ids": ["demo-done"],
        })
        store.save(settings)
        atomic_write(marker, (json.dumps({"format": _SYNTHETIC_FORMAT}) + "\n").encode("utf-8"))
    catalog = OpenAIModelCatalog(
        directory / "openai_models.json",
        transport=lambda *_args: b'{"data":[{"id":"gpt-6-luna"},{"id":"ci-unknown-model"}]}',
    )

    def workflow(settings, credentials, data_dir, **kwargs):
        return DigestWorkflow(settings, credentials, data_dir, client_factory=SyntheticClient,
                              summarizer=SyntheticSummarizer(), ding_sender=SyntheticDingTalk(settings),
                              pipeline_mode="legacy", **kwargs)

    return WorkerService(directory, settings_store=store, secret_store=_SyntheticSecretStore(marker),
                         model_catalog=catalog, client_factory=SyntheticClient, workflow_factory=workflow,
                         pipeline_mode="legacy")


def _response(output, request_id, *, result=None, error=None):
    envelope = {"id": request_id, "ok": error is None}
    envelope["result" if error is None else "error"] = result if error is None else error
    try:
        encoded = (json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8", errors="strict")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        encoded = (json.dumps({"id": request_id, "ok": False, "error": "Worker response exceeds the safe protocol limits."}, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        output.write(encoded)
    except TypeError:
        output.write(encoded.decode("utf-8"))
    output.flush()


def serve(data_directory, *, synthetic=False, service_factory=None, input_stream=None, output_stream=None):
    """Serve strict UTF-8 JSON lines, counting byte limits in both directions."""
    source = input_stream if input_stream is not None else sys.stdin.buffer
    target = output_stream if output_stream is not None else sys.stdout.buffer
    service = PluginWorkerService(data_directory, synthetic=synthetic, service_factory=service_factory)
    while True:
        raw = source.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return
        request_id = None
        try:
            encoded = raw.encode("utf-8", errors="strict") if isinstance(raw, str) else raw
            if len(encoded) > MAX_REQUEST_BYTES:
                if not encoded.endswith(b"\n"):
                    while True:
                        discarded = source.readline(8192)
                        if not discarded or discarded.endswith("\n" if isinstance(discarded, str) else b"\n"):
                            break
                _response(target, None, error="Request exceeds the local protocol size limit.")
                continue
            request = json.loads(encoded.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_json_keys,
                                 parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(request, dict):
                raise ValueError()
            candidate_id = request.get("id")
            if not isinstance(candidate_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", candidate_id):
                raise ValueError()
            request_id = candidate_id
            result = service.dispatch(request)
            _response(target, request_id, result=result)
            if service._shutdown:
                return
        except WorkerApiError as exc:
            _response(target, request_id, error=str(exc))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            _response(target, request_id, error="Invalid request JSON or UTF-8 encoding.")
        except Exception:
            _response(target, request_id, error=_OPERATION_ERROR)


def main(argv=None):
    """Accept only the documented explicit profile and acceptance-mode flag."""
    args = list(sys.argv[1:] if argv is None else argv)
    directory = None
    synthetic = False
    index = 0
    try:
        while index < len(args):
            argument = args[index]
            if argument == "--data-directory" and directory is None and index + 1 < len(args):
                index += 1
                directory = args[index]
            elif argument == "--synthetic" and not synthetic:
                synthetic = True
            else:
                raise ValueError()
            index += 1
        if directory is None:
            raise ValueError()
        serve(directory, synthetic=synthetic)
        return 0
    except (ValueError, WorkerApiError, OSError):
        sys.stderr.write("A safe explicit --data-directory profile is required.\n")
        return 2
