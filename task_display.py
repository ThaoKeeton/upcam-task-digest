"""Construct task display references using confirmed metadata only."""

import re
import unicodedata

from teambition_readonly import CollectionError, identifier, record_id


_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_VIEW_ID = re.compile(r"[A-Fa-f0-9]{24}")
_VIEW_URL = re.compile(
    r"https://www\.teambition\.com/project/(?P<project>[A-Za-z0-9_-]+)"
    r"/tasks/view/(?P<view>[A-Fa-f0-9]{24})(?:/task/[A-Za-z0-9_-]+)?"
)
_SOURCE_ERROR = "任务或项目标识缺失、无效或不一致，请重新运行只读诊断。"
_NUMBER_ERROR = "任务编号缺失或无效，请重新运行只读诊断并核实编号字段。"
_PREFIX_ERROR = "任务编号前缀缺失或无效，请填写已核实的项目前缀。"
_PREFIX_CONFLICT = "任务编号前缀与已保存的项目前缀不一致，请核实后重试。"
_VIEW_ERROR = "任务链接视图无效，请填写本项目的完整视图链接或视图编号。"
_TITLE_ERROR = "任务标题缺失或包含无效字符，请重新运行只读诊断。"


class TaskDisplayError(ValueError):
    """Display metadata errors contain fixed Chinese text without source values."""


def format_task_reference(task: dict, config: dict) -> dict:
    """Return the confirmed business number, visible title and direct task URL.

    v3 documents ``uniqueId`` as the numeric task ID. The official SDK defines
    ``project.uniqueIdPrefix`` separately; no other field supplies a number.
    """
    if not isinstance(task, dict) or not isinstance(config, dict):
        raise TaskDisplayError(_SOURCE_ERROR)
    try:
        project_id = identifier(config.get("project_id"))
        if identifier(task.get("projectId")) != project_id:
            raise CollectionError("Project mismatch")
        scope = config.get("task_display_scope")
        if scope is not None:
            tenant_id = identifier(config.get("tenant_id"))
            if not isinstance(scope, dict) or scope != {"tenant_id": tenant_id, "project_id": project_id}:
                raise CollectionError("Display metadata scope mismatch")
        task_id = record_id(task)
        # record_id supports the legacy aliases. Every supplied alias must
        # still agree, including taskId when id or _id is present.
        for key in ("id", "_id", "taskId"):
            if key in task and identifier(task[key]) != task_id:
                raise CollectionError("Task identity mismatch")
    except CollectionError:
        raise TaskDisplayError(_SOURCE_ERROR) from None

    raw_number = task.get("uniqueId")
    if type(raw_number) is int:
        if not 0 < raw_number < 10 ** 18:
            raise TaskDisplayError(_NUMBER_ERROR)
        digits = str(raw_number)
    elif (isinstance(raw_number, str) and re.fullmatch(r"[0-9]{1,18}", raw_number)
          and any(char != "0" for char in raw_number)):
        digits = raw_number
    else:
        raise TaskDisplayError(_NUMBER_ERROR)

    saved_prefix = validate_task_prefix(config.get("task_number_prefix", ""))
    prefix = saved_prefix
    if "project" in task:
        project = task["project"]
        if not isinstance(project, dict):
            raise TaskDisplayError(_SOURCE_ERROR)
        try:
            for key in ("id", "_id"):
                if key in project and identifier(project[key]) != project_id:
                    raise CollectionError("Nested project mismatch")
        except CollectionError:
            raise TaskDisplayError(_SOURCE_ERROR) from None
        if "uniqueIdPrefix" in project:
            raw_prefix = project["uniqueIdPrefix"]
            prefix = (saved_prefix if raw_prefix is None or raw_prefix == ""
                      else validate_task_prefix(raw_prefix, allow_empty=False))
            if saved_prefix and prefix != saved_prefix:
                raise TaskDisplayError(_PREFIX_CONFLICT)
    prefix = validate_task_prefix(prefix, allow_empty=False)
    number = prefix + "-" + digits

    content = task.get("content")
    if (not isinstance(content, str) or not content.strip()
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                   for char in content)):
        raise TaskDisplayError(_TITLE_ERROR)
    title = content if content.startswith(number + " ") else number + " " + content

    view_id = config.get("task_link_view_id", "")
    if not isinstance(view_id, str) or (view_id and not _VIEW_ID.fullmatch(view_id)):
        raise TaskDisplayError(_VIEW_ERROR)
    if view_id:
        url = (f"https://www.teambition.com/project/{project_id}/tasks/view/"
               f"{view_id}/task/{task_id}")
    else:
        # Official teambition/tb-skills documents this standalone task route.
        url = f"https://www.teambition.com/task/{task_id}"
    return {"number": number, "title": title, "url": url}


def normalize_task_link_view(value: str, project_id: str) -> str:
    """Accept a view ID or a strict current-project view URL; return only its ID."""
    if not isinstance(value, str):
        raise TaskDisplayError(_VIEW_ERROR)
    if value == "" or _VIEW_ID.fullmatch(value):
        return value
    match = _VIEW_URL.fullmatch(value)
    if match is None:
        raise TaskDisplayError(_VIEW_ERROR)
    try:
        current_project = identifier(project_id)
    except CollectionError:
        raise TaskDisplayError(_VIEW_ERROR) from None
    if match["project"] != current_project:
        raise TaskDisplayError(_VIEW_ERROR)
    return match["view"]


def validate_task_prefix(value: str, allow_empty=True) -> str:
    """Validate a confirmed ASCII project prefix without silently changing it."""
    if (not isinstance(value, str) or type(allow_empty) is not bool
            or not ((allow_empty and value == "") or _PREFIX.fullmatch(value))):
        raise TaskDisplayError(_PREFIX_ERROR)
    return value
