"""Parse observed v3 activity shapes without inferring undocumented semantics."""

import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from teambition_readonly import CollectionError, identifier, validate_binding
from standard_notices import standard_association_notification


MAX_CONTENT = 65536
QUOTE_ROLE = 'quoted-context-not-author-opinion'
OBSERVED_ACTIONS = frozenset({'create', 'comment', 'involve.add', 'update.taskflowstatus',
                              'standard', 'update_content', 'update_executor',
                              'move.cross.from.stage.to.stage'})


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


def quoted_context(value):
    """Decode only the verified single-layer ID/content envelope, or plain text.

    The reference is evidence, never a task event or the current author's opinion.
    No HTML cleanup, recursive decoding, field dropping or external lookup occurs.
    """
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode('utf-8')) > MAX_CONTENT:
            return None
    except UnicodeError:
        return None
    candidate = None
    if value.lstrip().startswith(('{', '[', '"')):
        try:
            candidate = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (ValueError, TypeError, RecursionError):
            # A serialized-looking container/string cannot fall back to text.
            return None
    if isinstance(candidate, dict):
        if set(candidate) != {'_id', 'content'}:
            return None
        reference, text = candidate['_id'], candidate['content']
        try:
            identifier(reference)
        except CollectionError:
            return None
        if len(reference) != 24 or not isinstance(text, str) or not text.strip():
            return None
        try:
            text.encode('utf-8')
        except UnicodeError:
            return None
        if unsupported_markup(text) or unsafe_controls(text):
            return None
        return {'format': 'id-content-json', 'reference_id': reference,
                'text': text, 'role': QUOTE_ROLE}
    if isinstance(candidate, (list, str)):
        return None
    if unsupported_markup(value) or unsafe_controls(value):
        return None
    return {'format': 'plain-text', 'reference_id': None, 'text': value, 'role': QUOTE_ROLE}


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


def _comment_file_ids(value):
    """Recognize references only; an ID says nothing about the file contents."""
    if not isinstance(value, list):
        return None
    try:
        if any(len(identifier(item)) != 24 for item in value) or len(set(value)) != len(value):
            return None
    except (CollectionError, TypeError):
        return None
    return value


def _preview_text(value):
    """Check opaque local metadata encoding and controls, without interpreting it."""
    if not isinstance(value, str) or unsafe_controls(value):
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def _unavailable_preview(value, body):
    """Validate only the observed empty-information stub, without fetching href."""
    if not isinstance(value, list) or len(value) != 1 or not isinstance(body, str) or not body.strip():
        return False
    preview = value[0]
    if not isinstance(preview, dict) or set(preview) != {'href', 'type', 'info'}:
        return False
    # The captured contract establishes a string shape, not a renderer enum.
    # Keep this opaque metadata local; never use it to infer preview contents.
    if not _preview_text(preview['type']) or len(preview['type']) != 8:
        return False
    info = preview['info']
    if (not isinstance(info, dict) or set(info) != {'describe', 'icon', 'title', 'extra'}
            or any(info[key] is not None for key in ('describe', 'icon', 'title'))):
        return False
    extra = info['extra']
    if (not isinstance(extra, dict) or set(extra) != {'contentType', 'errorMsg'}
            or extra['contentType'] is not None or not _preview_text(extra['errorMsg'])):
        return False
    href = preview['href']
    if (not isinstance(href, str) or not href.startswith(('http://', 'https://'))
            or any(char.isspace() or char in '<>\\' for char in href)):
        return False
    try:
        href.encode('utf-8')
        decoded = unquote(href, errors='strict')
        parsed = urlsplit(href)
        if (unsafe_controls(decoded) or any(char in '\r\n\t<>\\' for char in decoded)
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or '%' in parsed.netloc or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return False
    except (ValueError, UnicodeError):
        return False
    # Compare complete URL tokens, never a prefix, nested query URL, or quote.
    # A bare URL retains parentheses; only an explicit Markdown link supplies
    # delimiters. This prevents matching the prefix of href + '(other-path)'.
    body_urls = re.findall(r'''(?:^|[\s，。；！？、：])(https?://[^\s<>"'`，。；！？、]+)''', body)
    body_urls += re.findall(r'''\[[^\[\]\r\n]*\]\((https?://[^\s()<>"'`]+)\)''', body)
    return href in body_urls


def comment_input(raw, event_id, when):
    """Keep complete evidence; select only validated author text for summary.

    Suppression affects summary input selection, never the audit evidence. An
    attachment-only comment supplies a deterministic count, never file contents.
    No LLM, instruction execution, preview lookup or file download occurs.
    """
    content = raw.get('content')
    evidence_keys = ('attachments', 'dingFiles', 'files', 'images', 'data', 'mentions', 'renderMode',
                     'isOnlyNotifyMentions', 'quote', 'hrefPreview')
    extra = {k: raw[k] for k in evidence_keys if k in raw}
    try:
        encoded = json.dumps({'content': content, 'extra': extra}, ensure_ascii=False, allow_nan=False)
        too_large = len(encoded.encode('utf-8')) > MAX_CONTENT
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CollectionError('Comment evidence cannot be safely encoded') from None
    if too_large:
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
    # Only this exact observed tag is normalized. All original evidence is retained.
    normalized_body = body.replace('<br>', '\n') if isinstance(body, str) else None
    payload_evidence = {k: payload[k] for k in ('attachments', 'dingFiles', 'files', 'mentions')
                        if payload is not None and k in payload}
    mode = payload.get('renderMode') if payload is not None else None
    if payload is not None:
        if set(payload) - {'comment', 'attachments', 'dingFiles', 'files', 'renderMode', 'mentions',
                           'isOnlyNotifyMentions', 'quote', 'hrefPreview'}:
            reasons.append('存在尚未识别的评论字段；完整对象保留')
    quotes = {source_name: source['quote'] for source_name, source in
              (('content', payload or {}), ('event', extra)) if 'quote' in source}
    quote_contexts = {}
    for source_name, quote in quotes.items():
        if not isinstance(quote, str):
            reasons.append('quote 不是字符串；仅保留为引用证据，待复核')
        else:
            parsed_quote = quoted_context(quote)
            if parsed_quote is None:
                reasons.append('quote 不是已支持的纯文本或 _id/content 单层引用；不清洗或猜测内容')
            else:
                quote_contexts[source_name] = parsed_quote
    structured = [item for item in quote_contexts.values() if item['format'] == 'id-content-json']
    if len(structured) > 1 and any(item != structured[0] for item in structured[1:]):
        reasons.append('评论内部与外层引用身份或内容冲突')
    file_sources, previews = {}, {}
    for source_name, source in (('content', payload or {}), ('event', extra)):
        for key in ('attachments', 'dingFiles', 'files', 'mentions'):
            if key in source and not isinstance(source[key], (list, dict) if key == 'mentions' else list):
                reasons.append('附件或提及字段结构异常；证据保留')
        if any(source.get(key) for key in ('attachments', 'dingFiles', 'images', 'data')):
            reasons.append('存在尚未支持的非空附件、图片或数据；不推断内容')
        for key in ('images', 'data'):
            if (key in source and source[key] is not None
                    and not (isinstance(source[key], (list, dict)) and not source[key])):
                reasons.append('图片或数据字段仅支持空容器或 null；其他结构待复核')
        if 'files' in source:
            file_sources[source_name] = _comment_file_ids(source['files'])
            if file_sources[source_name] is None:
                reasons.append('files 必须是无重复的 24 字符文件 ID 列表；不读取文件内容')
        if 'hrefPreview' in source:
            previews[source_name] = source['hrefPreview']
            if not _unavailable_preview(source['hrefPreview'], normalized_body):
                reasons.append('hrefPreview 不符合已核验的不可用预览结构或正文 URL；不读取链接内容')
        if source.get('renderMode') not in (None, '', 'text', 'markdown'):
            reasons.append('renderMode 尚未支持；正文仅作原文字面内容')
        if 'isOnlyNotifyMentions' in source and type(source['isOnlyNotifyMentions']) is not bool:
            reasons.append('isOnlyNotifyMentions 必须为布尔值')
    if payload is not None:
        for key in ('renderMode', 'isOnlyNotifyMentions', 'files', 'attachments', 'dingFiles', 'hrefPreview'):
            if key in payload and key in extra and payload[key] != extra[key]:
                reasons.append('评论内部与外层元数据冲突')
    valid_files = (next(iter(file_sources.values())) if file_sources
                   and all(value is not None for value in file_sources.values()) else [])
    file_count = len(valid_files)
    attachment_shape = bool(payload is not None and body == ''
                            and payload.get('attachments') == [] and payload.get('dingFiles') == []
                            and _comment_file_ids(payload.get('files')) and mode in ('text', 'markdown')
                            and type(payload.get('isOnlyNotifyMentions')) is bool
                            and not quotes and not previews
                            and not any(source.get('mentions') for source in (payload, extra)))
    plain = isinstance(normalized_body, str) and bool(normalized_body.strip())
    rich = plain and unsupported_markup(normalized_body)
    controls = plain and unsafe_controls(body)
    if not plain and not attachment_shape:
        reasons.append('comment 正文缺失、为空或不是字符串')
    elif rich or controls:
        reasons.append('正文包含未支持富文本或控制字符；不推断渲染结果')
    normalized = ' '.join(normalized_body.split()) if plain and not rich and not controls else None
    attachment_only = attachment_shape and not reasons
    # Even a short confirmation can carry substantive attachments or mentions.
    has_extra = (any(extra.get(k) for k in ('attachments', 'dingFiles', 'files', 'images', 'data', 'mentions'))
                 or any(payload_evidence.values())
                 or any(quotes.values()) or bool(previews)
                 or any(source.get('isOnlyNotifyMentions') is True for source in (payload or {}, extra)))
    acknowledgement = bool(normalized and not reasons and not has_extra
                           and normalized.casefold() in {'收到', '好的', '谢谢', 'ok', '👍'})
    return {'event_id': event_id, 'create_time': when.isoformat() if when else None,
            'raw_content': content, 'parsed_payload': payload, 'body': body,
            'normalized_body': normalized_body,
            'quote_evidence': quotes, 'quote_role': QUOTE_ROLE, 'quote_contexts': quote_contexts,
            'preview_evidence': previews, 'file_count': file_count, 'attachment_only': attachment_only,
            'text': normalized, 'render_mode': mode,
            'is_only_notify_mentions': payload.get('isOnlyNotifyMentions') if payload is not None else None,
            'extra_evidence': extra, 'payload_evidence': payload_evidence,
            'omit_from_summary': acknowledgement or attachment_only,
            'omission_reason': ('仅附件评论；统计文件数，未读取文件内容' if attachment_only else
                                '仅确认性回复候选；原文保留，需结合上下文复核' if acknowledgement else None),
            'review_reasons': reasons, 'extraction_needs_review': bool(reasons),
            'semantic_review_required': not attachment_only}



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


def stage_movement(raw):
    """Parse the evidenced task-position move, never a completion transition."""
    if raw.get('action') != 'move.cross.from.stage.to.stage':
        return None
    try:
        data = bounded_object(raw.get('content'))
        if data is None or set(data) != {'from', 'to'}:
            return None
        positions = {}
        for endpoint in ('from', 'to'):
            value = data[endpoint]
            if not isinstance(value, dict) or set(value) != {'tasklist', 'stage'}:
                return None
            parsed = {}
            for key, label_key in (('tasklist', 'title'), ('stage', 'name')):
                node = value[key]
                if not isinstance(node, dict) or set(node) != {'_id', label_key}:
                    return None
                identity = identifier(node['_id'])
                label = node[label_key]
                if (len(identity) != 24 or not isinstance(label, str) or not label.strip()
                        or label != label.strip() or any(char in '\r\n\t' for char in label)
                        or unsafe_controls(label) or unsupported_markup(label)):
                    return None
                label.encode('utf-8', errors='strict')
                parsed[key + '_id'] = identity
                parsed[key + '_label'] = label
            positions[endpoint] = parsed
        before, after = positions['from'], positions['to']
        if (before['tasklist_id'], before['stage_id']) == (after['tasklist_id'], after['stage_id']):
            return None
        for key in ('tasklist', 'stage'):
            if before[key + '_id'] == after[key + '_id'] and before[key + '_label'] != after[key + '_label']:
                return None
        return {'pattern': 'task-position-movement', 'previous': before, 'target': after,
                'parsed_payload': data, 'completion_evidence': False, 'needs_review': False}
    except (CollectionError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


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
    if action == 'move.cross.from.stage.to.stage':
        return ('other', False) if stage_movement(raw) is not None else ('unknown', False)
    if action == 'standard':
        return ('other', False) if (standard_association_notification(raw) is not None
                                   or standard_commit_candidate(raw) is not None) else ('unknown', False)
    kinds = {'create': 'created', 'comment': 'comment', 'involve.add': 'other',
             'update_content': 'other'}
    return kinds.get(action, 'unknown') if isinstance(action, str) else 'unknown', False
