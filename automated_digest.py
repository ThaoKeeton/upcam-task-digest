"""Compose an unattended digest with deterministic checks and AI summaries."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path

from activity_adapter import (
    OBSERVED_ACTIONS, bounded_object, comment_input, executor_change, parse_activity,
    standard_commit_candidate, status_transition, stage_movement, standard_association_notification,
)
from digest_pipeline import build_digest, timestamp
from openai_summarizer import OpenAIResponsesClient, SummaryError
from render_digest import TEMPLATE, render_digest, text as digest_text
from runtime_mappings import validate_mappings
from teambition_readonly import CollectionError, identifier, record_id


MAX_SUMMARY_CHARS = 200
MAX_DIAGNOSTIC_CHARS = 12000
MAX_UNKNOWN_ACTION_GROUPS = 32
MAX_DIAGNOSTIC_TASK_SAMPLES = 3


class AutomatedDigestBlocked(RuntimeError):
    """Automated processing cannot safely produce a sendable report."""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_editorial_rules() -> list[str]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))["editorial_rules"]


def _summary_inputs(base_report: dict) -> tuple[list[dict], dict[str, str], dict[str, list[str]]]:
    by_task = {entry["task_id"]: entry for entry in base_report.get("summary_inputs", [])}
    digest_tasks = base_report["digest"]["tasks"]
    request_rows, row_to_task, substantive = [], {}, {}
    for index, task in enumerate(digest_tasks, 1):
        # Digest order and audit order are both stable, but IDs never go to the model.
        audit = base_report["audit"][index - 1]
        task_id = audit["task_id"]
        entry = by_task.get(task_id)
        if not entry:
            continue
        comments = [comment for comment in entry["comments"] if not comment.get("omit_from_summary")
                    and isinstance(comment.get("text"), str) and comment["text"]]
        texts = [comment["text"] for comment in comments]
        if not texts:
            continue
        task_key = f"task-{index}"
        row = {"task_key": task_key, "title": task["title"], "comment_bodies": texts}
        contexts, seen_contexts = [], set()
        for comment_index, comment in enumerate(comments):
            for context in comment.get("quote_contexts", {}).values():
                text = context["text"]
                if not text.strip() or (comment_index, text) in seen_contexts:
                    continue
                seen_contexts.add((comment_index, text))
                contexts.append({"comment_index": comment_index, "role": context["role"], "text": text})
        if contexts:
            row["quoted_contexts"] = contexts
        request_rows.append(row)
        row_to_task[task_key] = task_id
        substantive[task_id] = texts
    return request_rows, row_to_task, substantive


def _base_failure_issues(report: dict) -> list[str]:
    hard_task_ids = {item["task_id"] for item in report.get("audit", []) if item.get("hard_review")}
    failures = []
    for issue in report.get("issues", []):
        if issue.startswith("任务 ") and issue.endswith(" 待复核"):
            task_id = issue[len("任务 "):-len(" 待复核")]
            if task_id not in hard_task_ids:
                continue
        failures.append(issue)
    return failures


_GLOBAL_SOURCE_ISSUES = {
    "项目范围尚未核验": "项目范围尚未核验：打开“项目映射与范围”，完成只读验证并确认当前 Project ID、归档选项和筛选条件，然后保存。",
    "API 字段及事件规则尚未核验": "Teambition 字段尚未核验：在“项目映射与范围”选择含真实任务动态的时间窗，完成只读验证并保存。",
    "任务当前更新时间筛选口径尚未核验": "任务更新时间范围未核验：按任务当前 updated 字段选择采集范围，重新执行只读项目验证。",
}

_COLLECTION_ERROR_MESSAGES = {
    "Invalid identifier": "Tenant ID、Project ID、任务 ID 或事件 ID 格式无效；检查设置中的 ID，不要用姓名或员工编号代替 API ID。",
    "Unknown view configuration field": "本机项目视图配置包含未知字段；重新打开项目映射页，只保留界面支持的筛选条件。",
    "View verified must be boolean": "本机项目范围核验状态无效；重新执行只读项目验证并保存。",
    "View evidence must be a string": "本机项目范围验证记录无效；重新执行只读项目验证并保存。",
    "View equality values must be strings": "项目筛选条件值无效；核对字段 ID/值后重新保存。",
    "Cannot render incomplete collection": "Teambition 分页不完整；未调用 OpenAI、未发送钉钉。检查项目权限和查询范围后，重新采集并确认完整翻页。",
    "Pagination limit exceeded; incomplete collection": "Teambition 查询超过分页上限，结果不完整；缩小时间窗或检查项目数据量后再试。",
    "Invalid or repeated pagination token": "Teambition 分页标记无效或重复，结果不完整；稍后重新只读检查。",
    "Collection source differs from configuration": "返回的企业或 Project ID 与本机配置不一致；检查 Tenant ID、Project ID 后重新验证。",
    "Unsupported activity schema override": "任务动态字段配置不受当前仓库规则支持；恢复仓库默认 createTime/action 字段并重新验证。",
    "Selection contract differs from report window": "采集响应中的更新时间窗口与当前日报起止时间不一致；检查时区和窗口后重新只读查询。",
    "Task belongs to another project": "查询结果包含其他项目任务；检查 Project ID 或停止运行并核对只读接口范围。",
    "Task violates report selection contract": "任务当前更新时间不在所选半开时间窗内；核对本机起止时间和任务 updated 字段后重新查询。",
    "Returned candidate violates updated-time selection": "Teambition 返回任务的 updated 时间超出筛选窗口；来源校验失败，检查本机时区和采集起止时间。",
    "Task changed outside selection or has invalid updated timestamp": "采集中的任务 updated 字段变化或格式不合法；重新执行一次完整只读查询。",
    "Task changed during collection; retry a fresh bounded run": "任务在分页读取期间发生变化，当前结果不是稳定快照；重新执行一次只读查询。",
    "Missing archive scope": "任务缺少归档状态字段；确认 Teambition API 返回完整字段后再运行。",
    "Missing archive flag": "任务缺少归档状态字段；确认接口权限和字段返回后重新验证。",
    "Archive scope mismatch": "归档范围与本机设置不一致；检查“将已归档任务纳入采集范围”并重新验证。",
    "Missing configured view filter field": "有任务缺少已配置的项目筛选字段；核对筛选字段和值，或移除不适用的条件后重新验证。",
    "Unknown or unsupported view filter": "项目筛选条件不受支持；只使用界面列出的稳定字段并重新保存。",
    "Conflicting event identity": "同一动态 ID 返回了互相冲突的内容；来源证据不完整，检查接口查询结果后再试。",
    "Duplicate task in collection": "查询返回重复任务，无法确认完整范围；重新只读查询。",
    "Task timestamps/status cannot substitute for event window": "项目筛选条件不能使用 created/updated/isDone 代替动态时间；恢复真实 updated 窗口并移除这类条件。",
    "Conflicting duplicate; collection is not a snapshot": "同一任务在分页期间返回了冲突版本，无法确认一致范围；重新执行只读查询。",
    "Detail identity or project mismatch": "任务详情 ID 或 Project ID 与查询结果不一致；停止发送并核对项目权限和接口响应。",
    "Invalid page size": "本机分页参数无效；恢复仓库默认设置后重新只读诊断。",
    "Collection record budget exhausted": "Teambition 返回的任务与动态总记录超过 50,000 条的硬上限；为保护本机资源，结果未完成，不能发送。",
    "Task selection exceeds bounded collection budget": "符合条件的任务数超过“单次采集任务上限”。可在“自动运行计划”提高此值（最多 700），保存后重新只读诊断；任务详情/动态请求、总记录数、响应体大小和运行时长仍有硬上限，触及时会继续阻断。",
    "Collection request budget exhausted": "Teambition 分页和详情请求达到本机请求预算，结果不完整，不能发送。检查活动量；仍超限时需降低时间范围或项目范围。",
    "Collection response byte budget exhausted": "本次 Teambition 响应数据总量超过 128 MiB 的硬上限；结果不完整，不能发送。",
    "Unexpected HTTP status": "Teambition API 返回了未预期的 HTTP 状态；检查凭据、读取权限或稍后重试。",
    "Missing or conflicting record identity": "任务或动态缺少唯一 ID 或同一对象的 ID 字段互相冲突；核对 API 字段后再运行。",
    "Expected object record": "Teambition 返回了非对象任务/动态记录；接口字段结构不受当前规则支持。",
    "Missing or mismatched activity task binding": "任务动态绑定的 Task ID 与任务不一致；停止发送并核对 Teambition 项目和接口字段。",
    "Comment evidence exceeds review size budget": "评论动态超过本机安全读取上限；未调用 OpenAI、未发送钉钉。需要先核对该动态结构及仓库规则。",
    "HTTP request rejected": "Teambition 只读请求失败；检查网络、App 凭据及项目读取权限。没有发送日报。",
    "API rejected request or malformed envelope": "Teambition API 拒绝请求或返回结构不完整；检查凭据、权限和接口状态。没有发送日报。",
    "GET retry budget exhausted": "Teambition 只读请求多次失败；检查网络和项目权限后再试。",
    "Collection time budget exhausted": "Teambition 查询超过本机时间上限；缩小时间窗后重新只读检查。",
    "Response size limit exceeded": "Teambition 响应超过本机读取上限；缩小时间窗后重新验证。",
}


def collection_validation_message(error: Exception) -> str:
    """Translate known safe validation failures without echoing response data."""
    if isinstance(error, CollectionError):
        message = _COLLECTION_ERROR_MESSAGES.get(str(error))
        if message:
            return message
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "日报时间窗或任务字段格式不符合本机校验要求；检查本机时间、Project ID 与更新时间字段后重新只读诊断。"
    return "Teambition 来源数据未通过本机安全校验；未调用 OpenAI、未发送钉钉。重新只读诊断并核对项目、时间窗、分页、归档范围和字段。"


def _display_ids(values, limit=12):
    rows = sorted(values)
    shown = ", ".join(rows[:limit])
    if len(rows) > limit:
        shown += f"，另有 {len(rows) - limit} 项"
    return shown or "（ID 缺失或格式无效）"


def _safe_action(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        return value
    return "（格式无效，原值已隐藏）"


def _source_event_failure_message(report: dict, collection: dict, config: dict) -> str:
    """Explain blocking evidence using only IDs, allowlisted action names and fixed text."""
    reasons = []
    issues = _base_failure_issues(report)
    for issue in issues:
        if issue in _GLOBAL_SOURCE_ISSUES:
            reasons.append(_GLOBAL_SOURCE_ISSUES[issue])

    audits = {row.get("task_id"): row for row in report.get("audit", [])
              if isinstance(row, dict) and row.get("hard_review") is True}
    records = {}
    for row in collection.get("records", []):
        try:
            task_id = record_id(row.get("task", {}))
        except (CollectionError, AttributeError, TypeError):
            continue
        records[task_id] = row

    missing_owners, missing_statuses = set(), set()
    unknown_actions, undated_tasks = {}, set()
    malformed_comments, status_schema_errors = {}, {}
    status_name_mismatches = set()
    bad_executor_events, bad_commit_events, unrecognized_standard_events = set(), set(), set()
    status_conflicts, status_conflict_details = set(), {}
    executor_conflicts, no_window_events, position_conflicts, malformed_moves = set(), set(), set(), set()
    generic_tasks = set()
    owner_names = config.get("owner_names", {})
    status_names = config.get("status_names", {})

    for task_id, audit in audits.items():
        row = records.get(task_id)
        task = row.get("task", {}) if isinstance(row, dict) else {}
        if not isinstance(task, dict):
            generic_tasks.add(task_id)
            continue

        owner_id, status_id = task.get("executorId"), task.get("tfsId")
        try:
            owner_id = identifier(owner_id)
        except CollectionError:
            reasons.append(f"任务 {task_id} 缺少有效的当前执行人 ID；核对项目任务字段。")
        else:
            if owner_id not in owner_names:
                missing_owners.add(owner_id)
        try:
            status_id = identifier(status_id)
        except CollectionError:
            reasons.append(f"任务 {task_id} 缺少有效的当前状态 ID；核对项目任务字段。")
        else:
            if status_id not in status_names:
                missing_statuses.add(status_id)
        try:
            digest_text(task.get("content"))
        except (TypeError, ValueError):
            reasons.append(f"任务 {task_id} 标题缺失或格式无效；核对任务 content 字段。")

        evidence = audit.get("activity_evidence", [])
        if audit.get("undated_event_ids"):
            undated_tasks.add(task_id)
        if not evidence and not audit.get("undated_event_ids"):
            no_window_events.add(task_id)
        if audit.get("status_noop"):
            status_conflicts.add(task_id)
            status_conflict_details.setdefault(task_id, set()).add("事件中的旧状态 ID 与目标状态 ID 相同")
        if audit.get("executor_conflict"):
            executor_conflicts.add(task_id)
        if audit.get("position_conflict"):
            position_conflicts.add(task_id)

        transitions = audit.get("status_transitions", [])
        if transitions:
            chain_bad = False
            if any(previous.get("target_id") != current.get("previous_id")
                   for previous, current in zip(transitions, transitions[1:])):
                chain_bad = True
                status_conflict_details.setdefault(task_id, set()).add("相邻状态事件的旧状态与前一目标状态未衔接")
            last = transitions[-1]
            if last.get("target_id") != task.get("tfsId"):
                chain_bad = True
                status_conflict_details.setdefault(task_id, set()).add("最后一条状态事件目标 ID 与当前任务状态 ID 不一致")
            if type(task.get("isDone")) is not bool:
                chain_bad = True
                status_conflict_details.setdefault(task_id, set()).add("当前任务 isDone 缺失或不是布尔值")
            elif last.get("resolved") and task.get("isDone") is not True:
                chain_bad = True
                status_conflict_details.setdefault(task_id, set()).add(
                    "当前任务未完成，不能依据已配置的“已解决”状态映射确认解决")
            event_times = {}
            for activity in evidence:
                try:
                    event_times[record_id(activity, task_fallback=False)] = timestamp(activity.get("createTime"))
                except (CollectionError, ValueError, TypeError):
                    continue
            observed_times = [event_times.get(item.get("event_id")) for item in transitions]
            observed_times = [value for value in observed_times if value is not None]
            if len(observed_times) != len(set(observed_times)):
                chain_bad = True
                status_conflict_details.setdefault(task_id, set()).add("多条状态事件具有相同时间戳")
            if chain_bad:
                status_conflicts.add(task_id)

        for activity in evidence:
            if not isinstance(activity, dict):
                generic_tasks.add(task_id)
                continue
            action = activity.get("action")
            if not isinstance(action, str) or action not in OBSERVED_ACTIONS:
                unknown_actions.setdefault(_safe_action(action), set()).add(task_id)
                continue
            if action == "comment":
                try:
                    comment_id = record_id(activity, task_fallback=False)
                    parsed_comment = comment_input(activity, comment_id, None)
                except (CollectionError, TypeError, ValueError):
                    malformed_comments.setdefault(task_id, set()).add("评论对象超过本机安全解析上限或无法读取")
                else:
                    if parsed_comment.get("extraction_needs_review"):
                        malformed_comments.setdefault(task_id, set()).update(
                            parsed_comment.get("review_reasons", []))
            elif action == "update.taskflowstatus":
                payload = bounded_object(activity.get("content"))
                if payload is None:
                    status_schema_errors.setdefault(task_id, set()).add(
                        "content 不是有界、无重复键的 JSON 对象")
                    continue
                parsed_ids = {}
                for key, name_key in (("oldTfsId", "oldTaskflowstatus"), ("tfsId", "taskflowstatus")):
                    try:
                        status_id = identifier(payload.get(key))
                    except CollectionError:
                        status_schema_errors.setdefault(task_id, set()).add(f"{key} 缺失或格式无效")
                        continue
                    parsed_ids[key] = status_id
                    if status_id not in status_names:
                        missing_statuses.add(status_id)
                    name = payload.get(name_key)
                    if not isinstance(name, str) or not name or name != name.strip():
                        status_schema_errors.setdefault(task_id, set()).add(f"{name_key} 缺失或不是有效字符串")
                    elif status_id in status_names and status_names[status_id] != name:
                        status_name_mismatches.add(task_id)
                if type(payload.get("isDone")) is not bool:
                    status_schema_errors.setdefault(task_id, set()).add("事件 isDone 缺失或不是布尔值")
                if (not status_schema_errors.get(task_id) and not status_name_mismatches.intersection({task_id})
                        and all(item in status_names for item in parsed_ids.values())
                        and not status_transition(activity.get("content"), config)):
                    status_schema_errors.setdefault(task_id, set()).add(
                        "状态字段组合不符合当前已验证的事件契约")
            elif action == "update_executor":
                payload = bounded_object(activity.get("content"))
                if payload is None:
                    bad_executor_events.add(task_id)
                    continue
                ids = set()
                for key in ("_oldExecutorId", "_executorId"):
                    if payload.get(key) is None:
                        continue
                    try:
                        ids.add(identifier(payload.get(key)))
                    except CollectionError:
                        bad_executor_events.add(task_id)
                missing_owners.update(item for item in ids if item not in owner_names)
                change = executor_change(activity, config)
                if change is None or change.get("automation_conflict"):
                    bad_executor_events.add(task_id)
            elif action == "move.cross.from.stage.to.stage":
                if stage_movement(activity) is None:
                    malformed_moves.add(task_id)
            elif action == "standard":
                if standard_association_notification(activity) is not None:
                    continue  # An observed display notice never proves code provenance.
                candidate = standard_commit_candidate(activity)
                if candidate is None:
                    unrecognized_standard_events.add(task_id)
                else:
                    # A title pattern alone does not prove the linked commit/source.
                    bad_commit_events.add(task_id)

        for change in audit.get("parsed_changes", []):
            if (change.get("action") == "standard"
                    and change.get("pattern") != "association-display-notice"):
                bad_commit_events.add(task_id)
            elif (change.get("action") == "update_executor" and change.get("needs_review")
                  and change.get("automation_conflict")):
                bad_executor_events.add(task_id)

    if missing_owners:
        reasons.append("缺少成员姓名映射：" + _display_ids(missing_owners) +
                       "。在“项目映射与范围”导入 API 成员 ID→姓名 JSON 或 ID<TAB>姓名映射，保存后重新只读诊断；员工编号不能代替 API ID。")
    if missing_statuses:
        reasons.append("缺少状态名称映射：" + _display_ids(missing_statuses) +
                       "。核对状态名称，在“状态映射与‘已解决’标记”补齐并保存，再重新诊断。")
    if undated_tasks:
        reasons.append("以下任务的动态 createTime 缺失或不是带时区的 ISO 8601 时间：" +
                       _display_ids(undated_tasks) + "。核对 API 时间字段和原始动态；不要猜测其属于哪个日报窗口。")
    if no_window_events:
        reasons.append("以下任务当前 updated 时间在筛选窗口内，但窗口内没有可验证动态：" +
                       _display_ids(no_window_events) + "。核对本机起止时间、任务 updated 和动态 createTime；若接口证据不一致请停止发送。")
    if malformed_comments:
        for task_id in sorted(malformed_comments)[:12]:
            detail = "；".join(sorted(malformed_comments[task_id]))
            reasons.append(f"评论动态结构未通过本地校验：任务 {task_id}：{detail}。评论正文不会写入诊断。")
        if len(malformed_comments) > 12:
            reasons.append(f"另有 {len(malformed_comments) - 12} 个任务含评论结构问题。")
    if status_schema_errors:
        for task_id in sorted(status_schema_errors)[:12]:
            detail = "；".join(sorted(status_schema_errors[task_id]))
            reasons.append(f"状态动态字段结构未通过校验：任务 {task_id}：{detail}。请核对动态字段形状；不要手动跳过校验。")
        if len(status_schema_errors) > 12:
            reasons.append(f"另有 {len(status_schema_errors) - 12} 个任务含状态字段结构问题。")
    if status_name_mismatches:
        reasons.append("状态事件中的名称与已保存映射不一致：" + _display_ids(status_name_mismatches) +
                       "。请在“成员与状态映射”页核对动态返回的旧/新状态名称与对应 ID；诊断不会输出状态名称值。")
    if malformed_moves:
        reasons.append("位置变更动态不符合已核验的 from/to 分组与阶段结构：" + _display_ids(malformed_moves) +
                       "。请核对字段、ID 和名称类型；不按目标分组名称判断完成。")
    if position_conflicts:
        reasons.append("位置变更事件序列与当前任务分组/阶段不一致或存在同刻冲突：" +
                       _display_ids(position_conflicts) + "。请重新只读采集并核对任务位置历史。")
    if bad_commit_events:
        reasons.append("关联提交动态仅有标题模式，无法证明提交来源：" + _display_ids(bad_commit_events) +
                       "。在仓库规则补充可验证的提交证据前，该动态会继续阻断发送。")
    if unrecognized_standard_events:
        reasons.append("standard 动态尚未符合已核验结构或标题规则：" + _display_ids(unrecognized_standard_events) +
                       "。不能认定该动态是关联提交；核对动态结构与标题规则后再诊断。")
    if bad_executor_events:
        reasons.append("执行人变更动态结构或来源标记无法核实：" + _display_ids(bad_executor_events) +
                       "。核对动态字段及成员映射后重新只读诊断。")
    if status_conflicts:
        for task_id in sorted(status_conflicts)[:12]:
            details = "；".join(sorted(status_conflict_details.get(task_id, {"状态事件序列存在冲突"})))
            reasons.append(f"状态事件序列与任务当前状态不一致：任务 {task_id}：{details}。核对状态历史和“已解决”映射；不要手动跳过校验。")
        if len(status_conflicts) > 12:
            reasons.append(f"另有 {len(status_conflicts) - 12} 个任务存在状态事件序列冲突。")
    if executor_conflicts:
        reasons.append("执行人事件序列与任务当前执行人不一致或存在同刻冲突：" +
                       _display_ids(executor_conflicts) + "。核对执行人历史及 ID→姓名映射后重新诊断。")
    if generic_tasks:
        reasons.append("任务证据存在其他本机硬性校验问题：" + _display_ids(generic_tasks) +
                       "。检查任务字段、动态绑定和仓库已支持的事件格式。")

    for issue in issues:
        if issue not in _GLOBAL_SOURCE_ISSUES and not issue.startswith("任务 "):
            reasons.append("本机范围或来源校验未通过；重新验证 Project ID、更新时间窗口和归档选项。")
    # Put other hard failures first so repeated unsupported events cannot hide them.
    # Group only by the existing safe display value, never by raw action/content.
    if unknown_actions:
        affected_tasks = set().union(*unknown_actions.values())
        reasons.append(f"共 {len(unknown_actions)} 类未识别动态类型/类别，影响 {len(affected_tasks)} 个任务；"
                       f"每类最多显示 {MAX_DIAGNOSTIC_TASK_SAMPLES} 个任务 ID 样例。")
        reasons.append("先复制以下类型行，或导出安全结构，再核对仓库规则与 API 事件字段；"
                       "不要跳过动态或根据 content 形状猜测含义，这些动态仍会阻断发送。")
        for action, task_ids in sorted(unknown_actions.items())[:MAX_UNKNOWN_ACTION_GROUPS]:
            examples = ", ".join(sorted(task_ids)[:MAX_DIAGNOSTIC_TASK_SAMPLES])
            reasons.append(f"未识别动态类型 {action}：影响 {len(task_ids)} 个任务；任务 ID 样例：{examples}。")
        if len(unknown_actions) > MAX_UNKNOWN_ACTION_GROUPS:
            remaining = len(unknown_actions) - MAX_UNKNOWN_ACTION_GROUPS
            reasons.append(f"另有 {remaining} 类未识别动态类型/类别未显示"
                           f"（本机最多显示 {MAX_UNKNOWN_ACTION_GROUPS} 类）；请导出安全结构核对全部类别。")
    if not reasons:
        reasons.append("存在未分类的硬性来源/事件证据问题；请在“运行记录”查看本机安全诊断并停止发送。")
    # De-duplicate without changing display order or including raw task/comment data.
    details = list(dict.fromkeys(reasons))
    header = "自动校验未通过：未调用 OpenAI，未发送钉钉。\n"
    footer = "\n处理后再次点“只读诊断当前数据”；诊断通过后再运行并自动发送。"
    message = header + "\n".join(details) + footer
    if len(message) <= MAX_DIAGNOSTIC_CHARS:
        return message
    notice = (f"\n诊断显示已截断，部分类型或原因行未显示（本机显示上限 {MAX_DIAGNOSTIC_CHARS} 字）；"
              "请先复制已显示的类型行，并导出安全结构核对全部类型和硬性失败原因。")
    remaining = MAX_DIAGNOSTIC_CHARS - len(header) - len(footer) - len(notice)
    shown = []
    for detail in details:
        size = len(detail) + bool(shown)
        if size <= remaining:
            shown.append(detail)
            remaining -= size
    return header + "\n".join(shown) + notice + footer


def _validated_source_report(collection: dict, config: dict, day, window=None) -> dict:
    clean_config = validate_mappings(config)
    base = build_digest(collection, clean_config, day, window=window)
    if _base_failure_issues(base):
        raise AutomatedDigestBlocked(_source_event_failure_message(base, collection, clean_config))
    return base


def diagnose_automatic_source(collection: dict, config: dict, day, window=None) -> dict:
    """Run deterministic source/event checks locally; this never calls a model or sender."""
    try:
        base = _validated_source_report(collection, config, day, window)
    except AutomatedDigestBlocked as exc:
        return {"ok": False, "message": str(exc), "task_count": None}
    except (CollectionError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "message": collection_validation_message(exc), "task_count": None}
    return {"ok": True,
            "message": (f"只读校验通过：完整采集 {len(collection.get('records', []))} 个任务；"
                        "本地字段、更新时间范围、成员/状态映射及动态规则通过。未调用 OpenAI，未发送钉钉。"),
            "task_count": len(base.get("digest", {}).get("tasks", []))}


def _apply_summaries(base_report: dict, summary_by_task: dict[str, str],
                     substantive: dict[str, list[str]]) -> dict:
    result = deepcopy(base_report)
    hard_task_ids = {item["task_id"] for item in result.get("audit", []) if item.get("hard_review")}
    result["issues"] = _base_failure_issues(result)
    audit_by_task = {entry["task_id"]: entry for entry in result.get("audit", [])}
    summary_by_input = {entry["task_id"]: entry for entry in result.get("summary_inputs", [])}
    for task, audit in zip(result["digest"]["tasks"], result["audit"]):
        task_id = audit["task_id"]
        if task_id in hard_task_ids:
            continue
        proposed = summary_by_task.get(task_id, "")
        if task_id in substantive and not proposed:
            raise AutomatedDigestBlocked("OpenAI omitted a nonempty comment summary; this digest was not sent")
        if (len(proposed) > MAX_SUMMARY_CHARS or any(ord(char) < 32 for char in proposed)
                or any(char in "\u2028\u2029" for char in proposed)
                or proposed != proposed.strip()):
            raise AutomatedDigestBlocked("Summary text failed local validation; this digest was not sent")
        pieces = [piece for piece in task.get("summary", "").split("；")
                  if piece != "评论内容语义归纳待复核" and not piece.startswith("原文摘录：")]
        if proposed:
            pieces.append(proposed)
        task["summary"] = "；".join(pieces)
        audit["needs_review"] = False
        if task_id in summary_by_input:
            summary_by_input[task_id]["semantic_summary"] = proposed
            summary_by_input[task_id]["needs_review"] = False
    result["markdown"] = render_digest(result["digest"])
    result["ready_to_send"] = not result["issues"]
    return result


def requires_openai_summary(collection: dict, config: dict, day, window=None) -> bool:
    """Preflight locally and report whether substantive comment text needs AI."""
    clean_config = validate_mappings(config)
    base = _validated_source_report(collection, clean_config, day, window)
    request_rows, _, _ = _summary_inputs(base)
    return bool(request_rows)


def build_automated_digest(collection: dict, config: dict, day, api_key: str, model: str, effort: str,
                           *, client: OpenAIResponsesClient | None = None, window=None,
                           generated_at: datetime | None = None,
                           service_tier: str = "default") -> dict:
    """Summarize the minimal local evidence, then locally validate before delivery."""
    clean_config = validate_mappings(config)
    # Do not send task text to OpenAI when deterministic source, mapping, event,
    # or query validation has already produced a blocking issue.
    base = _validated_source_report(collection, clean_config, day, window)
    request_rows, row_to_task, substantive = _summary_inputs(base)
    summary_by_task = {}
    if request_rows:
        try:
            generated = (client or OpenAIResponsesClient()).summarize(
                api_key, model, effort, request_rows, load_editorial_rules(),
                service_tier=service_tier)
        except SummaryError as exc:
            raise AutomatedDigestBlocked(str(exc)) from None
        summary_by_task = {row_to_task[key]: value for key, value in generated.items()}
    # Keep an explicit empty result for acknowledgement-only comment groups.
    for entry in base.get("summary_inputs", []):
        summary_by_task.setdefault(entry["task_id"], "")
    result = _apply_summaries(base, summary_by_task, substantive)
    result["report_type"] = "automated-v1"
    result["automation"] = {
        "source_sha256": fingerprint(collection),
        "config_sha256": fingerprint(clean_config),
        "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
        "summaries_sha256": fingerprint(summary_by_task),
        "model": model,
        "reasoning_effort": effort,
        "service_tier": service_tier,
        "generated_at_utc": (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "summary_count": len(summary_by_task),
    }
    result["automation"]["report_sha256"] = fingerprint(result)
    return result


def validate_automated_report(report: dict, collection: dict, config: dict,
                              current_model: str, current_effort: str,
                              current_service_tier: str = "default") -> None:
    """Verify report provenance, content, current rules and local data before delivery."""
    if not isinstance(report, dict) or report.get("report_type") != "automated-v1":
        raise AutomatedDigestBlocked("Only an automated report with current provenance can be sent")
    if report.get("ready_to_send") is not True or report.get("issues"):
        raise AutomatedDigestBlocked("Automated validation found unresolved issues")
    automation = report.get("automation")
    if (not isinstance(automation, dict) or automation.get("model") != current_model
            or automation.get("reasoning_effort") != current_effort
            or automation.get("service_tier") != current_service_tier):
        raise AutomatedDigestBlocked("Model settings changed after summary generation")
    if (automation.get("source_sha256") != fingerprint(collection)
            or automation.get("config_sha256") != fingerprint(validate_mappings(config))
            or automation.get("template_sha256") != hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()):
        raise AutomatedDigestBlocked("Source, settings or editorial rules changed after generation")
    copy = deepcopy(report)
    attested = copy["automation"].pop("report_sha256", None)
    if not isinstance(attested, str) or fingerprint(copy) != attested:
        raise AutomatedDigestBlocked("Automated report changed after generation")
    # Recompute deterministic data and bind the rendered output to the model summary.
    base = build_digest(collection, validate_mappings(config), report["digest"]["date"],
                        window=(report["scope"]["start"], report["scope"]["end_exclusive"]))
    request_rows, row_to_task, substantive = _summary_inputs(base)
    summaries = report.get("summary_inputs")
    if not isinstance(summaries, list):
        raise AutomatedDigestBlocked("Summary provenance is incomplete")
    by_task = {}
    for entry in summaries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("task_id"), str)
                or entry["task_id"] in by_task or not isinstance(entry.get("semantic_summary"), str)):
            raise AutomatedDigestBlocked("Summary provenance is incomplete")
        by_task[entry["task_id"]] = entry["semantic_summary"]
    comment_task_ids = {entry["task_id"] for entry in base.get("summary_inputs", [])}
    if (set(by_task) != comment_task_ids
            or fingerprint(by_task) != automation.get("summaries_sha256")):
        raise AutomatedDigestBlocked("Summary content does not match the local task evidence")
    if not request_rows and any(by_task.values()):
        raise AutomatedDigestBlocked("Unexpected summary text for acknowledgement-only comments")
    expected = _apply_summaries(base, by_task, substantive)
    if not expected["ready_to_send"] or expected["markdown"] != report.get("markdown"):
        raise AutomatedDigestBlocked("Rendered report differs from locally validated evidence")

