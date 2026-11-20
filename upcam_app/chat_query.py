"""Bounded, strictly read-only operations for the standalone chat.

Model plans select from this fixed interface, never the remote MCP catalog.  The
existing client enforces the independently verified MCP business contracts.
"""
from __future__ import annotations

from datetime import datetime
import json
import unicodedata

from teambition_readonly import CollectionError, identifier, record_id, records_equal, validate_binding

MAX_TASKS = 10000
MAX_ACTIVITY_TASKS = 20
MAX_SOURCE_BYTES = 200 * 1024
VISIBLE_COVERAGE = '仅涵盖当前可见来源，不保证已删除或不可见历史及原子快照。'


class ChatQueryError(ValueError):
    """Safe local query error without credentials or remote response bodies."""


def _time(value):
    if value is None:
        return None
    try:
        if type(value) is not str or 'T' not in value or len(value) > 64:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise ChatQueryError('查询时间无效，请指定含时区的明确时间。') from None


def _validate_plan(plan):
    if type(plan) is not dict or set(plan) != {'operation', 'search', 'task_id', 'start', 'end'}:
        raise ChatQueryError('查询计划格式无效，未执行查询。')
    op, search, task_id = plan['operation'], plan['search'], plan['task_id']
    if op not in ('projects', 'tasks', 'task', 'activities'):
        raise ChatQueryError('查询操作不在只读范围内。')
    if search is not None and (type(search) is not str or not search.strip() or len(search) > 200
                               or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in search)):
        raise ChatQueryError('任务搜索条件无效。')
    if task_id is not None:
        try:
            identifier(task_id)
        except CollectionError:
            raise ChatQueryError('任务标识无效。') from None
    if search is not None and task_id is not None:
        raise ChatQueryError('请选择任务名称或任务标识。')
    if op in ('projects', 'tasks') and task_id is not None:
        raise ChatQueryError('该查询不接受任务标识。')
    if op == 'projects' and any(plan[k] is not None for k in ('search', 'start', 'end')):
        raise ChatQueryError('项目列表不接受这些查询条件。')
    if op == 'task' and (task_id is None and search is None):
        raise ChatQueryError('请指定任务名称或标识。')
    start, end = _time(plan['start']), _time(plan['end'])
    if start is not None and end is not None and start >= end:
        raise ChatQueryError('开始时间必须早于结束时间。')
    if op == 'task' and (start is not None or end is not None):
        raise ChatQueryError('任务详情不接受时间范围。')
    return op, search, task_id, start, end


class ChatReadService:
    def __init__(self, client, *, project_id, include_archived=False, list_projects=None):
        try:
            self.project_id = identifier(project_id)
        except CollectionError:
            raise ChatQueryError('请先保存聊天使用的项目。') from None
        if type(include_archived) is not bool or not callable(getattr(client, 'get', None)) or not callable(getattr(client, 'pages', None)):
            raise ChatQueryError('只读查询配置无效。')
        if list_projects is not None and not callable(list_projects):
            raise ChatQueryError('项目查询配置无效。')
        self.client, self.include_archived, self.list_projects = client, include_archived, list_projects

    def _tasks(self):
        params = {'projectId': self.project_id, 'pageSize': 100, 'includeArchived': self.include_archived}
        rows = list(self.client.pages('project.tasks', params, max_items=MAX_TASKS))
        if len(rows) > MAX_TASKS:
            raise ChatQueryError('项目任务过多，请缩小查询范围。')
        for row in rows:
            self._check_task(row)
        return rows

    def _check_task(self, row, expected_id=None):
        if (not isinstance(row, dict) or row.get('projectId') != self.project_id
                or type(row.get('isArchived')) is not bool
                or (row['isArchived'] and not self.include_archived)):
            raise ChatQueryError('任务范围无法核验，查询已停止。')
        task_id = record_id(row)
        if expected_id is not None and task_id != expected_id:
            raise ChatQueryError('任务详情与当前项目不符，查询已停止。')
        for key in ('id', '_id', 'taskId'):
            if key in row and identifier(row[key]) != task_id:
                raise ChatQueryError('任务范围无法核验，查询已停止。')
        if 'project' in row:
            project = row['project']
            if not isinstance(project, dict) or any(identifier(project[key]) != self.project_id
                    for key in ('id', '_id') if key in project):
                raise ChatQueryError('任务范围无法核验，查询已停止。')
        return task_id

    def _detail(self, task_id):
        value = self.client.get('task.details', {'taskId': task_id, 'projectId': self.project_id,
                                                'includeArchived': self.include_archived})
        row = value.get('result') if isinstance(value, dict) else None
        if isinstance(row, list) and len(row) == 1:
            row = row[0]
        self._check_task(row, task_id)
        return row

    def execute(self, plan):
        op, search, task_id, start, end = _validate_plan(plan)
        try:
            if op == 'projects':
                if self.list_projects is None:
                    raise ChatQueryError('项目列表暂不可用。')
                rows = self.list_projects()
                if type(rows) is not list or len(rows) > MAX_TASKS:
                    raise ChatQueryError('项目列表超过查询限制。')
                result = {'kind': 'projects', 'rows': rows, 'coverage': VISIBLE_COVERAGE, 'snapshot_guaranteed': False}
            elif op == 'tasks':
                rows = self._tasks()
                if search:
                    rows = self._matching(rows, search)
                if start is not None or end is not None:
                    rows = [row for row in rows if self._within(row.get('updated'), start, end)]
                result = {'kind': 'tasks', 'project_id': self.project_id, 'rows': rows, 'coverage': VISIBLE_COVERAGE, 'snapshot_guaranteed': False}
            else:
                initial = None
                if task_id is None:
                    initial = self._tasks()
                    selected = self._matching(initial, search) if search else list(initial)
                    if op == 'activities' and start is not None:
                        selected = [row for row in selected if self._within(row.get('updated'), start, None)]
                    if op == 'task' and len(selected) != 1:
                        result = {'kind': 'choose_task', 'project_id': self.project_id,
                                  'rows': selected, 'coverage': VISIBLE_COVERAGE, 'snapshot_guaranteed': False}
                        return self._bounded(result)
                    if len(selected) > MAX_ACTIVITY_TASKS:
                        raise ChatQueryError('匹配任务过多，请指定任务或缩小范围。')
                    ids = [record_id(row) for row in selected]
                else:
                    selected, ids = [], [task_id]
                rows = []
                for item in ids:
                    detail = self._detail(item)
                    matching = next((row for row in selected if record_id(row) == item), None)
                    if matching is not None and detail.get('updated') != matching.get('updated'):
                        raise ChatQueryError('任务在查询期间发生变化，请重新查询。')
                    if op == 'task':
                        rows.append(detail)
                        continue
                    events = list(self.client.pages('task.activities', {'taskId': item, 'projectId': self.project_id, 'pageSize': 100}))
                    for event in events:
                        record_id(event, task_fallback=False)
                        validate_binding(event, item)
                        if 'projectId' in event and identifier(event['projectId']) != self.project_id:
                            raise ChatQueryError('任务范围无法核验，查询已停止。')
                    events = [event for event in events if self._within(event.get('createTime'), start, end)]
                    if not records_equal(detail, self._detail(item)):
                        raise ChatQueryError('任务在查询期间发生变化，请重新查询。')
                    rows.append({'task': detail, 'activities': events})
                if initial is not None and op == 'activities' and not records_equal(initial, self._tasks()):
                    raise ChatQueryError('项目在查询期间发生变化，请重新查询。')
                coverage = ('仅检查当前更新时间不早于起点的可见任务；未推进更新时间的动态可能遗漏。'
                            if op == 'activities' and initial is not None and start is not None
                            else VISIBLE_COVERAGE)
                result = {'kind': op, 'project_id': self.project_id, 'rows': rows,
                          'start': plan['start'], 'end': plan['end'], 'coverage': coverage,
                          'snapshot_guaranteed': False}
            return self._bounded(result)
        except ChatQueryError:
            raise
        except (CollectionError, TypeError, ValueError, KeyError, OverflowError):
            raise ChatQueryError('MCP 只读查询不完整或来源校验失败；请缩小范围后重试。') from None
        except Exception:
            raise ChatQueryError('MCP 只读查询未完成，请检查连接后重试。') from None

    @staticmethod
    def _matching(rows, search):
        if any(type(row.get('content')) is not str for row in rows):
            raise ChatQueryError('部分任务缺少可核验的标题，无法完成名称搜索。')
        return [row for row in rows if search.casefold() in row['content'].casefold()]

    @staticmethod
    def _within(value, start, end):
        instant = _time(value)
        if instant is None:
            raise ChatQueryError('来源时间缺失，无法核验查询范围。')
        return (start is None or instant >= start) and (end is None or instant < end)

    @staticmethod
    def _bounded(result):
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(encoded) > MAX_SOURCE_BYTES:
                raise ChatQueryError('查询结果太大，请缩小范围；未截断结果。')
        except ChatQueryError:
            raise
        except Exception:
            raise ChatQueryError('查询结果格式无效。') from None
        return result
