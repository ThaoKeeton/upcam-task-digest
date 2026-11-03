"""Deterministic event-only daily digest. Unknown evidence blocks delivery."""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

from render_digest import render_digest, text
from report_contract import SHANGHAI, canonical_window, strict_day
from teambition_readonly import Client, CollectionError, SelectionUnavailable, identifier, record_id
from activity_adapter import comment_input, parse_activity, validate_binding


def day_window(day=None, now=None):
    if day is None:
        now = now or datetime.now(SHANGHAI)
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone-aware")
        day = (now.astimezone(SHANGHAI) - timedelta(days=1)).date()
    elif isinstance(day, str):
        day = strict_day(day)
    elif type(day) is not date:
        raise ValueError("Report day must be a date")
    return canonical_window(day.isoformat())


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


def build_digest(collection, config, day):
    # Caller must freeze the report day before collection; no implicit late clock read.
    if day is None:
        raise ValueError("Report day must be fixed before collection")
    start, end = day_window(day)
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
        issues.append("昨天视图口径尚未核验")
    if config.get("api_contract_verified") is not True:
        issues.append("API 字段及事件映射尚未核验")
    if collection.get('selection', {}).get('view_equivalent') is not True:
        issues.append("更新时间筛选与昨天视图的等价性尚未核验")
    if collection.get("include_archived") != config.get("include_archived", False):
        raise CollectionError("Archive scope mismatch")
    items, audit, summary_inputs, seen = [], [], [], set()
    for record in collection["records"]:
        task = record["task"]
        task_id = record_id(task)
        if task_id in seen:
            raise CollectionError("Duplicate task in collection")
        seen.add(task_id)
        if task.get("projectId") != project:
            raise CollectionError("Task belongs to another project")
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
            if not start <= when < end:
                continue
            kind, resolved = parse_activity(raw, config)
            if kind == "comment":
                comments.append(comment_input(raw, event_id, when))
            events.append((when, event_id, kind, resolved))
        if not events and not unknown_time:
            continue
        events.sort(key=lambda e: (e[0], e[1]))
        counts = Counter(e[2] for e in events)
        review = unknown_time or bool(counts["unknown"]) or bool(comments)
        # The last status transition of the day wins; reopen is never resolved.
        status_events = [e for e in events if e[2] == "status"]
        ambiguous_status = bool(status_events and sum(e[0] == status_events[-1][0] for e in status_events) > 1)
        review = review or ambiguous_status
        descriptions = []
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
        if comments:
            comments.sort(key=lambda c: (c['create_time'] or '', c['event_id']))
            candidates = [c for c in comments if not c['omit_from_summary']]
            excerpts = [c['text'] for c in candidates if c['text']]
            descriptions.append("评论内容语义归纳待复核")
            if excerpts:
                excerpt = ' / '.join(excerpts)
                descriptions.append("原文摘录：" + excerpt[:240] + ("…（全文见复核输入）" if len(excerpt) > 240 else ""))
            summary_inputs.append({'task_id': task_id, 'comments': comments,
                                   'semantic_summary': None, 'needs_review': True})
        title = safe_label(task.get("content"))
        owner = safe_label(config.get("owner_names", {}).get(task.get("executorId")))
        status = safe_label(config.get("status_names", {}).get(task.get("tfsId")))
        if "待复核" in (title, owner, status):
            review = True
            descriptions.append("标题、负责人或当前状态待复核")
        if review:
            issues.append(f"任务 {task_id} 待复核")
        # Final classification follows every event AND display-field review check.
        category = "other"
        if not review:
            if status_events and status_events[-1][3]:
                category = "resolved"
            elif counts["comment"]:
                category = "progress"
            elif counts["created"]:
                category = "new"
        items.append({"category": category, "title": title,
                      "url": f"https://www.teambition.com/project/{project}/task/{task_id}",
                      "summary": "；".join(descriptions), "owner": owner, "status": status})
        audit.append({"task_id": task_id, "event_ids": [e[1] for e in events], "needs_review": review,
                      "undated_event_ids": sorted(undated_ids),
                      "activity_evidence": [ids[e[1]] for e in events] + [ids[k] for k in sorted(undated_ids)]})
    combined = sorted(zip(items, audit), key=lambda pair: pair[1]["task_id"])
    data = {"date": start.date().isoformat(), "tasks": [pair[0] for pair in combined]}
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
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        validate_view(config)
        # Freeze before either file ingestion or live collection crosses midnight.
        fixed_day = day_window(args.date)[0].date().isoformat()
        if args.fixture:
            collection = json.loads(args.fixture.read_text(encoding="utf-8"))
        else:
            collection = Client(config["tenant_id"]).collect(config["project_id"],
                         include_archived=config.get("include_archived", False), day=fixed_day)
        result = build_digest(collection, config, fixed_day)
        # Output is sensitive at runtime. Operator must redirect outside the repository.
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready_to_send"] else 2
    except SelectionUnavailable:
        print("Digest blocked: verified updated-time API selection semantics unavailable; no request attempted.", file=sys.stderr)
        return 1
    except (CollectionError, ValueError, TypeError, KeyError, OSError):
        print("Digest failed; no send attempted. Check configuration/schema/collection.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
