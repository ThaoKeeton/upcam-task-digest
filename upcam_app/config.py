"""Non-secret local settings and local-time collection windows."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import re
import tempfile

from digest_pipeline import validate_view
from runtime_mappings import validate_mappings
from teambition_readonly import CollectionError
from task_display import TaskDisplayError, validate_task_prefix, normalize_task_link_view
from .dingtalk_mentions import MentionConfigError, normalize_mentions
from .preferences import PREFERENCE_DEFAULTS, PreferenceError, preference_values


SCHEMA_VERSION = 1
SCHEDULE_RECOVERY_WINDOW = timedelta(hours=24)
DEFAULT_SETTINGS = {
    **PREFERENCE_DEFAULTS,
    "schema_version": SCHEMA_VERSION,
    "tenant_id": "",
    "project_id": "",
    "task_number_prefix": "",
    "task_link_view_id": "",
    "task_display_scope": None,
    "include_archived": False,
    "max_tasks_per_run": 300,
    "max_concurrent_tasks": 4,
    "api_contract_verified": False,
    "view": {"verified": False, "evidence": "", "task_equals": {}},
    "owner_names": {},
    "status_names": {},
    "resolved_status_ids": [],
    "morning_run_at": "08:50",
    "morning_start_at": "16:50",
    "morning_end_at": "08:50",
    "afternoon_run_at": "16:50",
    "afternoon_start_at": "00:00",
    "afternoon_end_at": "16:50",
    "auto_send_enabled": False,
    "auto_send_enabled_since_local": "",
    "start_with_windows": False,
    "openai_model": "gpt-6-luna",
    "openai_reasoning_effort": "max",
    "openai_service_tier": "default",
    "dingtalk_mention_mode": "mobile",
    "dingtalk_mentions": "",
}


class SettingsError(ValueError):
    """A setting is invalid or cannot be loaded safely."""


def parse_clock(value: str, field_name: str = "time") -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise SettingsError(f"{field_name} must use 24-hour HH:MM format")
    return time.fromisoformat(value)


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise SettingsError("Settings must be a JSON object")
    merged = {**DEFAULT_SETTINGS, **settings}
    if merged.get("schema_version") != SCHEMA_VERSION:
        raise SettingsError("Unsupported settings version")
    try:
        merged.update(preference_values(merged))
    except PreferenceError as exc:
        raise SettingsError(str(exc)) from None
    try:
        merged["dingtalk_mentions"] = normalize_mentions(
            merged["dingtalk_mention_mode"], merged["dingtalk_mentions"])
    except MentionConfigError as exc:
        raise SettingsError(str(exc)) from None
    if any(not isinstance(merged.get(key), str) for key in ("tenant_id", "project_id")):
        raise SettingsError("Teambition tenant and project IDs must be text")
    try:
        merged['task_number_prefix'] = validate_task_prefix(merged.get('task_number_prefix'))
        merged['task_link_view_id'] = normalize_task_link_view(merged.get('task_link_view_id'), merged['project_id'])
    except TaskDisplayError as exc:
        raise SettingsError(str(exc)) from None
    if merged['task_number_prefix'] or merged['task_link_view_id']:
        current_display_scope = {'tenant_id': merged['tenant_id'], 'project_id': merged['project_id']}
        saved_display_scope = merged.get('task_display_scope')
        if saved_display_scope is not None and saved_display_scope != current_display_scope:
            raise SettingsError("任务编号前缀或视图链接属于其他企业/项目，请重新填写本项目的显示设置。")
        merged['task_display_scope'] = current_display_scope
    else:
        merged['task_display_scope'] = None
    if type(merged.get("include_archived")) is not bool or type(merged.get("api_contract_verified")) is not bool:
        raise SettingsError("Teambition scope verification flags must be true or false")
    max_tasks = merged.get("max_tasks_per_run")
    if type(max_tasks) is not int or not 1 <= max_tasks <= 700:
        raise SettingsError("Collection batch size must be an integer from 1 to 700")
    concurrent_tasks = merged.get("max_concurrent_tasks")
    if type(concurrent_tasks) is not int or not 1 <= concurrent_tasks <= 4:
        raise SettingsError("Concurrent task count must be an integer from 1 to 4")
    if not isinstance(merged.get("auto_send_enabled"), bool):
        raise SettingsError("Automatic run and send setting must be true or false")
    if not isinstance(merged.get("start_with_windows"), bool):
        raise SettingsError("Windows sign-in startup setting must be true or false")
    enabled_since = merged.get("auto_send_enabled_since_local")
    if not isinstance(enabled_since, str):
        raise SettingsError("Automatic run enable time must be text")
    if enabled_since:
        try:
            parsed_enabled_since = datetime.fromisoformat(enabled_since)
        except ValueError as exc:
            raise SettingsError("Automatic run enable time is invalid") from exc
        if parsed_enabled_since.tzinfo is not None:
            raise SettingsError("Automatic run enable time must use the PC's local time")
    try:
        validate_mappings(merged)
        validate_view({"view": merged.get("view")})
    except (CollectionError, ValueError, TypeError, KeyError) as exc:
        raise SettingsError("Teambition mapping or project view configuration is invalid") from exc
    if not isinstance(merged.get("openai_model"), str) or not merged["openai_model"].strip():
        raise SettingsError("OpenAI model must be text")
    effort = merged.get("openai_reasoning_effort")
    if effort not in ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
        raise SettingsError("OpenAI reasoning effort is invalid")
    if merged.get("openai_service_tier") not in ("default", "fast"):
        raise SettingsError("OpenAI API service tier must be Standard or Fast")
    morning_start = parse_clock(merged["morning_start_at"], "Morning collection start")
    morning_end = parse_clock(merged["morning_end_at"], "Morning collection end")
    parse_clock(merged["morning_run_at"], "Morning schedule")
    afternoon_start = parse_clock(merged["afternoon_start_at"], "Afternoon collection start")
    afternoon_end = parse_clock(merged["afternoon_end_at"], "Afternoon collection end")
    parse_clock(merged["afternoon_run_at"], "Afternoon schedule")
    if morning_start == morning_end:
        raise SettingsError("Morning collection start and end cannot be identical")
    if afternoon_end <= afternoon_start:
        raise SettingsError("Afternoon collection end must be later than its start")
    return merged


def collection_client_limits(settings: dict) -> dict:
    """Preserve per-batch budgets while enabling bounded project collection."""
    clean = validate_settings(settings)
    maximum = clean["max_tasks_per_run"]
    max_requests = min(1800, max(1000, maximum * 2 + 400))
    max_seconds = min(600, max(300, (max_requests + 3) // 4 + 60))
    return {
        "max_tasks": maximum,
        "concurrent_tasks": clean["max_concurrent_tasks"],
        "max_requests": max_requests,
        "max_pages": 50,
        "max_records": 50000,
        "max_seconds": max_seconds,
        "max_response_bytes": 128 * 1024 * 1024,
        "batch_mode": True,
        "max_project_tasks": 10000,
    }


def runtime_config(settings: dict) -> dict:
    """Return the validated Teambition/report configuration from local settings."""
    clean = validate_settings(settings)
    config = validate_mappings(clean)
    config["activity_fields"] = {"time": "createTime", "action": "action"}
    config["include_archived"] = clean["include_archived"]
    config["api_contract_verified"] = clean["api_contract_verified"]
    config["view"] = validate_view({"view": clean["view"]})
    return config


def parse_manual_window(start_text: str, end_text: str) -> tuple[datetime, datetime]:
    """Parse local wall-clock values entered as YYYY-MM-DD HH:MM."""
    try:
        start = datetime.strptime(start_text.strip(), "%Y-%m-%d %H:%M")
        end = datetime.strptime(end_text.strip(), "%Y-%m-%d %H:%M")
    except (AttributeError, ValueError) as exc:
        raise SettingsError("Use YYYY-MM-DD HH:MM for both collection boundaries") from exc
    if end <= start:
        raise SettingsError("Collection end must be after collection start")
    return start, end


def scheduled_window(settings: dict, run_kind: str, run_date: date) -> tuple[datetime, datetime]:
    settings = validate_settings(settings)
    if run_kind == "morning":
        start = datetime.combine(run_date - timedelta(days=1), parse_clock(settings["morning_start_at"]))
        end = datetime.combine(run_date, parse_clock(settings["morning_end_at"]))
    elif run_kind == "afternoon":
        start = datetime.combine(run_date, parse_clock(settings["afternoon_start_at"]))
        end = datetime.combine(run_date, parse_clock(settings["afternoon_end_at"]))
    else:
        raise SettingsError("Run kind must be morning or afternoon")
    if end <= start:
        raise SettingsError("Scheduled collection end must be after its start")
    return start, end


def scheduled_occurrences(settings: dict, after: datetime, through: datetime) -> list[tuple[datetime, str, str]]:
    """Return schedule slots in (after, through], using the PC's local wall clock."""
    if through <= after:
        return []
    settings = validate_settings(settings)
    rows = []
    day = after.date()
    while day <= through.date():
        for kind, key in (("morning", "morning_run_at"), ("afternoon", "afternoon_run_at")):
            slot = datetime.combine(day, parse_clock(settings[key]))
            if after < slot <= through:
                rows.append((slot, kind, f"{day.isoformat()}:{kind}"))
        day += timedelta(days=1)
    return sorted(rows)


def recoverable_schedule_occurrences(settings: dict, now: datetime, *,
                                     enabled_since: datetime | None = None) -> list[tuple[datetime, str, str]]:
    """Return recent local slots since auto-run was enabled, if provided."""
    if not isinstance(now, datetime) or now.tzinfo is not None:
        raise SettingsError("Schedule recovery requires the PC's local naive time")
    after = now - SCHEDULE_RECOVERY_WINDOW
    if enabled_since is not None:
        if not isinstance(enabled_since, datetime) or enabled_since.tzinfo is not None:
            raise SettingsError("Schedule recovery requires the PC's local naive time")
        after = max(after, enabled_since)
    return scheduled_occurrences(settings, after, now)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError("Local settings could not be read") from exc
        return validate_settings(data)

    def save(self, settings: dict) -> dict:
        clean = validate_settings(settings)
        atomic_write(self.path, (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return clean


def atomic_write(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def user_data_dir() -> Path:
    """Return the per-user app data directory without reading credential values."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise SettingsError("Windows LOCALAPPDATA is unavailable")
        return Path(base) / "UPCAM" / "TaskDigest"
    return Path.home() / ".local" / "share" / "upcam-task-digest"
