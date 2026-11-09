"""Narrow, credential-safe command boundary for the WinUI desktop frontend."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

from automated_digest import collection_validation_message, diagnose_automatic_source
from collection_progress import observe_collection, report_collection
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger, validate_manual_delivery_options
from openai_summarizer import KNOWN_FAST_MODELS
from render_digest import render_digest
from runtime_mappings import merge_discovered
from report_contract import SHANGHAI, updated_selection
from teambition_readonly import Client, CollectionError

from .config import (
    SettingsError, SettingsStore, collection_client_limits,
    atomic_write, parse_manual_window, recoverable_schedule_occurrences, runtime_config,
    scheduled_window, user_data_dir, validate_settings,
)
from .diagnostic_export import safe_structure_export
from .collection_checkpoint import CollectionCheckpointBusy, DpapiCollectionCheckpoint
from .collection_runtime import make_collection_client, list_mcp_projects
from .dingtalk_mentions import MentionConfigError, mention_options, reject_credential_mentions
from .diagnostic_files import write_private_diagnostic
from . import APP_VERSION, RULESET_REVISION
from .history import HistoryError, HistoryStore
from .openai_models import (
    ModelCatalogError, OpenAIModelCatalog, cache_matches_key, reasoning_efforts,
)
from .project_setup import (
    ProjectSetupError, analyze_project_probe, merge_owner_mappings,
    parse_owner_mapping_tsv,
)
from .preferences import PREFERENCE_DEFAULTS, PreferenceError, preference_values, validate_update_token
from .updates import GitHubUpdates, UpdateError
from .secrets import (
    INTERNAL_SECRET_FIELDS, SECRET_FIELDS, UPDATE_SECRET_FIELDS, revoke_missing_mcp_authorization, teambition_mcp_identity,
    DpapiSecretStore, SecretStoreError, mcp_connection_authorized, MCP_AUTHORIZATION_FIELD,
)
from .windows_integration import StartupIntegrationError, configure_user_startup
from .workflow import DigestWorkflow, WorkflowBlocked


MAX_PROTOCOL_BYTES = 2 * 1024 * 1024
_APP_EXE_NAME = "UPCAMTaskDigest.exe"
_FILTER_FIELDS = {
    "executorId", "tasklistId", "stageId", "tfsId",
    "scenariofieldconfigId", "visible", "parentTaskId",
}


class WorkerApiError(ValueError):
    """A safe, user-facing command failure."""


class WorkerService:
    """Own the existing local data and business logic for one desktop session.

    The service returns settings, report previews, history, and redacted
    diagnostics, but it never returns decrypted credentials to its caller.
    """

    def __init__(self, data_dir: Path | None = None, *, settings_store=None,
                 secret_store=None, history=None, model_catalog=None,
                 startup_configurer=None, client_factory=Client,
                 workflow_factory=DigestWorkflow, sample_path=None, update_manager=None,
                 checkpoint_factory=None):
        self.data_dir = Path(data_dir or user_data_dir())
        self.settings_store = settings_store or SettingsStore(self.data_dir / "settings.json")
        self.secret_store = secret_store or DpapiSecretStore(self.data_dir / "credentials.dpapi")
        self.history = history or HistoryStore(self.data_dir / "history.sqlite3")
        self.model_catalog = model_catalog or OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.startup_configurer = startup_configurer or configure_user_startup
        self.client_factory = client_factory
        self.workflow_factory = workflow_factory
        self.checkpoint_factory = checkpoint_factory
        self.sample_path = Path(sample_path or _resource_path("examples/digest.json"))
        self.update_manager = update_manager or GitHubUpdates(self.data_dir, current_version=APP_VERSION)
        self._job_lock = threading.Lock()
        self._handled_schedule_slots: set[str] = set()
        self._setup_probe: dict | None = None
        self._project_choices: dict[str, dict] = {}
        self._project_choices_identity: str | None = None
        self._last_diagnostic_shape: dict | None = None
        self._last_diagnostic_context: str | None = None
        self._shutdown = False

    def dispatch(self, request: dict) -> dict:
        if not isinstance(request, dict):
            raise WorkerApiError("请求格式无效。")
        operation = request.get("op")
        payload = request.get("payload", {})
        if not isinstance(operation, str) or not isinstance(payload, dict):
            raise WorkerApiError("请求格式无效。")
        handlers = {
            "initialize": self._initialize,
            "save": self._save,
            "save_preferences": self._save_preferences,
            "check_updates": self._check_updates,
            "download_update": self._download_update,
            "prepare_update": self._prepare_update,
            "finish_update": self._finish_update,
            "clear_credentials": self._clear_credentials,
            "clear_collection_progress": self._clear_collection_progress,
            "refresh_models": self._refresh_models,
            "import_owner_mappings": self._import_owner_mappings,
            "update_owner_mapping": self._update_owner_mapping,
            "update_status_mapping": self._update_status_mapping,
            "update_view_filter": self._update_view_filter,
            "probe_project": self._probe_project,
            "list_projects": self._list_projects,
            "select_project": self._select_project,
            "diagnose": self._diagnose,
            "export_diagnostic": self._export_diagnostic,
            "diagnostic_status": self._diagnostic_status,
            "export_diagnostic_to_file": self._export_diagnostic_to_file,
            "export_detailed_diagnostic_to_file": self._export_detailed_diagnostic_to_file,
            "preview_sample": self._preview_sample,
            "test_teambition": self._test_teambition,
            "validate_dingtalk_config": self._validate_dingtalk_config,
            "run_manual": self._run_manual,
            "run_due": self._run_due,
            "history": self._history,
            "history_detail": self._history_detail,
            "shutdown": self._shutdown_worker,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise WorkerApiError("不支持此操作。")
        try:
            return handler(payload)
        except WorkerApiError:
            raise
        except (SettingsError, SecretStoreError, ModelCatalogError, ProjectSetupError, MentionConfigError,
                CollectionError, WorkflowBlocked, HistoryError, DeliveryBlocked,
                StartupIntegrationError):
            raise WorkerApiError(_safe_error_message(sys.exc_info()[1])) from None
        except Exception:
            # Exception text can include headers, response bodies, or user-entered data.
            raise WorkerApiError("操作未完成；详细 API 响应和凭据没有写入日志。请检查设置并重试。") from None

    def _initialize(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        try:
            self._write_language_bootstrap(settings)
        except OSError:
            # This bootstrap contains only an interface language, never business data.
            # Its absence falls back to Windows defaults; ordinary UI loading continues.
            pass
        credentials = self.secret_store.load()
        reject_credential_mentions(settings, credentials)
        migrated = revoke_missing_mcp_authorization(settings, credentials)
        if migrated != settings:
            self.settings_store.save(migrated)
        settings = migrated
        self.history.recover_interrupted_sends()
        ledger_ready, ledger_message = self._ensure_ledger(credentials)
        credentials = self.secret_store.load()
        cache, cache_error = self._load_model_cache()
        cached_models = cache.get("models", []) if cache else []
        return {
            "settings": settings,
            "secret_presence": _secret_presence(credentials),
            "models": cached_models,
            "model_cache_fetched_at_utc": cache.get("fetched_at_utc") if cache else None,
            "model_cache_matches_key": cache_matches_key(cache, credentials.get("openai_api_key", "")),
            "model_cache_error": cache_error,
            "reasoning_efforts": {model_id: list(reasoning_efforts(model_id)) for model_id in
                                  {item["id"] for item in cached_models} | {settings["openai_model"]}},
            "fast_models": sorted(KNOWN_FAST_MODELS),
            "history": [_safe_history_row(row) for row in self.history.list_runs(100)],
            "ledger_ready": ledger_ready,
            "ledger_message": ledger_message,
            "data_directory": str(self.data_dir),
            "application_version": APP_VERSION,
            "ruleset_revision": RULESET_REVISION,
            "preferences": preference_values(settings),
        }

    def _save_preferences(self, payload: dict) -> dict:
        if (set(payload) - {"preferences", "github_token", "clear_github_token"}
                or not isinstance(payload.get("preferences"), dict)
                or set(payload["preferences"]) - set(PREFERENCE_DEFAULTS)
                or type(payload.get("clear_github_token", False)) is not bool):
            raise WorkerApiError("常规设置内容无效；未保存任何更改。")
        previous = self.settings_store.load()
        bootstrap_existed = (self.data_dir / "language.json").exists()
        old_secrets = self.secret_store.load()
        credentials = dict(old_secrets)
        try:
            token = validate_update_token(payload.get("github_token", ""))
            clean = validate_settings(dict(previous, **payload["preferences"]))
        except (PreferenceError, SettingsError) as exc:
            raise WorkerApiError(str(exc)) from None
        if payload.get("clear_github_token"):
            credentials.pop("github_update_token", None)
        if token:
            credentials["github_update_token"] = token
        reject_credential_mentions(clean, old_secrets, credentials)
        startup_attempted = False
        try:
            self.secret_store.save(credentials)
            self.settings_store.save(clean)
            self._write_language_bootstrap(clean)
            if clean["start_with_windows"] != previous["start_with_windows"] or clean["start_with_windows"]:
                startup_attempted = True
                self.startup_configurer(clean["start_with_windows"],
                                        executable=_current_application_executable(), frozen=True)
        except Exception:
            restored = True
            for restore in (lambda: self.secret_store.save(old_secrets),
                            lambda: self.settings_store.save(previous)):
                try:
                    restore()
                except Exception:
                    restored = False
            if startup_attempted:
                try:
                    self.startup_configurer(previous["start_with_windows"],
                                            executable=_current_application_executable(), frozen=True)
                except Exception:
                    restored = False
            try:
                if bootstrap_existed:
                    self._write_language_bootstrap(previous)
                else:
                    (self.data_dir / "language.json").unlink(missing_ok=True)
            except OSError:
                restored = False
            raise WorkerApiError("常规设置保存失败，已恢复原设置。" if restored else
                                 "常规设置保存未完成；恢复也未完成，请重新打开应用核对设置和启动项。") from None
        return {"preferences": preference_values(clean), "secret_presence": _secret_presence(credentials),
                "status": "常规设置已保存；未采集任务、未调用 OpenAI、未发送钉钉。"}

    def _write_language_bootstrap(self, settings: dict) -> None:
        # Inspired by PowerToys' separate language bootstrap, read before XAML.
        # Keep project configuration and all secrets out of this tiny document.
        content = (json.dumps({"language": settings["ui_language"]}) + "\n").encode("utf-8")
        target = self.data_dir / "language.json"
        if not target.exists() or target.stat().st_size > 4096 or target.read_bytes() != content:
            atomic_write(target, content)

    def _update_operation(self, payload: dict, operation: str) -> dict:
        self._require_empty_payload(payload)
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("已有采集或发送正在运行；请完成后再更新应用。")
        try:
            token = self.secret_store.load().get("github_update_token", "")
            if operation == "prepare_install":
                return self.update_manager.prepare_install(token, current_executable=Path(_current_application_executable()))
            return getattr(self.update_manager, operation)(token)
        except UpdateError as exc:
            raise WorkerApiError(exc.message) from None
        finally:
            self._job_lock.release()

    def _check_updates(self, payload: dict) -> dict:
        return self._update_operation(payload, "check")

    def _download_update(self, payload: dict) -> dict:
        return self._update_operation(payload, "download")

    def _prepare_update(self, payload: dict) -> dict:
        return self._update_operation(payload, "prepare_install")

    def _finish_update(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        executable = Path(_current_application_executable())
        try:
            verified = self.update_manager.verify_installed(executable)
        except UpdateError as exc:
            raise WorkerApiError(exc.message) from None
        settings = self.settings_store.load()
        if settings["start_with_windows"]:
            try:
                self.startup_configurer(True, executable=str(executable), frozen=True)
            except (OSError, StartupIntegrationError):
                return {"status": "startup_not_updated", "version": verified.get("version"),
                        "message": "新版已启动，但登录启动项未更新；原版本目录已保留，请在常规页重新保存启动设置。"}
        return {"status": "installed", "version": verified.get("version"),
                "message": "已从校验后的更新目录启动；本机数据和原版本目录已保留。"}

    def _save(self, payload: dict) -> dict:
        settings_input = payload.get("settings")
        secret_values = payload.get("secret_values", {})
        clear_secrets = payload.get("clear_secrets", [])
        authorize_mcp = payload.get("authorize_teambition_mcp", False)
        if type(authorize_mcp) is not bool:
            raise WorkerApiError("MCP 连接授权选项无效；未保存任何更改。")
        if not isinstance(settings_input, dict) or not isinstance(secret_values, dict):
            raise WorkerApiError("设置内容无效；未保存任何更改。")
        if not isinstance(clear_secrets, list) or any(not isinstance(key, str) for key in clear_secrets):
            raise WorkerApiError("保存选项无效；未保存任何更改。")
        if (set(secret_values) - set(SECRET_FIELDS)
                or any(not isinstance(value, str) for value in secret_values.values())
                or set(clear_secrets) - set(SECRET_FIELDS)):
            raise WorkerApiError("密钥字段无效；未保存任何更改。")

        previous_settings = self.settings_store.load()
        previous_secrets = self.secret_store.load()
        entered = {key: value for key, value in secret_values.items() if value}
        proposed_secrets = dict(previous_secrets)
        for key in clear_secrets:
            proposed_secrets.pop(key, None)
        proposed_secrets.update(entered)
        if ("teambition_mcp_token" in clear_secrets or
                proposed_secrets.get("teambition_mcp_token") != previous_secrets.get("teambition_mcp_token")):
            # Revocation is permanent until this save receives a new explicit
            # grant, even if the user later enters a previously authorized token.
            proposed_secrets.pop(MCP_AUTHORIZATION_FIELD, None)
        if authorize_mcp:
            identity = teambition_mcp_identity(proposed_secrets)
            if identity is None:
                raise WorkerApiError("请先输入 Teambition MCP 个人 Token，再确认连接授权。")
            proposed_secrets[MCP_AUTHORIZATION_FIELD] = identity
        credential_identity = _teambition_identity(proposed_secrets)
        previous_identity = _teambition_identity(previous_secrets)
        proposed = _verified_settings_for_save(
            settings_input, previous_settings, self._setup_probe,
            credential_identity=credential_identity, previous_credential_identity=previous_identity,
        )
        clean = validate_settings(proposed)
        reject_credential_mentions(clean, previous_secrets, proposed_secrets)
        scope_probe = (self._setup_probe.get("discovery") if _probe_matches(
            self._setup_probe, _scope_signature(clean), credential_identity) else None)
        scope_status = "项目范围只读核验有效。"
        if not clean["view"]["verified"]:
            # Saving the chosen scope requests verification; no client flag or
            # checkbox can establish a successful server-side read-only proof.
            scope_status = "请填写项目和 Teambition 凭据后保存，以完成只读核验。"
            if teambition_mcp_identity(proposed_secrets) and not credential_identity:
                scope_status = "Token 已保存在本机，但尚未授权连接官方 MCP；请在账户页确认后再核验。"
            if clean["tenant_id"] and clean["project_id"] and credential_identity:
                window = payload.get("verification_window")
                if not isinstance(window, dict):
                    scope_status = "缺少核验时间范围；请选择含任务动态的区间后重新保存。"
                else:
                    try:
                        proof = self._collect_scope_probe(clean, proposed_secrets, window)
                        clean = validate_settings(_verified_settings_for_save(
                            clean, previous_settings, proof,
                            credential_identity=credential_identity,
                            previous_credential_identity=previous_identity,
                        ))
                        scope_probe = proof["discovery"]
                        scope_status = "项目范围已自动完成只读核验。"
                    except CollectionError as exc:
                        scope_status = collection_validation_message(exc)
                    except ProjectSetupError as exc:
                        scope_status = str(exc)
                    except (SettingsError, ValueError, TypeError):
                        scope_status = "核验时间范围无效；请选择含任务动态的有效区间后重新保存。"
                    except Exception:
                        scope_status = "Teambition 只读核验未完成；请检查网络、凭据、项目读取权限和完整翻页后重新保存。"
            if not clean["view"]["verified"]:
                clean["auto_send_enabled"] = False
                clean["auto_send_enabled_since_local"] = ""
                scope_status += " 自动运行已关闭；未调用 OpenAI，未发送钉钉。"

        if _auto_enabled_changed(previous_settings, clean):
            clean["auto_send_enabled_since_local"] = (
                datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
                if clean["auto_send_enabled"] else "")
        if clean["auto_send_enabled"]:
            self._validate_auto_ready(clean, proposed_secrets)

        startup_enabled = clean["start_with_windows"]
        startup_changed = startup_enabled != bool(previous_settings.get("start_with_windows"))
        startup_applied = False
        self.secret_store.save(proposed_secrets)
        try:
            self.settings_store.save(clean)
            if startup_changed or startup_enabled:
                self.startup_configurer(startup_enabled,
                                        executable=_current_application_executable(),
                                        frozen=True)
                startup_applied = True
        except (OSError, StartupIntegrationError, SettingsError):
            # Restore the old values if the OS startup entry could not be updated.
            try:
                self.secret_store.save(previous_secrets)
                self.settings_store.save(previous_settings)
            except Exception:
                pass
            if startup_applied:
                try:
                    self.startup_configurer(bool(previous_settings.get("start_with_windows")),
                                            executable=_current_application_executable(),
                                            frozen=True)
                except Exception:
                    pass
            raise

        self._setup_probe = None
        ready, message = self._ensure_ledger(self.secret_store.load())
        return {
            "settings": clean,
            "secret_presence": _secret_presence(self.secret_store.load()),
            "ledger_ready": ready,
            "ledger_message": message,
            "scope_probe": scope_probe,
            "status": "本机设置已保存；服务密钥由 Windows 当前用户 DPAPI 加密。 " + scope_status,
        }

    def _clear_credentials(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        current = self.secret_store.load()
        reject_credential_mentions(settings, current)
        settings["auto_send_enabled"] = False
        settings["auto_send_enabled_since_local"] = ""
        settings["api_contract_verified"] = False
        settings["view"] = dict(settings["view"], verified=False, evidence="")
        self.settings_store.save(settings)
        retained = {key: value for key, value in current.items()
                    if key in INTERNAL_SECRET_FIELDS + UPDATE_SECRET_FIELDS}
        self.secret_store.save(retained)
        self._setup_probe = None
        return {"settings": settings, "secret_presence": _secret_presence(retained),
                "status": "已清除服务密钥并关闭自动运行；本机防重复身份已保留。"}

    def _clear_collection_progress(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("采集正在进行，暂不能清除进度；请等待完成或退出应用后重试。")
        try:
            factory = self.checkpoint_factory if self.checkpoint_factory is not None else DpapiCollectionCheckpoint
            checkpoint = factory(self.data_dir / "collection-progress.sqlite3")
            try:
                checkpoint.clear()
            finally:
                checkpoint.close()
        except CollectionCheckpointBusy:
            raise WorkerApiError("采集正在进行，暂不能清除进度；请等待完成或退出应用后重试。") from None
        except Exception:
            raise WorkerApiError("本地采集进度无法安全清除；请退出应用后重试。") from None
        finally:
            self._job_lock.release()
        return {"status": "未完成的采集进度已清除；配置、凭据、模型缓存、运行记录和发送账本保持不变。"}

    def _refresh_models(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        credentials = self.secret_store.load()
        api_key = credentials.get("openai_api_key", "")
        if not api_key:
            raise WorkerApiError("请先保存 OpenAI API Key，再刷新模型。")
        try:
            cache = self.model_catalog.refresh(api_key)
        except ModelCatalogError:
            raise WorkerApiError("OpenAI 模型列表刷新失败；原缓存保持不变。检查网络、Key 权限后再试。") from None
        if not cache_matches_key(cache, api_key):
            raise WorkerApiError("返回的模型缓存与当前 API Key 不匹配；原选择未更改。")
        return {
            "models": cache["models"],
            "fetched_at_utc": cache["fetched_at_utc"],
            "reasoning_efforts": {item["id"]: list(reasoning_efforts(item["id"]))
                                  for item in cache["models"]},
            "fast_models": sorted(KNOWN_FAST_MODELS),
            "model_cache_matches_key": True,
        }

    def _import_owner_mappings(self, payload: dict) -> dict:
        source, existing = payload.get("source"), payload.get("existing", {})
        if not isinstance(source, str) or not isinstance(existing, dict):
            raise WorkerApiError("成员映射内容无效；未导入任何行。")
        try:
            incoming = parse_owner_mapping_tsv(source)
            merged = merge_owner_mappings(existing, incoming)
        except ProjectSetupError as exc:
            raise WorkerApiError(str(exc) + " 支持 JSON 对象或每行“成员 ID<TAB>姓名”；未导入任何行。") from None
        return {"mappings": merged, "imported_count": len(incoming)}

    def _update_owner_mapping(self, payload: dict) -> dict:
        mapping = payload.get("mappings")
        if not isinstance(mapping, dict):
            raise WorkerApiError("成员映射格式无效。")
        self._validate_mapping_document({"owner_names": mapping, "status_names": {},
                                         "resolved_status_ids": []})
        return {"mappings": mapping}

    def _update_status_mapping(self, payload: dict) -> dict:
        mapping = payload.get("mappings")
        resolved = payload.get("resolved_status_ids")
        if not isinstance(mapping, dict) or not isinstance(resolved, list):
            raise WorkerApiError("状态映射格式无效。")
        self._validate_mapping_document({"owner_names": {}, "status_names": mapping,
                                         "resolved_status_ids": resolved})
        return {"mappings": mapping, "resolved_status_ids": sorted(set(resolved))}

    def _update_view_filter(self, payload: dict) -> dict:
        action, key, value = payload.get("action"), payload.get("field"), payload.get("value")
        if action not in ("put", "remove") or not isinstance(key, str) or key not in _FILTER_FIELDS:
            raise WorkerApiError("项目筛选字段无效。")
        settings = self.settings_store.load()
        # The UI may have unsaved edits, so accept and validate its current map.
        filters = payload.get("filters", {})
        if not isinstance(filters, dict):
            raise WorkerApiError("项目筛选条件格式无效。")
        updated = dict(filters)
        if action == "put":
            if not isinstance(value, str) or not value.strip():
                raise WorkerApiError("请输入项目筛选字段值或 ID。")
            updated[key] = value.strip()
        else:
            updated.pop(key, None)
        view = dict(settings.get("view", {}), task_equals=updated)
        from digest_pipeline import validate_view
        validate_view({"view": view})
        return {"filters": updated}

    def _list_projects(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        self._project_choices = {}
        self._project_choices_identity = None
        credentials = self.secret_store.load()
        identity = _teambition_identity(credentials)
        if identity is None:
            raise WorkerApiError("请先在账户页保存并确认授权本机连接 Teambition 官方 MCP；仅保存 Token 不会授权连接。")
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("已有采集或发送正在运行；请完成后再加载项目列表。")
        try:
            projects = list_mcp_projects(credentials, client_factory=self.client_factory)
            if _teambition_identity(self.secret_store.load()) != identity:
                raise WorkerApiError("MCP 授权在加载期间已改变；未返回项目列表，请重新授权后加载。")
        except WorkerApiError:
            raise
        except Exception as exc:
            if isinstance(exc, CollectionError) and str(exc) == "Teambition MCP authorization expired or denied":
                raise WorkerApiError("Teambition MCP 个人 Token 已失效或无读取权限；请重新生成 Token 并保存，确认当前账户有项目读取权限。") from None
            raise WorkerApiError("MCP 项目列表加载失败，可能是读取合同未核验、分页复核不一致或无权限；当前项目未更改。可手动输入 Tenant / Organization ID 和 Project ID 后再核验。") from None
        finally:
            self._job_lock.release()
        self._project_choices = {project["id"]: dict(project) for project in projects}
        self._project_choices_identity = identity
        return {"projects": projects, "status": ("当前账号参与的可见项目，列表可能不完整；找不到可手动填 ID。列表不含归档项目，选择后保存生效。" if projects else
                                                    "未找到当前账号参与的可见项目，列表可能不完整且不含归档项目；原项目配置保持不变，可手动填 ID。")}

    def _select_project(self, payload: dict) -> dict:
        if set(payload) != {"project_id"} or not isinstance(payload.get("project_id"), str):
            raise WorkerApiError("项目选择无效；当前项目未更改。")
        identity = _teambition_identity(self.secret_store.load())
        if identity is None or identity != self._project_choices_identity:
            raise WorkerApiError("项目列表对应的 MCP 授权已改变；请重新加载项目列表。")
        selected = self._project_choices.get(payload["project_id"])
        if selected is None:
            raise WorkerApiError("请选择本次加载的项目；当前项目未更改。")
        # Return a draft only. Persisting the scope still requires an explicit save.
        draft = {"project_id": selected["id"], "tenant_id": selected["organizationId"],
                 "name": selected["name"], "status": "项目已填入表单；原配置保持不变，保存后生效。"}
        if "uniqueIdPrefix" in selected:
            draft["task_number_prefix"] = selected["uniqueIdPrefix"]
            draft["task_display_scope"] = {"tenant_id": selected["organizationId"], "project_id": selected["id"]}
        return draft

    def _probe_project(self, payload: dict) -> dict:
        self._setup_probe = None
        settings = self.settings_store.load()
        secrets = self.secret_store.load()
        settings = _merge_probe_inputs(settings, payload)
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise WorkerApiError("请先填写 Tenant ID 和 Project ID。")
        if not secrets.get("teambition_mcp_token", "").strip():
            raise WorkerApiError("请先保存 Teambition MCP 个人 Token。")
        if not mcp_connection_authorized(secrets):
            raise WorkerApiError("请先在账户页保存并确认授权本机连接 Teambition 官方 MCP；仅保存 Token 不会授权连接。")
        try:
            proof = self._collect_scope_probe(settings, secrets, payload)
        except ProjectSetupError as exc:
            self._setup_probe = None
            raise WorkerApiError(str(exc)) from None
        except (CollectionError, OSError) as exc:
            self._setup_probe = None
            if str(exc) == "Teambition MCP authorization expired or denied":
                raise WorkerApiError("Teambition MCP 个人 Token 已失效或无读取权限；请重新生成 Token 并保存，确认当前账户有项目读取权限。") from None
            raise WorkerApiError("Teambition 只读查询未完成或翻页不完整；没有更改核验状态。请检查权限后再试。") from None
        except (SettingsError, ValueError, TypeError):
            self._setup_probe = None
            raise WorkerApiError("项目设置或时间范围无效；没有更改核验状态。") from None
        self._setup_probe = proof
        return {
            **proof["discovery"],
            "status": "只读样本通过。成员姓名和任务状态按 MCP 可见数据自动读取；未解析字段可在高级覆盖中补充。",
        }

    def _collect_scope_probe(self, settings: dict, credentials: dict, window: dict):
        """Build proof only from a complete response for this scope and identity."""
        start_local, end_local = parse_manual_window(window.get("start", ""), window.get("end", ""))
        start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
        report_day = end.date().isoformat()
        selection = updated_selection(report_day, start, end)
        validate_settings(settings)
        collection = self._collect_source(
            settings, credentials, day=report_day, window=(start, end),
            include_archived=settings["include_archived"])
        # Validate source metadata in memory; never persist it as a manual override.
        discovered = merge_discovered(runtime_config(settings), collection.get("metadata", {}))
        probe = analyze_project_probe(
            collection, tenant_id=settings["tenant_id"], project_id=settings["project_id"],
            expected_selection=selection, include_archived=settings["include_archived"])
        filters = settings.get("view", {}).get("task_equals", {})
        if any(record["task"].get(field) is None for record in collection["records"] for field in filters):
            raise ProjectSetupError("只读样本缺少已配置的筛选字段，无法核验当前项目范围。请核对筛选条件后重新保存。")
        discovery = {
            "task_count": probe.task_count, "activity_count": probe.activity_count,
            "owner_ids": list(probe.owner_ids), "status_ids": list(probe.status_ids),
            "status_name_suggestions": probe.status_name_suggestions,
            "conflicting_status_ids": list(probe.conflicting_status_ids),
            "observed_actions": list(probe.observed_actions), "selection": selection,
            "unresolved_owner_count": len(set(probe.owner_ids) - set(discovered["owner_names"])),
            "unresolved_status_count": len(set(probe.status_ids) - set(discovered["status_names"])),
        }
        return {"scope": _scope_signature(settings), "selection": selection,
                "credential_identity": _teambition_identity(credentials),
                "task_count": probe.task_count, "activity_count": probe.activity_count,
                "discovery": discovery}

    def _diagnose(self, payload: dict, *, detailed_target: Path | None = None,
                  detailed_parent_identity: tuple | None = None) -> dict:
        # Any attempted refresh supersedes the previous result, including a
        # failure before collection (for example, cleared credentials).
        self._last_diagnostic_shape = None
        self._last_diagnostic_context = None
        settings = self.settings_store.load()
        secrets = self.secret_store.load()
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise WorkerApiError("请先保存 Tenant ID 和 Project ID。")
        if not secrets.get("teambition_mcp_token", "").strip():
            raise WorkerApiError("只读诊断需要已保存的 Teambition MCP 个人 Token；不需要 OpenAI 或钉钉密钥。")
        if not mcp_connection_authorized(secrets):
            raise WorkerApiError("请先在账户页保存并确认授权本机连接 Teambition 官方 MCP；仅保存 Token 不会授权连接。")
        try:
            start_local, end_local = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
            start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
            day = end.date().isoformat()
            config = runtime_config(settings)
            report_collection("starting")
            collection = self._collect_source(
                settings, secrets, day=day, window=(start, end),
                include_archived=settings["include_archived"],
                candidate_mode="full_project" if detailed_target is not None else "updated_since_start")
            config = merge_discovered(config, collection.get("metadata", {}))
            report_collection("validating", completed=len(collection.get("records", [])),
                              total=len(collection.get("records", [])))
            diagnostic = diagnose_automatic_source(collection, config, day, (start, end))
            self._last_diagnostic_shape = safe_structure_export(collection, config, day, (start, end))
            self._last_diagnostic_context = _diagnostic_context(settings, secrets, payload)
            latest_credentials = self.secret_store.load()
        except (CollectionError, OSError) as exc:
            self._last_diagnostic_shape = None
            return {"ok": False, "message": collection_validation_message(exc),
                    "task_count": None, "export_available": False,
                    "safety": "未调用 OpenAI；未发送钉钉。"}
        except (SettingsError, ValueError, TypeError, ProjectSetupError) as exc:
            self._last_diagnostic_shape = None
            return {"ok": False, "message": _safe_error_message(exc),
                    "task_count": None, "export_available": False,
                    "safety": "未调用 OpenAI；未发送钉钉。"}
        except Exception:
            self._last_diagnostic_shape = None
            return {"ok": False,
                    "message": "只读诊断未完成；没有调用 OpenAI、没有发送钉钉，详细 API 响应不会写入记录。请检查网络、凭据和项目权限。",
                    "task_count": None, "export_available": False}
        metadata = self._last_diagnostic_shape["diagnostic"]
        created = datetime.fromisoformat(metadata["created_at_utc"]).strftime("%Y%m%dT%H%M%SZ")
        file_stem = f"teambition-diagnostic-v{APP_VERSION}-{created}-{metadata['id'][:8]}"
        catalog = self._last_diagnostic_shape.get("protocol_catalog", {})
        result = {**diagnostic, "export_available": bool(self._last_diagnostic_shape),
                "diagnostic_file_stem": file_stem,
                "diagnostic": metadata,
                "diagnostic_summary": {
                    "unknown_events_in_report_window": catalog.get("unknown_events_considered", 0),
                    "unknown_events_with_unknown_time": catalog.get("unknown_window_events", 0),
                    "catalog_complete": catalog.get("complete") is True,
                },
                "safety": "未调用 OpenAI；未发送钉钉。"}
        result["message"] = _redact_known_diagnostic_credentials(
            result.get("message", ""), secrets, latest_credentials)
        if detailed_target is not None:
            result["detailed_export"] = {"saved": False}
            try:
                # A detailed snapshot is built only for this explicit request;
                # the original project records never enter the protocol cache.
                from .detailed_diagnostic import build_detailed_snapshot, DetailedDiagnosticError
                current_credentials = latest_credentials
                if (_credentials_fingerprint(current_credentials) != _credentials_fingerprint(secrets)
                        or _diagnostic_context(self.settings_store.load(), current_credentials, payload)
                        != self._last_diagnostic_context):
                    raise WorkerApiError("设置或凭据在采集期间已改变，请重新执行详细诊断；未写入项目内容。")
                snapshot = build_detailed_snapshot(collection, config, metadata, credentials=secrets)
                content = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
                final_credentials = self.secret_store.load()
                if (_credentials_fingerprint(final_credentials) != _credentials_fingerprint(secrets)
                        or _diagnostic_context(self.settings_store.load(), final_credentials, payload)
                        != self._last_diagnostic_context):
                    raise WorkerApiError("设置或凭据在准备文件期间已改变，请重新执行详细诊断；未写入项目内容。")
                checked_target = self._detailed_export_target(str(detailed_target))
                parent_stat = checked_target.parent.stat()
                if (checked_target != detailed_target or detailed_parent_identity != (parent_stat.st_dev, parent_stat.st_ino)):
                    raise WorkerApiError("保存目录在诊断期间已改变；没有写入详细文件，请重新选择位置。")
                write_private_diagnostic(detailed_target, content,
                                         expected_parent_identity=detailed_parent_identity)
            except WorkerApiError as exc:
                result["detailed_export"]["message"] = str(exc)
            except OSError:
                result["detailed_export"]["message"] = "详细诊断未能确认保存成功，文件可能已生成；请检查本机保存位置、权限和可用空间。脱敏结构仍可导出。"
            except Exception:
                # Do not echo payloads or exceptions from credential filtering.
                result["detailed_export"]["message"] = (
                    "无法安全准备详细诊断：数据超过预算或认证信息无法可靠清理；没有保存项目正文。脱敏结构仍可导出。")
            else:
                result["detailed_export"] = {
                    "saved": True, "bytes": len(content),
                    "contains_project_content": True,
                    "data_altered": True,
                    "omissions": snapshot.get("omissions", {}),
                    "message": "详细诊断已保存，包含项目内容并已剔除配置凭据；请仅私下分享用于排查。",
                }
        return result

    def _export_detailed_diagnostic_to_file(self, payload: dict) -> dict:
        if payload.get("include_project_content") is not True:
            raise WorkerApiError("详细诊断包含项目内容，请先在界面明确选择该导出方式。")
        target = self._detailed_export_target(payload.get("path"))
        parent_stat = target.parent.stat()
        result = self._diagnose(payload, detailed_target=target,
                                detailed_parent_identity=(parent_stat.st_dev, parent_stat.st_ino))
        if "detailed_export" not in result:
            result["detailed_export"] = {"saved": False, "message": "采集或诊断未完成，没有写入详细项目数据。"}
        return result

    def _detailed_export_target(self, raw_target) -> Path:
        if (not isinstance(raw_target, str) or not raw_target
                or _is_windows_device_path(raw_target) or not Path(raw_target).is_absolute()):
            raise WorkerApiError("请选择有效的本机 JSON 保存路径。")
        try:
            target = Path(raw_target).resolve()
            data_dir = self.data_dir.resolve()
            source_checkout = next((parent for parent in Path(__file__).resolve().parents
                                    if (parent / ".git").exists()), None)
            if (_is_windows_device_path(str(target)) or target.suffix.casefold() != ".json"
                    or target.name.casefold() in {"settings.json", "openai_models.json", "credentials.json"}
                    or str(target).casefold() == str(data_dir).casefold()
                    or str(target).casefold().startswith(str(data_dir).casefold() + os.sep)
                    or any(parent.name.casefold() == ".git" for parent in target.parents)
                    or (source_checkout is not None and target.is_relative_to(source_checkout))):
                raise ValueError("Protected output location")
        except (OSError, ValueError, RuntimeError):
            raise WorkerApiError("请选择应用数据目录及 Git 仓库之外的本机 JSON 文件，避免覆盖配置或提交项目内容。") from None
        return target

    def _export_diagnostic(self, payload: dict) -> dict:
        return {"shape": self._checked_diagnostic(payload)}

    def _diagnostic_status(self, payload: dict) -> dict:
        return {"diagnostic": self._checked_diagnostic(payload)["diagnostic"]}

    def _checked_diagnostic(self, payload: dict) -> dict:
        message = "诊断缺失，或设置、凭据、时间范围已改变；请重新运行只读诊断后导出。"
        if not isinstance(self._last_diagnostic_shape, dict) or not self._last_diagnostic_context:
            raise WorkerApiError(message)
        try:
            context = _diagnostic_context(self.settings_store.load(), self.secret_store.load(), payload)
        except (SettingsError, ValueError, TypeError):
            raise WorkerApiError(message) from None
        if context != self._last_diagnostic_context:
            raise WorkerApiError(message)
        return self._last_diagnostic_shape

    def _export_diagnostic_to_file(self, payload: dict) -> dict:
        target = payload.get("path")
        if not isinstance(target, str) or not target or not Path(target).is_absolute():
            raise WorkerApiError("请选择有效的本机 JSON 文件路径。")
        shape = self._checked_diagnostic(payload)
        try:
            content = json.dumps(shape, ensure_ascii=False, indent=2) + "\n"
            atomic_write(Path(target), content.encode("utf-8"))
        except OSError:
            raise WorkerApiError("无法写入所选文件，请检查目标文件夹权限和可用空间。") from None
        return {"status": "已保存当前版本、时间窗和脱敏结构；分享前请检查任务、动态与缺失成员 ID。"}

    def _preview_sample(self, payload: dict) -> dict:
        try:
            start, end = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
            start, end = start.astimezone(SHANGHAI), end.astimezone(SHANGHAI)
            sample = json.loads(self.sample_path.read_text(encoding="utf-8"))
            sample["date"] = end.date().isoformat()
            sample['time_range'] = {'start': start.isoformat(), 'end_exclusive': end.isoformat(),
                                    'timezone': 'Asia/Shanghai'}
            markdown = render_digest(sample)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise WorkerApiError("无法读取仓库样例或当前日期范围无效。") from None
        return {"markdown": markdown, "synthetic": True,
                "status": "使用仓库虚构样例；未采集任务、未调用 OpenAI、未发送钉钉。"}

    def _test_teambition(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        secrets = self.secret_store.load()
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise WorkerApiError("请先保存 Tenant ID 和 Project ID。")
        if not secrets.get("teambition_mcp_token", "").strip():
            raise WorkerApiError("请先保存 Teambition MCP 个人 Token。")
        if not mcp_connection_authorized(secrets):
            raise WorkerApiError("请先在账户页保存并确认授权本机连接 Teambition 官方 MCP；仅保存 Token 不会授权连接。")
        now = datetime.now().astimezone(SHANGHAI).replace(microsecond=0)
        start, end = now - timedelta(minutes=5), now
        try:
            collection = self._collect_source(
                settings, secrets, day=end.date().isoformat(), window=(start, end),
                include_archived=settings["include_archived"])
        except (CollectionError, OSError) as exc:
            if str(exc) == "Teambition MCP authorization expired or denied":
                raise WorkerApiError("Teambition MCP 个人 Token 已失效或无读取权限；请重新生成 Token 并保存，确认当前账户有项目读取权限。") from None
            raise WorkerApiError("Teambition MCP 只读连接测试失败；请检查本机网络、个人 Token 和项目读取权限。") from None
        return {"status": f"Teambition 只读查询成功；读取 {len(collection.get('records', []))} 个任务。",
                "records": len(collection.get("records", [])),
                "safety": "未调用 OpenAI；未发送钉钉。"}

    def _validate_dingtalk_config(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        options = mention_options(settings)
        mention_count = sum(len(targets) for targets in options.values())
        credentials = self.secret_store.load()
        reject_credential_mentions(settings, credentials)
        webhook = credentials.get("dingtalk_webhook", "")
        secret = credentials.get("dingtalk_signing_secret", "")
        if not webhook or not secret:
            raise WorkerApiError("请先保存钉钉 Webhook 和签名密钥。")
        try:
            from .dingtalk import signed_webhook
            signed_webhook(webhook, secret, 1)
        except (ValueError, TypeError):
            raise WorkerApiError("钉钉 Webhook 或签名密钥格式无效；没有发送测试消息。") from None
        return {"status": f"Webhook 与签名格式通过本机检查；提醒对象 {mention_count} 个，仅校验本机格式，未核验群成员资格；没有发送测试消息。",
                "mention_count": mention_count, "mention_mode": settings["dingtalk_mention_mode"],
                "safety": "未调用 OpenAI；未发送钉钉。"}

    def _run_manual(self, payload: dict) -> dict:
        if set(payload) - {"start", "end", "manual_request_id"}:
            raise WorkerApiError("手动发送请求包含不支持的选项；未发送消息。")
        request_id = payload["manual_request_id"] if "manual_request_id" in payload else str(uuid.uuid4())
        validate_manual_delivery_options(True, request_id)
        start, end = self._manual_window(payload)
        result = self._run(start, end, schedule_key=None,
                           allow_confirmed_repeat=True, manual_request_id=request_id)
        return {**result, "manual_request_id": request_id}

    def _run_due(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        if not settings.get("auto_send_enabled"):
            return {"started": 0, "events": []}
        now = datetime.now().replace(microsecond=0)
        enabled_since_text = settings.get("auto_send_enabled_since_local", "")
        enabled_since = datetime.fromisoformat(enabled_since_text) if enabled_since_text else None
        slots = recoverable_schedule_occurrences(settings, now, enabled_since=enabled_since)
        valid_keys = {slot[2] for slot in slots}
        self._handled_schedule_slots.intersection_update(valid_keys)
        events = []
        started = 0
        for slot, kind, key in slots:
            if key in self._handled_schedule_slots or self.history.has_schedule_key(key):
                self._handled_schedule_slots.add(key)
                continue
            self._handled_schedule_slots.add(key)
            start, end = scheduled_window(settings, kind, slot.date())
            try:
                result = self._run(start, end, schedule_key=key)
                events.append({"schedule_key": key, "state": result["state"], "message": "日报已发送并收到钉钉确认。"})
            except WorkerApiError as exc:
                events.append({"schedule_key": key, "state": "blocked", "message": str(exc)})
            started += 1
            if started >= 1:
                # Process one slot at a time so a long run cannot queue another send.
                break
        return {"started": started, "events": events}

    def _history(self, payload: dict) -> dict:
        limit = payload.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise WorkerApiError("历史记录数量无效。")
        return {"runs": [_safe_history_row(row) for row in self.history.list_runs(limit)]}

    def _history_detail(self, payload: dict) -> dict:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or len(run_id) > 100:
            raise WorkerApiError("运行记录编号无效。")
        row = self.history.get(run_id)
        if not row:
            raise WorkerApiError("没有找到所选运行记录。")
        safe = _safe_history_row(row)
        safe["digest_markdown"] = row["digest_markdown"]
        return {"run": safe}

    def _shutdown_worker(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        self._shutdown = True
        return {"status": "worker stopped"}

    def _run(self, start: datetime, end: datetime, schedule_key: str | None, *,
             allow_confirmed_repeat: bool = False, manual_request_id: str | None = None) -> dict:
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("已有日报或诊断任务正在运行；当前操作未并行执行。")
        try:
            settings = self.settings_store.load()
            secrets = self.secret_store.load()
            ready, message = self._ensure_ledger(secrets)
            if not ready:
                raise WorkerApiError(message)
            run_options = {"schedule_key": schedule_key}
            if allow_confirmed_repeat or manual_request_id is not None:
                run_options.update(allow_confirmed_repeat=allow_confirmed_repeat,
                                   manual_request_id=manual_request_id)
            workflow_options = {"history": self.history, "model_catalog": self.model_catalog}
            if self.checkpoint_factory is not None:
                workflow_options["checkpoint_factory"] = self.checkpoint_factory
            if self.workflow_factory is DigestWorkflow:
                workflow_options["client_factory"] = self.client_factory
            result = self.workflow_factory(settings, secrets, self.data_dir, **workflow_options).run(
                                               start, end, **run_options)
            return {"run_id": result["run_id"], "state": result["state"],
                    "markdown": result["report"]["markdown"],
                    "window_start": start.isoformat(timespec="minutes"),
                    "window_end": end.isoformat(timespec="minutes")}
        except WorkflowBlocked as exc:
            raise WorkerApiError(str(exc)) from None
        finally:
            self._job_lock.release()

    def _client(self, settings: dict, credentials: dict):
        return make_collection_client(
            settings, credentials, self.data_dir,
            client_factory=self.client_factory, checkpoint_factory=self.checkpoint_factory,
        )

    def _collect_source(self, settings: dict, credentials: dict, **options):
        """Guard cache use without reentering the lock held by formal runs."""
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("已有日报或诊断任务正在运行；当前操作未并行执行。")
        try:
            return self._client(settings, credentials).collect(settings["project_id"], **options)
        finally:
            self._job_lock.release()

    def _validate_auto_ready(self, settings: dict, credentials: dict) -> None:
        if not mcp_connection_authorized(credentials):
            raise WorkerApiError("请先在账户页保存并确认授权本机连接 Teambition 官方 MCP；仅保存 Token 不会授权连接。")
        if (settings.get("api_contract_verified") is not True
                or settings.get("view", {}).get("verified") is not True
                or not settings.get("view", {}).get("evidence")):
            raise WorkerApiError("启用自动运行前，请保存项目配置并通过自动只读核验。")
        required = (*SECRET_FIELDS, *INTERNAL_SECRET_FIELDS)
        if any(not credentials.get(key) for key in required):
            raise WorkerApiError("启用自动运行前，请保存 Teambition、钉钉、OpenAI 凭据和防重复身份。")
        cache, _error = self._load_model_cache()
        api_key = credentials["openai_api_key"]
        model = settings["openai_model"]
        if not cache_matches_key(cache, api_key):
            raise WorkerApiError("模型缓存未匹配。若在更换 Key，请先在“自动运行”关闭并保存，再保存新 Key、刷新模型，最后重新开启自动运行。")
        if model not in {item["id"] for item in cache["models"]}:
            raise WorkerApiError("所选模型不在当前 API Key 的缓存列表中。")
        if settings["openai_reasoning_effort"] not in ("default",) + reasoning_efforts(model):
            raise WorkerApiError("当前模型没有已核验的所选 reasoning effort。")
        if settings["openai_service_tier"] == "fast" and model not in KNOWN_FAST_MODELS:
            raise WorkerApiError("当前模型未核验支持 Fast 服务等级，请选择 Standard。")
        self._validate_dingtalk_config_with(credentials)
        ready, message = self._ensure_ledger(credentials)
        if not ready:
            raise WorkerApiError(message)

    def _validate_dingtalk_config_with(self, credentials: dict) -> None:
        try:
            from .dingtalk import signed_webhook
            signed_webhook(credentials["dingtalk_webhook"],
                           credentials["dingtalk_signing_secret"], 1)
        except (KeyError, ValueError, TypeError):
            raise WorkerApiError("钉钉 Webhook 或签名密钥格式无效。") from None

    def _ensure_ledger(self, credentials: dict) -> tuple[bool, str]:
        path = self.data_dir / "delivery_ledger.sqlite3"
        identity = credentials.get("delivery_ledger_id")
        try:
            if not path.exists() and not identity:
                identity = initialize_ledger(path)
                try:
                    self.secret_store.save(dict(credentials, delivery_ledger_id=identity))
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                credentials["delivery_ledger_id"] = identity
            elif not path.exists() or not identity:
                return False, "发送防重账本或本机身份缺失；为避免重复发送，自动运行已停用。"
            guard = DeliveryGuard(path, identity, persistent_storage_verified=True)
            guard.close()
        except (OSError, DeliveryBlocked, SecretStoreError):
            return False, "本机发送防重账本无法验证；自动运行已停用。"
        return True, "本机发送防重账本已验证。"

    def _load_model_cache(self):
        try:
            return self.model_catalog.load(), None
        except ModelCatalogError:
            return None, "本机模型缓存无法读取；请在账户页手动刷新。"

    @staticmethod
    def _manual_window(payload: dict):
        try:
            return parse_manual_window(payload.get("start", ""), payload.get("end", ""))
        except SettingsError as exc:
            raise WorkerApiError(str(exc)) from None

    @staticmethod
    def _require_empty_payload(payload: dict):
        if payload:
            raise WorkerApiError("此操作不接受附加参数。")

    @staticmethod
    def _validate_mapping_document(mapping: dict):
        from runtime_mappings import validate_mappings
        try:
            validate_mappings(mapping)
        except (CollectionError, ValueError, TypeError, KeyError):
            raise WorkerApiError("成员或状态映射无效；请核对 ID、姓名和已解决状态。") from None


def serve(input_stream=None, output_stream=None, *, service=None) -> None:
    """Serve a bounded JSON-lines protocol; never emit diagnostic details to stdout."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    service = service or WorkerService()
    while True:
        raw = input_stream.readline(MAX_PROTOCOL_BYTES + 1)
        if raw == "":
            return
        request_id = None
        if len(raw.encode("utf-8", errors="replace")) > MAX_PROTOCOL_BYTES:
            if not raw.endswith("\n"):
                _discard_line(input_stream)
            _write_response(output_stream, request_id, False,
                            error="请求超过本机协议大小限制。")
            continue
        try:
            request = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys,
                                 parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            request_id = request.get("id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
                request_id = None
                raise ValueError("request id invalid")
            # Only the native read-only operation opts in. Existing clients keep
            # exactly one final response and never receive unsolicited frames.
            observer = None
            if request.get("progress") is True and request.get("op") in {"diagnose", "save"}:
                previous_phase, last_emit = None, -float("inf")

                def observer(progress):
                    nonlocal previous_phase, last_emit
                    now = time.monotonic()
                    if progress["phase"] == previous_phase and now - last_emit < 0.5:
                        return
                    previous_phase, last_emit = progress["phase"], now
                    output_stream.write(json.dumps({"id": request_id, "kind": "progress",
                                                    "progress": progress}, separators=(",", ":")) + "\n")
                    output_stream.flush()

            with observe_collection(observer):
                result = service.dispatch(request)
            _write_response(output_stream, request_id, True, result=result)
            if getattr(service, "_shutdown", False):
                return
        except WorkerApiError as exc:
            _write_response(output_stream, request_id, False, error=str(exc))
        except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
            _write_response(output_stream, request_id, False, error="请求 JSON 格式无效。")
        except Exception:
            _write_response(output_stream, request_id, False,
                            error="操作未完成；详细 API 响应和凭据没有写入日志。")


def _write_response(output_stream, request_id, ok, *, result=None, error=None):
    response = {"id": request_id, "ok": bool(ok)}
    if ok:
        response["result"] = result
    else:
        response["error"] = error or "操作未完成。"
    output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    output_stream.flush()


def _discard_line(stream):
    while True:
        chunk = stream.readline(8192)
        if not chunk or chunk.endswith("\n"):
            return


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_history_row(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "run_id", "created_at_utc", "window_start_local", "window_end_local",
        "source", "state", "delivery_note",
    )}


def _secret_presence(values: dict) -> dict[str, bool]:
    return {**{key: bool(values.get(key)) for key in SECRET_FIELDS + UPDATE_SECRET_FIELDS},
            "teambition_mcp_authorized": mcp_connection_authorized(values)}


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


def _current_application_executable() -> str:
    configured = os.environ.get("UPCAM_APP_EXECUTABLE", "")
    if not configured:
        return str(Path(sys.executable).resolve().parent / _APP_EXE_NAME)
    candidate = Path(configured).resolve()
    if candidate.name.casefold() != _APP_EXE_NAME.casefold():
        raise StartupIntegrationError("WinUI app executable path is invalid")
    return str(candidate)


def _scope_signature(settings: dict) -> dict:
    view = settings.get("view", {})
    filters = view.get("task_equals", {}) if isinstance(view, dict) else {}
    return {
        "tenant_id": settings.get("tenant_id", ""),
        "project_id": settings.get("project_id", ""),
        "include_archived": settings.get("include_archived", False),
        "task_equals": dict(filters) if isinstance(filters, dict) else {},
    }


def _verified_settings_for_save(settings_input: dict, old: dict, probe: dict | None,
                                *, credential_identity: str | None,
                                previous_credential_identity: str | None) -> dict:
    proposed = dict(settings_input)
    new_view = proposed.get("view", {})
    if not isinstance(new_view, dict):
        raise WorkerApiError("项目范围设置无效；未保存任何更改。")
    filters = new_view.get("task_equals", {})
    if not isinstance(filters, dict):
        raise WorkerApiError("项目筛选条件无效；未保存任何更改。")
    scope = _scope_signature(proposed)
    old_scope = _scope_signature(old)
    scope_unchanged = scope == old_scope
    credentials_unchanged = bool(credential_identity and credential_identity == previous_credential_identity)
    probe_matches = _probe_matches(probe, scope, credential_identity)
    old_view = old.get("view", {})
    old_view_verified = (isinstance(old_view, dict) and old_view.get("verified") is True
                         and bool(old_view.get("evidence")))
    prior_view_verified = (old.get("api_contract_verified") is True and old_view_verified
                           and scope_unchanged and credentials_unchanged)
    view_verified = bool(probe_matches or prior_view_verified)
    evidence = old_view.get("evidence", "") if prior_view_verified else ""
    if view_verified and probe_matches:
        signature = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        scope_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        evidence = ("desktop-readonly-probe/v2; "
                    f"tasks={probe['task_count']}; activities={probe['activity_count']}; "
                    f"selection={probe['selection']['start']}/{probe['selection']['end_exclusive']}; "
                    f"scope_sha256={scope_hash}; selection_saved=true")
    proposed["api_contract_verified"] = view_verified
    proposed["view"] = {"verified": view_verified,
                         "evidence": evidence if view_verified else "",
                         "task_equals": dict(filters)}
    return proposed


def _teambition_identity(credentials: dict) -> str | None:
    """Compare authentication in memory without returning credential material."""
    return teambition_mcp_identity(credentials) if mcp_connection_authorized(credentials) else None


def _is_windows_device_path(value: str) -> bool:
    """Device aliases have different comparison rules from ordinary paths."""
    normalized = value.replace("/", "\\")
    return normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))


def _credentials_fingerprint(credentials: dict) -> str:
    """Compare the in-memory redaction set without exposing any credential."""
    return hashlib.sha256(json.dumps(credentials, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _redact_known_diagnostic_credentials(message: str, *credential_sets: dict) -> str:
    """Diagnostic names/IDs must not echo configured credential values."""
    values = set()
    for credentials in credential_sets:
        for value in credentials.values():
            if isinstance(value, str) and value:
                values.add(value)
        webhook = credentials.get("dingtalk_webhook", "")
        try:
            for key, value in parse_qsl(urlsplit(webhook).query, keep_blank_values=False):
                if key.casefold() in {"access_token", "token", "sign", "signature"} and value:
                    values.add(value)
        except (TypeError, ValueError):
            pass
    for value in sorted(values, key=len, reverse=True):
        message = message.replace(value, "[凭据已移除]")
    return message


def _diagnostic_context(settings: dict, credentials: dict, payload: dict) -> str:
    """Bind an in-memory diagnostic to its source and exact half-open window."""
    start, end = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
    identity = _teambition_identity(credentials)
    if identity is None:
        raise ValueError("Missing Teambition identity")
    context = {
        "scope": _scope_signature(settings), "report_config": runtime_config(settings),
        "collection_limits": collection_client_limits(settings),
        "identity": identity,
        "start": start.astimezone(SHANGHAI).isoformat(),
        "end_exclusive": end.astimezone(SHANGHAI).isoformat(),
    }
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _probe_matches(probe: dict | None, scope: dict, credential_identity: str | None) -> bool:
    return bool(credential_identity and probe and probe.get("scope") == scope
                and probe.get("credential_identity") == credential_identity)


def _merge_probe_inputs(settings: dict, payload: dict) -> dict:
    proposed = dict(settings)
    for key in ("tenant_id", "project_id", "include_archived", "max_tasks_per_run"):
        if key in payload:
            proposed[key] = payload[key]
    if "task_equals" in payload:
        view = dict(proposed.get("view", {}))
        view["task_equals"] = payload["task_equals"]
        proposed["view"] = view
    return proposed


def _auto_enabled_changed(old: dict, proposed: dict) -> bool:
    return bool(old.get("auto_send_enabled")) != bool(proposed.get("auto_send_enabled"))


def _safe_error_message(exc: BaseException | None) -> str:
    message = str(exc) if exc is not None else ""
    if not message or any(marker in message.casefold() for marker in (
        "authorization", "bearer ", "access_token", "secret=", "api key",
    )):
        return "操作未完成；详细 API 响应和凭据没有写入日志。"
    if len(message) > 2400:
        notice = "\n错误说明已截断；请运行只读诊断并复制诊断文字，或导出安全结构查看完整问题。"
        return message[:2400 - len(notice)] + notice
    return message
