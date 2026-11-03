"""Parse observed v3 activity shapes without inferring undocumented semantics."""

import json
import re

from teambition_readonly import CollectionError, identifier, validate_binding


MAX_CONTENT = 65536
OBSERVED_ACTIONS = frozenset({'create', 'comment', 'involve.add', 'update.taskflowstatus',
                              'standard', 'update_content', 'update_executor'})


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
        if data['isDone'] != resolved:
            return None
        return {'target_id': target, 'previous_id': previous, 'resolved': resolved}
    except (ValueError, TypeError, CollectionError, RecursionError):
        return None


def comment_input(raw, event_id, when):
    """Keep complete evidence; suppress only exact acknowledgement-only candidates.

    Suppression affects summary input selection, never the audit evidence. An
    acknowledgement may still be meaningful in context, so all comments require
    semantic review. No LLM, instruction execution, or external requests occur.
    """
    content = raw.get('content')
    evidence_keys = ('attachments', 'dingFiles', 'files', 'images', 'data', 'mentions', 'renderMode')
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
        if set(payload) - {'comment', 'attachments', 'dingFiles', 'files', 'renderMode', 'mentions'}:
            reasons.append('存在尚未识别的评论字段；完整对象保留')
        if any(not isinstance(value, list) for value in payload_evidence.values()):
            reasons.append('附件或提及字段不是已知列表结构')
        # No nonempty renderMode enum has been verified in the handed-off contract.
        if mode not in (None, ''):
            reasons.append('renderMode 语义尚未核验；正文仅作原文字面内容')
    plain = isinstance(body, str) and bool(body.strip())
    rich = plain and bool(re.search(r'<[^>]+>|\{\s*"', body))
    controls = plain and any(ord(c) < 32 and c not in '\r\n\t' for c in body)
    if not plain:
        reasons.append('comment 正文缺失、为空或不是字符串')
    elif rich or controls:
        reasons.append('正文包含未支持富文本或控制字符；不推断渲染结果')
    normalized = ' '.join(body.split()) if plain and not rich and not controls else None
    # Even a short confirmation can carry substantive attachments or mentions.
    has_extra = any(extra.values()) or any(payload_evidence.values())
    acknowledgement = bool(normalized and not reasons and not has_extra
                           and normalized.casefold() in {'收到', '好的', '谢谢', 'ok', '👍'})
    return {'event_id': event_id, 'create_time': when.isoformat() if when else None,
            'raw_content': content, 'parsed_payload': payload, 'body': body,
            'text': normalized, 'render_mode': mode,
            'extra_evidence': extra, 'payload_evidence': payload_evidence,
            'omit_from_summary': acknowledgement,
            'omission_reason': '仅确认性回复候选；原文保留，需结合上下文复核' if acknowledgement else None,
            'review_reasons': reasons, 'extraction_needs_review': bool(reasons),
            'semantic_review_required': True}



def parse_activity(raw, config):
    action = raw.get('action')
    if action == 'update.taskflowstatus':
        transition = status_transition(raw.get('content'), config)
        if transition is None:
            return 'unknown', False
        return 'status', transition['resolved']
    # "standard" is observed but its business meaning has not been verified.
    kinds = {'create': 'created', 'comment': 'comment', 'involve.add': 'other',
             'update_content': 'other', 'update_executor': 'other'}
    return kinds.get(action, 'unknown') if isinstance(action, str) else 'unknown', False
