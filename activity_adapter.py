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
    extra = {k: raw[k] for k in ('attachments', 'files', 'images', 'data', 'mentions') if raw.get(k)}
    encoded = json.dumps({'content': content, 'extra': extra}, ensure_ascii=False)
    if len(encoded.encode('utf-8')) > MAX_CONTENT:
        raise CollectionError('Comment evidence exceeds review size budget')
    plain = isinstance(content, str) and bool(content.strip())
    rich = plain and bool(re.search(r'<[^>]+>|\{\s*"', content))
    controls = plain and any(ord(c) < 32 and c not in '\r\n\t' for c in content)
    normalized = ' '.join(content.split()) if plain else None
    # Do not suppress text when attachments/other payload may carry substance.
    has_extra = any(raw.get(k) for k in ('attachments', 'files', 'images', 'data', 'mentions'))
    acknowledgement = bool(plain and not rich and not controls and not has_extra
                           and normalized.casefold() in {'收到', '好的', '谢谢', 'ok', '👍'})
    return {'event_id': event_id, 'create_time': when.isoformat() if when else None, 'raw_content': content,
            'text': normalized if plain and not rich and not controls else None,
            'extra_evidence': extra,
            'omit_from_summary': acknowledgement,
            'omission_reason': '仅确认性回复候选；原文保留，需结合上下文复核' if acknowledgement else None,
            'extraction_needs_review': not plain or rich or controls,
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
