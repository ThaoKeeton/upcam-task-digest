"""Conservative local forms for explicit DingTalk mention identifiers.

These forms do not establish DingTalk account validity or group membership.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit


MAX_MENTION_TARGETS = 50
MAX_MENTION_INPUT_CHARS = 32768
_PATTERNS = {
    "mobile": re.compile(r"\+?[0-9]{6,20}\Z"),
    "user_id": re.compile(r"[A-Za-z0-9_.-]{1,128}\Z"),
}
_SEPARATORS = re.compile(r"[,; \t\r\n]+")
_CHINESE_SEPARATORS = str.maketrans({"，": ",", "；": ";"})


class MentionConfigError(ValueError):
    """A fixed safe message; supplied identifiers are never included."""


def validate_mention_targets(mode: str, targets) -> tuple[str, ...]:
    """Validate individual transport targets and freeze their distinct order."""
    if not isinstance(mode, str) or mode not in _PATTERNS:
        raise MentionConfigError("钉钉提醒类型无效；请选择手机号或钉钉 userId。")
    if not isinstance(targets, (list, tuple)) or len(targets) > MAX_MENTION_INPUT_CHARS:
        raise MentionConfigError("钉钉提醒对象格式无效；请输入明确的标识列表。")
    result, seen = [], set()
    for target in targets:
        if (not isinstance(target, str) or not _PATTERNS[mode].fullmatch(target)
                or target.casefold() == "all"):
            raise MentionConfigError("钉钉提醒对象格式无效；请填写所选类型的标识，不含 @ 前缀、姓名、链接或其他标记。")
        if target not in seen:
            seen.add(target)
            result.append(target)
            if len(result) > MAX_MENTION_TARGETS:
                raise MentionConfigError("钉钉提醒最多支持 50 个不同对象。")
    return tuple(result)


def normalize_mentions(mode: str, text: str) -> str:
    """Split supported separators and return one explicit identifier per line."""
    if not isinstance(text, str) or len(text) > MAX_MENTION_INPUT_CHARS:
        raise MentionConfigError("钉钉提醒对象内容无效或过长；请填写最多 50 个明确标识。")
    normalized = text.translate(_CHINESE_SEPARATORS)
    if any(ord(char) > 126 or ord(char) < 32 and char not in "\t\r\n" for char in normalized):
        raise MentionConfigError("钉钉提醒对象包含不支持的字符；请只填写标识并用换行、空格、逗号或分号分隔。")
    targets = [target for target in _SEPARATORS.split(normalized) if target]
    return "\n".join(validate_mention_targets(mode, targets))


def mention_options(settings: dict):
    """Freeze sender kwargs from validated local settings; empty means none."""
    mode = settings.get("dingtalk_mention_mode", "mobile")
    normalized = normalize_mentions(mode, settings.get("dingtalk_mentions", ""))
    if not normalized:
        return MappingProxyType({})
    key = "at_mobiles" if mode == "mobile" else "at_user_ids"
    return MappingProxyType({key: tuple(normalized.split("\n"))})


def reject_credential_mentions(settings: dict, *credential_sets: dict) -> None:
    """Reject complete known credentials in targets without disclosing values.

    Every provided credential set remains relevant, including replaced or
    cleared values. Query components are decoded before comparison. An ID that
    embeds a complete known value is blocked as well as an exact match.
    """
    options = mention_options(settings)
    targets = tuple(target for group in options.values() for target in group)
    if not targets:
        return
    values = set()
    for credentials in credential_sets:
        for value in credentials.values():
            if not isinstance(value, str) or not value:
                continue
            values.add(value)
            try:
                query = parse_qsl(urlsplit(value).query, keep_blank_values=False)
            except (TypeError, ValueError):
                continue
            values.update(component for key, component in query
                          if key.casefold() in {"access_token", "token", "sign", "signature"} and component)
    if any(value in target for target in targets for value in values):
        raise MentionConfigError("钉钉提醒对象不能包含本机服务凭据；请仅填写群成员标识。未保存或发送任何消息。") from None
