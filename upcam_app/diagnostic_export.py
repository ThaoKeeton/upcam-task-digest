"""Create shareable Teambition field-shape diagnostics with values removed."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from activity_adapter import (
    MAX_CONTENT, OBSERVED_ACTIONS, _unique_object, bounded_object, comment_input,
    executor_change, standard_commit_candidate, unsafe_controls,
)
from report_contract import report_window, updated_selection
from teambition_readonly import CollectionError, identifier, validate_binding


_MAX_DEPTH = 5
_MAX_FIELDS = 48
_MAX_ARRAY_SHAPES = 3
_MAX_TASKS = 1000
_MAX_ACTIVITIES_PER_TASK = 1000
_SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_OBSERVED_UNDERSCORE_FIELDS = frozenset({"_executorId", "_oldExecutorId"})


def _safe_field_name(value):
    return isinstance(value, str) and (value in _OBSERVED_UNDERSCORE_FIELDS
                                       or _SAFE_FIELD.fullmatch(value) is not None)


def _type_name(value) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, str):
        return "string"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unsupported"


def _field_shapes(value, depth: int):
    if depth >= _MAX_DEPTH:
        return {"type": _type_name(value), "depth_limited": True}
    if isinstance(value, dict):
        fields, hidden = {}, 0
        for key in sorted(value, key=lambda item: str(item))[:_MAX_FIELDS]:
            if not _safe_field_name(key):
                hidden += 1
                continue
            fields[key] = _field_shapes(value[key], depth + 1)
        hidden += max(0, len(value) - _MAX_FIELDS)
        return {"type": "object", "fields": fields, "other_field_count": hidden}
    if isinstance(value, list):
        shapes, seen = [], set()
        for item in value[:_MAX_ARRAY_SHAPES]:
            shaped = _field_shapes(item, depth + 1)
            encoded = json.dumps(shaped, sort_keys=True, ensure_ascii=True)
            if encoded not in seen:
                seen.add(encoded)
                shapes.append(shaped)
        return {"type": "array", "length": len(value), "sample_item_shapes": shapes}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": _type_name(value)}


def _parse_json_shape(value):
    if not isinstance(value, str):
        return {"type": _type_name(value), "length": None, "json_valid": False,
                "json_object": False, "fields": {}}
    result = {"type": "string", "length": len(value), "json_valid": False,
              "json_object": False, "fields": {}}
    if len(value.encode("utf-8", errors="replace")) > MAX_CONTENT:
        result["over_parser_size_limit"] = True
        return result
    try:
        decoded = json.loads(value, object_pairs_hook=_unique_object,
                             parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return result
    result["json_valid"] = True
    if isinstance(decoded, dict):
        result["json_object"] = True
        shaped = _field_shapes(decoded, 0)
        result["fields"] = shaped.get("fields", {})
        result["other_field_count"] = shaped.get("other_field_count", 0)
    elif isinstance(decoded, list):
        result["field_array"] = _field_shapes(decoded, 0)
    else:
        result["json_scalar_type"] = _type_name(decoded)
    return result


def _mapping_check(status_id, event_name, status_names):
    try:
        safe_id = identifier(status_id)
    except CollectionError:
        return {"id_valid": False, "mapping_present": False, "name_matches_mapping": None}
    mapping_present = safe_id in status_names
    expected = status_names.get(safe_id)
    name_matches = (event_name == expected if mapping_present and isinstance(event_name, str)
                    and isinstance(expected, str) else None)
    return {"id_valid": True, "mapping_present": mapping_present,
            "name_matches_mapping": name_matches}


def _status_shape_check(content, status_names):
    if not isinstance(content, str):
        return {"content_object": False}
    parsed = _parse_json_shape(content)
    if not parsed["json_object"]:
        return {"content_object": False, "json_valid": parsed["json_valid"]}
    try:
        data = json.loads(content, object_pairs_hook=_unique_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        return {"content_object": False, "json_valid": False}
    return {
        "content_object": True,
        "previous_status": _mapping_check(data.get("oldTfsId"), data.get("oldTaskflowstatus"), status_names),
        "target_status": _mapping_check(data.get("tfsId"), data.get("taskflowstatus"), status_names),
        "event_is_done_type": _type_name(data.get("isDone")),
    }


def _aware_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _in_report_window(value, bounds):
    if bounds is None or not _aware_timestamp(value):
        return None
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return bounds[0] <= instant < bounds[1]


def _text_check(value):
    """Classify text without emitting tags, codepoints, or any text values."""
    markup, controls = [], set()
    if isinstance(value, str):
        if "<br>" in value:
            markup.append("exact_br")
        without_breaks = value.replace("<br>", "\n")
        if re.search(r"<[^>]+>|<[/!A-Za-z]", without_breaks):
            markup.append("other_tag_like")
        if re.search(r'\{\s*"', value):
            markup.append("json_like")
        for char in value:
            if char in "\r\n\t":
                continue
            category = unicodedata.category(char)
            if category == "Cc":
                controls.add("ascii_control" if ord(char) < 128 else "unicode_control")
            elif category == "Cf":
                controls.add("unicode_format")
    return {"type": _type_name(value), "markup_categories": markup,
            "control_categories": sorted(controls)}


def _executor_shape_check(activity, owner_names):
    data = bounded_object(activity.get("content"))
    if data is None:
        return {"content_object": False, "needs_review": True}
    change = executor_change(activity, {"owner_names": owner_names})
    flags, sources = [], {}
    for source_name, source in (("content", data), ("event", activity)):
        payload = source.get("payload")
        flag = payload.get("fromAutomation") if isinstance(payload, dict) else None
        sources[source_name] = {"payload_present": "payload" in source,
                                "payload_type": _type_name(payload),
                                "from_automation_type": _type_name(flag)}
        if type(flag) is bool:
            flags.append(flag)
    return {
        "content_object": True,
        "previous_executor": _mapping_check(data.get("_oldExecutorId"), None, owner_names),
        "target_executor": _mapping_check(data.get("_executorId"), None, owner_names),
        "automation_sources": sources,
        "automation_flags_match": flags[0] == flags[1] if len(flags) == 2 else None,
        "needs_review": change is None or change["needs_review"],
    }


def _commit_shape_check(activity):
    candidate = standard_commit_candidate(activity)
    reasons = ["title_pattern_does_not_prove_source"] if candidate is not None else []
    if candidate is None:
        data = bounded_object(activity.get("content"))
        if data is None:
            reasons.append("content_object_unavailable")
        else:
            title, creator = data.get("title"), data.get("customCreator")
            if not isinstance(title, str):
                reasons.append("title_not_string")
            elif unsafe_controls(title):
                reasons.append("title_has_unsafe_controls")
            if (not isinstance(creator, dict) or not isinstance(creator.get("name"), str)
                    or not creator["name"].strip()):
                reasons.append("creator_name_unavailable")
            if not reasons:
                reasons.append("unrecognized_title_pattern")
    return {"candidate_recognized": candidate is not None,
            "automatic_review_required": True, "rejection_reasons": reasons}


def safe_structure_export(collection: dict, config: dict, day=None, window=None) -> dict:
    """Return IDs and schema metadata, but no names, titles, comments, credentials, or API bodies.

    IDs are retained to correlate this file with the in-app diagnostic. Every
    other API value is reduced to a type, length, boolean comparison, or fixed
    diagnostic category. Optional day/window use the report's half-open scope;
    omitted scope yields unknown membership, never an inferred calendar day.
    Invalid explicit report boundaries raise ValueError like report generation.
    """
    if window is not None and day is None:
        raise ValueError("A diagnostic window requires an explicit report date")
    bounds = report_window(day, *(window or (None, None))) if day is not None else None
    records = collection.get("records", []) if isinstance(collection, dict) else []
    if not isinstance(records, list):
        records = []
    owner_names = config.get("owner_names", {}) if isinstance(config, dict) else {}
    status_names = config.get("status_names", {}) if isinstance(config, dict) else {}
    if not isinstance(owner_names, dict):
        owner_names = {}
    if not isinstance(status_names, dict):
        status_names = {}

    tasks = []
    truncated_tasks = max(0, len(records) - _MAX_TASKS)
    for record in records[:_MAX_TASKS]:
        if not isinstance(record, dict):
            continue
        task = record.get("task")
        if not isinstance(task, dict):
            task = {}
        activities = record.get("activities", [])
        if not isinstance(activities, list):
            activities = []
        try:
            task_id = identifier(task.get("id", task.get("_id", task.get("taskId"))))
        except CollectionError:
            task_id = "（ID 无法识别）"
        try:
            current_owner_id = identifier(task.get("executorId"))
        except CollectionError:
            current_owner_id = None
        try:
            current_status_id = identifier(task.get("tfsId"))
        except CollectionError:
            current_status_id = None
        event_shapes = []
        truncated_activities = max(0, len(activities) - _MAX_ACTIVITIES_PER_TASK)
        for activity in activities[:_MAX_ACTIVITIES_PER_TASK]:
            if not isinstance(activity, dict):
                continue
            action = activity.get("action")
            safe_action = action if isinstance(action, str) and action in OBSERVED_ACTIONS else "unknown"
            raw_content = activity.get("content")
            try:
                event_id = identifier(activity.get("id"))
            except CollectionError:
                event_id = None
            shaped = {
                "event_id": event_id,
                "action": safe_action,
                "outer_field_names": sorted(key for key in activity if _safe_field_name(key))[:_MAX_FIELDS],
                "create_time_type": _type_name(activity.get("createTime")),
                "create_time_has_timezone": _aware_timestamp(activity.get("createTime")),
                "in_report_window": _in_report_window(activity.get("createTime"), bounds),
                "content_shape": _parse_json_shape(raw_content),
            }
            try:
                validate_binding(activity, task_id)
            except CollectionError:
                shaped["task_binding_valid"] = False
            else:
                shaped["task_binding_valid"] = True
            if safe_action == "comment":
                try:
                    comment = comment_input(activity, "event", None)
                    shaped["comment_check"] = {
                        "needs_review": comment["extraction_needs_review"],
                        "review_reasons": list(comment["review_reasons"]),
                        "body_check": _text_check(comment["body"]),
                        "quote_checks": {source: _text_check(value) for source, value
                                         in comment["quote_evidence"].items()},
                    }
                except (CollectionError, TypeError, ValueError):
                    shaped["comment_check"] = {
                        "needs_review": True, "review_reasons": ["评论结构超过安全解析范围"]}
            elif safe_action == "update.taskflowstatus":
                shaped["status_check"] = _status_shape_check(raw_content, status_names)
            elif safe_action == "standard":
                shaped["commit_check"] = _commit_shape_check(activity)
            elif safe_action == "update_executor":
                shaped["executor_check"] = _executor_shape_check(activity, owner_names)
            event_shapes.append(shaped)
        tasks.append({
            "task_id": task_id,
            "task_field_shapes": _field_shapes(task, 0).get("fields", {}),
            "current_owner_mapping_present": current_owner_id in owner_names if current_owner_id else False,
            "current_status_mapping_present": current_status_id in status_names if current_status_id else False,
            "current_done_type": _type_name(task.get("isDone")),
            "activities": event_shapes,
            "truncated_activity_count": truncated_activities,
        })
    return {
        "format": "upcam-teambition-safe-shape-v2",
        "privacy": "task/event IDs and field names are included; all names, titles, comments, credentials, and response values are omitted",
        "pagination_complete": collection.get("pagination_complete") is True if isinstance(collection, dict) else False,
        "window_evaluation_available": bounds is not None,
        "selection_check": {
            "matches_report_window": (collection.get("selection") == updated_selection(day, *bounds)
                                       if bounds is not None and isinstance(collection, dict) else None),
        },
        "task_count": len(records),
        "tasks": tasks,
        "truncated_task_count": truncated_tasks,
    }
