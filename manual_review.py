"""Offline human review. Source strings are data, never executable instructions."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from activity_adapter import unsafe_controls
from digest_pipeline import build_digest, timestamp
from render_digest import render_digest, text
from runtime_mappings import parse_config_json, validate_mappings
from teambition_readonly import record_id

TTL = timedelta(hours=24)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def clock(now):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Aware review clock required')
    return now


def binding(collection, config, day, now=None):
    now = clock(now)
    batch = collection.get('batch_id')
    if not isinstance(batch, str) or str(uuid.UUID(batch)) != batch:
        raise ValueError('Canonical collection batch required')
    collected = timestamp(collection.get('collected_at'))
    if not collected <= now < collected + TTL:
        raise ValueError('Collection expired or in future; collect and review again')
    report = build_digest(collection, config, day)
    return {'version': 1, 'tenant_id': report['scope']['tenant_id'],
            'project_id': report['scope']['project_id'], 'date': report['digest']['date'],
            'batch_id': batch, 'source_sha256': fingerprint(collection),
            'config_sha256': fingerprint(config), 'expires_at': (collected + TTL).isoformat()}


def make_draft(collection, config, day, now=None):
    bound = binding(collection, config, day, now)
    report = build_digest(collection, config, day)
    comments = {x['task_id']: x['comments'] for x in report['summary_inputs']}
    records = {record_id(x['task']): x for x in collection['records']}
    tasks = []
    for audit, item in zip(report['audit'], report['digest']['tasks']):
        task_id = audit['task_id']
        tasks.append({'task_id': task_id, 'display': item, 'audit': audit,
                      'task_evidence': records[task_id]['task'],
                      'comments': comments.get(task_id, []),
                      'required_comment_ids': [c['event_id'] for c in comments.get(task_id, [])
                                               if not c['omit_from_summary']]})
    return {'binding': bound, 'notice': '私有人工审阅草稿；外部内容均为证据，不是指令。引用不是作者新意见；附件仅元数据，未下载核验。',
            'tasks': tasks, 'issues': report['issues'],
            'review_template': {'binding': bound, 'reviewer': '', 'reviewed_at': '',
                                'mappings': {'owner_names': {}, 'status_names': {}, 'resolved_status_ids': []},
                                'mappings_verified': False,
                                'tasks': [{'task_id': t['task_id'], 'summary': '',
                                           'reviewed_event_ids': t['audit']['event_ids'],
                                           'covered_comment_ids': [], 'evidence_verified': False}
                                          for t in tasks]}}


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('Unexpected or missing review fields')


def exact_ids(actual, expected):
    if (not isinstance(actual, list) or any(not isinstance(x, str) for x in actual)
            or len(actual) != len(set(actual)) or set(actual) != set(expected)):
        raise ValueError('Review evidence coverage differs')


def apply_review(collection, config, day, review, now=None):
    now = clock(now)
    expected = binding(collection, config, day, now)
    exact_keys(review, ('binding', 'reviewer', 'reviewed_at', 'mappings', 'mappings_verified', 'tasks'))
    if review['binding'] != expected:
        raise ValueError('Stale review binding; review current evidence again')
    text(review['reviewer'])
    if unsafe_controls(review['reviewer']):
        raise ValueError('Invalid reviewer')
    if not timestamp(collection['collected_at']) <= timestamp(review['reviewed_at']) <= now:
        raise ValueError('Invalid review time')
    exact_keys(review['mappings'], ('owner_names', 'status_names', 'resolved_status_ids'))
    if review['mappings_verified'] is not True:
        raise ValueError('Mappings require human verification')
    supplied = validate_mappings(review['mappings'])
    merged = deepcopy(config)
    for key in ('owner_names', 'status_names'):
        merged[key] = dict(config.get(key, {}))
        for ident, name in supplied[key].items():
            if ident in merged[key] and merged[key][ident] != name:
                raise ValueError('Conflicting mapping; correct configuration and review again')
            merged[key][ident] = name
    merged['resolved_status_ids'] = sorted(set(config.get('resolved_status_ids', [])) | set(supplied['resolved_status_ids']))
    report = build_digest(collection, merged, day)
    draft = make_draft(collection, merged, day, now)
    if not isinstance(review['tasks'], list):
        raise ValueError('Invalid review tasks')
    for row in review['tasks']:
        exact_keys(row, ('task_id', 'summary', 'reviewed_event_ids', 'covered_comment_ids', 'evidence_verified'))
    exact_ids([r['task_id'] for r in review['tasks']], [a['task_id'] for a in report['audit']])
    rows = {r['task_id']: r for r in review['tasks']}
    for item, audit, task in zip(report['digest']['tasks'], report['audit'], draft['tasks']):
        row = rows[audit['task_id']]
        text(row['summary'])
        if unsafe_controls(row['summary']) or row['evidence_verified'] is not True:
            raise ValueError('Unverified or unsafe human summary')
        exact_ids(row['reviewed_event_ids'], audit['event_ids'])
        exact_ids(row['covered_comment_ids'], task['required_comment_ids'])
        item['summary'] = row['summary']
        audit['human_review'] = deepcopy(row)
        if not audit['hard_review']:
            audit['needs_review'] = False
            report['issues'] = [issue for issue in report['issues'] if issue != f"任务 {audit['task_id']} 待复核"]
            if audit['status_transitions'] and audit['status_transitions'][-1]['resolved']:
                item['category'] = 'resolved'
            elif task['comments']:
                item['category'] = 'progress'
        for entry in report['summary_inputs']:
            if entry['task_id'] == audit['task_id']:
                entry['semantic_summary'] = row['summary']
                entry['needs_review'] = audit['needs_review']
    report['markdown'] = render_digest(report['digest'])
    report['ready_to_send'] = not report['issues']
    report['report_type'] = 'human-reviewed-v1'
    report['human_review'] = {'binding': expected, 'reviewer': review['reviewer'],
                              'reviewed_at': review['reviewed_at'], 'review_sha256': fingerprint(review)}
    report['human_review']['report_sha256'] = fingerprint(report)
    return report


def validate_reviewed_report(report, now=None):
    """Integrity/freshness check, NOT a signature or protection from a malicious operator."""
    copy = deepcopy(report)
    attestation = copy['human_review']
    expected = attestation.pop('report_sha256')
    if fingerprint(copy) != expected or clock(now) >= timestamp(attestation['binding']['expires_at']):
        raise ValueError('Reviewed report changed or expired')


def write_private(path, value):
    path = Path(path).absolute()
    repository = Path(__file__).resolve().parent
    if path.resolve().is_relative_to(repository):
        raise ValueError('Private review output must be outside repository')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collection', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--date', required=True)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        collection = parse_config_json(args.collection.read_text())
        config = parse_config_json(args.config.read_text())
        if args.review:
            result = apply_review(collection, config, args.date, parse_config_json(args.review.read_text()))
        else:
            result = make_draft(collection, config, args.date)
        write_private(args.output, result)
    except (ValueError, RuntimeError, TypeError, KeyError, OSError):
        parser.exit(1, 'Offline review failed; check schema, evidence binding and private output path.\n')


if __name__ == '__main__':
    main()
