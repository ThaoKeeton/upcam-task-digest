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
    return dict(config, owner_names=owners, status_names=states, resolved_status_ids=list(resolved))


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
