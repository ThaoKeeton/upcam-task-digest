"""Parse observed v3 activity shapes without inferring undocumented semantics."""

import json
import re
import unicodedata

from teambition_readonly import CollectionError, identifier, validate_binding


MAX_CONTENT = 65536
OBSERVED_ACTIONS = frozenset({'create', 'comment', 'involve.add', 'update.taskflowstatus',
                              'standard', 'update_content', 'update_executor'})


def unsafe_controls(value):
    return any(unicodedata.category(char) in ('Cc', 'Cf') and char not in '\r\n\t' for char in value)


def unsupported_markup(value):
    # Also flag incomplete tag-looking text. No entity decoding or HTML stripping.
    return bool(re.search(r'<[^>]+>|<[/!A-Za-z]|\{\s*"', value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Nonstandard JSON constant')


def status_transition(content, config):
    """Return validated structured transition, or None (must be reviewed)."""
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_CONTENT:
        return None
    try:
        data = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            return None
        target, previous = identifier(data.get('tfsId')), identifier(data.get('oldTfsId'))
        names = config.get('status_names', {})
        if (not isinstance(data.get('taskflowstatus'), str)
                or not isinstance(data.get('oldTaskflowstatus'), str)
                or type(data.get('isDone')) is not bool
                or target not in names or previous not in names
                or names[target] != data['taskflowstatus']
                or names[previous] != data['oldTaskflowstatus']):
            return None
        resolved = target in config.get('resolved_status_ids', [])
        # This event flag's meaning is unverified; preserve it without treating it
        # as the target state's completion flag (or the current task's flag).
        return {'target_id': target, 'previous_id': previous, 'resolved': resolved,
                'event_is_done': data['isDone']}
    except (ValueError, TypeError, CollectionError, RecursionError):
        return None


def comment_input(raw, event_id, when):
    """Keep complete evidence; suppress only exact acknowledgement-only candidates.

    Suppression affects summary input selection, never the audit evidence. An
    acknowledgement may still be meaningful in context, so all comments require
    semantic review. No LLM, instruction execution, or external requests occur.
    """
    content = raw.get('content')
    evidence_keys = ('attachments', 'dingFiles', 'files', 'images', 'data', 'mentions', 'renderMode', 'isOnlyNotifyMentions', 'quote')
    extra = {k: raw[k] for k in evidence_keys if k in raw}
    encoded = json.dumps({'content': content, 'extra': extra}, ensure_ascii=False)
    if len(encoded.encode('utf-8')) > MAX_CONTENT:
        raise CollectionError('Comment evidence exceeds review size budget')
    reasons, payload = [], None
    if isinstance(content, str):
        try:
            candidate = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(candidate, dict):
                raise ValueError('Comment payload must be an object')
            payload = candidate
        except (ValueError, TypeError, RecursionError):
            reasons.append('content 不是有效且无重复键的评论 JSON 对象')
    else:
        reasons.append('content 必须是 JSON 字符串')
    body = payload.get('comment') if payload is not None else None
    payload_evidence = {k: payload[k] for k in ('attachments', 'dingFiles', 'files', 'mentions')
                        if payload is not None and k in payload}
    mode = payload.get('renderMode') if payload is not None else None
    if payload is not None:
        if set(payload) - {'comment', 'attachments', 'dingFiles', 'files', 'renderMode', 'mentions', 'isOnlyNotifyMentions', 'quote'}:
            reasons.append('存在尚未识别的评论字段；完整对象保留')
    quotes = {source_name: source['quote'] for source_name, source in
              (('content', payload or {}), ('event', extra)) if 'quote' in source}
    for quote in quotes.values():
        if not isinstance(quote, str):
            reasons.append('quote 不是字符串；仅保留为引用证据，待复核')
        elif unsupported_markup(quote) or unsafe_controls(quote):
            reasons.append('quote 包含未支持标记或控制字符；不作为作者新意见')
    for source in (payload or {}, extra):
        for key in ('attachments', 'dingFiles', 'files', 'mentions'):
            if key in source and not isinstance(source[key], (list, dict) if key == 'mentions' else list):
                reasons.append('附件或提及字段结构异常；证据保留')
        if source.get('renderMode') not in (None, '', 'text', 'markdown'):
            reasons.append('renderMode 尚未支持；正文仅作原文字面内容')
        if 'isOnlyNotifyMentions' in source and type(source['isOnlyNotifyMentions']) is not bool:
            reasons.append('isOnlyNotifyMentions 必须为布尔值')
    if payload is not None:
        for key in ('renderMode', 'isOnlyNotifyMentions'):
            if key in payload and key in extra and payload[key] != extra[key]:
                reasons.append('评论内部与外层元数据冲突')
    # Only this exact observed tag is normalized. All original evidence is retained.
    normalized_body = body.replace('<br>', '\n') if isinstance(body, str) else None
    plain = isinstance(normalized_body, str) and bool(normalized_body.strip())
    rich = plain and unsupported_markup(normalized_body)
    controls = plain and unsafe_controls(body)
    if not plain:
        reasons.append('comment 正文缺失、为空或不是字符串')
    elif rich or controls:
        reasons.append('正文包含未支持富文本或控制字符；不推断渲染结果')
    normalized = ' '.join(normalized_body.split()) if plain and not rich and not controls else None
    # Even a short confirmation can carry substantive attachments or mentions.
    has_extra = (any(extra.get(k) for k in ('attachments', 'dingFiles', 'files', 'images', 'data', 'mentions'))
                 or any(payload_evidence.values())
                 or any(quotes.values())
                 or any(source.get('isOnlyNotifyMentions') is True for source in (payload or {}, extra)))
    acknowledgement = bool(normalized and not reasons and not has_extra
                           and normalized.casefold() in {'收到', '好的', '谢谢', 'ok', '👍'})
    return {'event_id': event_id, 'create_time': when.isoformat() if when else None,
            'raw_content': content, 'parsed_payload': payload, 'body': body,
            'normalized_body': normalized_body,
            'quote_evidence': quotes, 'quote_role': 'quoted-context-not-author-opinion',
            'text': normalized, 'render_mode': mode,
            'is_only_notify_mentions': payload.get('isOnlyNotifyMentions') if payload is not None else None,
            'extra_evidence': extra, 'payload_evidence': payload_evidence,
            'omit_from_summary': acknowledgement,
            'omission_reason': '仅确认性回复候选；原文保留，需结合上下文复核' if acknowledgement else None,
            'review_reasons': reasons, 'extraction_needs_review': bool(reasons),
            'semantic_review_required': True}



def bounded_object(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_CONTENT:
        return None
    try:
        value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def executor_change(raw, config):
    data = bounded_object(raw.get('content'))
    if data is None:
        return None
    try:
        new_id, old_id = identifier(data.get('_executorId')), identifier(data.get('_oldExecutorId'))
    except CollectionError:
        return None
    payloads = {'content': data.get('payload', {}), 'event': raw.get('payload', {})}
    if any(not isinstance(p, dict) or ('fromAutomation' in p and type(p['fromAutomation']) is not bool)
           for p in payloads.values()):
        return None
    flags = [p['fromAutomation'] for p in payloads.values() if 'fromAutomation' in p]
    automation_conflict = len(set(flags)) > 1
    names = config.get('owner_names', {})
    # Names are exclusively looked up by ID in the supplied verified mapping.
    mapped = [names.get(key) for key in (old_id, new_id)]
    valid_names = all(isinstance(name, str) and name.strip() and not any(ord(c) < 32 for c in name) for name in mapped)
    return {'previous_id': old_id, 'target_id': new_id,
            'previous_name': mapped[0] if valid_names else None,
            'target_name': mapped[1] if valid_names else None,
            'from_automation': flags[0] if flags and not automation_conflict else None,
            'parsed_payload': data, 'automation_payload': payloads,
            'automation_conflict': automation_conflict,
            'needs_review': not valid_names or automation_conflict}


def standard_commit_candidate(raw):
    data = bounded_object(raw.get('content'))
    if data is None:
        return None
    title = data.get('title')
    creator = data.get('customCreator')
    if (not isinstance(title, str) or not isinstance(creator, dict)
            or not isinstance(creator.get('name'), str) or not creator['name'].strip()):
        return None
    # Match only the explicit standalone action phrase or a phrase followed by
    # a delimiter. No broad substring match (e.g. "未关联了提交") is accepted.
    if unsafe_controls(title) or not re.fullmatch(r'关联了提交(?:[：: \t]+[\s\S]+)?', title):
        return None
    return {'pattern': 'commit-link-title', 'title': title,
            'source_creator_name': creator['name'], 'parsed_payload': data,
            'needs_review': True}  # Source text is not proof of a commit or identity.


def parse_activity(raw, config):
    action = raw.get('action')
    if action == 'update.taskflowstatus':
        transition = status_transition(raw.get('content'), config)
        if transition is None:
            return 'unknown', False
        return 'status', transition['resolved']
    if action == 'update_executor':
        return ('other', False) if executor_change(raw, config) is not None else ('unknown', False)
    if action == 'standard':
        return ('other', False) if standard_commit_candidate(raw) is not None else ('unknown', False)
    kinds = {'create': 'created', 'comment': 'comment', 'involve.add': 'other',
             'update_content': 'other'}
    return kinds.get(action, 'unknown') if isinstance(action, str) else 'unknown', False
