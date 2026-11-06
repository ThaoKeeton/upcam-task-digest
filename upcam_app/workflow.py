"""Local Teambition collection, minimal OpenAI summary and guarded DingTalk send."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from automated_digest import (
    AutomatedDigestBlocked, build_automated_digest, collection_validation_message,
    requires_openai_summary,
)
from delivery_guard import DeliveryBlocked, DeliveryGuard, validate_manual_delivery_options
from digest_pipeline import validate_view
from report_contract import SHANGHAI
from runtime_mappings import validate_mappings
from teambition_readonly import Client, CollectionError

from .dingtalk import DingTalkSender
from .dingtalk_mentions import MentionConfigError, mention_options, reject_credential_mentions
from .history import HistoryError, HistoryStore
from .openai_models import ModelCatalogError, OpenAIModelCatalog, cache_matches_key
from .secrets import SECRET_FIELDS, INTERNAL_SECRET_FIELDS
from .config import runtime_config, user_data_dir
from .collection_runtime import make_collection_client


class WorkflowBlocked(RuntimeError):
    """An automatic run stopped before an unsafe or duplicate delivery."""


class DigestWorkflow:
    def __init__(self, settings: dict, secrets: dict[str, str], data_dir: Path | None = None,
                 *, client_factory=Client, summarizer=None, ding_sender=None,
                 history=None, model_catalog=None, guard_factory=DeliveryGuard,
                 checkpoint_factory=None):
        self.settings = settings
        self.secrets = dict(secrets)
        self.data_dir = Path(data_dir or user_data_dir())
        self.client_factory = client_factory
        self.summarizer = summarizer
        self.ding_sender = ding_sender or DingTalkSender()
        self.history = history or HistoryStore(self.data_dir / "history.sqlite3")
        self.model_catalog = model_catalog or OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.guard_factory = guard_factory
        self.checkpoint_factory = checkpoint_factory

    def run(self, start_local: datetime, end_local: datetime, *, schedule_key: str | None = None,
            allow_confirmed_repeat: bool = False, manual_request_id: str | None = None) -> dict:
        """Run one fixed local-time range; every delivery enters DeliveryGuard."""
        try:
            validate_manual_delivery_options(allow_confirmed_repeat, manual_request_id)
        except DeliveryBlocked as exc:
            raise WorkflowBlocked(str(exc)) from None
        if allow_confirmed_repeat and schedule_key is not None:
            raise WorkflowBlocked("定时任务不能使用手动重复发送选项；未发送消息。")
        if (not isinstance(start_local, datetime) or not isinstance(end_local, datetime)
                or end_local <= start_local):
            raise WorkflowBlocked("The selected collection window is invalid")
        if not self.settings.get("tenant_id") or not self.settings.get("project_id"):
            raise WorkflowBlocked("Teambition tenant and project IDs must be configured")
        config = runtime_config(self.settings)
        try:
            reject_credential_mentions(config, self.secrets)
        except MentionConfigError as exc:
            raise WorkflowBlocked(str(exc)) from None
        sender_options = mention_options(config)
        view = validate_view(config)
        if (config.get("api_contract_verified") is not True or view.get("verified") is not True
                or not view.get("evidence")):
            raise WorkflowBlocked("请先保存项目范围配置并完成自动只读核验；核验未通过时不会发送。可在项目范围页选择有动态的时间区间后重新保存。")

        required = tuple(key for key in SECRET_FIELDS if key != "openai_api_key") + INTERNAL_SECRET_FIELDS
        missing = [name for name in required if not self.secrets.get(name)]
        if missing:
            labels = {
                "teambition_app_id": "Teambition App ID",
                "teambition_app_secret": "Teambition App Secret",
                "dingtalk_webhook": "DingTalk webhook",
                "dingtalk_signing_secret": "DingTalk signing secret",
                "openai_api_key": "OpenAI API Key",
                "delivery_ledger_id": "delivery ledger identity",
            }
            human_labels = [labels.get(name, "required credential") for name in missing]
            raise WorkflowBlocked("Missing local configuration: " + ", ".join(human_labels))

        start_shanghai = start_local.astimezone(SHANGHAI)
        end_shanghai = end_local.astimezone(SHANGHAI)
        report_day = end_shanghai.date().isoformat()
        window = (start_shanghai, end_shanghai)
        if schedule_key and any(run.get("schedule_key") == schedule_key for run in self.history.list_runs(1000)):
            raise WorkflowBlocked("This scheduled run slot already has a local history record")

        try:
            client = make_collection_client(
                self.settings, self.secrets, self.data_dir,
                client_factory=self.client_factory, checkpoint_factory=self.checkpoint_factory,
            )
            collection = client.collect(
                config["project_id"], include_archived=config["include_archived"],
                day=report_day, window=window,
            )
        except CollectionError as exc:
            message = collection_validation_message(exc)
            self._record_blocked_attempt(start_local, end_local, schedule_key, message)
            raise WorkflowBlocked(message) from None
        except Exception:
            message = "Teambition 只读采集失败；未调用 OpenAI、未发送钉钉。检查网络、凭据和项目读取权限后，使用“只读诊断当前数据”重试。"
            self._record_blocked_attempt(start_local, end_local, schedule_key, message)
            raise WorkflowBlocked(message) from None

        try:
            needs_openai = requires_openai_summary(collection, config, report_day, window)
        except AutomatedDigestBlocked as exc:
            self._record_blocked_attempt(start_local, end_local, schedule_key, str(exc))
            raise WorkflowBlocked(str(exc)) from None
        except (ValueError, CollectionError) as exc:
            message = collection_validation_message(exc)
            self._record_blocked_attempt(start_local, end_local, schedule_key, message)
            raise WorkflowBlocked(message) from None

        model = self.settings.get("openai_model")
        effort = self.settings.get("openai_reasoning_effort")
        service_tier = self.settings.get("openai_service_tier", "default")
        api_key = self.secrets.get("openai_api_key", "")
        if needs_openai:
            if not api_key:
                raise WorkflowBlocked("OpenAI API Key is required for substantive comment summaries")
            try:
                cache = self.model_catalog.load()
            except ModelCatalogError:
                raise WorkflowBlocked("The local model cache is invalid; refresh models using the saved API Key") from None
            if not cache_matches_key(cache, api_key):
                raise WorkflowBlocked("Refresh the model list with the currently saved OpenAI API Key before running")
            if model not in {item["id"] for item in cache["models"]}:
                raise WorkflowBlocked("The selected model is not in this API Key's cached model list")

        try:
            report = build_automated_digest(
                collection, config, report_day, api_key, model, effort,
                client=self.summarizer, window=window, service_tier=service_tier,
            )
        except AutomatedDigestBlocked as exc:
            self._record_blocked_attempt(start_local, end_local, schedule_key, str(exc))
            raise WorkflowBlocked(str(exc)) from None
        except Exception:
            message = "自动摘要或本地校验失败；本次未发送钉钉。检查本机配置和运行记录中的诊断说明。"
            self._record_blocked_attempt(start_local, end_local, schedule_key, message)
            raise WorkflowBlocked(message) from None
        if report.get("ready_to_send") is not True or report.get("issues"):
            message = "自动日报仍含未解决的校验问题；本次未发送钉钉。请使用“只读诊断当前数据”检查范围、映射和事件。"
            self._record_blocked_attempt(start_local, end_local, schedule_key, message)
            raise WorkflowBlocked(message)

        try:
            run_id = self.history.create_draft(
                start_local, end_local, "teambition", report["markdown"], schedule_key,
            )
            self.history.start_delivery(run_id)
        except HistoryError as exc:
            raise WorkflowBlocked(str(exc)) from None

        guard = None
        try:
            guard = self.guard_factory(
                self.data_dir / "delivery_ledger.sqlite3",
                self.secrets["delivery_ledger_id"],
                persistent_storage_verified=True,
            )
            guard.attempt(
                report, tenant=config["tenant_id"], project=config["project_id"],
                destination=self.secrets["dingtalk_webhook"],
                send=lambda markdown: self.ding_sender.send_markdown(
                    self.secrets["dingtalk_webhook"], self.secrets["dingtalk_signing_secret"], markdown,
                    **sender_options),
                current_collection=collection, current_config=config,
                current_model=model, current_effort=effort, current_service_tier=service_tier,
                allow_confirmed_repeat=allow_confirmed_repeat, manual_request_id=manual_request_id,
            )
            self.history.finish_delivery(run_id, "sent", "DingTalk acknowledged delivery.")
        except DeliveryBlocked as exc:
            state = "delivery_unknown" if exc.outcome == "unknown" else "blocked"
            self.history.finish_delivery(run_id, state, str(exc))
            raise WorkflowBlocked(str(exc)) from None
        except Exception:
            try:
                self.history.finish_delivery(run_id, "blocked", "Automatic delivery stopped; inspect local run details.")
            except HistoryError:
                pass
            raise WorkflowBlocked("Automatic delivery stopped; no retry was attempted") from None
        finally:
            if guard is not None:
                guard.close()

        return {"run_id": run_id, "state": "sent", "report": report,
                "window_start": start_local, "window_end": end_local}

    def _record_blocked_attempt(self, start_local, end_local, schedule_key, note):
        """Save only the safe local diagnostic; never persist Teambition records or secrets."""
        try:
            self.history.create_blocked_attempt(start_local, end_local, note, schedule_key)
        except Exception:
            # Diagnostic persistence must never mask the original fail-closed result.
            pass
