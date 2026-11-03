"""Fictional evidence for conservative state, assignment and standard parsing."""
import copy
import json
from pathlib import Path
import unittest

from activity_adapter import executor_change, standard_commit_candidate, status_transition
from digest_pipeline import build_digest
from report_contract import updated_selection

FIXTURES=Path(__file__).parent/'fixtures'


class StructuredChangeTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((FIXTURES/'collection.json').read_text())
        self.data['selection']=updated_selection('2026-01-02')
        self.config=json.loads((FIXTURES/'config.json').read_text())
        self.config['view'].update(verified=True,evidence='fictional-test')
        self.config['api_contract_verified']=True

    def event(self, action, data, **extra):
        return dict({'id':'fictional-event','action':action,'content':json.dumps(data,ensure_ascii=False),
                     'createTime':'2026-01-02T01:00:00Z','boundToObjectId':'demo-old-task','boundToObjectType':'task'},**extra)

    def state(self, target='demo-done', previous='demo-open', flag=False):
        return {'tfsId':target,'oldTfsId':previous,'taskflowstatus':self.config['status_names'][target],
                'oldTaskflowstatus':self.config['status_names'][previous],'isDone':flag}

    def report(self,*events):
        self.data['records'][0]['activities']=list(events)
        return build_digest(self.data,self.config,'2026-01-02')

    def test_event_false_does_not_override_verified_target_and_current_state(self):
        self.data['records'][0]['task'].update(tfsId='demo-done',isDone=True)
        raw=self.event('update.taskflowstatus',self.state(flag=False))
        parsed=status_transition(raw['content'],self.config)
        self.assertTrue(parsed['resolved'])
        self.assertIs(parsed['event_is_done'],False)
        report=self.report(raw)
        self.assertEqual(report['digest']['tasks'][0]['category'],'resolved')
        self.assertTrue(report['ready_to_send'])
        self.assertIs(report['audit'][0]['status_transitions'][0]['event_is_done'],False)

    def test_current_state_conflicts_review_instead_of_inference(self):
        raw=self.event('update.taskflowstatus',self.state())
        for status,done in [('demo-open',False),('demo-done',False),('missing',True),('demo-done',None)]:
            self.data['records'][0]['task'].update(tfsId=status,isDone=done)
            report=self.report(raw)
            self.assertFalse(report['ready_to_send'])
            self.assertEqual(report['digest']['tasks'][0]['category'],'other')
            self.assertIn('状态事件序列或当前状态不一致',report['markdown'])

    def test_continuous_reopen_sequence_uses_ids_not_event_boolean(self):
        done=self.event('update.taskflowstatus',self.state(flag=False))
        reopened=self.event('update.taskflowstatus',self.state('demo-open','demo-done',flag=True),
                            id='fictional-reopen',createTime='2026-01-02T02:00:00Z')
        report=self.report(reopened,done)
        self.assertTrue(report['ready_to_send'])
        self.assertEqual(report['digest']['tasks'][0]['category'],'other')
        self.assertEqual([t['target_id'] for t in report['audit'][0]['status_transitions']],['demo-done','demo-open'])
        broken=self.event('update.taskflowstatus',self.state('demo-open','demo-open'),
                         id='fictional-broken',createTime='2026-01-02T02:00:00Z')
        self.assertFalse(self.report(done,broken)['ready_to_send'])

    def test_unknown_mapping_or_duplicate_timestamp_remains_review(self):
        done=self.event('update.taskflowstatus',self.state())
        duplicate=self.event('update.taskflowstatus',self.state('demo-open','demo-done'),id='fictional-same-time')
        later=self.event('update.taskflowstatus',self.state(),id='fictional-later',createTime='2026-01-02T03:00:00Z')
        self.data['records'][0]['task'].update(tfsId='demo-done',isDone=True)
        self.assertFalse(self.report(done,duplicate,later)['ready_to_send'])
        self.config['status_names']={}
        self.assertFalse(self.report(done)['ready_to_send'])

    def test_executor_json_automation_and_names_only_from_id_mapping(self):
        data={'_executorId':'fictional-new','_oldExecutorId':'fictional-old','payload':{'fromAutomation':True}}
        raw=self.event('update_executor',data)
        parsed=executor_change(raw,self.config)
        self.assertEqual(parsed['target_id'],'fictional-new')
        self.assertIsNone(parsed['target_name'])
        self.assertTrue(parsed['from_automation'])
        self.assertTrue(parsed['needs_review'])
        report=self.report(raw)
        self.assertFalse(report['ready_to_send'])
        self.assertIn('来源标记：自动化',report['markdown'])
        self.config['owner_names'].update({'fictional-new':'虚构新成员','fictional-old':'虚构旧成员'})
        report=self.report(raw)
        self.assertIn('虚构旧成员 → 虚构新成员',report['markdown'])
        self.assertEqual(report['audit'][0]['parsed_changes'][0]['parsed_payload'],data)

    def test_executor_malformed_ids_automation_conflict_and_duplicate_keys_review(self):
        for content in ('{','{"_executorId":"a","_executorId":"b","_oldExecutorId":"c"}',
                        json.dumps({'_executorId':None,'_oldExecutorId':'old'}),
                        json.dumps({'_executorId':'new','_oldExecutorId':'old','payload':{'fromAutomation':'true'}})):
            raw=self.event('update_executor',{});raw['content']=content
            self.assertFalse(self.report(raw)['ready_to_send'])
        raw=self.event('update_executor',{'_executorId':'new','_oldExecutorId':'old','payload':{'fromAutomation':True}},
                       payload={'fromAutomation':False})
        self.assertTrue(executor_change(raw,self.config)['automation_conflict'])
        self.assertFalse(self.report(raw)['ready_to_send'])

    def test_only_specific_standard_title_pattern_is_candidate_and_stays_review(self):
        for title in ('关联了提交','关联了提交：fictional-change','关联了提交 fictional-change'):
            data={'title':title,'customCreator':{'name':'虚构集成来源'}}
            raw=self.event('standard',data)
            parsed=standard_commit_candidate(raw)
            self.assertEqual(parsed['pattern'],'commit-link-title')
            self.assertEqual(parsed['source_creator_name'],'虚构集成来源')
            report=self.report(raw)
            self.assertFalse(report['ready_to_send'])
            self.assertIn('关联提交标题模式',report['markdown'])
            self.assertEqual(report['audit'][0]['parsed_changes'][0]['parsed_payload'],data)
        for title in ('未关联了提交','取消关联了提交','有人说关联了提交但尚未执行','完成任务','关联了提交失败'):
            raw=self.event('standard',{'title':title,'customCreator':{'name':'虚构来源'}})
            self.assertIsNone(standard_commit_candidate(raw))
            self.assertIn('未识别动态',self.report(raw)['markdown'])
        raw=self.event('standard',{'title':'关联了提交'})
        self.assertIsNone(standard_commit_candidate(raw))


    def test_executor_sequence_conflicts_block_with_complete_evidence(self):
        self.config['owner_names'].update({key:'虚构成员'+key for key in ('a','b','c','d')})
        def change(old,new,event_id,hour):
            return self.event('update_executor',{'_oldExecutorId':old,'_executorId':new},
                              id=event_id,createTime=f'2026-01-02T0{hour}:00:00Z')
        cases=[([change('a','b','first',1),change('c','d','second',2)],'d'),
               ([change('a','b','first',1),change('b','c','second',1)],'c'),
               ([change('a','b','first',1)],'c')]
        for events,current in cases:
            with self.subTest(events=events,current=current):
                self.data['records'][0]['task']['executorId']=current
                report=self.report(*events)
                self.assertFalse(report['ready_to_send'])
                self.assertEqual(report['digest']['tasks'][0]['category'],'other')
                self.assertIn('执行人事件序列、同刻变更或当前执行人不一致',report['markdown'])
                self.assertEqual(report['audit'][0]['activity_evidence'],events)
                self.assertTrue(report['audit'][0]['executor_conflict'])

    def test_unchanged_resolved_status_is_not_entry_into_resolved(self):
        self.data['records'][0]['task'].update(tfsId='demo-done',isDone=True)
        raw=self.event('update.taskflowstatus',self.state('demo-done','demo-done',flag=False))
        report=self.report(raw)
        self.assertFalse(report['ready_to_send'])
        self.assertEqual(report['digest']['tasks'][0]['category'],'other')
        self.assertIn('状态 ID 未变化',report['markdown'])
        self.assertEqual(report['audit'][0]['activity_evidence'],[raw])
        self.assertTrue(report['audit'][0]['status_noop'])

    def test_continuous_executor_chain_is_sorted_and_matches_current(self):
        self.config['owner_names'].update({'a':'虚构甲','b':'虚构乙','c':'虚构丙'})
        self.data['records'][0]['task']['executorId']='c'
        first=self.event('update_executor',{'_oldExecutorId':'a','_executorId':'b'},id='first')
        second=self.event('update_executor',{'_oldExecutorId':'b','_executorId':'c'},id='second',
                          createTime='2026-01-02T02:00:00Z')
        report=self.report(second,first)
        self.assertTrue(report['ready_to_send'])
        self.assertEqual([e['event_id'] for e in report['audit'][0]['parsed_changes']],['first','second'])


if __name__=='__main__':unittest.main()
