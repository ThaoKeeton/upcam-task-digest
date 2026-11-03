"""Local Teambition collection, minimal OpenAI summary and guarded DingTalk send."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from automated_digest import AutomatedDigestBlocked, build_automated_digest, requires_openai_summary
from delivery_guard import DeliveryBlocked, DeliveryGuard
from digest_pipeline import validate_view
from report_contract import SHANGHAI
from runtime_mappings import validate_mappings
from teambition_readonly import Client, CollectionError

from .dingtalk import DingTalkSender
from .history import HistoryError, HistoryStore
from .openai_models import ModelCatalogError, OpenAIModelCatalog, cache_matches_key
from .secrets import SECRET_FIELDS, INTERNAL_SECRET_FIELDS
from .config import runtime_config, user_data_dir


class WorkflowBlocked(RuntimeError):
    """An automatic run stopped before an unsafe or duplicate delivery."""


class DigestWorkflow:
    def __init__(self, settings: dict, secrets: dict[str, str], data_dir: Path | None = None,
                 *, client_factory=Client, summarizer=None, ding_sender=None,
                 history=None, model_catalog=None, guard_factory=DeliveryGuard):
        self.settings = settings
        self.secrets = dict(secrets)
        self.data_dir = Path(data_dir or user_data_dir())
        self.client_factory = client_factory
        self.summarizer = summarizer
        self.ding_sender = ding_sender or DingTalkSender()
        self.history = history or HistoryStore(self.data_dir / "history.sqlite3")
        self.model_catalog = model_catalog or OpenAIModelCatalog(self.data_dir / "openai_models.json")
        self.guard_factory = guard_factory

    def run(self, start_local: datetime, end_local: datetime, *, schedule_key: str | None = None) -> dict:
        """Run one fixed local-time range; every delivery enters DeliveryGuard."""
        if (not isinstance(start_local, datetime) or not isinstance(end_local, datetime)
                or end_local <= start_local):
            raise WorkflowBlocked("The selected collection window is invalid")
        if not self.settings.get("tenant_id") or not self.settings.get("project_id"):
            raise WorkflowBlocked("Teambition tenant and project IDs must be configured")
        config = runtime_config(self.settings)
        view = validate_view(config)
        if (config.get("api_contract_verified") is not True or view.get("verified") is not True
                or not view.get("evidence")):
            raise WorkflowBlocked("Teambition API fields and project view must be verified in setup")

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

        client = self.client_factory(
            config["tenant_id"],
            credentials=lambda: (self.secrets["teambition_app_id"], self.secrets["teambition_app_secret"]),
        )
        try:
            collection = client.collect(
                config["project_id"], include_archived=config["include_archived"],
                day=report_day, window=window,
            )
        except CollectionError as exc:
            raise WorkflowBlocked(str(exc)) from None
        except Exception:
            raise WorkflowBlocked("Teambition collection failed; no summary was sent to OpenAI") from None

        try:
            needs_openai = requires_openai_summary(collection, config, report_day, window)
        except AutomatedDigestBlocked as exc:
            raise WorkflowBlocked(str(exc)) from None
        except (ValueError, CollectionError):
            raise WorkflowBlocked("Automatic source validation failed; no text was sent to OpenAI") from None

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
            raise WorkflowBlocked(str(exc)) from None
        except Exception:
            raise WorkflowBlocked("Automatic summary failed; this digest was not sent") from None
        if report.get("ready_to_send") is not True or report.get("issues"):
            raise WorkflowBlocked("Automatic report validation failed; this digest was not sent")

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
                    self.secrets["dingtalk_webhook"], self.secrets["dingtalk_signing_secret"], markdown),
                current_collection=collection, current_config=config,
                current_model=model, current_effort=effort, current_service_tier=service_tier,
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
