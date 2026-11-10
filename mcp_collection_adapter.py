"""Deterministic, read-only MCP collection; no model, network, or storage policy.

The transport owns MCP/HTTP validation, retries, and byte/request budgets. The
collector owns cross-scan consistency and any session/checkpoint budgets. Page
methods here return the original validated business envelope for that collector.
List helpers finish pagination before returning and bound pages and raw rows.

Callers MUST supply explicit validators for the observed contract of each tool;
one tool's success fields do not establish another tool's response contract.
This module has no guessed success codes and does not synthesize missing archive
flags. A validator must return exactly True, not a merely truthy value. Detail
objects and one-item arrays are supported shapes; the caller's validator decides
which have been verified. Project ``my`` selection uses two bounded, sorted scans
with different page sizes. Their agreement is not proof of snapshot completeness.
No state-resolution semantics are inferred from names or current ``isDone``.
"""

import copy
from collections.abc import Mapping
import json
import threading
from types import MappingProxyType
import unicodedata
from datetime import datetime

from teambition_readonly import CollectionError, identifier, validate_binding


_READ_TOOLS = frozenset(('teambition.project.query', 'teambition.project.task-query',
                         'teambition.task.activity', 'teambition.task.query'))


def _timestamp(value):
    try:
        # An explicit separator and timezone prevent date-only and naive input.
        if not isinstance(value, str) or 'T' not in value:
            raise ValueError
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant
    except (ValueError, TypeError, OverflowError):
        raise CollectionError('Missing or invalid timezone-aware source timestamp') from None


def _name(value):
    if (not isinstance(value, str) or not value or value.strip() != value
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)):
        raise CollectionError('Invalid source name')
    return value


def _record_id(row, *, task=False):
    if not isinstance(row, dict):
        raise CollectionError('Expected source object')
    keys = ('id', '_id', 'taskId') if task else ('id', '_id')
    values = [identifier(row[key]) for key in keys if key in row]
    if not values or any(value != values[0] for value in values):
        raise CollectionError('Missing or conflicting source identity')
    return values[0]


def _token(value):
    if value is None or value == '':
        return
    if (not isinstance(value, str) or len(value) > 8192
            or any(unicodedata.category(char) in ('Cc', 'Cs') for char in value)):
        raise CollectionError('Invalid pagination token')


def _page_arguments(page_size, page_token):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise CollectionError('Invalid page size')
    _token(page_token)
    arguments = {'pageSize': page_size}
    if page_token is not None:
        arguments['pageToken'] = page_token
    return arguments


def _fingerprint(row):
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(',', ':'))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CollectionError('Invalid source JSON record') from None


def metadata_from_tasks(rows):
    """Merge only source ID/name evidence, without mutation, I/O, or inference.

    The caller passes final, identity/version-validated task rows after source
    reconciliation. This helper independently validates only the two name pairs.
    An absent name stays unknown. A supplied name without its ID is invalid.
    """
    metadata = {'owner_names': {}, 'status_names': {},
                'status_resolution_known': [], 'resolved_status_ids': []}
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionError('Expected source task object')
        for id_key, name_key, target in (('executorId', 'executorName', 'owner_names'),
                                          ('tfsId', 'tfsName', 'status_names')):
            source_id, source_name = row.get(id_key), row.get(name_key)
            if source_id not in (None, ''):
                identifier(source_id)
            if source_name is None:
                continue
            source_id, source_name = identifier(source_id), _name(source_name)
            names = metadata[target]
            if source_id in names and names[source_id] != source_name:
                raise CollectionError('Conflicting source identity names')
            names[source_id] = source_name
    return metadata


def _detail_row(envelope):
    row = envelope.get('result')
    if isinstance(row, list):
        if len(row) != 1:
            raise CollectionError('Missing or ambiguous task detail')
        row = row[0]
    if not isinstance(row, dict):
        raise CollectionError('Invalid task detail result')
    return row


class MCPCollectionAdapter:
    """Fixed MCP reads with injectable, fail-closed source-contract validators.

    ``validate_envelope`` is a mapping from exact read-only tool names to
    ``validator(envelope)`` callables affirming that tool's verified response
    contract. An unlisted tool is rejected before transport, with no fallback.
    Unknown tool names and non-callable mapping values are invalid. The mapping
    is copied, so later edits to caller-owned configuration cannot expand it.
    A single callable remains supported for explicitly injected offline tests.
    ``validate_task_archive(task)`` affirms the verified archive field contract;
    this adapter independently requires ``isArchived`` to be a boolean and
    checks it against ``include_archived``. Neither validator transforms data.
    All validators and transports must be safe to share if used concurrently.

    ``metadata`` returns a copy of session-local ID/name evidence. Discard this
    adapter after a failed collection; metadata is not a completeness assertion.
    """

    def __init__(self, transport, *, validate_envelope=None, validate_task_archive=None,
                 organization_id=None, max_pages=50, max_records=50000):
        if not callable(getattr(transport, 'call_tool', None)):
            raise CollectionError('MCP transport must provide call_tool')
        if isinstance(validate_envelope, Mapping):
            validators = dict(validate_envelope)
            if any(not isinstance(name, str) or name not in _READ_TOOLS or not callable(validator)
                   for name, validator in validators.items()):
                raise CollectionError('Invalid tool-specific source contract validators')
            validate_envelope = MappingProxyType(validators)
        elif validate_envelope is not None and not callable(validate_envelope):
            raise CollectionError('Invalid source contract validator')
        if validate_task_archive is not None and not callable(validate_task_archive):
            raise CollectionError('Invalid source contract validator')
        if (type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(max_records) is not int or not 1 <= max_records <= 1000000):
            raise CollectionError('Invalid MCP pagination budget')
        self.transport = transport
        self.validate_envelope = validate_envelope
        self.validate_task_archive = validate_task_archive
        self.organization_id = None if organization_id is None else identifier(organization_id)
        self.max_pages, self.max_records = max_pages, max_records
        self._metadata = {'owner_names': {}, 'status_names': {},
                          'status_resolution_known': [], 'resolved_status_ids': []}
        self._metadata_lock = threading.RLock()

    @property
    def metadata(self):
        with self._metadata_lock:
            return copy.deepcopy(self._metadata)

    def _call(self, name, arguments, check):
        if not isinstance(name, str) or name not in _READ_TOOLS:
            raise CollectionError('MCP tool outside read-only allowlist')
        validator = (self.validate_envelope.get(name) if isinstance(self.validate_envelope, Mapping)
                     else self.validate_envelope)
        if validator is None:
            raise CollectionError('MCP tool response contract is not verified')

        def validate(envelope):
            try:
                if not isinstance(envelope, dict) or validator(envelope) is not True:
                    raise CollectionError('MCP business request rejected or contract unverified')
            except Exception:
                raise CollectionError('MCP business request rejected or contract unverified') from None
            check(envelope)
            return True

        result = self.transport.call_tool(name, arguments, validate_envelope=validate)
        # Also validate returned data: an injected transport cannot omit checks.
        validate(result)
        return copy.deepcopy(result)

    def _check_page(self, envelope, page_size, check_row, *, task=False,
                    allow_missing_token=False, reject_duplicates=False):
        rows = envelope.get('result')
        if (not isinstance(rows, list) or len(rows) > page_size
                or ('nextPageToken' not in envelope and not allow_missing_token)):
            raise CollectionError('Invalid or incomplete MCP page envelope')
        if 'nextPageToken' in envelope:
            _token(envelope['nextPageToken'])
        seen = {}
        for row in rows:
            check_row(row)
            identity, fingerprint = _record_id(row, task=task), _fingerprint(row)
            if identity in seen and (reject_duplicates or fingerprint != seen[identity]):
                raise CollectionError('Conflicting duplicate source record')
            seen[identity] = fingerprint

    def _check_project(self, row, project_id):
        identity = _record_id(row)
        if project_id is not None and identity != project_id:
            raise CollectionError('Project identity mismatch')
        organization_id = identifier(row.get('organizationId'))
        if self.organization_id is not None and organization_id != self.organization_id:
            raise CollectionError('Project organization mismatch')
        _name(row.get('name'))
        if type(row.get('isArchived')) is not bool:
            raise CollectionError('Missing or invalid project archive flag')
        if project_id is None and row['isArchived'] is not False:
            raise CollectionError('Project outside requested active project scope')
        # A missing/null/empty prefix remains unresolved. The Client validates
        # nonempty prefixes for its task-number contract; do not infer one here.
        prefix = row.get('uniqueIdPrefix')
        if prefix is not None and not isinstance(prefix, str):
            raise CollectionError('Invalid project prefix type')
        for key in ('created', 'updated'):
            if key in row:
                _timestamp(row[key])

    def _require_archive_contract(self, include_archived):
        if type(include_archived) is not bool:
            raise CollectionError('include_archived must be boolean')
        if self.validate_task_archive is None:
            raise CollectionError('MCP task archive contract is not verified')

    def _check_task(self, row, project_id, include_archived, *, task_id=None, cutoff=None):
        identity = _record_id(row, task=True)
        if row.get('projectId') != project_id or (task_id is not None and identity != task_id):
            raise CollectionError('Task identity or project mismatch')
        if 'organizationId' in row:
            org = identifier(row['organizationId'])
            if self.organization_id is not None and org != self.organization_id:
                raise CollectionError('Task organization mismatch')
        try:
            accepted = self.validate_task_archive(row) is True
        except Exception:
            raise CollectionError('MCP task archive contract rejected') from None
        if not accepted or type(row.get('isArchived')) is not bool:
            raise CollectionError('Missing or unverified task archive flag')
        if row['isArchived'] and not include_archived:
            raise CollectionError('Task outside requested archive scope')
        updated = _timestamp(row.get('updated'))
        if cutoff is not None and updated < cutoff:
            raise CollectionError('Task outside requested updated lower bound')
        if 'created' in row:
            _timestamp(row['created'])
        return updated

    def _merge_names(self, rows, *, commit=False):
        incoming = metadata_from_tasks(rows)
        with self._metadata_lock:
            metadata = copy.deepcopy(self._metadata)
            for target in ('owner_names', 'status_names'):
                for source_id, source_name in incoming[target].items():
                    names = metadata[target]
                    if source_id in names and names[source_id] != source_name:
                        raise CollectionError('Conflicting source identity names')
                    names[source_id] = source_name
            if commit:
                self._metadata = metadata

    def project_page(self, project_id=None, *, page_size=100, page_token=None):
        if project_id is None:
            arguments = _page_arguments(page_size, page_token)
            arguments.update(my=True, includeArchived=False, sort='updated:desc')
            maximum_rows = page_size
        else:
            # A project-ID lookup is a single-record query, not a pageable
            # ``my``/TQL listing. Do not transmit paging parameters in this mode.
            _page_arguments(page_size, None)  # Keep validating the public argument.
            if page_token is not None:
                raise CollectionError('Project ID lookup does not support pagination')
            arguments = {'projectId': identifier(project_id)}
            maximum_rows = 1

        def check(data):
            self._check_page(data, maximum_rows, lambda row: self._check_project(row, project_id),
                             allow_missing_token=True, reject_duplicates=True)
            if project_id is not None and data.get('nextPageToken') not in (None, ''):
                raise CollectionError('Unexpected cursor in project ID lookup')

        return self._call('teambition.project.query', arguments, check)

    def task_page(self, project_id, *, updated_since=None, include_archived=False,
                  page_size=100, page_token=None):
        project_id = identifier(project_id)
        self._require_archive_contract(include_archived)
        arguments = dict(_page_arguments(page_size, page_token), projectId=project_id,
                         includeArchived=include_archived, enrichFields='tfs,members')
        cutoff = None if updated_since is None else _timestamp(updated_since)
        if cutoff is not None:
            # Re-serialize a parsed timestamp, never interpolate untrusted TQL.
            arguments['tql'] = "updated >= '" + cutoff.isoformat() + "'"

        def check(envelope):
            self._check_page(envelope, page_size, lambda row:
                             self._check_task(row, project_id, include_archived, cutoff=cutoff), task=True)
            self._merge_names(envelope['result'])

        result = self._call('teambition.project.task-query', arguments, check)
        self._merge_names(result['result'], commit=True)
        return result

    def _check_activity(self, row, task_id, project_id):
        _record_id(row)
        validate_binding(row, task_id)
        _timestamp(row.get('createTime'))
        if 'projectId' in row:
            source_project = identifier(row['projectId'])
            if project_id is not None and source_project != project_id:
                raise CollectionError('Activity project mismatch')

    def activity_page(self, task_id, *, page_size=100, page_token=None, project_id=None):
        task_id = identifier(task_id)
        if project_id is not None:
            project_id = identifier(project_id)
        arguments = dict(_page_arguments(page_size, page_token), taskId=task_id,
                         actions='', includeFileDetails=False, includeRtfHtml=False)
        return self._call('teambition.task.activity', arguments,
                          lambda data: self._check_page(data, page_size,
                              lambda row: self._check_activity(row, task_id, project_id)))

    def detail_envelope(self, task_id, *, project_id, include_archived=False,
                        expected_updated=None):
        task_id, project_id = identifier(task_id), identifier(project_id)
        self._require_archive_contract(include_archived)
        expected = None if expected_updated is None else _timestamp(expected_updated)

        def check(envelope):
            row = _detail_row(envelope)
            updated = self._check_task(row, project_id, include_archived, task_id=task_id)
            if expected is not None and updated != expected:
                raise CollectionError('Task changed during collection')
            self._merge_names([row])

        result = self._call('teambition.task.query', {'taskId': task_id}, check)
        self._merge_names([_detail_row(result)], commit=True)
        return result

    def task_detail(self, task_id, *, project_id, include_archived=False, expected_updated=None):
        return _detail_row(self.detail_envelope(task_id, project_id=project_id,
                           include_archived=include_archived, expected_updated=expected_updated))

    def _complete_pages(self, fetch_page, *, task=False, allow_missing_token=False,
                        reject_duplicates=False, record_limit=None):
        limit = self.max_records if record_limit is None else record_limit
        records, seen, raw_count, token = [], {}, 0, None
        for _ in range(self.max_pages):
            envelope = fetch_page(token)
            for row in envelope['result']:
                raw_count += 1
                if raw_count > limit:
                    raise CollectionError('MCP raw record budget exhausted; incomplete collection')
                identity, fingerprint = _record_id(row, task=task), _fingerprint(row)
                if identity in seen:
                    if reject_duplicates or fingerprint != seen[identity]:
                        raise CollectionError('Conflicting duplicate source record')
                    continue
                seen[identity] = fingerprint
                records.append(row)
            if allow_missing_token and 'nextPageToken' not in envelope:
                return records
            token = envelope['nextPageToken']
            if token is None or token == '':
                return records
            # Opaque cursors may repeat; only an explicit empty cursor completes.
        raise CollectionError('MCP page budget exhausted; incomplete collection')

    def projects(self, project_id=None, *, page_size=100):
        """Return stable currently visible project choices, possibly incomplete.

        For ``my`` selection, each scan has ``max_pages`` while both scans share
        ``max_records``. A missing project cursor is only a candidate endpoint:
        a second page-size scan must return the same IDs and raw content, with
        no duplicate IDs within either scan. Even agreement is not a snapshot.
        Explicit project-ID lookups use one non-pageable request; an unexpected
        cursor fails rather than being interpreted or followed.
        """
        first = self._complete_pages(lambda token:
            self.project_page(project_id, page_size=page_size, page_token=token),
            allow_missing_token=True, reject_duplicates=True)
        if project_id is not None:
            return first
        alternate_size = 50 if page_size == 100 else 100
        second = self._complete_pages(lambda token:
            self.project_page(page_size=alternate_size, page_token=token),
            allow_missing_token=True, reject_duplicates=True,
            record_limit=self.max_records - len(first))
        first_map = {_record_id(row): _fingerprint(row) for row in first}
        second_map = {_record_id(row): _fingerprint(row) for row in second}
        if first_map != second_map:
            raise CollectionError('Project listing changed between verification scans')
        return first

    def tasks(self, project_id, *, updated_since=None, include_archived=False, page_size=100):
        return self._complete_pages(lambda token:
            self.task_page(project_id, updated_since=updated_since, include_archived=include_archived,
                           page_size=page_size, page_token=token), task=True)

    def activities(self, task_id, *, page_size=100, project_id=None):
        return self._complete_pages(lambda token:
            self.activity_page(task_id, page_size=page_size, page_token=token, project_id=project_id))
