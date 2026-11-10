"""Synthetic-only regression tests for raw MCP activity summarization."""
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import unittest

from automated_digest import AutomatedDigestBlocked
from report_contract import updated_selection
from teambition_readonly import CollectionError


def pipeline(test):
    if importlib.util.find_spec('mcp_raw_digest') is None:
        test.fail('The standalone MCP raw-activity pipeline is not available')
    return importlib.import_module('mcp_raw_digest')


def api(test, name):
    result = getattr(pipeline(test), name, None)
    test.assertTrue(callable(result), f"Missing raw pipeline API: {name}")
    return result


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / 'fixtures'
        self.collection = json.loads((fixture / 'collection.json').read_text(encoding='utf-8'))
        self.config = json.loads((fixture / 'config.json').read_text(encoding='utf-8'))
        self.collection.update(selection=updated_selection('2026-01-02'),
                               collected_at='2026-01-03T01:00:00Z')
        self.config.update(api_contract_verified=True,
                           view={'verified': True, 'evidence': 'synthetic', 'task_equals': {}})
        self.record = self.collection['records'][0]

    def test_raw_window_includes_every_action_and_preserves_rich_text_exactly(self):
        rich = {'id': 'synthetic-rich', 'action': 'comment',
                'boundToObjectId': 'demo-old-task', 'boundToObjectType': 'task',
                'createTime': '2026-01-02T01:00:00Z',
                'content': '{"comment":"<p>虚构富文本</p>\\u200d", "future": {"nested": [1, 2]}}'}
        unknown = {'id': 'synthetic-future', 'action': 'future.unknown.action',
                   'boundToObjectId': 'demo-old-task', 'boundToObjectType': 'task',
                   'createTime': '2026-01-02T02:00:00Z',
                   'content': {'nested': ['原始值', {'newField': True}]}}
        self.record['activities'].extend([rich, unknown])
        before = copy.deepcopy(self.collection)
        source = pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')
        self.assertEqual(self.collection, before)
        self.assertEqual(len(source['rows']), 1)
        events = source['rows'][0]['activities']
        expected = [self.record['activities'][0], self.record['activities'][1], rich, unknown]
        self.assertEqual(events, expected)
        self.assertEqual(source['rows'][0]['task'], self.record['task'])
        self.assertNotIn(self.record['activities'][2], events)  # right endpoint is excluded

    def test_no_window_activity_means_no_model_rows(self):
        self.record['activities'] = [self.record['activities'][2]]
        source = pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')
        self.assertEqual(source['rows'], [])

    def test_selection_window_is_used_when_call_does_not_repeat_it(self):
        start = '2026-01-01T16:50:00+08:00'
        end = '2026-01-02T08:50:00+08:00'
        self.collection['selection'] = updated_selection('2026-01-02', start, end)
        self.record['activities'][0]['createTime'] = '2026-01-01T16:50:00+08:00'
        self.record['activities'][1]['createTime'] = '2026-01-02T08:50:00+08:00'
        try:
            selected = pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')
        except CollectionError as exc:
            self.fail(f'Valid selected window was rejected: {exc}')
        self.assertEqual(selected['rows'][0]['activities'], [self.record['activities'][0]])
        self.assertEqual(selected['scope']['start'], start)
        self.assertEqual(selected['scope']['end_exclusive'], end)

    def test_missing_activity_time_blocks_instead_of_guessing_scope(self):
        self.record['activities'][0].pop('createTime')
        with self.assertRaises(CollectionError):
            pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')

    def test_incomplete_pagination_and_wrong_project_are_rejected(self):
        for mutation in ('pagination', 'project'):
            source = copy.deepcopy(self.collection)
            if mutation == 'pagination': source['pagination_complete'] = False
            else: source['records'][0]['task']['projectId'] = 'wrong-project'
            with self.subTest(mutation=mutation), self.assertRaises(CollectionError):
                pipeline(self).select_raw_activities(source, self.config, '2026-01-02')

    def test_conflicting_task_alias_or_nested_project_blocks_before_model(self):
        for mutation in ('alias', 'nested_project'):
            collection = copy.deepcopy(self.collection)
            task = collection['records'][0]['task']
            if mutation == 'alias': task['taskId'] = 'other-task'
            else: task['project']['_id'] = 'other-project'
            with self.subTest(mutation=mutation), self.assertRaises(CollectionError):
                pipeline(self).select_raw_activities(collection, self.config, '2026-01-02')

    def test_archive_scope_must_be_boolean_before_model(self):
        self.config['include_archived'] = 0
        with self.assertRaises(CollectionError):
            pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')

    def test_event_explicitly_from_another_project_is_not_sent_to_model(self):
        self.record['activities'][0]['projectId'] = 'other-project'
        with self.assertRaises(CollectionError):
            pipeline(self).select_raw_activities(self.collection, self.config, '2026-01-02')

    def test_invalid_binding_or_conflicting_duplicate_event_blocks(self):
        for mutation in ('binding', 'duplicate'):
            source = copy.deepcopy(self.collection)
            if mutation == 'binding':
                source['records'][0]['activities'][0]['boundToObjectId'] = 'wrong-task'
            else:
                event = copy.deepcopy(source['records'][0]['activities'][0])
                event['content'] = 'conflict'
                source['records'][0]['activities'].append(event)
            with self.subTest(mutation=mutation), self.assertRaises(CollectionError):
                pipeline(self).select_raw_activities(source, self.config, '2026-01-02')


class FakeRawSummarizer:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def summarize_activities(self, key, model, effort, rows, rules, service_tier='default'):
        self.calls.append(copy.deepcopy((rows, rules)))
        if self.response is not None:
            return self.response
        return {row['task_key']: {'category': 'progress', 'summary': '虚构：仍需处理。'} for row in rows}


class GenerationTests(SourceSelectionTests):
    def build(self, client):
        return api(self, 'build_raw_digest')(self.collection, self.config, '2026-01-02',
                                               'synthetic-key', 'gpt-6-luna', 'max', client=client)

    def test_unknown_and_rich_events_reach_model_unmodified_and_report_is_bound(self):
        event = {'id': 'synthetic-rich', 'action': 'unknown.rich.action',
                 'boundToObjectId': 'demo-old-task', 'boundToObjectType': 'task',
                 'createTime': '2026-01-02T01:00:00Z',
                 'content': {'rich': '<p>虚构</p>', 'nested': ['raw']}}
        self.record['activities'].append(event)
        client = FakeRawSummarizer()
        report = self.build(client)
        self.assertEqual(len(client.calls), 1)
        self.assertIn(event, client.calls[0][0][0]['activities'])
        self.assertEqual(report['digest']['tasks'][0]['summary'], '虚构：仍需处理。')
        self.assertEqual(report['digest']['tasks'][0]['category'], 'progress')
        self.assertTrue(report['ready_to_send'])
        api(self, 'validate_raw_report')(report, self.collection, self.config, 'gpt-6-luna', 'max')
        modified = copy.deepcopy(report)
        modified['digest']['tasks'][0]['summary'] = '篡改'
        with self.assertRaises(AutomatedDigestBlocked):
            api(self, 'validate_raw_report')(modified, self.collection, self.config, 'gpt-6-luna', 'max')

    def test_empty_window_never_calls_model(self):
        self.record['activities'] = [self.record['activities'][2]]
        client = FakeRawSummarizer()
        report = self.build(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(report['digest']['tasks'], [])
        api(self, 'validate_raw_report')(report, self.collection, self.config, 'gpt-6-luna', 'max')

    def test_missing_extra_or_invalid_model_result_blocks(self):
        for output in ({}, {'task-1': {'category': 'wrong', 'summary': ''}},
                       {'task-1': {'category': 'progress', 'summary': ''}, 'task-2': {'category': 'other', 'summary': ''}}):
            with self.subTest(output=output), self.assertRaises(AutomatedDigestBlocked):
                self.build(FakeRawSummarizer(output))

    def test_request_limit_is_explained_without_truncation_or_source_contents(self):
        from openai_summarizer import SummaryError
        class TooLarge:
            def summarize_activities(self, *args, **kwargs):
                raise SummaryError("Raw activity request exceeds the local request size limit")
        with self.assertRaises(AutomatedDigestBlocked) as raised:
            self.build(TooLarge())
        self.assertIn("256 KiB", str(raised.exception))
        self.assertIn("缩短时间范围", str(raised.exception))
        self.assertNotIn("虚构旧任务", str(raised.exception))

    def test_undated_activity_diagnostic_names_time_boundary_problem(self):
        self.record['activities'][0]['createTime'] = None
        diagnostic = api(self, 'diagnose_raw_source')(self.collection, self.config, '2026-01-02')
        self.assertFalse(diagnostic['ok'])
        self.assertIn('动态时间', diagnostic['message'])
        self.assertIn('未调用 OpenAI', diagnostic['message'])

    def test_readonly_diagnostic_does_not_reject_unknown_or_rich(self):
        self.record['activities'][0]['action'] = 'new.unknown'
        self.record['activities'][0]['content'] = '<p>synthetic</p>'
        diagnostic = api(self, 'diagnose_raw_source')(self.collection, self.config, '2026-01-02')
        self.assertTrue(diagnostic['ok'])
        self.assertIn('未调用 OpenAI', diagnostic['message'])
