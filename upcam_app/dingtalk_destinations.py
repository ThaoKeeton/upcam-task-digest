"""Named DingTalk robot profiles stored inside the existing encrypted secret blob.

One selected profile is copied into the flat webhook fields used by digest and
chat sends. Switching replaces both fields together. An incomplete selection
clears them so a previous robot cannot be reused. Nothing here sends a message.
"""

from __future__ import annotations

import json
import re
import uuid

from .config import SettingsError
from .secrets import DINGTALK_DESTINATIONS_FIELD


_ROLES = frozenset(("test", "formal", "unspecified"))
_MAX_PROFILES = 8
_MAX_BLOB = 64 * 1024
_LEGACY_NAME = "原有配置"


class DestinationError(SettingsError):
    """A fixed message. Webhook values are never included."""


def _pairs(webhook: str, secret: str) -> bool:
    from .dingtalk import DingTalkConfigError, signed_webhook
    try:
        signed_webhook(webhook, secret, 1)
    except (DingTalkConfigError, TypeError, ValueError):
        return False
    return True


def _name(value: str) -> str:
    if (not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 32
            or any(ord(char) < 32 or char in "\u2028\u2029" for char in value)):
        raise DestinationError("机器人配置名称无效；请使用 1–32 个可见字符，不含换行。")
    return value


def _profiles(secrets: dict) -> list[dict]:
    raw = secrets.get(DINGTALK_DESTINATIONS_FIELD, "")
    if raw in ("", None):
        return []
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_BLOB:
        raise DestinationError("本机机器人配置已损坏；未发送消息。")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise DestinationError("本机机器人配置已损坏；未发送消息。") from None
    rows = payload.get("profiles") if isinstance(payload, dict) else None
    if payload.get("version") != 1 or not isinstance(rows, list) or len(rows) > _MAX_PROFILES:
        raise DestinationError("本机机器人配置已损坏；未发送消息。")
    cleaned = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "name", "role", "webhook", "secret"}:
            raise DestinationError("本机机器人配置已损坏；未发送消息。")
        identity, name, role = row["id"], row["name"], row["role"]
        webhook, secret = row["webhook"], row["secret"]
        if (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity)
                or identity in seen or role not in _ROLES
                or not isinstance(webhook, str) or not isinstance(secret, str)
                or len(webhook) > 2048 or len(secret) > 256):
            raise DestinationError("本机机器人配置已损坏；未发送消息。")
        _name(name)
        seen.add(identity)
        cleaned.append({"id": identity, "name": name, "role": role,
                        "webhook": webhook, "secret": secret})
    return cleaned


def _store(secrets: dict, profiles: list[dict]) -> None:
    blob = json.dumps({"version": 1, "profiles": profiles}, ensure_ascii=False, sort_keys=True)
    if len(blob.encode("utf-8")) > _MAX_BLOB:
        raise DestinationError("机器人配置过多或过长；未保存。")
    secrets[DINGTALK_DESTINATIONS_FIELD] = blob


def _selected(settings: dict, profiles: list[dict]) -> dict | None:
    identity = settings.get("dingtalk_destination_id") or ""
    return next((row for row in profiles if row["id"] == identity), None)


def _ready(row: dict | None) -> bool:
    return bool(row and _pairs(row["webhook"], row["secret"]))


def materialize(settings: dict, secrets: dict) -> None:
    """Point the flat send fields at the selected profile, or clear both."""
    chosen = _selected(settings, _profiles(secrets))
    if _ready(chosen):
        secrets["dingtalk_webhook"] = chosen["webhook"]
        secrets["dingtalk_signing_secret"] = chosen["secret"]
    elif DINGTALK_DESTINATIONS_FIELD in secrets or settings.get("dingtalk_destination_id"):
        secrets["dingtalk_webhook"] = ""
        secrets["dingtalk_signing_secret"] = ""


def ensure(settings: dict, secrets: dict) -> bool:
    """Migrate a legacy single robot and refresh the active pair. No send."""
    changed = False
    profiles = _profiles(secrets) if DINGTALK_DESTINATIONS_FIELD in secrets else []
    if DINGTALK_DESTINATIONS_FIELD not in secrets:
        webhook = secrets.get("dingtalk_webhook", "")
        secret = secrets.get("dingtalk_signing_secret", "")
        if webhook or secret:
            # Reuse a destination id already stored with this legacy pair. A new
            # worker must not mint another id and change the authorization binding.
            identity = settings.get("dingtalk_destination_id") or ""
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
                identity = uuid.uuid4().hex
            profiles = [{"id": identity, "name": _LEGACY_NAME, "role": "unspecified",
                         "webhook": webhook if isinstance(webhook, str) else "",
                         "secret": secret if isinstance(secret, str) else ""}]
            settings["dingtalk_destination_id"] = identity
            _store(secrets, profiles)
            changed = True
    identity = settings.get("dingtalk_destination_id") or ""
    if identity and not any(row["id"] == identity for row in profiles):
        settings["dingtalk_destination_id"] = ""
        changed = True
    before = (secrets.get("dingtalk_webhook", ""), secrets.get("dingtalk_signing_secret", ""))
    materialize(settings, secrets)
    if (secrets.get("dingtalk_webhook", ""), secrets.get("dingtalk_signing_secret", "")) != before:
        changed = True
    return changed


def credential_material(credentials: dict) -> dict:
    """Expose every saved robot to local redaction without changing storage."""
    expanded = dict(credentials)
    try:
        profiles = _profiles(credentials)
    except DestinationError:
        return expanded
    for index, row in enumerate(profiles):
        if row["webhook"]:
            expanded[f"dingtalk_profile_{index}_webhook"] = row["webhook"]
        if row["secret"]:
            expanded[f"dingtalk_profile_{index}_secret"] = row["secret"]
    return expanded


def public_view(settings: dict, secrets: dict) -> dict:
    profiles = _profiles(secrets) if DINGTALK_DESTINATIONS_FIELD in secrets else []
    identity = settings.get("dingtalk_destination_id") or ""
    chosen = _selected(settings, profiles)
    rows = []
    for row in profiles:
        rows.append({
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "selected": row["id"] == identity,
            "ready": _ready(row),
        })
    return {
        "selected_id": identity if chosen else "",
        "current_name": chosen["name"] if chosen else "",
        "current_role": chosen["role"] if chosen else "",
        "current_ready": _ready(chosen),
        "profiles": rows,
    }


def _persist_view(settings: dict, secrets: dict, profiles: list[dict]) -> dict:
    _store(secrets, profiles)
    materialize(settings, secrets)
    view = public_view(settings, secrets)
    blob = json.dumps(view, ensure_ascii=False)
    for row in profiles:
        if row["webhook"] and row["webhook"] in blob:
            raise DestinationError("机器人配置无法安全显示；未保存。")
        if row["secret"] and row["secret"] in blob:
            raise DestinationError("机器人配置无法安全显示；未保存。")
    return view


def save_profile(settings: dict, secrets: dict, payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"id", "name", "role", "webhook", "signing_secret"}:
        raise DestinationError("机器人配置内容无效；未保存，也未发送。")
    ensure(settings, secrets)
    profiles = _profiles(secrets)
    role = payload.get("role")
    if role not in ("test", "formal"):
        raise DestinationError("请把机器人标为测试群或正式群；未保存，也未发送。")
    name = _name(payload.get("name"))
    identity = payload.get("id") or ""
    if identity:
        if not isinstance(identity, str):
            raise DestinationError("机器人配置内容无效；未保存，也未发送。")
        current = next((row for row in profiles if row["id"] == identity), None)
        if current is None:
            raise DestinationError("没有找到要编辑的机器人配置；未保存，也未发送。")
    else:
        if len(profiles) >= _MAX_PROFILES:
            raise DestinationError("最多保存 8 套机器人配置；未保存，也未发送。")
        current = {"id": uuid.uuid4().hex, "name": name, "role": role, "webhook": "", "secret": ""}
        profiles.append(current)
    if any(row["id"] != current["id"] and row["name"] == name for row in profiles):
        raise DestinationError("已有同名机器人配置；未保存，也未发送。")
    webhook = payload.get("webhook", "")
    signing = payload.get("signing_secret", "")
    if not isinstance(webhook, str) or not isinstance(signing, str):
        raise DestinationError("机器人配置内容无效；未保存，也未发送。")
    if webhook:
        current["webhook"] = webhook
    if signing:
        current["secret"] = signing
    if not identity and not _ready(current):
        raise DestinationError("新建机器人需要同时填写 Webhook 和签名密钥；未保存，也未发送。")
    if _pairs(current["webhook"], current["secret"]) is False and (current["webhook"] or current["secret"]):
        if current["webhook"] and current["secret"]:
            raise DestinationError("Webhook 或签名密钥格式无效；未保存，也未发送。")
    current["name"] = name
    current["role"] = role
    if not settings.get("dingtalk_destination_id"):
        settings["dingtalk_destination_id"] = current["id"]
    return _persist_view(settings, secrets, profiles)


def select_profile(settings: dict, secrets: dict, identity: str) -> dict:
    if not isinstance(identity, str):
        raise DestinationError("请选择一套已保存的机器人配置；未发送。")
    ensure(settings, secrets)
    profiles = _profiles(secrets)
    if not any(row["id"] == identity for row in profiles):
        raise DestinationError("请选择一套已保存的机器人配置；未发送。")
    settings["dingtalk_destination_id"] = identity
    return _persist_view(settings, secrets, profiles)


def absorb_flat_edit(settings: dict, secrets: dict, *, webhook, signing_secret,
                     clear_webhook: bool, clear_secret: bool) -> None:
    """Keep ordinary single-field saves on the selected profile. No send."""
    ensure(settings, secrets)
    if not any((webhook, signing_secret, clear_webhook, clear_secret)):
        materialize(settings, secrets)
        return
    profiles = _profiles(secrets)
    current = _selected(settings, profiles)
    if current is None:
        current = {"id": uuid.uuid4().hex, "name": _LEGACY_NAME, "role": "unspecified",
                   "webhook": "", "secret": ""}
        profiles.append(current)
        settings["dingtalk_destination_id"] = current["id"]
    if clear_webhook:
        current["webhook"] = ""
    if clear_secret:
        current["secret"] = ""
    if isinstance(webhook, str) and webhook:
        current["webhook"] = webhook
    if isinstance(signing_secret, str) and signing_secret:
        current["secret"] = signing_secret
    if current["webhook"] and current["secret"] and not _pairs(current["webhook"], current["secret"]):
        raise DestinationError("Webhook 或签名密钥格式无效；未保存，也未发送。")
    _store(secrets, profiles)
    materialize(settings, secrets)
