"""Select formal task content from locally validated structured evidence."""

import re


class FormalContentError(ValueError):
    """Formal content evidence has an invalid local shape."""


def _file_count(summary_entry: dict | None) -> int:
    if summary_entry is None:
        return 0
    if not isinstance(summary_entry, dict) or not isinstance(summary_entry.get("comments", []), list):
        raise FormalContentError("Invalid formal comment evidence")
    file_ids = set()
    for comment in summary_entry.get("comments", []):
        if not isinstance(comment, dict):
            raise FormalContentError("Invalid formal comment evidence")
        if comment.get("extraction_needs_review") is not False:
            continue
        payload, extra = comment.get("payload_evidence", {}), comment.get("extra_evidence", {})
        if not isinstance(payload, dict) or not isinstance(extra, dict):
            raise FormalContentError("Invalid formal file evidence")
        sources = [source["files"] for source in (payload, extra) if "files" in source]
        for files in sources:
            if (not isinstance(files, list)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", value)
                           for value in files)
                    or len(files) != len(set(files))):
                raise FormalContentError("Invalid formal file evidence")
        if len(sources) == 2 and sources[0] != sources[1]:
            raise FormalContentError("Conflicting formal file evidence")
        # Prefer the payload when present, including an explicit empty list.
        if sources:
            file_ids.update(sources[0])
    return len(file_ids)


def formal_task_content(audit: dict, summary_entry: dict | None,
                        proposed: str, has_substantive: bool) -> dict:
    """Return category and summary without changing source or review evidence.

    The caller validates the source, hard review flags and model summary before
    selection. Diagnostic strings, notice text and location names are never
    consulted here; their evidence remains in the caller's audit.
    """
    if not isinstance(audit, dict) or not isinstance(proposed, str) or type(has_substantive) is not bool:
        raise FormalContentError("Invalid formal content inputs")
    counts = audit.get("event_counts", {})
    transitions = audit.get("status_transitions", [])
    changes = audit.get("parsed_changes", [])
    if (not isinstance(counts, dict)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or not isinstance(transitions, list)
            or any(not isinstance(change, dict) or type(change.get("resolved")) is not bool
                   for change in transitions)
            or not isinstance(changes, list)
            or any(not isinstance(change, dict) for change in changes)):
        raise FormalContentError("Invalid formal task evidence")

    if transitions and transitions[-1]["resolved"] is True:
        category = "resolved"
    elif has_substantive and proposed:
        category = "progress"
    elif counts.get("created", 0) > 0:
        category = "new"
    else:
        category = "other"

    pieces = [proposed] if proposed else []
    for change in changes:
        if change.get("action") != "update_executor" or change.get("needs_review") is not False:
            continue
        previous, target = change.get("previous_name"), change.get("target_name")
        if any(not isinstance(name, str) or not name.strip() or name != name.strip()
               or any(ord(char) < 32 or char in "\x7f\u2028\u2029" for char in name)
               for name in (previous, target)):
            raise FormalContentError("Invalid formal owner evidence")
        pieces.append(f"执行人变更：{previous} → {target}")
    count = _file_count(summary_entry)
    if count:
        pieces.append(f"评论附有 {count} 个文件，未读取文件内容")
    return {"category": category, "summary": "；".join(pieces)}
