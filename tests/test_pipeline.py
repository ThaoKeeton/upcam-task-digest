import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest

from digest_pipeline import build_digest, day_window, timestamp
from teambition_readonly import CollectionError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((FIXTURES/'collection.json').read_text())
        self.config = json.loads((FIXTURES/'config.json').read_text())
        # Only synthetic test contracts are marked verified in memory.
        self.config['api_contract_verified'] = True
        self.config['view'].update(verified=True, evidence='fictional-test-contract')

    def result(self):
        return build_digest(self.data, self.config, '2026-01-02')

    def test_shanghai_yesterday_and_aware_half_open_boundaries(self):
        start,end = day_window(now=datetime(2026,1,2,17,tzinfo=timezone.utc))
        self.assertEqual(start.isoformat(), '2026-01-02T00:00:00+08:00')
        self.assertEqual(end.isoformat(), '2026-01-03T00:00:00+08:00')
        for value in ('2026-01-02', '2026-01-02T12:00:00', None):
            with self.assertRaises(ValueError):
                timestamp(value)
        with self.assertRaises(ValueError):
            day_window(now=datetime(2026,1,2))
        result = self.result()
        self.assertIn('评论 2 条', result['digest']['tasks'][0]['summary'])
        self.assertEqual(result['audit'][0]['event_ids'], ['demo-event-1','demo-event-2'])
        self.assertFalse(result['ready_to_send'])

    def test_old_task_comment_and_current_status_never_infers_resolution(self):
        self.data['records'][0]['task'].update(isDone=True, tfsId='demo-done', created='2026-01-02T00:00:00Z')
        self.assertEqual(self.result()['digest']['tasks'][0]['category'], 'other')
        self.data['records'][0]['activities'] = []
        self.assertEqual(self.result()['digest']['tasks'], [])

    def test_status_resolution_reopen_and_deterministic_order(self):
        events = self.data['records'][0]['activities']
        events[:] = [
            {'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'update.taskflowstatus','createTime':'2026-01-02T01:00:00Z','content':json.dumps({'tfsId': 'demo-done', 'oldTfsId': 'demo-open', 'taskflowstatus': '已解决', 'oldTaskflowstatus': '待处理', 'isDone': True})},
            {'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'b','action':'update.taskflowstatus','createTime':'2026-01-02T02:00:00Z','content':json.dumps({'tfsId': 'demo-open', 'oldTfsId': 'demo-open', 'taskflowstatus': '待处理', 'oldTaskflowstatus': '待处理', 'isDone': False})},
        ]
        self.assertEqual(self.result()['digest']['tasks'][0]['category'], 'other')
        expected = self.result(); events.reverse()
        self.assertEqual(expected, self.result())
        events[:] = [events[1]]
        self.assertEqual(self.result()['digest']['tasks'][0]['category'], 'resolved')

    def test_event_categories_merge_once_with_deduplicated_evidence(self):
        events = self.data['records'][0]['activities']
        events[:] = [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'create','createTime':'2026-01-02T01:00:00Z'}]
        events.append(copy.deepcopy(events[0]))
        report = self.result()
        self.assertEqual(report['digest']['tasks'][0]['category'],'new')
        self.assertEqual(report['digest']['tasks'][0]['summary'],'新建记录 1 条')
        events.append({'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'b','action':'comment','createTime':'2026-01-02T02:00:00Z'})
        report = self.result()
        self.assertEqual(report['digest']['tasks'][0]['category'],'other')
        self.assertEqual(len(report['digest']['tasks']),1)

    def test_unknown_type_time_status_target_and_same_time_status_block_send(self):
        cases = [
            [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'unrecognized','createTime':'2026-01-02T01:00:00Z'}],
            [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'comment','createTime':'not-a-time'}],
            [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'update.taskflowstatus','createTime':'2026-01-02T01:00:00Z'}],
            [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'update.taskflowstatus','createTime':'2026-01-02T01:00:00Z','content':json.dumps({'tfsId': 'unmapped', 'oldTfsId': 'demo-open', 'taskflowstatus': '未知状态', 'oldTaskflowstatus': '待处理', 'isDone': False})}],
            [{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'a','action':'update.taskflowstatus','createTime':'2026-01-02T01:00:00Z','content':json.dumps({'tfsId': 'demo-done', 'oldTfsId': 'demo-open', 'taskflowstatus': '已解决', 'oldTaskflowstatus': '待处理', 'isDone': True})},
             {'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'b','action':'update.taskflowstatus','createTime':'2026-01-02T01:00:00Z','content':json.dumps({'tfsId': 'demo-open', 'oldTfsId': 'demo-open', 'taskflowstatus': '待处理', 'oldTaskflowstatus': '待处理', 'isDone': False})}],
        ]
        for events in cases:
            with self.subTest(events=events):
                self.data['records'][0]['activities'] = events
                report = self.result()
                self.assertFalse(report['ready_to_send'])
                self.assertTrue(report['issues'])
                self.assertIn('待复核',report['digest']['tasks'][0]['summary'])
                self.assertEqual(report['digest']['tasks'][0]['category'],'other')

    def test_missing_labels_and_view_contract_block_send(self):
        self.config['owner_names'] = {}
        report = self.result()
        self.assertEqual(report['digest']['tasks'][0]['owner'], '待复核')
        self.assertFalse(report['ready_to_send'])
        self.config['view']['verified'] = False
        self.config['api_contract_verified'] = False
        self.assertGreaterEqual(len(self.result()['issues']), 3)

    def test_incomplete_conflicting_duplicate_cross_project_fail_closed(self):
        self.data['pagination_complete'] = False
        with self.assertRaises(CollectionError): self.result()
        self.data['pagination_complete'] = True
        self.data['records'].append(copy.deepcopy(self.data['records'][0]))
        with self.assertRaises(CollectionError): self.result()
        self.data['records'].pop()
        self.data['records'][0]['task']['projectId']='other'
        with self.assertRaises(CollectionError): self.result()

    def test_event_identity_conflict_and_task_scope(self):
        events=self.data['records'][0]['activities']
        duplicate=copy.deepcopy(events[0]); duplicate['action']='other'
        events.append(duplicate)
        with self.assertRaises(CollectionError): self.result()
        events.pop(); events[0]['taskId']='another-task'
        with self.assertRaises(CollectionError): self.result()

    def test_view_filters_and_archive_policy(self):
        self.config['view']['task_equals']={'createTime':'anything'}
        with self.assertRaises(CollectionError): self.result()
        self.config['view']['task_equals']={'missing':'anything'}
        with self.assertRaises(CollectionError): self.result()
        self.config['view']['task_equals']={'executorId':'other-person'}
        self.assertEqual(self.result()['digest']['tasks'], [])
        self.config['view']['task_equals']={}
        self.data['records'][0]['task']['isArchived']=True
        self.assertEqual(self.result()['digest']['tasks'], [])
        self.config['include_archived']=True
        with self.assertRaises(CollectionError): self.result()
        self.data['include_archived']=True
        self.assertEqual(len(self.result()['digest']['tasks']), 1)

    def test_cli_is_offline_by_explicit_fixture_and_review_exit(self):
        result = subprocess.run([sys.executable,str(ROOT/'digest_pipeline.py'),
            '--config',str(FIXTURES/'config.json'),'--fixture',str(FIXTURES/'collection.json'),
            '--date','2026-01-02'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        report=json.loads(result.stdout)
        self.assertFalse(report['ready_to_send'])
        self.assertIn('**📝 其他变更（1）**', report['markdown'])
        self.assertEqual(result.stderr,'')


if __name__ == '__main__':
    unittest.main()
