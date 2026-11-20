"""Fail-closed, deterministic Teambition MCP Streamable HTTP transport.

No model, discovery-driven planning, credential persistence, automatic retries,
redirects, or server-initiated requests. Tool descriptions are never retained.
The caller supplies a *confirmed* business-envelope success policy; this module
intentionally does not guess the service's ``code``/``errorCode`` success values.

``MCPTransport(token_provider=callable, transport=None, before_request=None,
after_request=None, before_dispatch=None)`` uses urllib-compatible injected
transports for offline tests.
Every HTTP attempt, including initialize/notification/catalog, is bracketed by:

* ``reservation = before_request(metadata)`` before dispatch. Raising prevents
  dispatch. Metadata contains method, optional allowlisted tool name, request
  byte count, timeout and response byte limit, never arguments, credentials or
  bodies. A returned dict can narrow this request through these controls:
  ``_mcp_response_limit``: positive integer, at most the offered response_limit;
  ``_mcp_timeout``: finite positive number, at most the offered timeout;
  ``_mcp_page_size``: positive integer, at most an explicitly present, already
  validated tools/call pageSize. It never adds pageSize to a detail/handshake.
  The adjusted pageSize is revalidated against the current catalog schema and
  the request is reencoded. Unknown _mcp_* controls and request-patch fields
  are rejected. Legacy ledger ``params`` may contain only pageSize and is never
  applied to the request. Other non-request ledger fields remain opaque.
* ``before_dispatch(metadata)`` is optional final admission, after all request
  construction/encoding/schema work. Its metadata contains only effective safe
  limits and method/tool names. It may return None (unchanged timeout), or a
  finite positive timeout no larger than the effective timeout. It cannot patch
  any request field. A callback failure or invalid return prevents dispatch and
  still invokes after_request once with dispatched=False. Only local scalar
  checks/accounting and the final monotonic deadline occur before transport.
* ``after_request(reservation, outcome)`` exactly once after a reservation has
  been returned, even if adjustment fails before dispatch. Outcome adds
  response_bytes, http_status, result_count, dispatched, completed and uncertain;
  it reports effective limits and the actual reencoded request byte count.
  ``records``/``complete`` are compatibility aliases. ``response_limit + 1``
  bytes must be reserved to cover the bounded oversize-detection byte. Invalid
  adjustment has dispatched=False, completed=False, uncertain=True and no read
  bytes. Dispatched unknown, oversized, malformed or interrupted outcomes are
  uncertain: a durable budget must retain its conservative reservation rather
  than settle to zero. A before_request exception returns no reservation and
  therefore does not call after_request.

``request_count`` counts dispatched attempts and ``response_bytes`` counts bytes
actually read, including rejected bodies. Instances serialize calls, bind session
IDs to a token fingerprint, and stop after protocol/transport failures.
Response size overflow raises the distinct safe MCPResponseLimitError;
other malformed responses remain CollectionError. Safe transient HTTP statuses
raise MCPHTTPError with bounded retry_after metadata; only the caller decides whether and when to retry. Create a
new instance to recover deliberately; no uncertain request is silently repeated.
"""

import copy
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from teambition_readonly import CollectionError

ENDPOINT = 'https://open.teambition.com/api/mcp/v2'
PROTOCOL_VERSION = '2025-03-26'
_AUTH_ERROR = 'Teambition MCP authorization expired or denied'
_FAILURE = 'Teambition MCP request failed or response was invalid'
_RETRYABLE_STATUSES = frozenset((429, 500, 502, 503, 504))


class MCPResponseLimitError(CollectionError):
    """The bounded response exceeds this request's configured byte limit."""

    def __init__(self):
        super().__init__('Teambition MCP response byte limit exceeded')


class MCPHTTPError(CollectionError):
    """Safe transient status; retrying is an explicit caller budget decision."""

    def __init__(self, status, retry_after=None):
        super().__init__('Teambition MCP HTTP request failed')
        self.status = status
        self.retry_after = retry_after
        self.retryable = status in _RETRYABLE_STATUSES


def _http_failure(status, headers):
    if status in (401, 403):
        raise CollectionError(_AUTH_ERROR)
    if status in _RETRYABLE_STATUSES:
        value = _header(headers, 'Retry-After')
        delay = None
        if value is not None:
            try:
                if value.isascii() and value.isdigit():
                    delay = int(value)
                else:
                    instant = parsedate_to_datetime(value)
                    if instant.tzinfo is None:
                        _reject()
                    delay = max(0, (instant - datetime.now(timezone.utc)).total_seconds())
                if not 0 <= delay <= 60:
                    _reject()
            except (ValueError, TypeError, OverflowError):
                raise CollectionError(_FAILURE) from None
        raise MCPHTTPError(status, delay)
    _reject()

# A remote catalog may narrow these fields, but never expand local authority.
_FIELDS = {
    'teambition.project.query': {
        'projectId': 'string', 'my': 'boolean', 'tql': 'string', 'pageSize': 'integer',
        'pageToken': 'string', 'sort': 'string', 'includeArchived': 'boolean'},
    'teambition.project.task-query': {
        'projectId': 'string', 'tql': 'string', 'pageSize': 'integer',
        'pageToken': 'string', 'enrichFields': 'string', 'includeArchived': 'boolean',
        'unfinished': 'boolean', 'assignedToMe': 'boolean'},
    'teambition.task.activity': {
        'taskId': 'string', 'actions': 'string', 'excludeActions': 'string',
        'pageToken': 'string', 'language': 'string', 'pageSize': 'integer',
        'fileExpireSeconds': 'integer', 'includeFileDetails': 'boolean',
        'includeRtfHtml': 'boolean'},
    # These two names were observed in discovery, not business-call verified.
    'teambition.task.query': {
        'taskId': 'string', 'parentTaskId': 'string', 'tql': 'string', 'fields': 'array',
        'enrichFields': 'string', 'includeArchived': 'boolean', 'pageSize': 'integer',
        'pageToken': 'string'},
    'teambition.member.query': {
        'userId': 'string', 'name': 'string', 'search': 'string', 'email': 'string',
        'employeeNumber': 'string', 'pageToken': 'string', 'pageSize': 'integer'},
}
READ_ONLY_TOOLS = frozenset(_FIELDS)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject():
    raise CollectionError(_FAILURE)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _reject()
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda value: _reject())
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise CollectionError(_FAILURE) from None


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CollectionError(_FAILURE) from None


def _is_type(value, kind):
    return {'string': isinstance(value, str), 'integer': type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1,
            'number': type(value) is int or (type(value) is float and math.isfinite(value)),
            'boolean': type(value) is bool, 'array': isinstance(value, list),
            'object': isinstance(value, dict), 'null': value is None}.get(kind, False)


_ANNOTATIONS = {'title', 'description', 'default', 'examples', '$schema', '$id',
                'deprecated', 'readOnly', 'writeOnly'}
_CONSTRAINTS = {'type', 'properties', 'required', 'additionalProperties', 'items',
                'enum', 'const', 'minimum', 'maximum', 'exclusiveMinimum',
                'exclusiveMaximum', 'minLength', 'maxLength', 'minItems',
                'maxItems', 'uniqueItems', 'minProperties', 'maxProperties'}


def _validate_schema(schema, value, *, depth=0):
    """Deliberately bounded JSON Schema subset; unknown constraints fail closed."""
    if (depth > 16 or not isinstance(schema, dict)
            or set(schema) - _ANNOTATIONS - _CONSTRAINTS):
        _reject()
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if (not kinds or any(not isinstance(kind, str) or kind not in ('string', 'integer', 'number', 'boolean',
                                     'array', 'object', 'null') for kind in kinds)
            or not any(_is_type(value, kind) for kind in kinds)):
        _reject()
    if 'enum' in schema:
        if not isinstance(schema['enum'], list) or not any(
                type(value) is type(item) and value == item for item in schema['enum']):
            _reject()
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        _reject()
    for key, size in (('minLength', len(value) if isinstance(value, str) else None),
                      ('maxLength', len(value) if isinstance(value, str) else None),
                      ('minItems', len(value) if isinstance(value, list) else None),
                      ('maxItems', len(value) if isinstance(value, list) else None),
                      ('minProperties', len(value) if isinstance(value, dict) else None),
                      ('maxProperties', len(value) if isinstance(value, dict) else None)):
        if key in schema:
            bound = schema[key]
            if type(bound) is not int or bound < 0:
                _reject()
            if size is not None and ((key.startswith('min') and size < bound)
                                     or (key.startswith('max') and size > bound)):
                _reject()
    for key in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
        if key in schema:
            bound = schema[key]
            if type(bound) not in (int, float) or (type(bound) is float and not math.isfinite(bound)):
                _reject()
            if type(value) in (int, float) and (
                    (key == 'minimum' and value < bound)
                    or (key == 'maximum' and value > bound)
                    or (key == 'exclusiveMinimum' and value <= bound)
                    or (key == 'exclusiveMaximum' and value >= bound)):
                _reject()
    if isinstance(value, dict):
        props, required = schema.get('properties', {}), schema.get('required', [])
        if (not isinstance(props, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required) or set(required) - props.keys()
                or set(required) - value.keys()
                or type(schema.get('additionalProperties', False)) is not bool
                or set(value) - props.keys()):
            _reject()
        for key, item in value.items():
            _validate_schema(props[key], item, depth=depth + 1)
    if isinstance(value, list):
        if 'items' not in schema or type(schema.get('uniqueItems', False)) is not bool:
            _reject()
        for item in value:
            _validate_schema(schema['items'], item, depth=depth + 1)
        if schema.get('uniqueItems') and len({_encode(item) for item in value}) != len(value):
            _reject()


def _header(headers, name):
    values = []
    if hasattr(headers, 'get_all'):
        values = headers.get_all(name, [])
    elif hasattr(headers, 'items'):
        values = [value for key, value in headers.items() if str(key).lower() == name.lower()]
    else:
        _reject()
    if len(values) > 1 or any(not isinstance(value, str) for value in values):
        _reject()
    return values[0] if values else None


def _sse(raw, *, allow_pending=False):
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeError:
        raise CollectionError(_FAILURE) from None
    chunks = text.split('\n\n')
    # SSE dispatches only complete events, not an unterminated final data line.
    if chunks[-1].strip() or len(chunks) > 129:
        _reject()
    messages = []
    for chunk in chunks[:-1]:
        data, event = [], 'message'
        for line in chunk.split('\n'):
            if not line or line.startswith(':'):
                continue
            name, separator, value = line.partition(':')
            if value.startswith(' '):
                value = value[1:]
            if name == 'data':
                data.append(value)
            elif name == 'event':
                event = value
            elif name == 'retry' and (not value.isascii() or not value.isdigit()):
                _reject()
            elif name == 'id' and '\x00' in value:
                _reject()
        if data:
            if event not in ('message', ''):
                _reject()
            message = _json('\n'.join(data))
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                _reject()
            if 'method' in message:
                # Passive notifications carry untrusted text, never instructions.
                # Catalog changes require a new client, not use of stale schemas.
                if ('id' in message or 'result' in message or 'error' in message
                        or message['method'] not in ('notifications/progress', 'notifications/message')
                        or not isinstance(message.get('params'), dict)):
                    _reject()
                continue
            messages.append(message)
    if len(messages) > 1 or (not messages and not allow_pending):
        _reject()
    return messages[0] if messages else None


class MCPTransport:
    def __init__(self, *, token_provider, transport=None, before_request=None,
                 after_request=None, before_dispatch=None, timeout=30, max_response_bytes=8 * 1024 * 1024,
                 max_request_bytes=1024 * 1024, max_catalog_pages=50, clock=time.monotonic):
        if (not callable(token_provider) or not callable(clock)
                or (transport is not None and not callable(transport))
                or (before_request is not None and not callable(before_request))
                or (after_request is not None and not callable(after_request))
                or (before_dispatch is not None and not callable(before_dispatch))
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 30
                or type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 8 * 1024 * 1024
                or type(max_request_bytes) is not int or not 1024 <= max_request_bytes <= 1024 * 1024
                or type(max_catalog_pages) is not int or not 1 <= max_catalog_pages <= 100):
            _reject()
        self.token_provider = token_provider
        self.transport = transport or build_opener(ProxyHandler({}), NoRedirect()).open
        self.before_request, self.after_request = before_request, after_request
        self.before_dispatch = before_dispatch
        self.timeout, self.clock = timeout, clock
        self.max_response_bytes, self.max_request_bytes = max_response_bytes, max_request_bytes
        self.max_catalog_pages = max_catalog_pages
        self.request_count, self.response_bytes = 0, 0
        self._session = self._token_fingerprint = self._catalog = None
        self._initialized = self._negotiated = self._failed = False
        self._request_id = 0
        self._lock = threading.RLock()

    def _token(self):
        try:
            token = self.token_provider()
        except Exception:
            raise CollectionError(_AUTH_ERROR) from None
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise CollectionError(_AUTH_ERROR)
        digest = hashlib.sha256(token.encode('ascii')).digest()
        if self._token_fingerprint is not None and digest != self._token_fingerprint:
            raise CollectionError(_AUTH_ERROR)
        self._token_fingerprint = digest
        return token

    def _bounded_request(self, message, reservation):
        """Apply only explicitly supported, narrowing reservation controls."""
        response_limit, timeout = self.max_response_bytes, self.timeout
        if isinstance(reservation, dict):
            allowed = {'_mcp_response_limit', '_mcp_page_size', '_mcp_timeout'}
            if any(isinstance(key, str) and key.startswith('_mcp_') and key not in allowed
                   for key in reservation):
                _reject()
            # Legacy ledger params contain only pageSize. They are never a
            # request patch: only the explicit _mcp_page_size control is applied.
            if ('params' in reservation and (not isinstance(reservation['params'], dict)
                    or set(reservation['params']) - {'pageSize'})):
                _reject()
            if set(reservation) & {'arguments', 'headers', 'endpoint', 'url',
                                  'method', 'tool_name', 'name'}:
                _reject()
            if '_mcp_response_limit' in reservation:
                response_limit = reservation['_mcp_response_limit']
                if type(response_limit) is not int or not 0 < response_limit <= self.max_response_bytes:
                    _reject()
            if '_mcp_timeout' in reservation:
                timeout = reservation['_mcp_timeout']
                if (type(timeout) not in (int, float) or not 0 < timeout <= self.timeout
                        or not math.isfinite(timeout)):
                    _reject()
            if '_mcp_page_size' in reservation:
                size = reservation['_mcp_page_size']
                params = message.get('params', {})
                arguments = params.get('arguments', {})
                original = arguments.get('pageSize')
                if (message['method'] != 'tools/call' or type(size) is not int
                        or type(original) is not int or not 0 < size <= original
                        or params.get('name') not in READ_ONLY_TOOLS
                        or self._catalog is None or params['name'] not in self._catalog):
                    _reject()
                # Never mutate the original arguments or use ledger params.
                message = copy.deepcopy(message)
                arguments = message['params']['arguments']
                arguments['pageSize'] = size
                _validate_schema(self._catalog[params['name']], arguments)
        body = _encode(message)
        if len(body) > self.max_request_bytes:
            _reject()
        return message, body, response_limit, timeout

    def _exchange(self, method, params, *, decode, notification=False):
        if self._failed:
            _reject()
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        request_id = None
        if not notification:
            self._request_id += 1
            request_id = self._request_id
            message['id'] = request_id
        body = _encode(message)
        if len(body) > self.max_request_bytes:
            _reject()
        headers = {'Authorization': 'Bearer ' + self._token(),
                   'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if method != 'initialize':
            headers['MCP-Protocol-Version'] = PROTOCOL_VERSION
        if self._session is not None:
            headers['Mcp-Session-Id'] = self._session
        metadata = {'method': method, 'request_bytes': len(body),
                    'response_limit': self.max_response_bytes, 'timeout': self.timeout}
        if method == 'tools/call':
            metadata['tool_name'] = params['name']
            metadata['pageSize'] = params['arguments'].get('pageSize', 1)
        try:
            reservation = self.before_request(dict(metadata)) if self.before_request else None
        except CollectionError:
            raise
        except Exception:
            raise CollectionError(_FAILURE) from None
        outcome = dict(metadata, response_bytes=0, http_status=None, records=0,
                       complete=False, completed=False, result_count=0, uncertain=True, dispatched=False)
        try:
            message, body, response_limit, timeout = self._bounded_request(message, reservation)
            outcome.update(request_bytes=len(body), response_limit=response_limit, timeout=timeout)
            if method == 'tools/call':
                outcome['pageSize'] = message['params']['arguments'].get('pageSize', 1)
            request = Request(ENDPOINT, method='POST', data=body, headers=headers)
            if self.before_dispatch is not None:
                final_timeout = self.before_dispatch({key: outcome[key] for key in metadata})
                if final_timeout is not None:
                    if (type(final_timeout) not in (int, float)
                            or not 0 < final_timeout <= timeout or not math.isfinite(final_timeout)):
                        _reject()
                    timeout = final_timeout
            # No encoding, schema validation, durable I/O or owner-thread RPC
            # after final admission. Only local scalar accounting precedes send.
            outcome['timeout'] = timeout
            deadline = self.clock() + timeout
            self.request_count += 1
            outcome['dispatched'] = True
            with self.transport(request, timeout=timeout) as response:
                status = response.status
                outcome['http_status'] = status if type(status) is int else None
                if type(status) is not int:
                    _reject()
                if status != (202 if notification else 200):
                    _http_failure(status, response.headers)
                if not callable(getattr(response, 'geturl', None)) or response.geturl() != ENDPOINT:
                    _reject()
                encoding = _header(response.headers, 'Content-Encoding')
                if encoding is not None and encoding.lower() != 'identity':
                    _reject()
                session = _header(response.headers, 'Mcp-Session-Id')
                if session is not None:
                    if not 1 <= len(session) <= 256 or any(not 33 <= ord(c) <= 126 for c in session):
                        _reject()
                    if method == 'initialize' and self._session is None:
                        self._session = session
                    elif session != self._session:
                        _reject()
                length = _header(response.headers, 'Content-Length')
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        _reject()
                    if int(length) > response_limit:
                        raise MCPResponseLimitError()
                content_type = _header(response.headers, 'Content-Type')
                media = (content_type or '').split(';', 1)[0].strip().lower()
                if not notification and media not in ('application/json', 'text/event-stream'):
                    _reject()
                chunks, streamed = [], None
                read = getattr(response, 'read1', response.read)
                while True:
                    if self.clock() >= deadline:
                        _reject()
                    remaining = response_limit + 1 - outcome['response_bytes']
                    chunk = read(min(65536, remaining))
                    if not isinstance(chunk, bytes):
                        _reject()
                    outcome['response_bytes'] += len(chunk)
                    self.response_bytes += len(chunk)
                    if outcome['response_bytes'] > response_limit:
                        raise MCPResponseLimitError()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if media == 'text/event-stream' and not notification:
                        current = b''.join(chunks)
                        if current.endswith((b'\n\n', b'\r\r', b'\r\n\r\n')):
                            streamed = _sse(current, allow_pending=True)
                            if streamed is not None:
                                break
                raw = b''.join(chunks)
                if self.clock() >= deadline or (length is not None and int(length) != len(raw)):
                    _reject()
                if notification:
                    if raw:
                        _reject()
                    result = None
                else:
                    if media == 'application/json':
                        data = _json(raw)
                    elif media == 'text/event-stream':
                        data = streamed if streamed is not None else _sse(raw)
                    else:
                        _reject()
                    if (not isinstance(data, dict) or data.get('jsonrpc') != '2.0'
                            or type(data.get('id')) is not int or data['id'] != request_id
                            or 'error' in data or 'method' in data or 'result' not in data):
                        _reject()
                    result = decode(data['result'])
                    if method == 'tools/call':
                        rows = result['result']
                        outcome['records'] = len(rows) if isinstance(rows, list) else 1
                        outcome['result_count'] = outcome['records']
                        if ('pageSize' in message['params']['arguments']
                                and outcome['result_count'] > message['params']['arguments']['pageSize']):
                            _reject()
            outcome['complete'], outcome['completed'], outcome['uncertain'] = True, True, False
            return result
        except HTTPError as exc:
            outcome['http_status'] = exc.code if type(exc.code) is int else None
            status = exc.code
            try:
                exc.close()
            except Exception:
                pass
            try:
                _http_failure(status, exc.headers)
            except MCPHTTPError as failure:
                raise failure from None
            except CollectionError as failure:
                self._failed = True
                raise failure from None
        except MCPHTTPError:
            raise
        except CollectionError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
            raise CollectionError(_FAILURE) from None
        finally:
            if self.after_request is not None:
                try:
                    self.after_request(reservation, dict(outcome))
                except CollectionError:
                    self._failed = True
                    raise
                except Exception:
                    self._failed = True
                    raise CollectionError(_FAILURE) from None

    def initialize(self):
        with self._lock:
            if self._initialized:
                if self._failed:
                    _reject()
                return
            def validate(result):
                if (not isinstance(result, dict) or result.get('protocolVersion') != PROTOCOL_VERSION
                        or not isinstance(result.get('capabilities'), dict)
                        or not isinstance(result['capabilities'].get('tools'), dict)
                        or not isinstance(result.get('serverInfo'), dict)
                        or not isinstance(result['serverInfo'].get('name'), str)
                        or not isinstance(result['serverInfo'].get('version'), str)):
                    _reject()
                return None
            if not self._negotiated:
                self._exchange('initialize', {'protocolVersion': PROTOCOL_VERSION,
                               'capabilities': {}, 'clientInfo': {'name': 'upcam-readonly', 'version': '1'}}, decode=validate)
                self._negotiated = True
            self._exchange('notifications/initialized', None, decode=lambda value: None, notification=True)
            self._initialized = True

    def list_tools(self):
        """Return a defensive copy of allowlisted schemas, with no descriptions."""
        with self._lock:
            self.initialize()
            if self._catalog is None:
                catalog, names, cursors = {}, set(), set()
                cursor = None
                for _ in range(self.max_catalog_pages):
                    def validate(result):
                        if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
                            _reject()
                        if len(result['tools']) > 1000:
                            _reject()
                        for item in result['tools']:
                            if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                                    or not item['name'] or item['name'] in names
                                    or not isinstance(item.get('inputSchema'), dict)):
                                _reject()
                            names.add(item['name'])
                            if item['name'] in READ_ONLY_TOOLS:
                                catalog[item['name']] = copy.deepcopy(item['inputSchema'])
                        next_cursor = result.get('nextCursor')
                        if next_cursor not in (None, '') and (
                                not isinstance(next_cursor, str) or len(next_cursor) > 8192
                                or next_cursor in cursors):
                            _reject()
                        return next_cursor
                    cursor = self._exchange('tools/list', {'cursor': cursor} if cursor is not None else {}, decode=validate)
                    if cursor in (None, ''):
                        self._catalog = catalog
                        break
                    cursors.add(cursor)
                else:
                    _reject()
            return copy.deepcopy(self._catalog)

    def call_tool(self, name, arguments, *, validate_envelope=None):
        """Call an allowlisted read only after local + current-schema validation.

        ``validate_envelope(envelope)`` must return exactly True. Its input is a
        deep copy; mutations do not modify the returned server envelope. No
        policy means no business request. Every error is a CollectionError.
        """
        with self._lock:
            if (not isinstance(name, str) or name not in READ_ONLY_TOOLS
                    or not isinstance(arguments, dict) or not callable(validate_envelope)
                    or set(arguments) - _FIELDS[name].keys()):
                _reject()
            try:
                arguments = copy.deepcopy(arguments)
            except Exception:
                raise CollectionError(_FAILURE) from None
            for key, value in arguments.items():
                if not _is_type(value, _FIELDS[name][key]):
                    _reject()
                if key == 'fields' and any(not isinstance(item, str) for item in value):
                    _reject()
            # Serialization also bounds user-provided data before discovery.
            if len(_encode(arguments)) > self.max_request_bytes:
                _reject()
            catalog = self.list_tools()
            if name not in catalog:
                _reject()
            _validate_schema(catalog[name], arguments)
            def validate(result):
                if (not isinstance(result, dict) or ('isError' in result and type(result['isError']) is not bool)
                        or result.get('isError') is True):
                    _reject()
                structured = result.get('structuredContent')
                content = result.get('content')
                textual = None
                if content is not None and (not isinstance(content, list) or len(content) > 128):
                    _reject()
                if structured is None and content is not None:
                    if (not isinstance(content, list) or len(content) != 1
                            or not isinstance(content[0], dict) or content[0].get('type') != 'text'
                            or not isinstance(content[0].get('text'), str)):
                        _reject()
                    textual = _json(content[0]['text'])
                envelope = structured if structured is not None else textual
                if (not isinstance(envelope, dict) or 'result' not in envelope
                        or not isinstance(envelope['result'], (dict, list))
                        or envelope.get('errorMessage') not in (None, '')
                        or ('nextPageToken' in envelope and envelope['nextPageToken'] is not None
                            and (not isinstance(envelope['nextPageToken'], str)
                                 or len(envelope['nextPageToken']) > 8192))):
                    _reject()
                try:
                    approved = validate_envelope(copy.deepcopy(envelope)) is True
                except Exception:
                    raise CollectionError(_FAILURE) from None
                if not approved:
                    _reject()
                return envelope
            return self._exchange('tools/call', {'name': name, 'arguments': copy.deepcopy(arguments)}, decode=validate)
