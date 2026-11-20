"""Standalone MCP raw-activity selection and OpenAI digest pipeline.

The source boundary is deterministic: identity, complete pagination and the
requested half-open activity window are checked locally. Activity payloads are
not classified, stripped, normalized or interpreted by this module.
"""
from __future__ import annotations

from copy import deepcopy

from digest_pipeline import day_window, field, timestamp, validate_view
from report_contract import task_updated_timestamp, validate_selection
from runtime_mappings import validate_mappings
from teambition_readonly import CollectionError, identifier, record_id, validate_binding


def select_raw_activities(collection, config, day, window=None):
    """Return all original in-window activities grouped by task, without edits."""
    if not isinstance(collection, dict):
        raise CollectionError('Invalid collection JSON')
    if (collection.get('data_altered') is True or collection.get('sendable') is False
            or collection.get('format') == 'upcam-teambition-detailed-diagnostic-v1'):
        raise CollectionError('Cannot render incomplete collection')
    config = validate_mappings(config)
    view = validate_view(config)
    if view.get('verified') is not True or not view.get('evidence') or config.get('api_contract_verified') is not True:
        raise CollectionError('Source verification is required')
    if collection.get('pagination_complete') is not True:
        raise CollectionError('Cannot render incomplete collection')
    tenant, project = identifier(collection.get('tenant_id')), identifier(collection.get('project_id'))
    if tenant != identifier(config.get('tenant_id')) or project != identifier(config.get('project_id')):
        raise CollectionError('Collection source differs from configuration')
    if (type(collection.get('include_archived')) is not bool
            or type(config.get('include_archived', False)) is not bool
            or collection['include_archived'] != config.get('include_archived', False)):
        raise CollectionError('Archive scope mismatch')
    if config.get('activity_fields', {'time': 'createTime', 'action': 'action'}) != {'time': 'createTime', 'action': 'action'} or config.get('action_rules'):
        raise CollectionError('Unsupported activity schema override')
    candidate_selection = collection.get('selection')
    if window is None and isinstance(candidate_selection, dict) and candidate_selection.get('mode') == 'v3-project-activities-half-open':
        window = (candidate_selection.get('start'), candidate_selection.get('end_exclusive'))
    start, end = day_window(day, window=window)
    try:
        selection = validate_selection(candidate_selection, day, start, end)
    except (TypeError, ValueError, AttributeError):
        raise CollectionError('Selection contract differs from report window') from None
    collected_at = None
    if 'collected_at' in collection:
        try:
            collected_at = timestamp(collection['collected_at'])
        except (TypeError, ValueError):
            raise CollectionError('Invalid collection timestamp') from None
    records = collection.get('records')
    if not isinstance(records, list):
        raise CollectionError('Invalid collection JSON')
    selected, seen_tasks = [], set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('task'), dict):
            raise CollectionError('Invalid collection JSON')
        task = record['task']
        task_id = record_id(task)
        if task_id in seen_tasks:
            raise CollectionError('Duplicate task in collection')
        seen_tasks.add(task_id)
        if task.get('projectId') != project:
            raise CollectionError('Task belongs to another project')
        for key in ('id', '_id', 'taskId'):
            if key in task and identifier(task[key]) != task_id:
                raise CollectionError('Missing or conflicting record identity')
        if 'project' in task:
            nested = task['project']
            if not isinstance(nested, dict):
                raise CollectionError('Task belongs to another project')
            for key in ('id', '_id'):
                if key in nested and identifier(nested[key]) != project:
                    raise CollectionError('Task belongs to another project')
        try:
            task_updated_timestamp(task.get('updated'))
        except (TypeError, ValueError):
            raise CollectionError('Task violates report selection contract') from None
        if type(task.get('isArchived')) is not bool:
            raise CollectionError('Missing archive scope')
        if task['isArchived'] and not config.get('include_archived', False):
            continue
        filters = view.get('task_equals', {})
        if any(field(task, key) is None for key in filters):
            raise CollectionError('Missing configured view filter field')
        if any(field(task, key) != value for key, value in filters.items()):
            continue
        activities = record.get('activities')
        if not isinstance(activities, list):
            raise CollectionError('Invalid collection JSON')
        included, event_ids = [], {}
        for event in activities:
            if not isinstance(event, dict):
                raise CollectionError('Invalid collection JSON')
            event_id = record_id(event, task_fallback=False)
            if event_id in event_ids and event_ids[event_id] != event:
                raise CollectionError('Conflicting event identity')
            event_ids[event_id] = event
            validate_binding(event, task_id)
            if 'projectId' in event and identifier(event['projectId']) != project:
                raise CollectionError('Activity project mismatch')
            try:
                when = timestamp(event.get('createTime'))
            except (TypeError, ValueError):
                # A timestamp is necessary to decide whether the event is in scope.
                raise CollectionError('Missing or invalid timezone-aware source timestamp') from None
            if collected_at is not None and when > collected_at:
                raise CollectionError('Activity timestamp exceeds collection time')
            if start <= when < end:
                included.append(deepcopy(event))
        if included:
            selected.append((task_id, deepcopy(task), included))
    selected.sort(key=lambda item: item[0])
    rows = [{'task_key': f'task-{index}', 'task': task, 'activities': events,
             'resolved_status_ids': list(config['resolved_status_ids'])}
            for index, (_, task, events) in enumerate(selected, 1)]
    scope = {'tenant_id': tenant, 'project_id': project, 'timezone': 'Asia/Shanghai',
             'start': start.isoformat(), 'end_exclusive': end.isoformat(),
             'include_archived': config.get('include_archived', False),
             'selection': deepcopy(selection), 'pagination_complete': True,
             'snapshot_guaranteed': False}
    return {'rows': rows, 'task_ids': [task_id for task_id, _, _ in selected], 'scope': scope}


# The raw standalone pipeline intentionally keeps its own model rules; the
# legacy plugin's comment-only rules remain unchanged.
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from automated_digest import AutomatedDigestBlocked, collection_validation_message, fingerprint
from digest_pipeline import identity_label
from openai_summarizer import OpenAIResponsesClient, SummaryError
from render_digest import TEMPLATE, render_digest
from task_display import TaskDisplayError, format_task_reference


RAW_RULES = Path(__file__).parent / 'templates' / 'mcp_raw_editorial_rules.json'
RAW_REPORT_TYPE = 'automated-mcp-raw-v1'
_CATEGORIES = frozenset(('resolved', 'progress', 'new', 'other'))


def requires_raw_summary(collection, config, day, window=None):
    return bool(select_raw_activities(collection, config, day, window)['rows'])


def diagnose_raw_source(collection, config, day, window=None):
    """Check source and window without inspecting the semantics of activities."""
    try:
        source = select_raw_activities(collection, config, day, window)
    except (CollectionError, ValueError, TypeError, KeyError) as exc:
        return {'ok': False, 'message': collection_validation_message(exc), 'task_count': None}
    prefiltered = collection.get('selection', {}).get('candidate_scope') == 'current-visible-project-tasks-updated-since'
    candidate_notice = ('已按任务更新时间预选，未推进更新时间的旧任务动态可能漏采；'
                        '可用“详细诊断导出…”进行全量复核。' if prefiltered else '')
    return {'ok': True,
            'message': (f"只读范围检查通过：{len(source['rows'])} 个任务有窗口内动态；"
                        '活动原文将在运行时交给 OpenAI 整理。'
                        f'{candidate_notice}已完成接口分页；不代表历史项目的完整快照。'
                        '未调用 OpenAI，未发送钉钉。'),
            'task_count': len(source['rows'])}


def _validate_results(rows, results):
    keys = [row['task_key'] for row in rows]
    if not isinstance(results, dict) or set(results) != set(keys):
        raise AutomatedDigestBlocked('OpenAI 未完整返回各任务结果，本次未发送。')
    for key in keys:
        value = results[key]
        if (not isinstance(value, dict) or set(value) != {'category', 'summary'}
                or value.get('category') not in _CATEGORIES
                or not isinstance(value.get('summary'), str)):
            raise AutomatedDigestBlocked('OpenAI 返回的任务结果格式无效，本次未发送。')
        summary = value['summary']
        if (len(summary) > 200 or summary != summary.strip()
                or any(ord(char) < 32 or char in '\u2028\u2029' for char in summary)):
            raise AutomatedDigestBlocked('OpenAI 摘要格式无效，本次未发送。')


def _assemble(source, config, day, results):
    _validate_results(source['rows'], results)
    tasks, audit = [], []
    for row, task_id in zip(source['rows'], source['task_ids']):
        try:
            display = format_task_reference(row['task'], config)
        except TaskDisplayError as exc:
            raise AutomatedDigestBlocked(f'任务 {task_id}：{exc}') from None
        output = results[row['task_key']]
        task = row['task']
        tasks.append({'category': output['category'], 'title': display['title'], 'url': display['url'],
                      'summary': output['summary'],
                      'owner': identity_label(task, 'executorId', config['owner_names'], '成员', allow_unresolved=True),
                      'status': identity_label(task, 'tfsId', config['status_names'], '状态', allow_unresolved=True)})
        audit.append({'task_id': task_id, 'task_key': row['task_key'],
                      'event_ids': [record_id(event, task_fallback=False) for event in row['activities']]})
    data = {'date': day, 'tasks': tasks,
            'time_range': {'start': source['scope']['start'], 'end_exclusive': source['scope']['end_exclusive'],
                           'timezone': 'Asia/Shanghai'}}
    try:
        markdown = render_digest(data)
    except (ValueError, TypeError, KeyError):
        raise AutomatedDigestBlocked('日报显示内容无效，本次未发送。') from None
    return {'digest': data, 'markdown': markdown, 'audit': audit, 'scope': deepcopy(source['scope']),
            'raw_results': deepcopy(results), 'issues': [], 'ready_to_send': True}


def load_raw_editorial_rules():
    return json.loads(RAW_RULES.read_text(encoding='utf-8'))


def build_raw_digest(collection, config, day, api_key, model, effort, *, client=None,
                     window=None, generated_at=None, service_tier='default'):
    """Pass each in-window raw event to the model and build a bound report."""
    config = validate_mappings(config)
    source = select_raw_activities(collection, config, day, window)
    results = {}
    if source['rows']:
        try:
            rules = load_raw_editorial_rules()
            results = (client or OpenAIResponsesClient()).summarize_activities(
                api_key, model, effort, source['rows'], rules, service_tier=service_tier)
        except SummaryError as exc:
            if str(exc) in {"Raw activity request exceeds the local request size limit",
                            "Raw activity input exceeds the local request size limit",
                            "Raw activity input exceeds the local JSON limits"}:
                raise AutomatedDigestBlocked(
                    "本次原始活动请求超过 256 KiB 或结构安全上限；未截断内容、未调用 OpenAI、未发送钉钉。"
                    "请缩短时间范围后重试。") from None
            raise AutomatedDigestBlocked(str(exc)) from None
    result = _assemble(source, config, day, results)
    result['report_type'] = RAW_REPORT_TYPE
    result['automation'] = {'source_sha256': fingerprint(collection), 'config_sha256': fingerprint(config),
                            'template_sha256': hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                            'rules_sha256': hashlib.sha256(RAW_RULES.read_bytes()).hexdigest(),
                            'summaries_sha256': fingerprint(results), 'model': model,
                            'reasoning_effort': effort, 'service_tier': service_tier,
                            'generated_at_utc': (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec='seconds'),
                            'summary_count': len(results)}
    result['automation']['report_sha256'] = fingerprint(result)
    return result


def validate_raw_report(report, collection, config, current_model, current_effort, current_service_tier='default'):
    """Recreate the source scope/render and validate provenance before delivery."""
    if (not isinstance(report, dict) or report.get('report_type') != RAW_REPORT_TYPE
            or report.get('ready_to_send') is not True or report.get('issues')):
        raise AutomatedDigestBlocked('无效的自动日报，本次未发送。')
    config = validate_mappings(config)
    automation = report.get('automation')
    if (not isinstance(automation, dict) or automation.get('model') != current_model
            or automation.get('reasoning_effort') != current_effort
            or automation.get('service_tier') != current_service_tier
            or automation.get('source_sha256') != fingerprint(collection)
            or automation.get('config_sha256') != fingerprint(config)
            or automation.get('template_sha256') != hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
            or automation.get('rules_sha256') != hashlib.sha256(RAW_RULES.read_bytes()).hexdigest()
            or automation.get('summaries_sha256') != fingerprint(report.get('raw_results'))):
        raise AutomatedDigestBlocked('日报来源、设置或摘要已变化，本次未发送。')
    attested = deepcopy(report)
    saved = attested['automation'].pop('report_sha256', None)
    if not isinstance(saved, str) or fingerprint(attested) != saved:
        raise AutomatedDigestBlocked('日报生成后已改变，本次未发送。')
    try:
        day = report['digest']['date']
        scope = report['scope']
        source = select_raw_activities(collection, config, day, (scope['start'], scope['end_exclusive']))
        expected = _assemble(source, config, day, report['raw_results'])
    except (KeyError, TypeError, ValueError, CollectionError):
        raise AutomatedDigestBlocked('日报范围或来源无效，本次未发送。') from None
    if any(expected.get(key) != report.get(key) for key in expected):
        raise AutomatedDigestBlocked('日报与当前来源不一致，本次未发送。')
