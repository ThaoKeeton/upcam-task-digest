"""Explicit synthetic-only service for packaged Windows UI acceptance tests."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from report_contract import updated_selection

from .config import DEFAULT_SETTINGS, SettingsStore
from .openai_models import OpenAIModelCatalog
from .secrets import MemorySecretStore
from .worker_api import WorkerService
from .workflow import DigestWorkflow


class SyntheticClient:
    """No transport exists: this class cannot reach an enterprise endpoint."""

    def __init__(self, tenant_id, **_kwargs):
        self.tenant_id = tenant_id

    def collect(self, project_id, *, day, window, include_archived=False, **_kwargs):
        start, end = window
        instant = start + (end - start) / 2
        timestamp = instant.isoformat()
        return {
            "tenant_id": self.tenant_id, "project_id": project_id,
            "include_archived": include_archived, "pagination_complete": True,
            "selection": updated_selection(day, start, end),
            "batch_id": str(uuid.uuid4()),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "records": [{
                "task": {
                    "id": "ci-synthetic-task", "projectId": project_id,
                    "isArchived": False, "content": "CI 虚构：验证原生界面布局",
                    "uniqueId": 101, "project": {"_id": project_id, "uniqueIdPrefix": "DEMO"},
                    "executorId": "demo-person", "tfsId": "demo-open",
                    "created": timestamp, "updated": timestamp, "isDone": False,
                    "tasklistId": "2" * 24, "stageId": "4" * 24,
                },
                "activities": [{
                    "id": "ci-synthetic-event", "action": "create",
                    "boundToObjectId": "ci-synthetic-task", "boundToObjectType": "task",
                    "createTime": timestamp, "content": "",
                }, {
                    "id": "ci-synthetic-comment", "action": "comment",
                    "boundToObjectId": "ci-synthetic-task", "boundToObjectType": "task",
                    "createTime": timestamp,
                    "content": json.dumps({
                        "comment": "CI 虚构回复：验证仍未结束。",
                        "quote": json.dumps({"_id": "a" * 24, "content": "CI 虚构历史引用：初稿已准备。"}, ensure_ascii=False),
                    }, ensure_ascii=False),
                }, {
                    "id": "ci-synthetic-position", "action": "move.cross.from.stage.to.stage",
                    "boundToObjectId": "ci-synthetic-task", "boundToObjectType": "task",
                    "createTime": timestamp,
                    "content": json.dumps({
                        "from": {"tasklist": {"_id": "1" * 24, "title": "CI 虚构分组甲"},
                                 "stage": {"_id": "3" * 24, "name": "CI 虚构阶段甲"}},
                        "to": {"tasklist": {"_id": "2" * 24, "title": "CI 虚构完成分组"},
                               "stage": {"_id": "4" * 24, "name": "CI 虚构阶段乙"}},
                    }, ensure_ascii=False),
                }, {
                    "id": "ci-synthetic-commit-notice", "action": "standard",
                    "boundToObjectId": "ci-synthetic-task", "boundToObjectType": "task",
                    "createTime": timestamp,
                    "content": json.dumps({"title": "关联了提交 「CI 虚构通知内容\n」",
                                           "customCreator": {"name": "ci-unverified-account"}}, ensure_ascii=False),
                }, {
                    "id": "ci-synthetic-mr-notice", "action": "standard",
                    "boundToObjectId": "ci-synthetic-task", "boundToObjectType": "task",
                    "createTime": timestamp,
                    "content": json.dumps({"title": "关联了合并请求 「CI 虚构通知内容」",
                                           "customCreator": {"name": "ci-unverified-account"}}, ensure_ascii=False),
                }],
            }],
        }


class SyntheticDingTalk:
    def send_markdown(self, *_args):
        return True


class SyntheticSummarizer:
    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        return {row["task_key"]: "CI 虚构摘要" for row in rows}


def make_smoke_service(directory: Path) -> WorkerService:
    """Never read an actual user profile, credentials, registry, or environment secrets."""
    directory = Path(directory)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings.update({
        "tenant_id": "demo-org", "project_id": "demo-project",
        "owner_names": {"demo-person": "虚构成员甲", "demo-person-2": "虚构成员乙"},
        "status_names": {
            "demo-open": "待处理", "demo-progress": "进行中",
            "demo-review": "待验收", "demo-done": "已解决",
        },
        "resolved_status_ids": ["demo-done"],
    })
    store = SettingsStore(directory / "settings.json")
    store.save(settings)
    secret_store = MemorySecretStore()
    secret_store.save({
        "teambition_app_id": "synthetic-app-id", "teambition_app_secret": "synthetic-app-secret",
        "openai_api_key": "synthetic-key-cannot-call-openai",
        "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=synthetic-only",
        "dingtalk_signing_secret": "synthetic-only",
    })
    catalog = OpenAIModelCatalog(
        directory / "openai_models.json",
        transport=lambda *_args: json.dumps({"data": [{"id": "gpt-6-luna"}, {"id": "ci-unknown-model"}]}).encode(),
    )

    def workflow(settings, credentials, data_dir, **kwargs):
        return DigestWorkflow(settings, credentials, data_dir,
                              client_factory=SyntheticClient,
                              summarizer=SyntheticSummarizer(),
                              ding_sender=SyntheticDingTalk(), **kwargs)

    return WorkerService(
        directory, settings_store=store, secret_store=secret_store,
        model_catalog=catalog, client_factory=SyntheticClient,
        workflow_factory=workflow,
        startup_configurer=lambda *_args, **_kwargs: None,
    )
