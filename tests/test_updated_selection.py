"""Fictional regression data for the independently verified v3 q contract."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import unittest

from digest_pipeline import build_digest
from report_contract import updated_selection
from report_contract import SHANGHAI
from upcam_app.config import DEFAULT_SETTINGS, scheduled_window
from datetime import date, timedelta
from teambition_readonly import CollectionError
from test_collection import FakeTransport, client, page

FIXTURES=Path(__file__).parent/'fixtures'


class UpdatedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.task=json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        self.config=json.loads((FIXTURES/'config.json').read_text())
        self.day='2026-01-02'
        self.query="updated >= '2026-01-02T00:00:00+08:00' AND updated < '2026-01-03T00:00:00+08:00'"

    def test_five_small_pages_keep_q_and_tokens_then_only_selected_details(self):
        tasks=[dict(self.task,id=f'fictional-task-{i}') for i in range(9)]
        pages=[page(tasks[i:i+2],f'next-{i+2}' if i+2<len(tasks) else '') for i in range(0,len(tasks),2)]
        replies=[]
        for task in tasks: replies.extend([{'code':200,'result':task},page()])
        t=FakeTransport(*pages,*replies)
        result=client(t).collect('demo-project',day=self.day,page_size=2)
        self.assertEqual(len(result['records']),9)
        self.assertEqual(result['selection'],updated_selection(self.day))
        self.assertFalse(result['selection']['historical_event_coverage'])
        for i,req in enumerate(t.requests[:5]):
            q=parse_qs(urlsplit(req.full_url).query)
            self.assertEqual(q['q'],[self.query])
            self.assertIn('%2B08%3A00',req.full_url)
            self.assertEqual(q['pageSize'],['2'])
            self.assertEqual(q['includeArchived'],['false'])
            self.assertEqual(q.get('pageToken'),None if i==0 else [f'next-{i*2}'])
        self.assertEqual(len(t.requests),23)
        self.assertTrue(all(req.method=='GET' for req in t.requests))

    def test_millisecond_and_utc_equivalent_boundaries(self):
        accepted=['2026-01-02T00:00:00.000+08:00','2026-01-02T23:59:59.999+08:00',
                  '2026-01-01T16:00:00.000Z','2026-01-02T15:59:59.999Z']
        for updated in accepted:
            with self.subTest(updated=updated):
                task=dict(self.task,updated=updated)
                t=FakeTransport(page([task]),{'code':200,'result':task},page())
                self.assertEqual(len(client(t).collect('demo-project',day=self.day)['records']),1)
        rejected=['2026-01-01T23:59:59.999+08:00','2026-01-03T00:00:00.000+08:00',
                  '2026-01-02T16:00:00.000Z','2026-01-02T00:00:00',None,'invalid']
        for updated in rejected:
            with self.subTest(updated=updated):
                t=FakeTransport(page([dict(self.task,updated=updated)]))
                with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
                self.assertEqual(len(t.requests),1)

    def test_empty_candidates_do_not_trigger_detail_or_history_scan(self):
        t=FakeTransport(page())
        result=client(t).collect('demo-project',day=self.day)
        self.assertEqual(result['records'],[])
        self.assertEqual(len(t.requests),1)
        self.assertIn('q=',t.requests[0].full_url)

    def test_query_error_never_falls_back_to_unfiltered(self):
        t=FakeTransport({'code':400,'errorMessage':'fictional query rejection'})
        with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
        self.assertEqual(len(t.requests),1)
        self.assertEqual(parse_qs(urlsplit(t.requests[0].full_url).query)['q'],[self.query])

    def test_budget_and_pagination_cycle_still_fail_closed(self):
        for replies,kwargs in [([page([self.task],'next'),page([], 'next')],{}),
                               ([page([self.task],'next'),page()],{'max_requests':1}),
                               ([page([self.task,dict(self.task,id='second')])],{'max_tasks':1})]:
            t=FakeTransport(*replies)
            with self.assertRaises(CollectionError):client(t,**kwargs).collect('demo-project',day=self.day)
            self.assertTrue(all('/project/' in req.full_url and 'q=' in req.full_url for req in t.requests))

    def test_task_changed_between_list_and_detail_aborts(self):
        for updated in ('2026-01-03T00:00:00+08:00','2026-01-02T09:00:00Z',None):
            t=FakeTransport(page([self.task]),{'code':200,'result':dict(self.task,updated=updated)})
            with self.assertRaises(CollectionError):client(t).collect('demo-project',day=self.day)
            self.assertEqual(len(t.requests),2)

    def test_old_task_recent_update_with_unrecognized_event_remains_review(self):
        activity={'id':'fictional-activity','action':'standard','createTime':'2026-01-02T01:00:00Z',
                  'boundToObjectId':self.task['id'],'boundToObjectType':'task'}
        t=FakeTransport(page([self.task]),{'code':200,'result':self.task},page([activity]))
        collection=client(t).collect('demo-project',day=self.day)
        report=build_digest(collection,self.config,self.day)
        self.assertEqual(len(report['digest']['tasks']),1)
        self.assertFalse(report['ready_to_send'])
        self.assertEqual(report['scope']['selection']['basis'],'current-latest-updated')

    def test_report_rejects_selection_date_query_or_membership_drift(self):
        collection=json.loads((FIXTURES/'collection.json').read_text())
        collection['selection']=updated_selection(self.day)
        for field,value in [('date','2026-01-03'),('q','updated >= anything'),('historical_event_coverage',True)]:
            changed=copy.deepcopy(collection);changed['selection'][field]=value
            with self.assertRaises(CollectionError):build_digest(changed,self.config,self.day)
        collection['records'][0]['task']['updated']='2026-01-03T00:00:00+08:00'
        with self.assertRaises(CollectionError):build_digest(collection,self.config,self.day)

    def test_selected_task_without_window_events_retained_for_review(self):
        for activities in ([],[{'id':'fictional-old-event','action':'create','createTime':'2020-01-01T00:00:00Z',
                               'boundToObjectId':self.task['id'],'boundToObjectType':'task'}]):
            t=FakeTransport(page([self.task]),{'code':200,'result':self.task},page(activities))
            report=build_digest(client(t).collect('demo-project',day=self.day),self.config,self.day)
            self.assertEqual(len(report['digest']['tasks']),1)
            self.assertEqual(report['digest']['tasks'][0]['category'],'other')
            self.assertIn('动态证据不足',report['markdown'])
            self.assertFalse(report['ready_to_send'])

    def test_fixed_day_and_page_size_validation_before_transport(self):
        for day,size in [(None,2),('20260102',2),('2026-01-02',0),('2026-01-02',101),('2026-01-02',True)]:
            t=FakeTransport()
            with self.assertRaises(CollectionError):client(t).collect('demo-project',day=day,page_size=size)
            self.assertEqual(t.requests,[])

    def test_custom_half_open_window_drives_query_membership_and_event_filter(self):
        window=('2026-01-01T16:50:00+08:00','2026-01-02T08:50:00+08:00')
        task=dict(self.task,updated='2026-01-02T07:00:00+08:00')
        events=[
            {'id':'fictional-at-start','action':'create','createTime':window[0],
             'boundToObjectId':task['id'],'boundToObjectType':'task'},
            {'id':'fictional-at-end','action':'create','createTime':window[1],
             'boundToObjectId':task['id'],'boundToObjectType':'task'},
        ]
        t=FakeTransport(page([task]),{'code':200,'result':task},page(events))
        collection=client(t).collect('demo-project',day=self.day,window=window)
        report=build_digest(collection,self.config,self.day,window=window)
        query=parse_qs(urlsplit(t.requests[0].full_url).query)['q'][0]
        self.assertEqual(query,"updated >= '2026-01-01T16:50:00+08:00' AND updated < '2026-01-02T08:50:00+08:00'")
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
                t = FakeTransport(page([task]), {'code': 200, 'result': task}, page(events))
                collection = client(t).collect('demo-project', day=self.day, window=(start, end))
                report = build_digest(collection, self.config, self.day, window=(start, end))
                self.assertEqual(report['audit'][0]['event_ids'],
                                 ['synthetic-boundary-0', 'synthetic-boundary-1'])
                self.assertEqual(report['scope']['end_exclusive'], expected_end)
                q = parse_qs(urlsplit(t.requests[0].full_url).query)['q'][0]
                self.assertEqual(q, f"updated >= '{expected_start}' AND updated < '{expected_end}'")


if __name__=='__main__':unittest.main()
