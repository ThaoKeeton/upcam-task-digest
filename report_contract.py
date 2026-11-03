"""Canonical report date/window shared by generation and delivery validation."""

from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def strict_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Report date must use YYYY-MM-DD")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Noncanonical report date")
    return day


def canonical_window(day):
    start = datetime.combine(strict_day(day), time.min, SHANGHAI)
    return start, start + timedelta(days=1)


def validated_report_source(report):
    """Validate a trusted pipeline report's structural binding, not a signature."""
    day = report["digest"]["date"]
    start, end = canonical_window(day)
    scope = report["scope"]
    if (scope.get("timezone") != "Asia/Shanghai"
            or scope.get("start") != start.isoformat()
            or scope.get("end_exclusive") != end.isoformat()
            or scope.get("pagination_complete") is not True
            or type(scope.get("include_archived")) is not bool):
        raise ValueError("Report window or collection scope mismatch")
    source = tuple(scope.get(key) for key in ("tenant_id", "project_id"))
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in source):
        raise ValueError("Missing or invalid report source")
    return (*source, day)
