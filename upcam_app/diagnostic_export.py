"""Create shareable Teambition field-shape diagnostics with values removed."""

from __future__ import annotations

import json
import re
from datetime import datetime

from activity_adapter import (
    MAX_CONTENT, OBSERVED_ACTIONS, _unique_object, comment_input, standard_commit_candidate,
)
from teambition_readonly import CollectionError, identifier


_MAX_DEPTH = 5
_MAX_FIELDS = 48
_MAX_ARRAY_SHAPES = 3
_MAX_TASKS = 1000
_MAX_ACTIVITIES_PER_TASK = 1000
_SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


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
            if not isinstance(key, str) or not _SAFE_FIELD.fullmatch(key):
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
        result["fields"] = _field_shapes(decoded, 0).get("fields", {})
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


def safe_structure_export(collection: dict, config: dict) -> dict:
    """Return IDs and schema metadata, but no names, titles, comments, credentials, or API bodies.

    IDs are retained to correlate this file with the in-app diagnostic. Every
    other API value is reduced to a type, length, boolean comparison, or shape.
    """
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
                "outer_field_names": sorted(key for key in activity if isinstance(key, str)
                                             and _SAFE_FIELD.fullmatch(key))[:_MAX_FIELDS],
                "create_time_type": _type_name(activity.get("createTime")),
                "create_time_has_timezone": _aware_timestamp(activity.get("createTime")),
                "content_shape": _parse_json_shape(raw_content),
            }
            if safe_action == "comment":
                try:
                    comment = comment_input(activity, "event", None)
                    shaped["comment_check"] = {
                        "needs_review": comment["extraction_needs_review"],
                        "review_reasons": list(comment["review_reasons"]),
                    }
                except (CollectionError, TypeError, ValueError):
                    shaped["comment_check"] = {
                        "needs_review": True, "review_reasons": ["评论结构超过安全解析范围"]}
            elif safe_action == "update.taskflowstatus":
                shaped["status_check"] = _status_shape_check(raw_content, status_names)
            elif safe_action == "standard":
                shaped["commit_check"] = {
                    "candidate_recognized": standard_commit_candidate(activity) is not None,
                    "automatic_review_required": True,
                }
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
        "format": "upcam-teambition-safe-shape-v1",
        "privacy": "task/event IDs and field names are included; all names, titles, comments, credentials, and response values are omitted",
        "pagination_complete": collection.get("pagination_complete") is True if isinstance(collection, dict) else False,
        "task_count": len(records),
        "tasks": tasks,
        "truncated_task_count": truncated_tasks,
    }
