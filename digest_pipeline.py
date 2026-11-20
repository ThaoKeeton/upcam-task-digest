"""Deterministic event-only daily digest. Unknown evidence blocks delivery."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

from render_digest import render_digest, text
from report_contract import (SHANGHAI, canonical_window, report_window, strict_day,
                             validate_selection, task_updated_timestamp)
from teambition_readonly import Client, CollectionError, identifier, record_id
from activity_adapter import (comment_input, parse_activity, validate_binding, status_transition,
                              executor_change, standard_commit_candidate, stage_movement,
                              standard_association_notification)
from attachment_activities import attachment_change
from runtime_mappings import load_runtime_mappings, parse_config_json, validate_mappings
from status_resolution import resolved_in_window


ACTIVITY_SELECTION_MODE = 'v3-project-activities-half-open'
CHAIN_ACTIONS = frozenset({'update.taskflowstatus', 'update_executor', 'move.cross.from.stage.to.stage'})


def day_window(day=None, now=None, window=None):
    if day is None:
        if window is not None:
            raise ValueError("A custom window requires an explicit report date")
        now = now or datetime.now(SHANGHAI)
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone-aware")
        day = (now.astimezone(SHANGHAI) - timedelta(days=1)).date()
    elif isinstance(day, str):
        day = strict_day(day)
    elif type(day) is not date:
        raise ValueError("Report day must be a date")
    return report_window(day.isoformat(), *(window or (None, None)))


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be ISO 8601")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp lacks explicit timezone")
    return parsed.astimezone(SHANGHAI)


def field(obj, path):
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def safe_label(value):
    try:
        text(value)
        return value
    except (ValueError, TypeError):
        return "待复核"


def identity_label(task, field_name, names, label, *, allow_unresolved):
    """Display a missing source label honestly without inventing a mapping."""
    object_id = task.get(field_name)
    if isinstance(object_id, str) and object_id in names:
        return safe_label(names[object_id])
    if not allow_unresolved:
        return "待复核"
    if field_name == 'executorId' and field_name in task and object_id is None:
        return '未分配'
    try:
        value = identifier(object_id)
        if len(value) <= 128:
            return f'未解析{label}（ID：{value}）'
    except CollectionError:
        pass
    return f'未解析{label}'


def validate_view(config):
    view = config.get("view", {})
    if not isinstance(view, dict) or set(view) - {"verified", "evidence", "task_equals"}:
        raise CollectionError("Unknown view configuration field")
    if "verified" in view and type(view["verified"]) is not bool:
        raise CollectionError("View verified must be boolean")
    if "evidence" in view and not isinstance(view["evidence"], str):
        raise CollectionError("View evidence must be a string")
    filters = view.get("task_equals", {})
    supported = {"executorId", "tasklistId", "stageId", "tfsId", "scenariofieldconfigId", "visible", "parentTaskId"}
    if not isinstance(filters, dict) or set(filters) - supported:
        raise CollectionError("Unknown or unsupported view filter")
    if any(not isinstance(value, str) for value in filters.values()):
        raise CollectionError("View equality values must be strings")
    return view


def build_digest(collection, config, day, window=None):
    # Caller freezes the report date before collection. A custom range is half-open.
    if day is None:
        raise ValueError("Report day must be fixed before collection")
    config = validate_mappings(config)
    report_day = strict_day(day).isoformat() if isinstance(day, str) else day.isoformat()
    selection = collection.get('selection')
    if window is None and isinstance(selection, dict) and selection.get('mode') == ACTIVITY_SELECTION_MODE:
        window = (selection.get('start'), selection.get('end_exclusive'))
    start, end = day_window(report_day, window=window)
    view = validate_view(config)
    if collection.get("pagination_complete") is not True:
        raise CollectionError("Cannot render incomplete collection")
    project = identifier(collection["project_id"])
    tenant = identifier(collection.get("tenant_id"))
    if tenant != identifier(config.get("tenant_id")) or project != identifier(config.get("project_id")):
        raise CollectionError("Collection source differs from configuration")
    schema = config.get("activity_fields", {"time": "createTime", "action": "action"})
    if schema != {"time": "createTime", "action": "action"} or config.get("action_rules"):
        raise CollectionError("Unsupported activity schema override")
    issues = []
    if view.get("verified") is not True or not view.get("evidence"):
        issues.append("项目范围尚未核验")
    if config.get("api_contract_verified") is not True:
        issues.append("API 字段及事件规则尚未核验")
    activity_selection = isinstance(selection, dict) and selection.get('mode') == ACTIVITY_SELECTION_MODE
    if activity_selection:
        try:
            validate_selection(selection, report_day, start, end)
        except (TypeError, ValueError):
            raise CollectionError('Selection contract differs from report window') from None
    elif isinstance(selection, dict) and selection.get('mode') == 'v3-project-updated-half-open':
        raise CollectionError('Selection contract differs from report window')
    else:
        issues.append("项目动态时间窗采集口径尚未核验")
    if collection.get("include_archived") != config.get("include_archived", False):
        raise CollectionError("Archive scope mismatch")
    if 'collected_at' in collection:
        try:
            collected_at = timestamp(collection['collected_at'])
        except (TypeError, ValueError):
            raise CollectionError('Invalid collection timestamp') from None
        # Check the entire observed source before any view or time filtering.
        # A future event cannot serve as historical reconciliation evidence.
        for record in collection['records']:
            for raw in record['activities']:
                if not isinstance(raw, dict):
                    continue
                try:
                    observed_at = timestamp(raw.get('createTime'))
                except (TypeError, ValueError):
                    continue  # The ordinary evidence checks handle unknown times.
                if observed_at > collected_at:
                    raise CollectionError('Activity timestamp exceeds collection time')
    items, audit, summary_inputs, seen = [], [], [], set()
    for record in collection["records"]:
        task = record["task"]
        task_id = record_id(task)
        if task_id in seen:
            raise CollectionError("Duplicate task in collection")
        seen.add(task_id)
        if task.get("projectId") != project:
            raise CollectionError("Task belongs to another project")
        task_updated = None
        if activity_selection:
            try:
                task_updated = task_updated_timestamp(task.get('updated'))
            except (TypeError, ValueError):
                raise CollectionError('Task violates report selection contract') from None
        if type(task.get("isArchived")) is not bool:
            raise CollectionError("Missing archive scope")
        if task["isArchived"] and not config.get("include_archived", False):
            continue
        # Explicit stable view filters only; no inferred created/updated predicate.
        filters = view.get("task_equals", {})
        if any(key in ("created", "updated", "isDone") for key in filters):
            raise CollectionError("Task timestamps/status cannot substitute for event window")
        if any(field(task, key) is None for key in filters):
            raise CollectionError("Missing configured view filter field")
        if any(field(task, key) != value for key, value in filters.items()):
            continue
        events, unknown_time, ids, comments, undated_ids = [], False, {}, [], []
        post_events = []
        for raw in record["activities"]:
            event_id = record_id(raw, task_fallback=False)
            if event_id in ids:
                if ids[event_id] != raw:
                    raise CollectionError("Conflicting event identity")
                continue
            ids[event_id] = raw
            validate_binding(raw, task_id)
            try:
                when = timestamp(raw.get("createTime"))
            except (ValueError, TypeError):
                unknown_time = True
                undated_ids.append(event_id)
                if raw.get('action') == 'comment':
                    comments.append(comment_input(raw, event_id, None))
                continue
            if when >= end:
                action = raw.get('action')
                if isinstance(action, str) and action in CHAIN_ACTIONS:
                    post_events.append((when, event_id))
                continue
            if when < start:
                continue
            kind, resolved = parse_activity(raw, config)
            if kind == "comment":
                comments.append(comment_input(raw, event_id, when))
            events.append((when, event_id, kind, resolved))
        no_window_evidence = (not events and not unknown_time and activity_selection
                              and start <= task_updated < end)
        if not events and not unknown_time and not no_window_evidence:
            continue
        events.sort(key=lambda e: (e[0], e[1]))
        counts = Counter(e[2] for e in events)
        review = unknown_time or bool(counts["unknown"]) or bool(comments) or no_window_evidence
        # Classification uses the window; later evidence only reconciles it with
        # the current task. Unrelated post-window streams are not interpreted.
        status_events = [e for e in events if e[2] == "status"]
        transitions = [dict(status_transition(ids[e[1]].get('content'), config), event_id=e[1]) for e in status_events]
        parsed_changes = []
        for event in events:
            raw = ids[event[1]]
            change = None
            if raw.get('action') == 'update_executor':
                change = executor_change(raw, config)
            elif raw.get('action') == 'standard':
                change = standard_association_notification(raw) or standard_commit_candidate(raw)
            elif raw.get('action') == 'move.cross.from.stage.to.stage':
                change = stage_movement(raw)
            elif raw.get('action') in ('upload.customfield.work', 'customfield.create.v2'):
                change = attachment_change(raw, config)
            if change is not None:
                parsed_changes.append(dict(change, event_id=event[1], action=raw['action']))
        executor_changes = [c for c in parsed_changes if c['action'] == 'update_executor']
        position_changes = [c for c in parsed_changes if c['action'] == 'move.cross.from.stage.to.stage']
        event_times = {event[1]: event[0] for event in events}
        active_streams = set()
        for event in events:
            action = ids[event[1]].get('action')
            if isinstance(action, str) and action in CHAIN_ACTIONS:
                active_streams.add(action)
        post_events.sort()
        post_window_context = [ids[event_id] for _, event_id in post_events
                               if ids[event_id]['action'] in active_streams]
        event_times.update({event_id: when for when, event_id in post_events})
        status_validation_transitions = list(transitions)
        executor_validation_changes = list(executor_changes)
        position_validation_changes = list(position_changes)
        bad_status_context = bad_executor_context = bad_position_context = False
        for raw in post_window_context:
            action = raw['action']
            if action == 'update.taskflowstatus':
                change = status_transition(raw.get('content'), config)
                if change is None:
                    bad_status_context = True
                else:
                    status_validation_transitions.append(dict(change, event_id=record_id(raw, task_fallback=False)))
            elif action == 'update_executor':
                change = executor_change(raw, config)
                if change is None or change.get('automation_conflict'):
                    bad_executor_context = True
                else:
                    # Intermediate contextual names are never displayed. Their
                    # validated IDs, rather than a name lookup, link this chain.
                    executor_validation_changes.append(dict(change, event_id=record_id(raw, task_fallback=False)))
            elif action == 'move.cross.from.stage.to.stage':
                change = stage_movement(raw)
                if change is None:
                    bad_position_context = True
                else:
                    position_validation_changes.append(dict(change, event_id=record_id(raw, task_fallback=False)))

        def same_time(changes):
            return len({event_times[c['event_id']] for c in changes}) != len(changes)

        ambiguous_status = same_time(status_validation_transitions)
        status_noop = any(change['previous_id'] == change['target_id'] for change in status_validation_transitions)
        status_conflict = bad_status_context or any(previous['target_id'] != current['previous_id']
                              for previous, current in zip(status_validation_transitions, status_validation_transitions[1:]))
        if transitions:
            latest = status_validation_transitions[-1]
            status_conflict = status_conflict or task.get('tfsId') != latest['target_id']
            status_conflict = status_conflict or type(task.get('isDone')) is not bool
            # Completion describes the current final state, which may be later
            # than the final status used to classify this report window.
            status_conflict = status_conflict or (latest['resolved'] and task.get('isDone') is not True)
        executor_conflict = bad_executor_context or same_time(executor_validation_changes)
        executor_conflict = executor_conflict or any(previous['target_id'] != current['previous_id']
                            for previous, current in zip(executor_validation_changes, executor_validation_changes[1:]))
        if executor_changes:
            executor_conflict = executor_conflict or executor_validation_changes[-1]['target_id'] != task.get('executorId')
        def position_key(value):
            return value['tasklist_id'], value['stage_id']
        position_conflict = (bad_position_context or same_time(position_validation_changes)
                             or any(position_key(previous['target']) != position_key(current['previous'])
                                    for previous, current in zip(position_validation_changes, position_validation_changes[1:])))
        if position_changes:
            position_conflict = position_conflict or position_key(position_validation_changes[-1]['target']) != (
                task.get('tasklistId'), task.get('stageId'))
        attachment_references, attachment_conflict = {}, False
        for change in parsed_changes:
            if change.get('pattern') != 'customfield-file-reference':
                continue
            for file_id in change['file_ids']:
                reference = {'file_id': file_id, 'field_id': change['field_id'], 'name': change['file_names'][file_id]}
                if file_id in attachment_references and attachment_references[file_id] != reference:
                    attachment_conflict = True
                else:
                    attachment_references[file_id] = reference
        review = (review or ambiguous_status or status_conflict or status_noop or executor_conflict or position_conflict or attachment_conflict
                  or any(c['needs_review'] for c in parsed_changes))
        descriptions = []
        if no_window_evidence:
            descriptions.append("任务更新时间在报告窗口内，但本日动态证据不足，待复核")
        for kind, label in (("created", "新建记录"), ("comment", "评论"),
                            ("status", "状态变更"), ("other", "其他变更")):
            if counts[kind]:
                descriptions.append(f"{label} {counts[kind]} 条")
        if counts["unknown"]:
            descriptions.append(f"未识别动态 {counts['unknown']} 条，待复核")
        if unknown_time:
            descriptions.append("存在时间不明动态，是否属于本日待复核")
        if ambiguous_status:
            descriptions.append("同一时刻多次状态变更，待复核")
        if status_conflict:
            descriptions.append("状态事件序列或当前状态不一致，待复核")
        if status_noop:
            descriptions.append("状态 ID 未变化，不能认定进入新状态，待复核")
        if executor_conflict:
            descriptions.append("执行人事件序列、同刻变更或当前执行人不一致，待复核")
        if position_conflict:
            descriptions.append("位置变更事件序列、同刻变更或当前任务位置不一致，待复核")
        if attachment_conflict:
            descriptions.append("附件引用的字段或文件名存在冲突，待复核")
        if position_changes:
            descriptions.append(f"任务位置变更 {len(position_changes)} 条（不作为完成依据）")
        notice_counts = Counter(c['association_kind'] for c in parsed_changes
                                if c.get('pattern') == 'association-display-notice')
        for kind, label in (('commit', '提交'), ('merge-request', '合并请求')):
            if notice_counts[kind]:
                descriptions.append(f"Teambition 显示关联{label} {notice_counts[kind]} 条")
        if notice_counts:
            descriptions.append("代码来源、实际提交及合并结果未核实")
        for change in parsed_changes:
            if change['action'] == 'update_executor':
                if change['needs_review']:
                    descriptions.append("执行人变更已提取 ID，姓名或来源待复核")
                else:
                    descriptions.append(f"执行人变更：{change['previous_name']} → {change['target_name']}")
                if change['from_automation'] is True:
                    descriptions.append("来源标记：自动化")
            elif change['action'] == 'standard' and change.get('pattern') != 'association-display-notice':
                descriptions.append("检测到关联提交标题模式，具体提交及来源待复核")
        if comments:
            comments.sort(key=lambda c: (c['create_time'] or '', c['event_id']))
            candidates = [c for c in comments if not c['omit_from_summary']]
            excerpts = [c['text'] for c in candidates if c['text']]
            attachment_comments = [c for c in comments if c.get('attachment_only')]
            # The same local file ID may be referenced by several comments.
            # Count distinct validated IDs within this task, without reading files.
            file_ids = {file_id for c in comments if not c['extraction_needs_review']
                        for file_id in c['payload_evidence'].get('files', c['extra_evidence'].get('files', []))}
            file_count = len(file_ids)
            if attachment_comments:
                descriptions.append(f"仅附件评论 {len(attachment_comments)} 条")
            if file_count:
                descriptions.append(f"评论附有 {file_count} 个文件，未读取文件内容")
            if any(c['semantic_review_required'] for c in candidates):
                descriptions.append("评论内容语义归纳待复核")
            if excerpts:
                excerpt = ' / '.join(excerpts)
                descriptions.append("原文摘录：" + excerpt[:240] + ("…（全文见复核输入）" if len(excerpt) > 240 else ""))
            summary_inputs.append({'task_id': task_id, 'comments': comments,
                                   'semantic_summary': None, 'needs_review': True})
        title = safe_label(task.get("content"))
        allow_unresolved = config.get('allow_unresolved_display') is True
        owner = identity_label(task, 'executorId', config.get('owner_names', {}), '成员',
                               allow_unresolved=allow_unresolved)
        status = identity_label(task, 'tfsId', config.get('status_names', {}), '状态',
                                allow_unresolved=allow_unresolved)
        if "待复核" in (title, owner, status):
            review = True
            descriptions.append("标题、负责人或当前状态待复核")
        hard_review = (unknown_time or bool(counts["unknown"]) or no_window_evidence
                       or ambiguous_status or status_conflict or status_noop or executor_conflict or position_conflict or attachment_conflict
                       or any(c['needs_review'] for c in parsed_changes)
                       or any(c['extraction_needs_review'] for c in comments)
                       or "待复核" in (title, owner, status))
        if review:
            issues.append(f"任务 {task_id} 待复核")
        # Final classification follows every event AND display-field review check.
        category = "other"
        if not review:
            if resolved_in_window(transitions):
                category = "resolved"
            elif counts["comment"]:
                category = "progress"
            elif counts["created"]:
                category = "new"
        items.append({"category": category, "title": title,
                      "url": f"https://www.teambition.com/task/{task_id}",
                      "summary": "；".join(descriptions), "owner": owner, "status": status})
        audit.append({"task_id": task_id, "event_ids": [e[1] for e in events], "needs_review": review,
                      "hard_review": bool(hard_review),
                      "executor_conflict": executor_conflict, "status_noop": status_noop,
                      "status_conflict": bool(status_conflict),
                      "position_changes": position_changes, "position_conflict": position_conflict,
                      "attachment_conflict": attachment_conflict,
                      "attachment_references": [attachment_references[key] for key in sorted(attachment_references)],
                      "event_counts": dict(counts),
                      "status_transitions": transitions, "parsed_changes": parsed_changes,
                      "status_validation_transitions": status_validation_transitions,
                      "post_window_context": post_window_context,
                      "undated_event_ids": sorted(undated_ids),
                      "activity_evidence": [ids[e[1]] for e in events] + [ids[k] for k in sorted(undated_ids)]})
    combined = sorted(zip(items, audit), key=lambda pair: pair[1]["task_id"])
    data = {"date": report_day, "tasks": [pair[0] for pair in combined],
            "time_range": {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                           "timezone": "Asia/Shanghai"}}
    return {"digest": data, "markdown": render_digest(data), "audit": [pair[1] for pair in combined],
            "summary_inputs": sorted(summary_inputs, key=lambda item: item['task_id']),
            "issues": issues, "ready_to_send": not issues,
            "scope": {"tenant_id": tenant, "project_id": project,
                      "timezone": "Asia/Shanghai", "start": start.isoformat(), "end_exclusive": end.isoformat(),
                      "include_archived": config.get("include_archived", False),
                      "selection": collection.get('selection'),
                      "pagination_complete": True, "snapshot_guaranteed": False}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path, help="Offline normalized collection")
    source.add_argument("--collect", action="store_true", help="Explicitly allow Teambition GET requests")
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to yesterday in Asia/Shanghai")
    parser.add_argument("--start", help="Optional timezone-aware ISO 8601 start; inclusive")
    parser.add_argument("--end", help="Optional timezone-aware ISO 8601 end; exclusive")
    args = parser.parse_args()
    try:
        config = validate_mappings(parse_config_json(args.config.read_text(encoding="utf-8")))
        validate_view(config)
        # Freeze before either file ingestion or live collection crosses midnight.
        if bool(args.start) != bool(args.end):
            raise ValueError("Both --start and --end are required")
        if args.start and not args.date:
            raise ValueError("--date is required for a custom collection window")
        fixed_day = day_window(args.date)[0].date().isoformat()
        custom_window = (args.start, args.end) if args.start else None
        if args.fixture:
            collection = json.loads(args.fixture.read_text(encoding="utf-8"))
        else:
            config = load_runtime_mappings(config)
            collection = Client(config["tenant_id"]).collect(config["project_id"],
                         include_archived=config.get("include_archived", False), day=fixed_day,
                         window=custom_window)
        result = build_digest(collection, config, fixed_day, window=custom_window)
        # Output is sensitive at runtime. Operator must redirect outside the repository.
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready_to_send"] else 2
    except (CollectionError, ValueError, TypeError, KeyError, OSError):
        print("Digest failed; no send attempted. Check configuration/schema/collection.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
