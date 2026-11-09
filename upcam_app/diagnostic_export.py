"""Create shareable Teambition field-shape diagnostics with values removed."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
import uuid

from activity_adapter import (
    MAX_CONTENT, OBSERVED_ACTIONS, _unique_object, bounded_object, comment_input,
    executor_change, standard_commit_candidate, unsafe_controls, stage_movement,
    standard_association_notification,
)
from report_contract import report_window, validate_selection
from teambition_readonly import CollectionError, identifier, validate_binding
from . import APP_VERSION, RULESET_REVISION
from .diagnostic_protocol import _counter_labels, action_fingerprint
from .diagnostic_markup import markup_shape


_MAX_DEPTH = 5
_MAX_FIELDS = 48
_MAX_ARRAY_SHAPES = 3
_MAX_TASKS = 1000
_MAX_ACTIVITIES_PER_TASK = 1000
_MAX_EXPORT_BYTES = 16 * 1024 * 1024
_MAX_TASK_METADATA_BYTES = 2 * 1024 * 1024
_MAX_EVENT_DETAIL_BYTES = 8 * 1024 * 1024
_MAX_CATALOG_TASKS = 1000
_MAX_CATALOG_EVENTS = 50000
_MAX_CATALOG_GROUPS = 1024
_SAFE_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_OBSERVED_UNDERSCORE_FIELDS = frozenset({"_executorId", "_oldExecutorId"})
_EXPORT_ACTION_TOKENS = OBSERVED_ACTIONS | frozenset({"update_tasklist"})
_SOURCE_FIELDS = frozenset({
    "provider", "source", "sourceId", "appId", "integration", "integrationId",
    "repository", "repositoryId", "repositoryUrl", "repo", "repoId", "repoUrl",
    "commit", "commitId", "commitHash", "sha", "hash", "url", "objectLink", "objectLinkId",
})
_SOURCE_NESTED_FIELDS = _SOURCE_FIELDS | frozenset({"id", "_id", "type", "name", "owner", "fullName"})
_QUOTE_FIELDS = frozenset({
    "id", "_id", "comment", "content", "text", "quote", "renderMode", "attachments",
    "dingFiles", "files", "images", "mentions", "isOnlyNotifyMentions", "author", "creator",
    "user", "name", "creatorId", "createTime", "data", "type", "children", "blocks",
    "ops", "insert", "attributes", "bold", "italic", "link", "url", "title",
})


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


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Numeric value exceeds the finite parser range")
    return result


def _field_shapes(value, depth: int, allowed_fields=None):
    if depth >= _MAX_DEPTH:
        return {"type": _type_name(value), "depth_limited": True}
    if isinstance(value, dict):
        fields, hidden = {}, 0
        for key in sorted(value, key=lambda item: str(item))[:_MAX_FIELDS]:
            if (key not in allowed_fields if allowed_fields is not None else not _safe_field_name(key)):
                hidden += 1
                continue
            fields[key] = _field_shapes(value[key], depth + 1, allowed_fields)
        hidden += max(0, len(value) - _MAX_FIELDS)
        return {"type": "object", "fields": fields, "other_field_count": hidden}
    if isinstance(value, list):
        shapes, seen = [], set()
        for item in value[:_MAX_ARRAY_SHAPES]:
            shaped = _field_shapes(item, depth + 1, allowed_fields)
            encoded = json.dumps(shaped, sort_keys=True, ensure_ascii=True)
            if encoded not in seen:
                seen.add(encoded)
                shapes.append(shaped)
        return {"type": "array", "length": len(value), "sample_item_shapes": shapes}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": _type_name(value)}


def _parse_json_shape(value, allowed_fields=None):
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
                             parse_float=_finite_float,
                             parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return result
    result["json_valid"] = True
    if isinstance(decoded, dict):
        result["json_object"] = True
        shaped = _field_shapes(decoded, 0, allowed_fields)
        result["fields"] = shaped.get("fields", {})
        result["other_field_count"] = shaped.get("other_field_count", 0)
    elif isinstance(decoded, list):
        result["field_array"] = _field_shapes(decoded, 0, allowed_fields)
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


def _quote_check(value):
    """Inspect quoted encoding without emitting text or approving extraction."""
    result = _text_check(value)
    shape = _parse_json_shape(value, _QUOTE_FIELDS)
    result["json_shape"] = shape
    if not shape["json_valid"]:
        return result
    # The first parse checked the byte budget, duplicate keys and constants.
    decoded = json.loads(value)
    if isinstance(decoded, str):
        result["decoded_string_shape"] = _parse_json_shape(decoded, _QUOTE_FIELDS)
        result["decoded_string_text_check"] = _text_check(decoded)
    elif isinstance(decoded, dict):
        result["decoded_text_checks"] = {
            key: _text_check(decoded[key]) for key in ("comment", "content", "text", "quote")
            if key in decoded
        }
    return result


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
    notice = standard_association_notification(activity)
    candidate = standard_commit_candidate(activity)
    data = bounded_object(activity.get("content"))
    reasons = ["title_pattern_does_not_prove_source"] if candidate is not None else []
    if candidate is None:
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
    source_shapes = {
        source_name: {key: _field_shapes(source[key], 0, _SOURCE_NESTED_FIELDS)
                      for key in sorted(_SOURCE_FIELDS) if key in source}
        for source_name, source in (("content", data or {}), ("event", activity))
    }
    return {"candidate_recognized": candidate is not None,
            "display_notice_supported": notice is not None,
            "association_kind": notice['association_kind'] if notice else None,
            "automatic_review_required": notice is None, "rejection_reasons": [] if notice else reasons,
            "source_field_shapes": source_shapes, "source_verified": False,
            "object_verified": False,
            "field_presence_is_proof": False}


def _activities(record):
    value = record.get("activities", []) if isinstance(record, dict) else []
    return value if isinstance(value, list) else []


def _task_diagnostic_priority(record, bounds):
    """Keep relevant evidence visible when bounded detail omits inactive tasks."""
    priority = 3
    for activity in _activities(record):
        if not isinstance(activity, dict):
            return 0
        membership = _in_report_window(activity.get("createTime"), bounds)
        if membership is None:
            return 0
        if membership:
            action = activity.get("action")
            priority = min(priority, 1 if not isinstance(action, str) or action not in OBSERVED_ACTIONS else 2)
    task = record.get("task", {}) if isinstance(record, dict) else {}
    if isinstance(task, dict) and _in_report_window(task.get("updated"), bounds) is True:
        priority = min(priority, 2)
    return priority


def _protocol_catalog(records, bounds, total_events):
    """Scan bounded source identities independently of expensive detail shapes."""
    groups = {}
    considered = unidentified = omitted = unknown_window = scanned = 0
    complete = bounds is not None and len(records) <= _MAX_CATALOG_TASKS
    for task_index, record in enumerate(records[:_MAX_CATALOG_TASKS]):
        if not isinstance(record, dict) or not isinstance(record.get("activities", []), list):
            complete = False
            continue
        for activity in _activities(record):
            if scanned >= _MAX_CATALOG_EVENTS:
                break
            scanned += 1
            if not isinstance(activity, dict):
                complete = False
                continue
            action = activity.get("action")
            if isinstance(action, str) and action in OBSERVED_ACTIONS:
                continue
            membership = _in_report_window(activity.get("createTime"), bounds)
            if membership is None:
                unknown_window += 1
                complete = False
                continue
            if not membership:
                continue
            considered += 1
            fingerprint = action_fingerprint(action)
            digest = fingerprint["sha256"]
            if digest is None:
                unidentified += 1
            elif digest in groups:
                groups[digest]["event_count"] += 1
                groups[digest]["tasks"].add(task_index)
            elif len(groups) >= _MAX_CATALOG_GROUPS:
                omitted += 1
                complete = False
            else:
                groups[digest] = {"action_fingerprint": fingerprint, "event_count": 1,
                                  "tasks": {task_index}}
        if scanned >= _MAX_CATALOG_EVENTS:
            break
    unscanned = total_events - scanned
    return {
        "format": "upcam-action-catalog-v1",
        "complete": complete and unscanned == 0,
        "unknown_events_considered": considered,
        "unidentified_events": unidentified,
        "omitted_events": omitted,
        "unscanned_events": unscanned,
        "unknown_window_events": unknown_window,
        **_counter_labels(considered, unknown_window),
        "groups": [{"action_fingerprint": group["action_fingerprint"],
                    "event_count": group["event_count"], "task_count": len(group["tasks"])}
                   for _digest, group in sorted(groups.items())],
        "limits": {"tasks": _MAX_CATALOG_TASKS, "events": _MAX_CATALOG_EVENTS,
                   "groups": _MAX_CATALOG_GROUPS},
    }


def _embedded_json_cost(value, depth):
    """Charge pretty UTF-8 bytes, including every embedded line's indentation."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return len(encoded.encode("utf-8")) + (encoded.count("\n") + 1) * depth * 2 + 2


def _serialized_size(value, stop_after):
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False).iterencode(value):
        size += len(part.encode("utf-8"))
        if size > stop_after:
            break
    return size


def _activity_shape(activity, task_id, owner_names, status_names, bounds):
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
        "action_token": action if isinstance(action, str) and action in _EXPORT_ACTION_TOKENS else None,
        "action_fingerprint": action_fingerprint(action),
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
                "markup_shape": markup_shape(comment["body"], comment["render_mode"]),
                "quote_checks": {source: dict(_quote_check(value),
                                              adapter_supported=source in comment["quote_contexts"])
                                 for source, value in comment["quote_evidence"].items()},
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
    elif safe_action == "move.cross.from.stage.to.stage":
        shaped["movement_check"] = {"adapter_supported": stage_movement(activity) is not None,
                                     "completion_evidence": False}
    return shaped


def safe_structure_export(collection: dict, config: dict, day=None, window=None) -> dict:
    """Return IDs and schema metadata, but no names, titles, comments, credentials, or API bodies.

    Task/event and unmapped current-owner IDs correlate this file with the UI. Every
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

    # Reading list lengths counts omissions without scanning omitted event values.
    total_events = sum(len(_activities(record)) for record in records)
    # This is an export ordering only: source task order, event counts and
    # sendability do not change. Catalog/detail truncation stays explicit.
    diagnostic_records = sorted(records, key=lambda record: _task_diagnostic_priority(record, bounds))
    catalog = _protocol_catalog(diagnostic_records, bounds, total_events)
    tasks, reasons = [], set()
    metadata_bytes = detail_bytes = included_events = 0
    detail_budget_exhausted = False
    if len(records) > _MAX_TASKS:
        reasons.add("task_count_limit")
    for record in diagnostic_records[:_MAX_TASKS]:
        if not isinstance(record, dict):
            reasons.add("invalid_task_record")
            continue
        task = record.get("task")
        if not isinstance(task, dict):
            task = {}
        activities = _activities(record)
        if not isinstance(record.get("activities", []), list):
            reasons.add("invalid_activity_list")
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
        shaped_task = {
            "task_id": task_id,
            "task_field_shapes": _field_shapes(task, 0).get("fields", {}),
            "current_owner_mapping_present": current_owner_id in owner_names if current_owner_id else False,
            "unmapped_current_owner_id": (current_owner_id
                                          if current_owner_id not in owner_names else None),
            "current_status_mapping_present": current_status_id in status_names if current_status_id else False,
            "current_done_type": _type_name(task.get("isDone")),
            "activities": [],
            "truncated_activity_count": len(activities),
            "detail_truncated": bool(activities),
        }
        # Reserve array expansion, commas, and the final boolean/count changes.
        metadata_cost = _embedded_json_cost(shaped_task, 2) + 32
        if metadata_bytes + metadata_cost > _MAX_TASK_METADATA_BYTES:
            reasons.add("task_metadata_byte_budget")
            break
        metadata_bytes += metadata_cost
        if len(activities) > _MAX_ACTIVITIES_PER_TASK:
            reasons.add("activity_count_limit")
        for activity in activities[:_MAX_ACTIVITIES_PER_TASK]:
            if detail_budget_exhausted:
                break
            if not isinstance(activity, dict):
                reasons.add("invalid_activity_record")
                continue
            if detail_bytes >= _MAX_EVENT_DETAIL_BYTES:
                detail_budget_exhausted = True
                reasons.add("event_detail_byte_budget")
                break
            shaped = _activity_shape(activity, task_id, owner_names, status_names, bounds)
            detail_cost = _embedded_json_cost(shaped, 4)
            if detail_bytes + detail_cost > _MAX_EVENT_DETAIL_BYTES:
                detail_budget_exhausted = True
                reasons.add("event_detail_byte_budget")
                break
            detail_bytes += detail_cost
            shaped_task["activities"].append(shaped)
            included_events += 1
        shaped_task["truncated_activity_count"] = len(activities) - len(shaped_task["activities"])
        shaped_task["detail_truncated"] = shaped_task["truncated_activity_count"] > 0
        tasks.append(shaped_task)
    matches_selection = None
    if bounds is not None and isinstance(collection, dict):
        try:
            validate_selection(collection.get("selection"), day, *bounds)
        except (TypeError, ValueError):
            matches_selection = False
        else:
            matches_selection = True
    result = {
        "format": "upcam-teambition-safe-shape-v5",
        "diagnostic": {
            "id": uuid.uuid4().hex,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "application_version": APP_VERSION,
            "ruleset_revision": RULESET_REVISION,
            "capabilities": ["action-fingerprint/v1", "comment-markup-shape/v1",
                             "commit-source-field-shapes/v1", "protocol-catalog/v1",
                             "explicit-counter-labels/v1"],
            "report_window": ({"start": bounds[0].isoformat(), "end_exclusive": bounds[1].isoformat(),
                               "timezone": "Asia/Shanghai"} if bounds else None),
        },
        "privacy": "diagnostic version/time/window, task/event and unmapped current-owner IDs, bounded action fingerprints, allowlisted protocol tokens, field and markup shapes are included; names, titles, comments, credentials, arbitrary markup names/attributes and other response values are omitted",
        "pagination_complete": collection.get("pagination_complete") is True if isinstance(collection, dict) else False,
        "window_evaluation_available": bounds is not None,
        "selection_check": {
            "matches_report_window": matches_selection,
        },
        "task_count": len(records),
        "activity_count": total_events,
        "tasks": tasks,
        "truncated_task_count": len(records) - len(tasks),
        "truncated_activity_count": total_events - included_events,
        "detail_truncated": bool(reasons),
        "detail_truncation_reasons": sorted(reasons),
        "protocol_catalog": catalog,
        "export_limits": {
            "max_json_bytes": _MAX_EXPORT_BYTES,
            "max_task_metadata_bytes": _MAX_TASK_METADATA_BYTES,
            "max_event_detail_bytes": _MAX_EVENT_DETAIL_BYTES,
            "max_tasks": _MAX_TASKS,
            "max_activities_per_task": _MAX_ACTIVITIES_PER_TASK,
            "charged_task_metadata_bytes": metadata_bytes,
            "charged_event_detail_bytes": detail_bytes,
            "serialization": {"encoding": "utf-8", "ensure_ascii": False, "indent": 2},
        },
    }
    # A final streaming check also covers envelope/catalog overhead and a file newline.
    # Default detail budgets leave ample space; this guard also supports smaller budgets.
    if _serialized_size(result, _MAX_EXPORT_BYTES - 1) + 1 > _MAX_EXPORT_BYTES:
        result["tasks"] = []
        result["truncated_task_count"] = len(records)
        result["truncated_activity_count"] = total_events
        result["detail_truncated"] = True
        result["detail_truncation_reasons"] = sorted(reasons | {"json_byte_budget"})
        result["export_limits"]["charged_task_metadata_bytes"] = 0
        result["export_limits"]["charged_event_detail_bytes"] = 0
        if _serialized_size(result, _MAX_EXPORT_BYTES - 1) + 1 > _MAX_EXPORT_BYTES:
            raise ValueError("Diagnostic byte budget cannot contain its bounded protocol catalog")
    return result
