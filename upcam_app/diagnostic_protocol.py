"""Offline protocol identity checks, never action semantics or source approval."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from activity_adapter import _unique_object


FINGERPRINT_ALGORITHM = "sha256-upcam-action-v1"
_DOMAIN = b"upcam-teambition-action/v1\0"
_PROTOCOL_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_CANDIDATES = 128


def _counter_labels(considered, unknown_window):
    """Describe compatibility counters without interpreting input-provided prose."""
    return {
        "unknown_events_in_report_window": considered,
        "unknown_events_with_unknown_time": unknown_window,
        "counter_definitions": {
            "unknown_events_considered": (
                "Unsupported-action events confirmed inside the report window among scanned events."
            ),
            "unknown_events_in_report_window": "Alias of unknown_events_considered.",
            "unknown_window_events": (
                "Unsupported-action events with undetermined report-window membership because event "
                "time is missing/invalid or the report window is unavailable."
            ),
            "unknown_events_with_unknown_time": "Alias of unknown_window_events.",
        },
    }


def _fingerprint_digest(value):
    if (isinstance(value, dict) and value.get("algorithm") == FINGERPRINT_ALGORITHM
            and isinstance(value.get("sha256"), str) and _DIGEST.fullmatch(value["sha256"])
            and type(value.get("length")) is int and 1 <= value["length"] <= 80):
        return value["sha256"]
    return None


def _match_catalog(catalog, by_digest):
    keys = ("unknown_events_considered", "unidentified_events", "omitted_events", "unscanned_events")
    if (not isinstance(catalog, dict) or catalog.get("format") != "upcam-action-catalog-v1"
            or type(catalog.get("complete")) is not bool
            or any(type(catalog.get(k)) is not int or catalog[k] < 0 for k in keys)
            or not isinstance(catalog.get("groups"), list) or len(catalog["groups"]) > 1024):
        raise ValueError("Invalid protocol catalog")
    unknown_window = catalog.get("unknown_window_events", 0)
    if type(unknown_window) is not int or unknown_window < 0:
        raise ValueError("Invalid unknown window count")
    labels = _counter_labels(catalog["unknown_events_considered"], unknown_window)
    for alias in ("unknown_events_in_report_window", "unknown_events_with_unknown_time"):
        if alias in catalog and (type(catalog[alias]) is not int or catalog[alias] != labels[alias]):
            raise ValueError("Protocol catalog counter aliases are inconsistent")
    if catalog["complete"] and (catalog["omitted_events"] or catalog["unscanned_events"] or unknown_window):
        raise ValueError("Incomplete catalog cannot claim completeness")
    matches, seen, represented = [], set(), 0
    for group in catalog["groups"]:
        if not isinstance(group, dict):
            raise ValueError("Invalid protocol group")
        digest = _fingerprint_digest(group.get("action_fingerprint"))
        count, tasks = group.get("event_count"), group.get("task_count")
        if (digest is None or digest in seen or type(count) is not int or count <= 0
                or type(tasks) is not int or not 1 <= tasks <= count):
            raise ValueError("Invalid protocol group counts or identity")
        seen.add(digest)
        represented += count
        if digest in by_digest:
            matches.append({"candidate": by_digest[digest], "event_count": count,
                            "task_count": tasks, "semantics_verified": False})
    if represented + catalog["unidentified_events"] + catalog["omitted_events"] != catalog["unknown_events_considered"]:
        raise ValueError("Protocol catalog counts are inconsistent")
    order = {candidate: i for i, candidate in enumerate(by_digest.values())}
    matches.sort(key=lambda match: order[match["candidate"]])
    return {**{k: catalog[k] for k in keys}, "unknown_window_events": unknown_window, **labels,
            "catalog_complete": catalog["complete"], "matches": matches}


def action_fingerprint(value) -> dict:
    """Hash an exact bounded identifier; never export an arbitrary action value."""
    valid = isinstance(value, str) and _PROTOCOL_TOKEN.fullmatch(value) is not None
    return {
        "algorithm": FINGERPRINT_ALGORITHM,
        "length": len(value) if isinstance(value, str) else None,
        "sha256": hashlib.sha256(_DOMAIN + value.encode("ascii")).hexdigest() if valid else None,
    }


def match_action_candidates(report: dict, candidates: list[str]) -> dict:
    """Compare caller-supplied protocol names against a redacted diagnostic.

    Only the caller's matching candidates are returned. Missing fingerprints
    remain unidentified; a nonmatch is not evidence that an event is absent.
    Matching identity never approves its meaning, source or send eligibility.
    """
    if (not isinstance(report, dict) or not isinstance(report.get("format"), str)
            or re.fullmatch(r"upcam-teambition-safe-shape-v(?:[3-9]|[1-9][0-9]{1,2})", report["format"]) is None
            or not isinstance(report.get("tasks"), list) or len(report["tasks"]) > 1000):
        raise ValueError("Unsupported diagnostic structure")
    if (not isinstance(candidates, list) or not 1 <= len(candidates) <= _MAX_CANDIDATES
            or any(not isinstance(c, str) or not _PROTOCOL_TOKEN.fullmatch(c) for c in candidates)):
        raise ValueError("Invalid bounded protocol candidates")
    counts = {candidate: {"events": 0, "tasks": set()} for candidate in dict.fromkeys(candidates)}
    by_digest = {action_fingerprint(candidate)["sha256"]: candidate for candidate in counts}
    if "protocol_catalog" in report:
        return _match_catalog(report["protocol_catalog"], by_digest)
    considered = unidentified = unknown_window = 0
    for index, task in enumerate(report["tasks"]):
        if not isinstance(task, dict) or not isinstance(task.get("activities"), list) or len(task["activities"]) > 1000:
            raise ValueError("Invalid diagnostic task structure")
        for event in task["activities"]:
            if not isinstance(event, dict):
                raise ValueError("Invalid diagnostic event structure")
            if event.get("action") != "unknown":
                continue
            membership = event.get("in_report_window")
            if membership is not True:
                if membership is not False:
                    unknown_window += 1
                continue
            considered += 1
            fingerprint = event.get("action_fingerprint")
            digest = None
            if _fingerprint_digest(fingerprint) is not None:
                digest = _fingerprint_digest(fingerprint)
            # The legacy export has one explicitly allowed unsupported token.
            elif event.get("action_token") == "update_tasklist":
                digest = action_fingerprint("update_tasklist")["sha256"]
            if digest is None:
                unidentified += 1
            elif digest in by_digest:
                item = counts[by_digest[digest]]
                item["events"] += 1
                item["tasks"].add(index)
    return {
        "unknown_events_considered": considered,
        "unknown_window_events": unknown_window,
        **_counter_labels(considered, unknown_window),
        "unidentified_events": unidentified,
        "matches": [{"candidate": candidate, "event_count": value["events"],
                     "task_count": len(value["tasks"]), "semantics_verified": False}
                    for candidate, value in counts.items() if value["events"]],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线匹配诊断动作指纹，不验证动作含义或调用网络。")
    parser.add_argument("--input", required=True, help="脱敏 JSON 路径")
    parser.add_argument("--action", action="append", required=True, help="候选协议名称，可重复")
    args = parser.parse_args(argv)
    try:
        with Path(args.input).open("rb") as stream:
            content = stream.read(_MAX_FILE_BYTES + 1)
        if len(content) > _MAX_FILE_BYTES:
            raise ValueError("Size limit")
        shape = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        matches = match_action_candidates(shape, args.action)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        print("诊断文件或候选类型无效、格式不支持或超过读取上限；没有输出文件内容。", file=sys.stderr)
        return 2
    print(json.dumps(matches, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


