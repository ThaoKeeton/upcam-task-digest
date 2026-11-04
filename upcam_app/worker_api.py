"""Narrow, credential-safe command boundary for the WinUI desktop frontend."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import threading

from automated_digest import collection_validation_message, diagnose_automatic_source
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from openai_summarizer import KNOWN_FAST_MODELS
from render_digest import render_digest
from report_contract import SHANGHAI, updated_selection
from teambition_readonly import Client, CollectionError

from .config import (
    SettingsError, SettingsStore, collection_client_limits,
    parse_manual_window, recoverable_schedule_occurrences, runtime_config,
    scheduled_window, user_data_dir, validate_settings,
)
from .diagnostic_export import safe_structure_export
from .history import HistoryError, HistoryStore
from .openai_models import (
    ModelCatalogError, OpenAIModelCatalog, cache_matches_key, reasoning_efforts,
)
from .project_setup import (
    ProjectSetupError, analyze_project_probe, merge_owner_mappings,
    parse_owner_mapping_tsv,
)
from .secrets import (
    INTERNAL_SECRET_FIELDS, SECRET_FIELDS,
    DpapiSecretStore, SecretStoreError,
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
                 workflow_factory=DigestWorkflow, sample_path=None):
        self.data_dir = Path(data_dir or user_data_dir())
        self.settings_store = settings_store or SettingsStore(self.data_dir / "settings.json")
        self.secret_store = secret_store or DpapiSecretStore(self.data_dir / "credentials.dpapi")
        self.history = history or HistoryStore(self.data_dir / "history.sqlite3")
        self.model_catalog = model_catalog or OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.startup_configurer = startup_configurer or configure_user_startup
        self.client_factory = client_factory
        self.workflow_factory = workflow_factory
        self.sample_path = Path(sample_path or _resource_path("examples/digest.json"))
        self._job_lock = threading.Lock()
        self._handled_schedule_slots: set[str] = set()
        self._setup_probe: dict | None = None
        self._last_diagnostic_shape: dict | None = None
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
            "clear_credentials": self._clear_credentials,
            "refresh_models": self._refresh_models,
            "import_owner_mappings": self._import_owner_mappings,
            "update_owner_mapping": self._update_owner_mapping,
            "update_status_mapping": self._update_status_mapping,
            "update_view_filter": self._update_view_filter,
            "probe_project": self._probe_project,
            "diagnose": self._diagnose,
            "export_diagnostic": self._export_diagnostic,
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
        except (SettingsError, SecretStoreError, ModelCatalogError, ProjectSetupError,
                CollectionError, WorkflowBlocked, HistoryError, DeliveryBlocked,
                StartupIntegrationError):
            raise WorkerApiError(_safe_error_message(sys.exc_info()[1])) from None
        except Exception:
            # Exception text can include headers, response bodies, or user-entered data.
            raise WorkerApiError("操作未完成；详细 API 响应和凭据没有写入日志。请检查设置并重试。") from None

    def _initialize(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        credentials = self.secret_store.load()
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
            "reasoning_efforts": {item["id"]: list(reasoning_efforts(item["id"]))
                                  for item in cached_models},
            "fast_models": sorted(KNOWN_FAST_MODELS),
            "history": [_safe_history_row(row) for row in self.history.list_runs(100)],
            "ledger_ready": ledger_ready,
            "ledger_message": ledger_message,
            "data_directory": str(self.data_dir),
            "application_version": "0.3.0",
        }

    def _save(self, payload: dict) -> dict:
        settings_input = payload.get("settings")
        secret_values = payload.get("secret_values", {})
        clear_secrets = payload.get("clear_secrets", [])
        scope_confirmed = payload.get("scope_confirmed", False)
        if not isinstance(settings_input, dict) or not isinstance(secret_values, dict):
            raise WorkerApiError("设置内容无效；未保存任何更改。")
        if type(scope_confirmed) is not bool or not isinstance(clear_secrets, list):
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
        if _auto_enabled_changed(previous_settings, settings_input):
            settings_input = dict(settings_input)
            settings_input["auto_send_enabled_since_local"] = (
                datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
                if settings_input.get("auto_send_enabled") is True else "")

        proposed = _verified_settings_for_save(
            settings_input, previous_settings, self._setup_probe,
            scope_confirmed=scope_confirmed,
        )
        clean = validate_settings(proposed)
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
            "status": "本机设置已保存；服务密钥由 Windows 当前用户 DPAPI 加密。",
        }

    def _clear_credentials(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        settings = self.settings_store.load()
        settings["auto_send_enabled"] = False
        settings["auto_send_enabled_since_local"] = ""
        self.settings_store.save(settings)
        current = self.secret_store.load()
        retained = {key: value for key, value in current.items()
                    if key in INTERNAL_SECRET_FIELDS}
        self.secret_store.save(retained)
        return {"settings": settings, "secret_presence": _secret_presence(retained),
                "status": "已清除服务密钥并关闭自动运行；本机防重复身份已保留。"}

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
        return {"filters": updated, "scope_confirmed": False}

    def _probe_project(self, payload: dict) -> dict:
        settings = self.settings_store.load()
        secrets = self.secret_store.load()
        settings = _merge_probe_inputs(settings, payload)
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise WorkerApiError("请先填写 Tenant ID 和 Project ID。")
        if not secrets.get("teambition_app_id") or not secrets.get("teambition_app_secret"):
            raise WorkerApiError("请先保存 Teambition App ID 和 App Secret。")
        try:
            start_local, end_local = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
            start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
            report_day = end.date().isoformat()
            selection = updated_selection(report_day, start, end)
            validate_settings(settings)
            client = self._client(settings, secrets)
            collection = client.collect(settings["project_id"], day=report_day,
                                        window=(start, end),
                                        include_archived=settings["include_archived"])
            probe = analyze_project_probe(
                collection, tenant_id=settings["tenant_id"], project_id=settings["project_id"],
                expected_selection=selection, include_archived=settings["include_archived"])
        except ProjectSetupError as exc:
            self._setup_probe = None
            raise WorkerApiError(str(exc)) from None
        except (CollectionError, OSError):
            self._setup_probe = None
            raise WorkerApiError("Teambition 只读查询未完成或翻页不完整；没有更改核验状态。请检查权限后再试。") from None
        except (SettingsError, ValueError, TypeError):
            self._setup_probe = None
            raise WorkerApiError("项目设置或时间范围无效；没有更改核验状态。") from None
        scope = _scope_signature(settings)
        self._setup_probe = {"scope": scope, "selection": selection,
                             "task_count": probe.task_count,
                             "activity_count": probe.activity_count,
                             "tenant_id": probe.tenant_id,
                             "project_id": probe.project_id}
        return {
            "task_count": probe.task_count,
            "activity_count": probe.activity_count,
            "owner_ids": list(probe.owner_ids),
            "status_ids": list(probe.status_ids),
            "status_name_suggestions": probe.status_name_suggestions,
            "conflicting_status_ids": list(probe.conflicting_status_ids),
            "observed_actions": list(probe.observed_actions),
            "selection": selection,
            "status": "只读样本通过。成员姓名仍需根据项目成员页核对；状态名称建议也需核对。",
        }

    def _diagnose(self, payload: dict) -> dict:
        settings = self.settings_store.load()
        secrets = self.secret_store.load()
        if not settings.get("tenant_id") or not settings.get("project_id"):
            raise WorkerApiError("请先保存 Tenant ID 和 Project ID。")
        if not secrets.get("teambition_app_id") or not secrets.get("teambition_app_secret"):
            raise WorkerApiError("只读诊断需要已保存的 Teambition App ID 和 App Secret；不需要 OpenAI 或钉钉密钥。")
        try:
            start_local, end_local = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
            start, end = start_local.astimezone(SHANGHAI), end_local.astimezone(SHANGHAI)
            day = end.date().isoformat()
            config = runtime_config(settings)
            collection = self._client(settings, secrets).collect(
                settings["project_id"], day=day, window=(start, end),
                include_archived=settings["include_archived"])
            diagnostic = diagnose_automatic_source(collection, config, day, (start, end))
            self._last_diagnostic_shape = safe_structure_export(collection, config)
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
        return {**diagnostic, "export_available": bool(self._last_diagnostic_shape),
                "safety": "未调用 OpenAI；未发送钉钉。"}

    def _export_diagnostic(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        if not isinstance(self._last_diagnostic_shape, dict):
            raise WorkerApiError("没有可导出的诊断；请先运行只读诊断。")
        return {"shape": self._last_diagnostic_shape}

    def _preview_sample(self, payload: dict) -> dict:
        try:
            _start, end = parse_manual_window(payload.get("start", ""), payload.get("end", ""))
            sample = json.loads(self.sample_path.read_text(encoding="utf-8"))
            sample["date"] = end.date().isoformat()
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
        if not secrets.get("teambition_app_id") or not secrets.get("teambition_app_secret"):
            raise WorkerApiError("请先保存 Teambition App ID 和 App Secret。")
        now = datetime.now().astimezone(SHANGHAI).replace(microsecond=0)
        start, end = now - timedelta(minutes=5), now
        try:
            collection = self._client(settings, secrets).collect(
                settings["project_id"], day=end.date().isoformat(), window=(start, end),
                include_archived=settings["include_archived"])
        except (CollectionError, OSError):
            raise WorkerApiError("Teambition 只读连接测试失败；请检查本机网络、凭据和项目读取权限。") from None
        return {"status": f"Teambition 只读查询成功；读取 {len(collection.get('records', []))} 个任务。",
                "records": len(collection.get("records", [])),
                "safety": "未调用 OpenAI；未发送钉钉。"}

    def _validate_dingtalk_config(self, payload: dict) -> dict:
        self._require_empty_payload(payload)
        credentials = self.secret_store.load()
        webhook = credentials.get("dingtalk_webhook", "")
        secret = credentials.get("dingtalk_signing_secret", "")
        if not webhook or not secret:
            raise WorkerApiError("请先保存钉钉 Webhook 和签名密钥。")
        try:
            from .dingtalk import signed_webhook
            signed_webhook(webhook, secret, 1)
        except (ValueError, TypeError):
            raise WorkerApiError("钉钉 Webhook 或签名密钥格式无效；没有发送测试消息。") from None
        return {"status": "Webhook 与签名格式通过本机检查；没有发送测试消息。",
                "safety": "未调用 OpenAI；未发送钉钉。"}

    def _run_manual(self, payload: dict) -> dict:
        start, end = self._manual_window(payload)
        return self._run(start, end, schedule_key=None)

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

    def _run(self, start: datetime, end: datetime, schedule_key: str | None) -> dict:
        if not self._job_lock.acquire(blocking=False):
            raise WorkerApiError("已有日报或诊断任务正在运行；当前操作未并行执行。")
        try:
            settings = self.settings_store.load()
            secrets = self.secret_store.load()
            result = self.workflow_factory(settings, secrets, self.data_dir,
                                           history=self.history,
                                           model_catalog=self.model_catalog).run(
                                               start, end, schedule_key=schedule_key)
            return {"run_id": result["run_id"], "state": result["state"],
                    "markdown": result["report"]["markdown"],
                    "window_start": start.isoformat(timespec="minutes"),
                    "window_end": end.isoformat(timespec="minutes")}
        except WorkflowBlocked as exc:
            raise WorkerApiError(str(exc)) from None
        finally:
            self._job_lock.release()

    def _client(self, settings: dict, credentials: dict):
        return self.client_factory(
            settings["tenant_id"],
            credentials=lambda: (credentials["teambition_app_id"],
                                 credentials["teambition_app_secret"]),
            **collection_client_limits(settings),
        )

    def _validate_auto_ready(self, settings: dict, credentials: dict) -> None:
        if (settings.get("api_contract_verified") is not True
                or settings.get("view", {}).get("verified") is not True
                or not settings.get("view", {}).get("evidence")):
            raise WorkerApiError("启用自动运行前，请完成项目范围只读验证并确认范围。")
        required = (*SECRET_FIELDS, *INTERNAL_SECRET_FIELDS)
        if any(not credentials.get(key) for key in required):
            raise WorkerApiError("启用自动运行前，请保存 Teambition、钉钉、OpenAI 凭据和防重复身份。")
        cache, _error = self._load_model_cache()
        api_key = credentials["openai_api_key"]
        model = settings["openai_model"]
        if not cache_matches_key(cache, api_key):
            raise WorkerApiError("请先用当前保存的 OpenAI API Key 刷新模型列表。")
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
    return {key: bool(values.get(key)) for key in SECRET_FIELDS}


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
                                *, scope_confirmed: bool) -> dict:
    proposed = dict(settings_input)
    new_view = proposed.get("view", {})
    if not isinstance(new_view, dict):
        raise WorkerApiError("项目范围设置无效；未保存任何更改。")
    filters = new_view.get("task_equals", {})
    if not isinstance(filters, dict):
        raise WorkerApiError("项目筛选条件无效；未保存任何更改。")
    scope = _scope_signature(proposed)
    old_scope = _scope_signature(old)
    identity_unchanged = (scope["tenant_id"] == old_scope["tenant_id"]
                          and scope["project_id"] == old_scope["project_id"])
    scope_unchanged = scope == old_scope
    probe_matches = bool(probe and probe.get("scope") == scope)
    old_view = old.get("view", {})
    old_view_verified = (isinstance(old_view, dict) and old_view.get("verified") is True
                         and bool(old_view.get("evidence")))
    prior_api_verified = old.get("api_contract_verified") is True and identity_unchanged
    prior_view_verified = old_view_verified and scope_unchanged
    view_verified = bool(scope_confirmed and (probe_matches or prior_view_verified))
    evidence = old_view.get("evidence", "") if prior_view_verified else ""
    if view_verified and probe_matches:
        signature = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        import hashlib
        scope_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        evidence = ("desktop-readonly-probe/v1; "
                    f"tasks={probe['task_count']}; activities={probe['activity_count']}; "
                    f"selection={probe['selection']['start']}/{probe['selection']['end_exclusive']}; "
                    f"scope_sha256={scope_hash}; operator_confirmed=true")
    proposed["api_contract_verified"] = bool(prior_api_verified or probe_matches)
    proposed["view"] = {"verified": view_verified,
                         "evidence": evidence if view_verified else "",
                         "task_equals": dict(filters)}
    return proposed


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
    return message[:2400]
