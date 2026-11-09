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


def report_window(day, start=None, end=None):
    """Resolve an optional explicit half-open range into Asia/Shanghai time.

    Custom windows are a caller-selected scope; they still require the same
    local view/API verification gates as the original calendar-day contract.
    """
    report_day = strict_day(day)
    if start is None and end is None:
        return canonical_window(report_day.isoformat())
    if start is None or end is None:
        raise ValueError("Both custom window boundaries are required")

    def aware_boundary(value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Custom window boundaries must include a timezone")
        return value.astimezone(SHANGHAI)

    start = aware_boundary(start)
    end = aware_boundary(end)
    full_start, full_end = canonical_window(report_day.isoformat())
    is_calendar_day = (start, end) == (full_start, full_end)
    if end <= start or (not is_calendar_day and end.date() != report_day):
        raise ValueError("Custom window must end after it starts and close on the report date")
    return start, end


def updated_selection(day, start=None, end=None, *, candidate_mode="updated_since_start"):
    """Describe candidate collection separately from the activity-time window.

    The normal path lists the whole readable project, then locally limits costly
    detail/history reads to updated >= start minus one day, without an end cap.
    The overlap covers small timestamp skew; it is not proof that all activity
    advances updated. ``full_project`` preserves the previous audit contract.
    Neither strategy is a historical project snapshot.
    """
    if candidate_mode not in ("updated_since_start", "full_project"):
        raise ValueError("Invalid task candidate mode")
    report_day = strict_day(day)
    start, end = report_window(report_day.isoformat(), start, end)
    full_start, full_end = canonical_window(report_day.isoformat())
    selection = {'mode': 'v3-project-activities-half-open', 'view_equivalent': False,
            'date': report_day.isoformat(), 'start': start.isoformat(), 'end_exclusive': end.isoformat(),
            'q': None, 'basis': 'activity-createTime',
            'candidate_scope': 'current-visible-project-tasks',
            'historical_event_coverage': False, 'snapshot_guaranteed': False,
            'window_kind': 'calendar_day' if (start, end) == (full_start, full_end) else 'custom'}
    if candidate_mode == "updated_since_start":
        selection.update(candidate_scope="current-visible-project-tasks-updated-since",
                         candidate_updated_since=(start - timedelta(days=1)).isoformat(),
                         candidate_overlap_seconds=86400,
                         activity_updated_index_guaranteed=False)
    return selection


def validate_selection(selection, day, start=None, end=None):
    """Accept only canonical current or previous full-project activity scope."""
    if not any(selection == updated_selection(day, start, end, candidate_mode=mode)
               for mode in ("updated_since_start", "full_project")):
        raise ValueError("Selection contract differs from report window")
    return selection


def task_is_candidate(updated, selection):
    """Filter a validated list row locally; never impose a report-end cap."""
    instant = task_updated_timestamp(updated)
    cutoff = selection.get("candidate_updated_since")
    return cutoff is None or instant >= task_updated_timestamp(cutoff)


def task_updated_timestamp(value):
    """Validate an aware task version timestamp without selecting its window."""
    if not isinstance(value, str):
        raise ValueError('Missing task updated timestamp')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('Task updated timestamp lacks timezone')
    return instant


def updated_in_window(value, day, start=None, end=None):
    instant = task_updated_timestamp(value)
    start, end = report_window(day, start, end)
    if not start <= instant < end:
        raise ValueError('Task updated timestamp outside selection window')
    return instant


def validated_report_source(report):
    """Validate a trusted pipeline report's source and exact half-open scope."""
    day = report["digest"]["date"]
    scope = report["scope"]
    try:
        start, end = report_window(day, scope.get("start"), scope.get("end_exclusive"))
    except (TypeError, ValueError):
        raise ValueError("Invalid report window") from None
    selection = scope.get("selection")
    if (scope.get("timezone") != "Asia/Shanghai"
            or scope.get("start") != start.isoformat()
            or scope.get("end_exclusive") != end.isoformat()
            or scope.get("pagination_complete") is not True
            or type(scope.get("include_archived")) is not bool):
        raise ValueError("Report window or collection scope mismatch")
    validate_selection(selection, day, start, end)
    source = tuple(scope.get(key) for key in ("tenant_id", "project_id"))
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in source):
        raise ValueError("Missing or invalid report source")
    return (*source, day)
