"""Fictional regressions for the independent review of c858832."""
import copy
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import digest_pipeline
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from teambition_readonly import CollectionError

FIXTURES = Path(__file__).parent / 'fixtures'


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((FIXTURES/'config.json').read_text())
        self.config['api_contract_verified'] = True
        self.config['view'].update(verified=True, evidence='fictional-review')
        self.collection = json.loads((FIXTURES/'collection.json').read_text())
        self.collection['tenant_id'] = 'demo-org'

    def report(self):
        return digest_pipeline.build_digest(self.collection,self.config,'2026-01-02')

    def test_midnight_during_collection_keeps_precollection_yesterday(self):
        current = [datetime.fromisoformat('2026-01-03T23:59:59+08:00')]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0].astimezone(tz)
        def collect(*args, **kwargs):
            current[0] = datetime.fromisoformat('2026-01-04T00:00:01+08:00')
            return self.collection
        stdout = io.StringIO()
        with patch('sys.argv',['digest_pipeline.py','--config',str(FIXTURES/'config.json'),'--collect']), \
             patch.object(digest_pipeline,'datetime',Clock,create=True), \
             patch('digest_pipeline.Client') as client, patch('sys.stdout',stdout):
            client.return_value.collect.side_effect = collect
            digest_pipeline.main()
        report=json.loads(stdout.getvalue())
        self.assertEqual(report['digest']['date'],'2026-01-02')
        self.assertEqual(report['scope']['start'],'2026-01-02T00:00:00+08:00')
        self.assertEqual(report['scope']['end_exclusive'],'2026-01-03T00:00:00+08:00')
        self.assertEqual((report['scope']['tenant_id'],report['scope']['project_id']),('demo-org','demo-project'))

    def test_collection_tenant_and_project_must_match_configuration(self):
        for key in ('tenant_id','project_id'):
            with self.subTest(key=key):
                config=copy.deepcopy(self.config); config[key]='different'
                with self.assertRaises(CollectionError):
                    digest_pipeline.build_digest(self.collection,config,'2026-01-02')
        missing=copy.deepcopy(self.collection); missing.pop('tenant_id')
        with self.assertRaises(CollectionError):
            digest_pipeline.build_digest(missing,self.config,'2026-01-02')

    def test_unknown_view_keys_and_filter_fields_fail_even_without_tasks(self):
        views = [dict(self.config['view'], task_equal={'executorId':'other'}),
                 dict(self.config['view'], filters={}),
                 dict(self.config['view'], task_equals={'exectuorId':'other'}),
                 dict(self.config['view'], task_equals=[])]
        self.collection['records']=[]
        for view in views:
            with self.subTest(view=view):
                config=copy.deepcopy(self.config); config['view']=view
                with self.assertRaises(CollectionError):
                    digest_pipeline.build_digest(self.collection,config,'2026-01-02')

    def test_delivery_rejects_date_alias_window_mismatch_and_source_relabel(self):
        report=self.report()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.sqlite3'; identity=initialize_ledger(path)
            guard=DeliveryGuard(path,identity,persistent_storage_verified=True)
            sender=Mock(return_value=True)
            try:
                # Existing interfaces must reject every malformed report before sender or claim.
                cases=[]
                for day in ('20260102','2026-W01-5','2026-01-03'):
                    altered=copy.deepcopy(report); altered['digest']['date']=day
                    cases.append((altered,'demo-org','demo-project'))
                for key,value in (('start','2026-01-01T00:00:00+08:00'),
                                  ('end_exclusive','2026-01-04T00:00:00+08:00'),
                                  ('timezone','UTC')):
                    altered=copy.deepcopy(report); altered['scope'][key]=value
                    cases.append((altered,'demo-org','demo-project'))
                cases += [(report,'different','demo-project'),(report,'demo-org','different')]
                for key in ('tenant_id','project_id'):
                    altered=copy.deepcopy(report); altered['scope'].pop(key)
                    cases.append((altered,'demo-org','demo-project'))
                for candidate,tenant,project in cases:
                    with self.subTest(tenant=tenant,project=project,scope=candidate['scope']), self.assertRaises(DeliveryBlocked):
                        guard.attempt(candidate,tenant=tenant,project=project,destination='demo-destination',send=sender)
                sender.assert_not_called()
                self.assertEqual(guard.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
            finally:
                guard.close()

    def test_missing_owner_after_resolved_event_forces_review_category(self):
        self.config['owner_names']={}
        self.collection['records'][0]['activities']=[{
            'id':'demo-resolved','action':'demo.status','created':'2026-01-02T01:00:00Z',
            'data':{'newStatusId':'demo-done'}}]
        report=self.report()
        self.assertFalse(report['ready_to_send'])
        self.assertEqual(report['digest']['tasks'][0]['category'],'other')
        self.assertNotIn('**✅ 已解决',report['markdown'])

    def test_generation_rejects_date_aliases_and_implicit_late_day(self):
        for day in ('20260102','2026-W01-5','2026-1-2','2026-02-30',None):
            with self.subTest(day=day), self.assertRaises(ValueError):
                digest_pipeline.build_digest(self.collection,self.config,day)

    def test_invalid_view_rejected_before_collector_is_called(self):
        config=copy.deepcopy(self.config)
        config['view']['task_equal']={'executorId':'other'}
        with patch('sys.argv',['digest_pipeline.py','--config','fictional.json','--collect']), \
             patch('digest_pipeline.Path.read_text',return_value=json.dumps(config)), \
             patch('digest_pipeline.Client') as client, patch('sys.stderr',io.StringIO()):
            self.assertEqual(digest_pipeline.main(),1)
            client.assert_not_called()


if __name__ == '__main__': unittest.main()
