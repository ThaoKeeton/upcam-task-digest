"""Read-only project probe analysis used by the desktop setup screen."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata
from datetime import datetime

from report_contract import updated_in_window
from teambition_readonly import CollectionError, identifier, record_id, validate_binding


class ProjectSetupError(ValueError):
    """A project setup probe did not establish enough reliable evidence."""


@dataclass(frozen=True)
class ProjectProbe:
    tenant_id: str
    project_id: str
    selection: dict
    include_archived: bool
    task_count: int
    activity_count: int
    owner_ids: tuple[str, ...]
    status_ids: tuple[str, ...]
    status_name_suggestions: dict[str, str]
    conflicting_status_ids: tuple[str, ...]
    observed_actions: tuple[str, ...]
    api_contract_verified: bool = True
    view_verified: bool = False


def parse_owner_mapping_tsv(source) -> dict[str, str]:
    """Parse either a JSON object or ID<TAB>name rows without partial writes."""
    if not isinstance(source, str) or not source.strip():
        raise ProjectSetupError("批量成员映射内容为空或超出大小限制。")
    try:
        if len(source.encode("utf-8")) > 1024 * 1024:
            raise ProjectSetupError("批量成员映射内容为空或超出大小限制。")
    except UnicodeError:
        raise ProjectSetupError("批量成员映射内容编码无效。") from None
    normalized = source.lstrip("\ufeff \t\r\n")
    entries = None
    if normalized.startswith("{"):
        try:
            decoded = json.loads(normalized, object_pairs_hook=_pairs_without_duplicates)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ProjectSetupError("JSON 格式无效或成员 ID 重复；请粘贴“成员 ID”对应“姓名”的 JSON 对象。") from None
        if not isinstance(decoded, dict):
            raise ProjectSetupError("JSON 必须是“成员 ID”对应“姓名”的对象。")
        entries = decoded.items()
    else:
        tsv_entries = []
        for line in source.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                raise ProjectSetupError("每行必须包含且只包含一个制表符，格式为成员 ID<TAB>姓名。")
            tsv_entries.append((parts[0], parts[1]))
        entries = tsv_entries

    mapping = {}
    try:
        for member_id, name in entries:
            identifier(member_id)
            if (member_id in mapping or not isinstance(name, str) or not name.strip()
                    or name != name.strip()
                    or any(unicodedata.category(char) in ("Cc", "Cf") for char in name)):
                raise ProjectSetupError("成员 ID 重复或姓名无效。")
            mapping[member_id] = name
    except CollectionError:
        raise ProjectSetupError("成员 ID 无效。") from None
    if not mapping:
        raise ProjectSetupError("没有可导入的成员映射。")
    return mapping


def merge_owner_mappings(existing, incoming) -> dict[str, str]:
    """Merge explicit UI mappings only when an existing ID keeps its name."""
    result = dict(existing)
    for member_id, name in incoming.items():
        if member_id in result and result[member_id] != name:
            raise ProjectSetupError("已有成员 ID 对应其他姓名；未导入任何映射。")
        result[member_id] = name
    return result


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _status_transition(activity):
    if activity.get("action") != "update.taskflowstatus":
        return None
    content = activity.get("content")
    if isinstance(content, str):
        if len(content.encode("utf-8")) > 65536:
            return None
        try:
            content = json.loads(content, object_pairs_hook=_pairs_without_duplicates)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return None
    if not isinstance(content, dict):
        return None
    ids = (content.get("oldTfsId"), content.get("tfsId"))
    names = (content.get("oldTaskflowstatus"), content.get("taskflowstatus"))
    try:
        old_id, current_id = (identifier(item) for item in ids)
    except CollectionError:
        return None
    if any(not isinstance(name, str) or not name or name != name.strip()
           or any(unicodedata.category(char) in ("Cc", "Cf") for char in name)
           for name in names):
        return None
    return ((old_id, names[0]), (current_id, names[1]))


def _executor_ids(activity):
    if activity.get("action") != "update_executor":
        return ()
    content = activity.get("content")
    if isinstance(content, str):
        if len(content.encode("utf-8")) > 65536:
            return ()
        try:
            content = json.loads(content, object_pairs_hook=_pairs_without_duplicates)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return ()
    if not isinstance(content, dict):
        return ()
    result = []
    for key in ("_oldExecutorId", "_executorId"):
        value = content.get(key)
        if value is None:
            continue
        try:
            result.append(identifier(value))
        except CollectionError:
            continue
    return tuple(result)


def _valid_timestamp(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def analyze_project_probe(collection, *, tenant_id, project_id, expected_selection,
                          include_archived=False) -> ProjectProbe:
    """Verify a complete real-shaped read-only response without inferring scope.

    Names inferred from status-change event fields are suggestions only. Owners
    are intentionally returned as IDs because the approved task APIs do not
    establish an ID-to-person-name mapping.
    """
    try:
        tenant = identifier(tenant_id)
        project = identifier(project_id)
    except CollectionError:
        raise ProjectSetupError("请先填写有效的 Tenant ID 和 Project ID。") from None
    if not isinstance(collection, dict) or collection.get("pagination_complete") is not True:
        raise ProjectSetupError("只读查询没有完整翻页，不能核验项目字段或映射范围。请稍后重试。")
    if (collection.get("tenant_id") != tenant or collection.get("project_id") != project
            or collection.get("include_archived") is not include_archived
            or collection.get("selection") != expected_selection):
        raise ProjectSetupError("查询结果与当前租户、项目或时间范围不一致；未保存核验状态。")
    records = collection.get("records")
    if not isinstance(records, list):
        raise ProjectSetupError("只读查询结果结构不完整，未能核验字段契约。")
    if not records:
        raise ProjectSetupError("所选区间没有任务样本，无法核验任务字段。请在“自选时间 / 运行”中选择包含任务动态的区间后重试。")

    owners, statuses, seen_tasks = set(), set(), set()
    suggestions, conflicts, actions = {}, set(), set()
    activities_seen = 0
    try:
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("task"), dict):
                raise ProjectSetupError("只读查询包含无法识别的任务记录，未保存核验状态。")
            task = record["task"]
            task_id = record_id(task)
            if task_id in seen_tasks:
                raise ProjectSetupError("只读查询包含重复任务，未保存核验状态。")
            seen_tasks.add(task_id)
            if task.get("projectId") != project or type(task.get("isArchived")) is not bool:
                raise ProjectSetupError("任务身份或归档字段无法核验，未保存核验状态。")
            try:
                updated_in_window(task.get("updated"), expected_selection["date"],
                                  expected_selection["start"], expected_selection["end_exclusive"])
            except (KeyError, TypeError, ValueError):
                raise ProjectSetupError("任务更新时间字段与所选时间窗不符，未保存核验状态。") from None
            if task["isArchived"] and not include_archived:
                raise ProjectSetupError("只读查询返回了超出当前归档范围的任务，未保存核验状态。")
            for key, target in (("executorId", owners), ("tfsId", statuses)):
                value = task.get(key)
                if value is None or value == "":
                    continue
                target.add(identifier(value))
            activities = record.get("activities")
            if not isinstance(activities, list):
                raise ProjectSetupError("任务动态结构无法核验，未保存核验状态。")
            for activity in activities:
                if not isinstance(activity, dict):
                    raise ProjectSetupError("任务动态结构无法核验，未保存核验状态。")
                event_id = record_id(activity, task_fallback=False)
                validate_binding(activity, task_id)
                action, created = activity.get("action"), activity.get("createTime")
                if not isinstance(action, str) or not action or not _valid_timestamp(created):
                    raise ProjectSetupError("动态的 action/createTime 字段无法核验。请更换含完整动态的区间后重试。")
                actions.add(action)
                activities_seen += 1
                transition = _status_transition(activity)
                if transition is not None:
                    statuses.update(status_id for status_id, _name in transition)
                    for status_id, name in transition:
                        if status_id in suggestions and suggestions[status_id] != name:
                            conflicts.add(status_id)
                        else:
                            suggestions[status_id] = name
                owners.update(_executor_ids(activity))
    except CollectionError:
        raise ProjectSetupError("返回的成员或状态 ID 无法安全识别；未保存核验状态。") from None
    if activities_seen == 0:
        raise ProjectSetupError("查询区间内没有动态样本，无法核验动态字段。请选取有任务动态的时间区间后重试。")
    for status_id in conflicts:
        suggestions.pop(status_id, None)
    return ProjectProbe(
        tenant_id=tenant, project_id=project, selection=dict(expected_selection),
        include_archived=include_archived, task_count=len(records),
        activity_count=activities_seen, owner_ids=tuple(sorted(owners)),
        status_ids=tuple(sorted(statuses)), status_name_suggestions=dict(sorted(suggestions.items())),
        conflicting_status_ids=tuple(sorted(conflicts)), observed_actions=tuple(sorted(actions)),
    )

