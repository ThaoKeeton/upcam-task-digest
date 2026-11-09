"""Fictional project scans with local activity-time selection and closing checks."""
import copy
import json
from pathlib import Path
import unittest

from digest_pipeline import build_digest
from report_contract import updated_selection
from report_contract import SHANGHAI
from upcam_app.config import DEFAULT_SETTINGS, scheduled_window
from datetime import date, timedelta
from teambition_readonly import CollectionError
from test_collection import FakeTransport, arguments, tool_name, client, page

FIXTURES=Path(__file__).parent/'fixtures'


class UpdatedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.task=json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        self.config=json.loads((FIXTURES/'config.json').read_text())
        self.config['api_contract_verified']=True
        self.config['view'].update(verified=True,evidence='synthetic-activity-selection')
        self.day='2026-01-02'

    def transport(self, task, activities=()):
        return FakeTransport(page([task]),{'code':200,'result':task},
                             page(activities),page([task]))

    def test_five_candidate_pages_and_closing_pages_keep_the_same_lower_bound_q(self):
        tasks=[dict(self.task,id=f'fictional-task-{i}') for i in range(9)]
        pages=[page(tasks[i:i+2],f'next-{i+2}' if i+2<len(tasks) else '') for i in range(0,len(tasks),2)]
        closing_tasks=list(reversed(tasks))
        closing=[page(closing_tasks[i:i+2],f'closing-{i+2}' if i+2<len(tasks) else '')
                 for i in range(0,len(tasks),2)]
        replies=[]
        for task in tasks: replies.extend([{'code':200,'result':task},page()])
        t=FakeTransport(*pages,*replies,*closing)
        result=client(t).collect('demo-project',day=self.day,page_size=2)
        self.assertEqual(len(result['records']),9)
        self.assertEqual(result['selection'],updated_selection(self.day))
        self.assertEqual(result['selection']['mode'],'v3-project-activities-half-open')
        self.assertEqual(result['selection']['basis'],'activity-createTime')
        self.assertEqual(result['selection']['candidate_scope'],'current-visible-project-tasks-updated-since')
        self.assertEqual(result['selection']['q'], "updated >= '2026-01-02T00:00:00+08:00'")
        self.assertFalse(result['selection']['view_equivalent'])
        self.assertFalse(result['selection']['historical_event_coverage'])
        self.assertFalse(result['selection']['snapshot_guaranteed'])
        for phase,requests in (('next',t.requests[:5]),('closing',t.requests[-5:])):
            for i,req in enumerate(requests):
                query=arguments(req)
                self.assertEqual(query['tql'], result['selection']['q'])
                self.assertEqual(query['pageSize'],2)
                self.assertEqual(query['includeArchived'],False)
                self.assertEqual(query.get('pageToken'),None if i==0 else f'{phase}-{i*2}')
        self.assertEqual(len(t.requests),28)
        self.assertTrue(all(req.method=='POST' for req in t.requests))

    def test_full_project_audit_validates_versions_without_window_membership(self):
        accepted=['2026-01-02T00:00:00.000+08:00','2026-01-02T23:59:59.999+08:00',
                  '2026-01-01T16:00:00.000Z','2026-01-02T15:59:59.999Z',
                  '2026-01-01T23:59:59.999+08:00','2026-01-03T00:00:00.000+08:00',
                  '2026-01-02T16:00:00.000Z','2020-01-01T00:00:00Z',
                  '2026-01-05T00:00:00+08:00']
        for updated in accepted:
            with self.subTest(updated=updated):
                task=dict(self.task,updated=updated)
                t=self.transport(task)
                self.assertEqual(len(client(t).collect('demo-project',day=self.day,
                                                      candidate_mode='full_project')['records']),1)
                self.assertEqual(len(t.requests),4)
        rejected=['2026-01-02T00:00:00',None,'invalid',123,True]
        for updated in rejected:
            with self.subTest(updated=updated):
                t=FakeTransport(page([dict(self.task,updated=updated)]))
                with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
                self.assertEqual(len(t.requests),1)

    def test_activity_millisecond_and_utc_equivalent_boundaries_select_locally(self):
        accepted=['2026-01-02T00:00:00.000+08:00','2026-01-02T23:59:59.999+08:00',
                  '2026-01-01T16:00:00.000Z','2026-01-02T15:59:59.999Z']
        excluded=['2026-01-01T23:59:59.999+08:00','2026-01-03T00:00:00.000+08:00',
                  '2026-01-02T16:00:00.000Z']
        task=dict(self.task,updated='2026-01-05T00:00:00+08:00')
        for when,selected in [(value,True) for value in accepted]+[(value,False) for value in excluded]:
            activity={'id':'fictional-boundary-event','action':'create','content':'',
                      'createTime':when,'boundToObjectId':task['id'],'boundToObjectType':'task'}
            with self.subTest(when=when):
                collection=client(self.transport(task,[activity])).collect('demo-project',day=self.day)
                self.assertEqual(collection['records'][0]['activities'],[activity])
                report=build_digest(collection,self.config,self.day)
                self.assertEqual([row['event_ids'] for row in report['audit']],
                                 [['fictional-boundary-event']] if selected else [])

    def test_empty_candidates_require_a_second_empty_scan_without_details_or_history(self):
        t=FakeTransport(page(),page())
        result=client(t).collect('demo-project',day=self.day)
        self.assertEqual(result['records'],[])
        self.assertEqual(len(t.requests),2)
        for request in t.requests:
            self.assertEqual(tool_name(request), 'teambition.project.task-query')
            self.assertEqual(arguments(request)['tql'], updated_selection(self.day)['q'])

    def test_project_scan_error_never_falls_back_to_unfiltered_query(self):
        t=FakeTransport({'code':400,'errorMessage':'fictional project scan rejection'})
        with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
        self.assertEqual(len(t.requests),1)
        self.assertEqual(arguments(t.requests[0])['tql'], updated_selection(self.day)['q'])

    def test_budget_and_unterminated_business_pagination_still_fail_closed(self):
        for replies,kwargs in [([page([self.task],'same'),page([], 'same')],{'max_pages':2}),
                               ([page([self.task],'next'),page()],{'max_requests':4}),
                               ([page([self.task,dict(self.task,id='second')])],{'max_tasks':1})]:
            t=FakeTransport(*replies)
            with self.assertRaises(CollectionError):client(t,**kwargs).collect('demo-project',day=self.day)
            self.assertTrue(all(tool_name(req) == 'teambition.project.task-query'
                                and arguments(req).get('tql') == updated_selection(self.day)['q']
                                for req in t.requests))

    def test_task_changed_between_list_and_detail_aborts(self):
        for updated in ('2026-01-03T00:00:00+08:00','2026-01-02T09:00:00Z',None):
            t=FakeTransport(page([self.task]),{'code':200,'result':dict(self.task,updated=updated)})
            with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
            self.assertEqual(len(t.requests),2)

    def test_equal_task_updated_instants_do_not_create_false_version_drift(self):
        task=dict(self.task,updated='2026-01-02T08:00:00+08:00')
        detail=dict(task,updated='2026-01-02T00:00:00Z')
        t=FakeTransport(page([task]),{'code':200,'result':detail},page(),page([task]))
        result=client(t).collect('demo-project',day=self.day)
        self.assertEqual(result['records'][0]['task'],detail)
        self.assertEqual(len(t.requests),4)

    def test_old_task_recent_update_with_unrecognized_event_remains_review(self):
        activity={'id':'fictional-activity','action':'standard','createTime':'2026-01-02T01:00:00Z',
                  'boundToObjectId':self.task['id'],'boundToObjectType':'task'}
        t=self.transport(self.task,[activity])
        collection=client(t).collect('demo-project',day=self.day)
        report=build_digest(collection,self.config,self.day)
        self.assertEqual(len(report['digest']['tasks']),1)
        self.assertFalse(report['ready_to_send'])
        self.assertEqual(report['scope']['selection']['basis'],'activity-createTime')

    def test_report_rejects_selection_contract_drift_and_accepts_outside_task_updated(self):
        collection=json.loads((FIXTURES/'collection.json').read_text())
        collection['selection']=updated_selection(self.day)
        for field,value in [('date','2026-01-03'),('q','updated >= anything'),
                            ('historical_event_coverage',True),('snapshot_guaranteed',True),
                            ('view_equivalent',True),('basis','current-latest-updated'),
                            ('candidate_scope','updated-window'),('mode','v3-project-updated-half-open')]:
            changed=copy.deepcopy(collection);changed['selection'][field]=value
            with self.subTest(field=field),self.assertRaises(CollectionError):
                build_digest(changed,self.config,self.day)
        for updated in ('2020-01-01T00:00:00Z','2026-01-05T00:00:00+08:00'):
            collection['records'][0]['task']['updated']=updated
            with self.subTest(updated=updated):
                self.assertEqual(len(build_digest(collection,self.config,self.day)['digest']['tasks']),1)

    def test_fully_scanned_old_task_without_window_events_does_not_enter_digest(self):
        task=dict(self.task,updated='2020-01-01T00:00:00Z')
        for activities in ([],[{'id':'fictional-old-event','action':'create','createTime':'2020-01-01T00:00:00Z',
                               'boundToObjectId':task['id'],'boundToObjectType':'task'}]):
            with self.subTest(history_empty=not activities):
                t=self.transport(task,activities)
                report=build_digest(client(t).collect('demo-project',day=self.day,
                                                     candidate_mode='full_project'),self.config,self.day)
                self.assertEqual(report['digest']['tasks'],[])
                self.assertEqual(report['audit'],[])
                self.assertEqual(report['summary_inputs'],[])
                self.assertTrue(report['ready_to_send'])

    def test_selected_task_without_window_events_retained_for_review(self):
        for activities in ([],[{'id':'fictional-old-event','action':'create','createTime':'2020-01-01T00:00:00Z',
                               'boundToObjectId':self.task['id'],'boundToObjectType':'task'}]):
            with self.subTest(history_empty=not activities):
                t=self.transport(self.task,activities)
                report=build_digest(client(t).collect('demo-project',day=self.day),self.config,self.day)
                self.assertEqual(len(report['digest']['tasks']),1)
                self.assertEqual(report['digest']['tasks'][0]['category'],'other')
                self.assertIn('动态证据不足',report['markdown'])
                self.assertFalse(report['ready_to_send'])

    def test_unknown_activity_time_cannot_be_reported_as_no_window_activity(self):
        for when in (None,'invalid','2026-01-02T08:00:00'):
            activity={'id':'fictional-undated-event','action':'create','createTime':when,
                      'boundToObjectId':self.task['id'],'boundToObjectType':'task'}
            with self.subTest(when=when):
                transport = self.transport(self.task, [activity])
                with self.assertRaises(CollectionError):
                    client(transport).collect('demo-project', day=self.day)
                self.assertEqual(len(transport.requests), 3)

    def test_fixed_day_and_page_size_validation_before_transport(self):
        for day,size in [(None,2),('20260102',2),('2026-01-02',0),('2026-01-02',101),('2026-01-02',True)]:
            t=FakeTransport()
            with self.assertRaises(CollectionError):client(t).collect('demo-project',day=day,page_size=size)
            self.assertEqual(t.requests,[])

    def test_custom_half_open_window_filters_activities_with_lower_bound_candidate_query(self):
        window=('2026-01-01T16:50:00+08:00','2026-01-02T08:50:00+08:00')
        task=dict(self.task,updated='2026-01-05T07:00:00+08:00')
        events=[
            {'id':'fictional-at-start','action':'create','createTime':window[0],
             'boundToObjectId':task['id'],'boundToObjectType':'task'},
            {'id':'fictional-at-end','action':'create','createTime':window[1],
             'boundToObjectId':task['id'],'boundToObjectType':'task'},
        ]
        t=self.transport(task,events)
        collection=client(t).collect('demo-project',day=self.day,window=window)
        report=build_digest(collection,self.config,self.day,window=window)
        for request in (t.requests[0],t.requests[-1]):
            self.assertEqual(arguments(request)['tql'], f"updated >= '{window[0]}'")
        self.assertEqual(collection['selection']['window_kind'],'custom')
        self.assertEqual(report['digest']['date'],self.day)
        self.assertEqual(report['audit'][0]['event_ids'],['fictional-at-start'])
        self.assertEqual(report['scope']['start'],window[0])
        self.assertEqual(report['scope']['end_exclusive'],window[1])

    def test_custom_window_requires_both_aware_boundaries_and_report_end_date(self):
        for window in [(None,'2026-01-02T08:50:00+08:00'),
                       ('2026-01-01T16:50:00','2026-01-02T08:50:00'),
                       ('2026-01-01T16:50:00+08:00','2026-01-03T08:50:00+08:00')]:
            t=FakeTransport()
            with self.assertRaises(CollectionError):
                client(t).collect('demo-project',day=self.day,window=window)
            self.assertEqual(t.requests,[])

    def test_original_scheduled_cutoffs_keep_end_day_and_exclude_only_endpoint(self):
        windows = (
            ('morning', '2026-01-01T16:50:00+08:00', '2026-01-02T08:50:00+08:00'),
            ('afternoon', '2026-01-02T00:00:00+08:00', '2026-01-02T16:50:00+08:00'),
        )
        for kind, expected_start, expected_end in windows:
            with self.subTest(kind=kind):
                local_start, local_end = scheduled_window(DEFAULT_SETTINGS, kind, date(2026, 1, 2))
                start, end = local_start.replace(tzinfo=SHANGHAI), local_end.replace(tzinfo=SHANGHAI)
                self.assertEqual(start.isoformat(), expected_start)
                self.assertEqual(end.isoformat(), expected_end)
                ending_day_midnight = end.replace(hour=0, minute=0)
                instants = (ending_day_midnight, end - timedelta(microseconds=1), end,
                            end + timedelta(seconds=59, microseconds=999999))
                task = dict(self.task, updated=(end - timedelta(seconds=1)).isoformat())
                events = [{'id': 'synthetic-boundary-' + str(i), 'action': 'create',
                           'boundToObjectId': task['id'], 'boundToObjectType': 'task',
                           'createTime': instant.isoformat(), 'content': ''}
                          for i, instant in enumerate(instants)]
                t = self.transport(task,events)
                collection = client(t).collect('demo-project', day=self.day, window=(start, end))
                report = build_digest(collection, self.config, self.day, window=(start, end))
                self.assertEqual(report['audit'][0]['event_ids'],
                                 ['synthetic-boundary-0', 'synthetic-boundary-1'])
                self.assertEqual(report['scope']['end_exclusive'], expected_end)
                for request in (t.requests[0],t.requests[-1]):
                    self.assertEqual(arguments(request)['tql'],
                                     f"updated >= '{expected_start}'")


if __name__=='__main__':unittest.main()
