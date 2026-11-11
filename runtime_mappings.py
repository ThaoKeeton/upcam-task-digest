"""Validate explicit mappings and load the private user map in memory only."""

import json
import os
import unicodedata

from teambition_readonly import CollectionError, identifier

USER_MAP_VARIABLE = 'TEAMBITION_USER_MAP_JSON'
MAX_JSON_BYTES = 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Nonstandard JSON constant')


def parse_config_json(source):
    """Reject duplicates anywhere, with errors containing no input or identifiers."""
    try:
        if not isinstance(source, str) or not source.strip() or len(source.encode('utf-8')) > MAX_JSON_BYTES:
            raise ValueError('Invalid JSON input')
        result = json.loads(source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(result, dict):
            raise ValueError('Expected object')
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CollectionError('Invalid configuration or mapping JSON') from None


def validate_name_map(value):
    if not isinstance(value, dict):
        raise CollectionError('Mapping must be an ID-to-name object')
    result = {}
    for key, name in value.items():
        identifier(key)
        if (not isinstance(name, str) or not name.strip() or name != name.strip()
                or any(unicodedata.category(char) in ('Cc', 'Cf') for char in name)):
            raise CollectionError('Mapping contains an invalid name')
        result[key] = name
    return result


def validate_mappings(config):
    """Missing explicit maps stay empty; no name or state is inferred or fetched."""
    owners = validate_name_map(config.get('owner_names', {}))
    states = validate_name_map(config.get('status_names', {}))
    resolved = config.get('resolved_status_ids', [])
    if not isinstance(resolved, list):
        raise CollectionError('Resolved status IDs must be a list')
    seen = set()
    for key in resolved:
        identifier(key)
        if key in seen or key not in states:
            raise CollectionError('Resolved status mapping is duplicate or incomplete')
        seen.add(key)
    if 'status_resolution_known' in config:
        known = config['status_resolution_known']
        if not isinstance(known, list):
            raise CollectionError('Invalid status resolution evidence')
        known_ids = set()
        for key in known:
            identifier(key)
            if key in known_ids or key not in states:
                raise CollectionError('Invalid status resolution evidence')
            known_ids.add(key)
        if not seen <= known_ids:
            raise CollectionError('Invalid status resolution evidence')
    return dict(config, owner_names=owners, status_names=states, resolved_status_ids=list(resolved))


def merge_discovered(config, metadata):
    """Merge verified, session-local MCP identity metadata without persisting it.

    The presence of a state name does not establish whether it is a resolved
    state.  Only IDs with explicit completion evidence are marked as known.
    Existing user mappings remain untouched; conflicting evidence fails closed.
    """
    result = validate_mappings(config)
    allowed = {'owner_names', 'status_names', 'status_resolution_known', 'resolved_status_ids'}
    if not isinstance(metadata, dict) or set(metadata) - allowed:
        raise CollectionError('Invalid discovered project metadata')
    owners = validate_name_map(metadata.get('owner_names', {}))
    statuses = validate_name_map(metadata.get('status_names', {}))

    def id_set(value):
        if not isinstance(value, list):
            raise CollectionError('Invalid discovered project metadata')
        clean = [identifier(item) for item in value]
        if len(clean) != len(set(clean)):
            raise CollectionError('Invalid discovered project metadata')
        return set(clean)

    known = id_set(metadata.get('status_resolution_known', []))
    resolved = id_set(metadata.get('resolved_status_ids', []))
    if not known <= set(statuses) or not resolved <= known:
        raise CollectionError('Invalid discovered project metadata')
    for key, incoming in (('owner_names', owners), ('status_names', statuses)):
        merged = dict(result[key])
        for object_id, name in incoming.items():
            if object_id in merged and merged[object_id] != name:
                raise CollectionError('Conflicting discovered project metadata')
            merged[object_id] = name
        result[key] = merged
    # The existing manual status map was already explicitly classified by the
    # user under the prior contract. New status names have no such guarantee.
    previous_known = set(validate_name_map(config.get('status_names', {})))
    previous_resolved = set(result['resolved_status_ids'])
    overlapping = previous_known & known
    if any((status in previous_resolved) != (status in resolved) for status in overlapping):
        raise CollectionError('Conflicting discovered project metadata')
    result['status_resolution_known'] = sorted(previous_known | known)
    result['resolved_status_ids'] = sorted(previous_resolved | resolved)
    result['allow_unresolved_display'] = True
    return result


def load_runtime_mappings(config, *, environ=None):
    """Used by explicit live collection only, never implicit offline fixture runs.

    Identical ID/name entries merge; conflicts fail before collection. No map,
    credential, or raw error is logged, serialized, or written to disk here.
    """
    result = validate_mappings(config)
    environment = os.environ if environ is None else environ
    if USER_MAP_VARIABLE not in environment:
        return result
    incoming = validate_name_map(parse_config_json(environment[USER_MAP_VARIABLE]))
    if not incoming:
        raise CollectionError('Configured user mapping is empty')
    merged = dict(result['owner_names'])
    for key, name in incoming.items():
        if key in merged and merged[key] != name:
            raise CollectionError('User mappings conflict; refusing to override')
        merged[key] = name
    return dict(result, owner_names=merged)
