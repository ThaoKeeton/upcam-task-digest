"""Opt-in, private project evidence with bounded credential removal.

This pure transformation preserves business text for diagnosis. It is not full
text anonymization and its output is not a digest or a sendable report. No file,
environment, network, link preview, or attachment access takes place here.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlencode, urlsplit, urlunsplit


MAX_DETAILED_BYTES = 16 * 1024 * 1024
MAX_STRING_BYTES = 256 * 1024
MAX_TASKS = 700
MAX_ACTIVITIES = 50000
MAX_DEPTH = 16
MAX_NODES = 2000000
MAX_UNIT_NODES = 20000
MAX_UNIT_BYTES = 1024 * 1024
MAX_JSON_LAYERS = 4
_MAX_CREDENTIAL_BYTES = 128 * 1024
_MAX_CREDENTIAL_NODES = 2048
_MAX_VARIANTS = 4096
_MAX_VARIANT_BYTES = 2 * 1024 * 1024
_MAX_REFERENCED_MAPPINGS = 4096
_ERROR = "详细诊断无法安全生成，请重新运行只读诊断。"
_REDACTED = "[REDACTED]"

_TASK_FIELDS = frozenset({
    "id", "_id", "taskId", "projectId", "content", "executorId", "tfsId", "isDone",
    "isArchived", "created", "updated", "tasklistId", "stageId", "scenariofieldconfigId", "visible", "parentTaskId",
})
_EVENT_FIELDS = frozenset({
    "id", "_id", "taskId", "action", "boundToObjectId", "boundToObjectType", "createTime", "content",
    "creatorId", "updateTime",
    "attachments", "dingFiles", "files", "images", "data", "mentions", "renderMode",
    "isOnlyNotifyMentions", "quote", "hrefPreview", "payload", "provider", "source", "sourceId",
    "appId", "integration", "integrationId", "repository", "repositoryId", "repositoryUrl",
    "repo", "repoId", "repoUrl", "commit", "commitId", "commitHash", "sha", "hash", "url",
    "objectLink", "objectLinkId",
})
_SCOPE_FIELDS = frozenset({
    "tenant_id", "project_id", "include_archived", "batch_id", "collected_at", "pagination_complete",
})
_SELECTION_FIELDS = frozenset({
    "mode", "view_equivalent", "date", "start", "end_exclusive", "q", "basis",
    "historical_event_coverage", "window_kind",
})
_DIAGNOSTIC_FIELDS = frozenset({
    "id", "created_at_utc", "application_version", "ruleset_revision", "capabilities",
})
_CONFIG_FIELDS = frozenset({
    "tenant_id", "project_id", "include_archived", "api_contract_verified", "action_rules",
})
_FILTER_FIELDS = frozenset({
    "executorId", "tasklistId", "stageId", "tfsId", "scenariofieldconfigId", "visible", "parentTaskId",
})
_OBSERVED_ACTIONS = frozenset({
    "create", "comment", "involve.add", "update.taskflowstatus", "standard", "update_content", "update_executor",
    "move.cross.from.stage.to.stage",
})
_CREDENTIAL_NAMES = frozenset({
    "authorization", "proxyauthorization", "authentication", "auth", "cookie", "setcookie",
    "token", "accesstoken", "refreshtoken", "idtoken", "authtoken", "bearertoken", "apikey",
    "password", "passwd", "appsecret", "clientsecret", "privatekey", "webhook", "signingsecret",
    "signingkey", "credential", "credentials", "headers", "secret", "secrets", "settings",
    "appid", "clientid", "httpresponse", "requestheaders", "responseheaders",
})
_CREDENTIAL_SUFFIXES = tuple(sorted(_CREDENTIAL_NAMES))
_AUTH_QUERY_NAMES = frozenset({"sign", "signature", "sig", "key", "auth", "jwt", "session", "sessionid"})
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+")
_AUTH_SCHEME = re.compile(r"(?i)\b(?:Bearer|Basic)\s+")
_API_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|"
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|AIza[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"SEC[0-9A-Fa-f]{64})(?![A-Za-z0-9])"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
_PRIVATE_KEY_BLOCK = re.compile(r"(?i)-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_AUTH_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:authorization|proxy[-_ ]*authorization|authentication|cookie|set[-_ ]*cookie|"
    r"(?:access|refresh|id|auth|bearer)[-_ ]*token|token|api[-_ ]*key|password|passwd|"
    r"app[-_ ]*(?:secret|id)|client[-_ ]*(?:secret|id)|private[-_ ]*key|signing[-_ ]*(?:secret|key)|"
    r"credentials?|webhook)[\"']?\s*[:=]"
)
_AUTH_MARKUP = re.compile(r"(?i)<\s*/?\s*(?:authorization|cookie|token|password|credentials?|api[-_]*key|private[-_]*key)\b")
_ASSIGNMENT_KEY = re.compile(r"(?<![\w.%-])([\w.%-]+)[\"']?\s*[:=]")
_MARKUP_KEY = re.compile(r"<\s*/?\s*([\w.:-]+)\b")
_JSON_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})')


class DetailedDiagnosticError(ValueError):
    """A fixed, value-free error for an unverifiable or unbounded export."""


def _fail():
    raise DetailedDiagnosticError(_ERROR)


def _string_bytes(value):
    if type(value) is not str or len(value) > MAX_STRING_BYTES:
        _fail()
    size = len(value.encode("utf-8", errors="strict"))
    if size > MAX_STRING_BYTES:
        _fail()
    return size


def _canonical_key(value):
    _string_bytes(value)
    for _ in range(MAX_JSON_LAYERS):
        decoded = _json_unescape_view(unquote(value, errors="strict"))
        if decoded == value:
            break
        value = decoded
    if _json_unescape_view(unquote(value, errors="strict")) != value:
        _fail()
    _string_bytes(value)
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _credential_key(value, *, query=False):
    name = _canonical_key(value)
    return (name in _CREDENTIAL_NAMES or name.endswith(_CREDENTIAL_SUFFIXES)
            or (query and (name in _AUTH_QUERY_NAMES or name.endswith(("signature", "credential")))))


def _has_authentication_label(value):
    if _AUTH_LABEL.search(value) or _AUTH_MARKUP.search(value):
        return True
    return any(_credential_key(match.group(1))
               for pattern in (_ASSIGNMENT_KEY, _MARKUP_KEY) for match in pattern.finditer(value))


def _json_unescape_view(value):
    """An inspection view only; never interpret arbitrary prose as valid JSON."""
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def decode(match):
        token = match.group()[1:]
        return chr(int(token[1:], 16)) if token.startswith("u") else escapes[token]

    return _JSON_ESCAPE.sub(decode, value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _fail()
    return number


def _invalid_constant(_value):
    _fail()


def _strict_json(value):
    """Bound nesting before json.loads allocates nested Python containers."""
    _string_bytes(value)
    depth, tokens, quoted, escaped = 0, 0, False, False
    for char in value:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            tokens += 1
        elif char in "[{":
            depth += 1
            tokens += 1
            if depth > MAX_DEPTH:
                _fail()
        elif char in "]}":
            depth -= 1
        elif char in ",:":
            tokens += 1
        if tokens > MAX_UNIT_NODES:
            _fail()
    return json.loads(value, object_pairs_hook=_unique_object, parse_float=_finite_float,
                      parse_constant=_invalid_constant)


def _percent_case(value):
    return re.sub(r"%[0-9A-Fa-f]{2}", lambda m: m.group().lower(), value)


def _literal_secret_pattern(value):
    # Hex digits may vary independently in otherwise identical JSON/URL escapes.
    parts, position = [], 0
    for match in re.finditer(r"\\u([0-9A-Fa-f]{4})|%([0-9A-Fa-f]{2})", value):
        digits = match.group(1) or match.group(2)
        parts.append(re.escape(value[position:match.start()]))
        parts.append(re.escape(match.group()[:-len(digits)]))
        parts.extend("[" + digit.lower() + digit.upper() + "]" if digit.isalpha() else digit for digit in digits)
        position = match.end()
    parts.append(re.escape(value[position:]))
    return "".join(parts)


def _sensitive_url_in_text(value):
    for match in _URL.finditer(value):
        parsed = urlsplit(match.group())
        if parsed.username is not None or parsed.password is not None:
            return True
        if parsed.hostname and parsed.hostname.casefold() in {"oapi.dingtalk.com", "api.dingtalk.com"} and "/robot/" in parsed.path:
            return True
        if any(_credential_key(key, query=True) for key, _ in
               parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256, errors="strict")):
            return True
    return False


def _known_pattern(credentials):
    if type(credentials) is not dict:
        _fail()
    raw_values, active = set(), set()
    nodes, byte_count = 0, 0

    def collect(value, depth=0):
        nonlocal nodes, byte_count
        nodes += 1
        if depth > 8 or nodes > _MAX_CREDENTIAL_NODES:
            _fail()
        if type(value) is str:
            byte_count += _string_bytes(value)
            if byte_count > _MAX_CREDENTIAL_BYTES:
                _fail()
            if value:
                raw_values.add(value)
                if "://" in value:
                    parsed = urlsplit(value)
                    for key, secret in parse_qsl(parsed.query, max_num_fields=256, errors="strict"):
                        if secret and _credential_key(key, query=True):
                            raw_values.add(secret)
                    for secret in (parsed.username, parsed.password):
                        if secret:
                            raw_values.add(unquote(secret, errors="strict"))
        elif type(value) in (dict, list):
            marker = id(value)
            if marker in active:
                _fail()
            active.add(marker)
            for item in value.values() if type(value) is dict else value:
                collect(item, depth + 1)
            active.remove(marker)
        elif value is not None:
            _fail()

    collect(credentials)
    variants = set()
    for secret in raw_values:
        seeds = {secret, unquote(secret, errors="strict"), unquote_plus(secret, errors="strict")}
        for _ in range(2):
            expanded = set(seeds)
            for item in seeds:
                expanded.update((quote(item, safe=""), quote_plus(item),
                                 json.dumps(item, ensure_ascii=True)[1:-1],
                                 json.dumps(item, ensure_ascii=False)[1:-1], item.replace("/", "\\/")))
            seeds = expanded
            if len(seeds) > 256:
                _fail()
        variants.update(value for value in seeds if value)
        variants.update(_percent_case(value) for value in seeds if value)
        if len(variants) > _MAX_VARIANTS:
            _fail()
    if sum(len(v.encode("utf-8")) for v in variants) > _MAX_VARIANT_BYTES:
        _fail()
    pattern = re.compile("|".join(_literal_secret_pattern(v) for v in sorted(variants, key=lambda v: (-len(v), v)))) if variants else None
    return pattern, len(raw_values)


class _Scrubber:
    def __init__(self, credentials):
        self.known, self.known_count = _known_pattern(credentials)
        self.nodes = self.unit_nodes = self.unit_bytes = 0
        self.active = set()
        self.stats = {"removed_fields": 0, "known_value_replacements": 0,
                      "authentication_format_replacements": 0, "url_credentials_removed": 0,
                      "json_strings_reencoded": 0, "encoded_strings_normalized": 0}

    def known_text(self, value):
        if self.known is None:
            return value
        output, count = self.known.subn(_REDACTED, value)
        self.stats["known_value_replacements"] += count
        return output

    def clean(self, value):
        self.unit_nodes = self.unit_bytes = 0
        return self.walk(value)

    def charge_string(self, value):
        self.unit_bytes += _string_bytes(value)
        if self.unit_bytes > MAX_UNIT_BYTES:
            _fail()

    def walk(self, value, depth=0, layers=0):
        self.nodes += 1
        self.unit_nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES or self.unit_nodes > MAX_UNIT_NODES:
            _fail()
        kind = type(value)
        if kind is str:
            return self.text(value, depth, layers)
        if value is None or kind in (bool, int):
            return value
        if kind is float:
            if not math.isfinite(value):
                _fail()
            return value
        if kind not in (dict, list):
            _fail()
        marker = id(value)
        if marker in self.active:
            _fail()
        self.active.add(marker)
        try:
            if kind is list:
                if len(value) > MAX_UNIT_NODES:
                    _fail()
                return [self.walk(item, depth + 1, layers) for item in value]
            if len(value) > MAX_UNIT_NODES:
                _fail()
            output = {}
            for key, item in value.items():
                if type(key) is not str:
                    _fail()
                self.charge_string(key)
                if _credential_key(key) or self.key_has_authentication(key):
                    self.stats["removed_fields"] += 1
                    continue
                output[key] = self.walk(item, depth + 1, layers)
            return output
        finally:
            self.active.remove(marker)

    def key_has_authentication(self, key):
        _string_bytes(key)
        if self.known is not None and self.known.search(key):
            self.stats["known_value_replacements"] += 1
            return True
        # Keys are never rewritten: removal prevents key collisions after redaction.
        return self.text(key, 0, 0, parse_json=False) != key

    def text(self, value, depth, layers, *, parse_json=True):
        self.charge_string(value)
        if depth > MAX_DEPTH:
            _fail()
        if parse_json and value.lstrip().startswith(("{", "[", '"')):
            if layers >= MAX_JSON_LAYERS:
                _fail()
            parsed = _strict_json(value)
            cleaned = self.walk(parsed, depth + 1, layers + 1)
            output = json.dumps(cleaned, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            _string_bytes(output)
            if output != value:
                self.stats["json_strings_reencoded"] += 1
            return output

        # A percent-encoded authentication envelope must not bypass field checks.
        decoded = value
        inspection_views = [value]
        for _ in range(3):
            newer = unquote(decoded, errors="strict")
            if newer == decoded:
                break
            decoded = newer
            self.charge_string(decoded)
            inspection_views.append(decoded)
        if unquote(decoded, errors="strict") != decoded:
            _fail()
        # Check every decoded layer, including embedded escaped JSON in prose.
        # A matching inspection view is omitted whole because its string boundary
        # is not a validated JSON envelope.
        for view in inspection_views:
            if view != value and self.known is not None and self.known.search(view):
                self.stats["known_value_replacements"] += 1
                self.stats["encoded_strings_normalized"] += 1
                return _REDACTED
            escaped_view = view
            for _ in range(MAX_JSON_LAYERS):
                newer = _json_unescape_view(escaped_view)
                if newer == escaped_view:
                    break
                escaped_view = newer
                self.charge_string(escaped_view)
                if self.known is not None and self.known.search(escaped_view):
                    self.stats["known_value_replacements"] += 1
                    self.stats["encoded_strings_normalized"] += 1
                    return _REDACTED
                normalized_view = unicodedata.normalize("NFKC", escaped_view)
                if (_has_authentication_label(normalized_view) or _AUTH_SCHEME.search(normalized_view)
                        or _API_KEY.search(normalized_view) or _JWT.search(normalized_view)
                        or _PRIVATE_KEY_BLOCK.search(normalized_view)):
                    self.stats["authentication_format_replacements"] += 1
                    self.stats["encoded_strings_normalized"] += 1
                    return _REDACTED
            if _json_unescape_view(escaped_view) != escaped_view:
                _fail()
        normalized = unicodedata.normalize("NFKC", decoded)
        if decoded != value and (decoded.lstrip().startswith(("{", "[", '"'))
                                 or _has_authentication_label(normalized) or _AUTH_SCHEME.search(normalized)
                                 or _API_KEY.search(normalized)
                                 or _JWT.search(normalized) or _PRIVATE_KEY_BLOCK.search(normalized)
                                 or _sensitive_url_in_text(normalized)
                                 or (self.known is not None and self.known.search(decoded))):
            if layers >= MAX_JSON_LAYERS:
                _fail()
            self.stats["encoded_strings_normalized"] += 1
            return self.text(decoded, depth + 1, layers + 1, parse_json=parse_json)

        for match in _URL.finditer(value):
            if match.end() < len(value) and _sensitive_url_in_text(match.group()):
                # A URL token ending at a quote/space can leave a secret suffix
                # after its query label is removed. Omit the complete string.
                self.stats["authentication_format_replacements"] += 1
                return _REDACTED
        output = _URL.sub(lambda m: self.url(m.group(), depth + 1, layers), value)
        normalized = unicodedata.normalize("NFKC", output)
        if (_has_authentication_label(normalized) or _AUTH_SCHEME.search(normalized)
                or _PRIVATE_KEY_BLOCK.search(normalized)):
            # Arbitrary prose is not a reliable assignment parser. Drop the whole
            # string rather than retaining a suffix of a quoted/multiline secret.
            self.stats["authentication_format_replacements"] += 1
            return _REDACTED
        if _API_KEY.search(normalized) or _JWT.search(normalized):
            output = normalized
            for pattern in (_API_KEY, _JWT):
                output, count = pattern.subn(_REDACTED, output)
                self.stats["authentication_format_replacements"] += count
        return self.known_text(output)

    def url(self, value, depth, layers):
        if depth > MAX_DEPTH:
            _fail()
        parsed = urlsplit(value)
        if not parsed.hostname:
            _fail()
        # A robot webhook is itself a credential, including unconfigured webhooks.
        if parsed.hostname.casefold() in {"oapi.dingtalk.com", "api.dingtalk.com"} and "/robot/" in parsed.path:
            self.stats["url_credentials_removed"] += 1
            return _REDACTED
        _ = parsed.port  # Reject malformed ports without putting the URL in errors.
        changed = False
        netloc = parsed.netloc
        if parsed.username is not None or parsed.password is not None:
            netloc = netloc.rsplit("@", 1)[-1]
            self.stats["url_credentials_removed"] += 1
            changed = True
        query = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256, errors="strict"):
            if _credential_key(key, query=True) or self.key_has_authentication(key):
                self.stats["url_credentials_removed"] += 1
                changed = True
                continue
            cleaned = self.text(item, depth + 1, layers)
            if cleaned != item:
                changed = True
            query.append((key, cleaned))
        fragment = self.text(parsed.fragment, depth + 1, layers) if parsed.fragment else ""
        if fragment != parsed.fragment:
            changed = True
        if not changed:
            return value
        return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), fragment))

    def verify_known_absent(self, value):
        if type(value) is str:
            if self.known is not None and self.known.search(value):
                _fail()
        elif type(value) is dict:
            for key, item in value.items():
                self.verify_known_absent(key)
                self.verify_known_absent(item)
        elif type(value) is list:
            for item in value:
                self.verify_known_absent(item)


def _project(value, fields):
    if type(value) is not dict:
        _fail()
    # Membership lookup keeps oversized unrelated source objects out of traversal.
    return {key: value[key] for key in sorted(fields) if key in value}


def _instant(value):
    if type(value) is not str or len(value) > 128:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _bounds(metadata):
    if type(metadata) is not dict or type(metadata.get("report_window")) is not dict:
        _fail()
    window = metadata["report_window"]
    start, end = _instant(window.get("start")), _instant(window.get("end_exclusive"))
    if start is None or end is None or start >= end:
        _fail()
    return start, end


def _json_size(value, *, extra_indent=0, ceiling=None):
    size = extra_indent + 2
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2)
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8", errors="strict")) + chunk.count("\n") * extra_indent
        if ceiling is not None and size > ceiling:
            return size
    return size


def _references(value, owners, statuses, depth=0):
    if depth > MAX_DEPTH:
        _fail()
    if type(value) is dict:
        for key, item in value.items():
            if type(item) is str and key in {"executorId", "_executorId", "_oldExecutorId"}:
                owners.setdefault(item, None)
            elif type(item) is str and key in {"tfsId", "oldTfsId"}:
                statuses.setdefault(item, None)
            if type(item) in (dict, list):
                _references(item, owners, statuses, depth + 1)
            elif (type(item) is str and item != _REDACTED and key in {"content", "quote"}
                  and item.lstrip().startswith(("{", "[", '"'))):
                _references(_strict_json(item), owners, statuses, depth + 1)
    elif type(value) is list:
        for item in value:
            _references(item, owners, statuses, depth + 1)


def _runtime_config(config, scrubber, records, omissions, reasons):
    base = _project(config, _CONFIG_FIELDS)
    if "activity_fields" in config:
        base["activity_fields"] = _project(config["activity_fields"], {"time", "action"})
    if "view" in config:
        view = _project(config["view"], {"verified", "evidence"})
        if "task_equals" in config["view"]:
            view["task_equals"] = _project(config["view"]["task_equals"], _FILTER_FIELDS)
        base["view"] = view
    runtime = scrubber.clean(base)
    owners, statuses = {}, {}
    for record in records:
        _references(record["task"], owners, statuses)
    for record in records:
        for event in record["activities"]:
            _references(event, owners, statuses)
    resolved = config.get("resolved_status_ids", [])
    if type(resolved) is not list or len(resolved) > MAX_ACTIVITIES:
        _fail()
    # Keep duplicates and unmapped IDs: they can explain a source validation
    # failure. Their mapped entries are needed to avoid inventing new failures.
    for value in resolved:
        if type(value) is str:
            statuses.setdefault(value, None)
    mapping_budget = max(256, MAX_DETAILED_BYTES // 16)
    charged = 0
    for name, references in (("owner_names", owners), ("status_names", statuses)):
        source = config.get(name, {})
        if type(source) is not dict:
            _fail()
        output = {}
        for index, key in enumerate(references):
            if key not in source:
                continue
            if index >= _MAX_REFERENCED_MAPPINGS:
                omissions["runtime_mapping_entries"] += 1
                reasons.add("mapping_count_limit")
                continue
            cleaned = scrubber.clean({key: source[key]})
            cost = _json_size(cleaned, extra_indent=4)
            if charged + cost > mapping_budget:
                omissions["runtime_mapping_entries"] += 1
                reasons.add("mapping_byte_budget")
                continue
            output.update(cleaned)
            charged += cost
        runtime[name] = output
    runtime["resolved_status_ids"] = scrubber.clean(resolved)
    return runtime


def _build(collection, config, metadata, credentials):
    if type(collection) is not dict or type(config) is not dict:
        _fail()
    start, end = _bounds(metadata)
    scrubber = _Scrubber(credentials)
    diagnostic = _project(metadata, _DIAGNOSTIC_FIELDS)
    diagnostic["report_window"] = _project(metadata["report_window"], {"start", "end_exclusive", "timezone"})
    diagnostic = scrubber.clean(diagnostic)
    scope = _project(collection, _SCOPE_FIELDS)
    if "selection" in collection:
        scope["selection"] = _project(collection["selection"], _SELECTION_FIELDS)
    scope = scrubber.clean(scope)
    source_records = collection.get("records")
    if type(source_records) is not list or len(source_records) > MAX_TASKS:
        _fail()
    total_events = 0
    candidates, task_priorities = [], {}
    outside = invalid_time = in_window = malformed = 0
    for task_index, record in enumerate(source_records):
        if type(record) is not dict or type(record.get("task")) is not dict or type(record.get("activities")) is not list:
            _fail()
        events = record["activities"]
        total_events += len(events)
        if total_events > MAX_ACTIVITIES:
            _fail()
        task_priorities[task_index] = 3
        for event_index, event in enumerate(events):
            if type(event) is not dict:
                malformed += 1
                continue
            instant = _instant(event.get("createTime"))
            if instant is not None and not start <= instant < end:
                outside += 1
                continue
            if instant is None:
                invalid_time += 1
                priority = 0
            else:
                in_window += 1
                action = event.get("action")
                priority = 1 if type(action) is not str or action not in _OBSERVED_ACTIONS else 2
            candidates.append((priority, task_index, event_index))
            task_priorities[task_index] = min(task_priorities[task_index], priority)

    omissions = {"tasks": 0, "activities": malformed, "runtime_mapping_entries": 0,
                 "malformed_activities": malformed, "reasons": []}
    reasons = {"invalid_activity_record"} if malformed else set()
    records, by_index = [], {}
    envelope_reserve = max(2048, min(64 * 1024, MAX_DETAILED_BYTES // 8))
    available = MAX_DETAILED_BYTES - envelope_reserve - max(256, MAX_DETAILED_BYTES // 16)
    if available <= 0:
        _fail()
    charged = 0
    task_budget = max(512, available // 4)
    for index in sorted(task_priorities, key=lambda i: (task_priorities[i], i)):
        task = scrubber.clean(_project(source_records[index]["task"], _TASK_FIELDS))
        record = {"task": task, "activities": []}
        cost = _json_size(record, extra_indent=4) + 32
        if charged + cost > task_budget:
            omissions["tasks"] += 1
            reasons.add("task_byte_budget")
            continue
        charged += cost
        records.append(record)
        by_index[index] = record
    accepted = []
    for _priority, task_index, event_index in sorted(candidates):
        if task_index not in by_index:
            omissions["activities"] += 1
            continue
        if charged + 64 >= available:
            omissions["activities"] += 1
            reasons.add("event_byte_budget")
            continue
        event = scrubber.clean(_project(source_records[task_index]["activities"][event_index], _EVENT_FIELDS))
        cost = _json_size(event, extra_indent=8) + 64
        if charged + cost > available:
            omissions["activities"] += 1
            reasons.add("event_byte_budget")
            continue
        charged += cost
        by_index[task_index]["activities"].append(event)
        accepted.append((by_index[task_index], event))
    runtime = _runtime_config(config, scrubber, records, omissions, reasons)
    result = {
        "format": "upcam-teambition-detailed-diagnostic-v1", "contains_project_content": True,
        "sendable": False, "data_altered": True, "diagnostic": diagnostic, "scope": scope,
        "runtime_config": runtime, "records": records,
        "counts": {"tasks": len(source_records), "activities": total_events,
                   "exported_tasks": len(records), "exported_activities": len(accepted),
                   "in_window_activities": in_window, "invalid_time_activities": invalid_time,
                   "out_of_window_activities": outside},
        "omissions": omissions,
        "credential_redaction": {"performed": True, "full_text_anonymized": False,
                                 "known_secret_count": scrubber.known_count, **scrubber.stats},
    }
    omissions["reasons"] = sorted(reasons)
    # Incremental charges prevent building a giant export and then cutting it.
    # This strict final check also accounts for pretty JSON and a file newline.
    while _json_size(result, ceiling=MAX_DETAILED_BYTES) > MAX_DETAILED_BYTES:
        reasons.add("json_byte_budget")
        if accepted:
            for _ in range(max(1, len(accepted) // 2)):
                record, event = accepted.pop()
                # Entries were appended in priority order; keep difficult events first.
                if record["activities"][-1] is not event:
                    _fail()
                record["activities"].pop()
                omissions["activities"] += 1
            result["counts"]["exported_activities"] = len(accepted)
        elif records:
            records.pop()
            omissions["tasks"] += 1
            result["counts"]["exported_tasks"] = len(records)
        else:
            _fail()
        omissions["reasons"] = sorted(reasons)
    scrubber.verify_known_absent(result)
    return result


def build_detailed_snapshot(collection: dict, config: dict, diagnostic_metadata: dict, *, credentials: dict) -> dict:
    """Return bounded private evidence, preserving content after credential removal.

    A timezone-aware report_window is mandatory. Only its half-open range and
    events whose timestamps cannot be assigned are eligible for body export.
    All exceptions deliberately exclude source values, URLs, keys, and paths.
    """
    try:
        return _build(collection, config, diagnostic_metadata, credentials)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, AttributeError):
        raise DetailedDiagnosticError(_ERROR) from None
