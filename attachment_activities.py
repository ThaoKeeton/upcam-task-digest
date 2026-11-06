"""Validate evidenced customfield file references for local audit only.

No file is opened, fetched, previewed or interpreted. The returned payload and
names remain local evidence; callers select only reference identity semantics.
Event identity, source mapping and report-window validation belong to callers.
"""

import json
import re
import unicodedata


MAX_CONTENT = 65536
_ACTIONS = frozenset({"upload.customfield.work", "customfield.create.v2"})
_UPLOAD_KEYS = frozenset({"customfield", "works"})
_WORK_KEYS = frozenset({"_id", "fileName", "meta"})
_BINDING_KEYS = frozenset({"boundToObjectType", "boundToObjectId", "scope", "scopeId"})
_FIELD_KEYS = frozenset({"customfieldId", "customfieldName", "oldValue", "oldValueString",
                         "value", "valueString", "secretLevel"})
_FILE_KEYS = frozenset({"_id", "fileName", "fileSize", "thumbnail", "thumbnailUrl",
                        "fileCategory", "title", "source", "meta", "secretLevel"})
_UNSUPPORTED_OUTER = frozenset({
    "payload", "attachments", "dingfiles", "files", "images", "data", "hrefpreview",
    "thumbnail", "thumbnailurl", "customfield", "customfieldid", "customfieldname",
    "fieldid", "filename", "filenames", "fileid", "fileids", "filemetadata",
    "works", "value", "valuestring", "oldvalue", "oldvaluestring", "parsedpayload",
    "quote", "url", "href", "preview", "previews", "meta",
})


def _object(value, keys):
    return isinstance(value, dict) and set(value) == keys


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonstandard JSON constant")


def _bounded_object(content):
    if not isinstance(content, str):
        return None
    try:
        if len(content.encode("utf-8")) > MAX_CONTENT:
            return None
        data = json.loads(content, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _identifier(value, *, fixed_length=False):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))
            and (not fixed_length or len(value) == 24))


def _text(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(unicodedata.category(char) in ("Cc", "Cf") for char in value)):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _filename(value):
    # A reference name is never an authority to follow a local path or URL.
    # Reject nested percent-encoded separators without recursive decoding.
    return (_text(value) and value not in (".", "..")
            and not any(char in value for char in "/\\:")
            and re.search(r"%(?:25)*(?:2f|5c|3a)", value, re.IGNORECASE) is None)


def _upload(data, task_id):
    if (not _object(data, _UPLOAD_KEYS) or not _object(data["customfield"], {"type"})
            or data["customfield"]["type"] != "work"
            or not isinstance(data["works"], list) or not data["works"]):
        return None
    field_id, names = None, {}
    for work in data["works"]:
        if (not _object(work, _WORK_KEYS)
                or not _identifier(work["_id"], fixed_length=True)
                or not _filename(work["fileName"])
                or not _object(work["meta"], {"boundToObjectType", "payload"})
                or work["meta"]["boundToObjectType"] != "file"):
            return None
        binding = work["meta"]["payload"]
        if (not _object(binding, _BINDING_KEYS)
                or binding["boundToObjectType"] != "customfield"
                or not _identifier(binding["boundToObjectId"], fixed_length=True)
                or binding["scope"] != "task" or binding["scopeId"] != task_id):
            return None
        if field_id is not None and binding["boundToObjectId"] != field_id:
            return None
        field_id = binding["boundToObjectId"]
        if work["_id"] in names:
            return None
        names[work["_id"]] = work["fileName"]
    return field_id, names


def _customfield(data):
    if (not _object(data, _FIELD_KEYS)
            or not _identifier(data["customfieldId"], fixed_length=True)
            or not _text(data["customfieldName"])
            or data["oldValue"] != [] or data["oldValueString"] != ""
            or type(data["secretLevel"]) is not int
            or not isinstance(data["value"], list) or len(data["value"]) != 1):
        return None
    # Only the observed single-name valueString has established semantics.
    # Do not invent a delimiter for multi-file customfield values.
    file = data["value"][0]
    if (not _object(file, _FILE_KEYS)
            or not _identifier(file["_id"], fixed_length=True)
            or not _filename(file["fileName"])
            or type(file["fileSize"]) is not int or file["fileSize"] < 0
            or file["thumbnail"] is not None or file["thumbnailUrl"] is not None
            or file["fileCategory"] != "application" or file["source"] != "awos"
            or file["title"] != file["fileName"] or data["valueString"] != file["fileName"]
            or not _object(file["meta"], {"boundToObjectType"})
            or file["meta"]["boundToObjectType"] != "file"
            or type(file["secretLevel"]) is not int):
        return None
    return data["customfieldId"], {file["_id"]: file["fileName"]}


def attachment_change(raw, config=None):
    """Return exact file-reference evidence, or None for any unsupported shape.

    ``config`` is accepted for adapter-call compatibility and cannot widen the
    evidenced protocol. Metadata in ``parsed_payload`` and ``file_names`` is
    local audit evidence only; no author progress or file-content fact follows.
    Keep distinct raw events. A task-level consumer may union their ``file_ids``
    after checking each repeated ID agrees on task, field and file name.
    """
    if not isinstance(raw, dict):
        return None
    # Ordinary collector envelope fields are validated by the caller. A second
    # attachment payload or preview has no evidenced relationship to content.
    if any(not isinstance(key, str)
           or key.lower().replace("_", "").replace("-", "") in _UNSUPPORTED_OUTER
           for key in raw):
        return None
    action = raw.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        return None
    task_id = raw.get("boundToObjectId")
    if (raw.get("boundToObjectType") != "task" or not _identifier(task_id)
            or ("taskId" in raw and raw["taskId"] != task_id)):
        return None
    data = _bounded_object(raw.get("content"))
    if data is None:
        return None
    parsed = _upload(data, task_id) if action == "upload.customfield.work" else _customfield(data)
    if parsed is None:
        return None
    field_id, names = parsed
    return {"pattern": "customfield-file-reference", "field_id": field_id,
            "file_ids": list(names), "file_names": names, "parsed_payload": data,
            "completion_evidence": False, "needs_review": False}
