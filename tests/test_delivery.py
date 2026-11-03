import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'delivery.sqlite3'
        self.identity=initialize_ledger(self.path)
        self.report={'ready_to_send':True,'issues':[],'digest':{'date':'2026-01-02'},'markdown':'fictional report'}
        self.report['scope'] = {'tenant_id':'demo-org','project_id':'demo-project',
            'timezone':'Asia/Shanghai','start':'2026-01-02T00:00:00+08:00',
            'end_exclusive':'2026-01-03T00:00:00+08:00','include_archived':False,'pagination_complete':True}
        self.scope={'tenant':'demo-org','project':'demo-project','destination':'demo-destination'}

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self):
        return DeliveryGuard(self.path,self.identity,persistent_storage_verified=True)

    def state(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT state FROM deliveries').fetchone()[0]

    def test_success_reopen_and_changed_text_cannot_bypass(self):
        calls=[]
        guard=self.guard()
        def send(markdown):
            self.assertEqual(self.state(),'in_flight')
            calls.append(markdown)
            return True
        self.assertEqual(guard.attempt(self.report,**self.scope,send=send),'confirmed')
        guard.close()
        self.assertEqual(self.state(),'confirmed')
        self.report['markdown']='changed report'
        guard=self.guard()
        with self.assertRaises(DeliveryBlocked): guard.attempt(self.report,**self.scope,send=send)
        guard.close()
        self.assertEqual(calls,['fictional report'])

    def test_uncertain_failure_never_retries(self):
        calls=[]
        def send(markdown):
            calls.append(markdown)
            raise TimeoutError('fictional sensitive URL that must not escape')
        guard=self.guard()
        with self.assertRaises(DeliveryBlocked) as ctx:
            guard.attempt(self.report,**self.scope,send=send)
        self.assertNotIn('sensitive',str(ctx.exception))
        self.assertEqual(self.state(),'unknown')
        guard.close(); guard=self.guard()
        with self.assertRaises(DeliveryBlocked): guard.attempt(self.report,**self.scope,send=send)
        self.assertEqual(len(calls),1)
        guard.close()

    def test_rejection_is_not_implicit_success(self):
        guard=self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: {'status':200,'error':1})
        self.assertEqual(self.state(),'unknown'); guard.close()

    def test_crash_claim_and_restart(self):
        guard=self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE deliveries SET state='in_flight'")
        guard.close(); guard=self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: self.fail('must not send'))
        guard.close()

    def test_concurrent_claims_call_sender_once(self):
        calls=[]
        def worker(_):
            guard=self.guard()
            try:
                guard.attempt(self.report,**self.scope,send=lambda body: calls.append(body) or True)
                return True
            except DeliveryBlocked:
                return False
            finally:
                guard.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes=list(pool.map(worker,range(8)))
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(len(calls),1)

    def test_missing_storage_identity_review_and_database_failure_fail_closed(self):
        with self.assertRaises(DeliveryBlocked): DeliveryGuard(self.path,self.identity)
        with self.assertRaises(DeliveryBlocked): DeliveryGuard(self.path,'wrong',persistent_storage_verified=True)
        with self.assertRaises(FileExistsError): initialize_ledger(self.path)
        guard=self.guard(); self.report['ready_to_send']=False
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: self.fail('must not send'))
        guard.close()
        self.path.unlink()
        with self.assertRaises(DeliveryBlocked): self.guard()
        self.assertFalse(self.path.exists())
        self.path.write_text('not a database')
        with self.assertRaises(DeliveryBlocked): self.guard()

    def test_independent_destinations_have_separate_claims(self):
        guard=self.guard()
        guard.attempt(self.report,**self.scope,send=lambda _:True)
        scope=dict(self.scope,destination='demo-other-destination')
        self.assertEqual(guard.attempt(self.report,**scope,send=lambda _:True),'confirmed')
        guard.close()

    def test_claim_commit_failure_prevents_send(self):
        guard=self.guard()
        guard.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: self.fail('must not send'))
        guard.close()

    def test_post_send_commit_failure_retains_claim(self):
        guard=self.guard()
        def send(_):
            guard.db.execute('PRAGMA query_only=ON')
            return True
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=send)
        self.assertEqual(self.state(),'in_flight')
        guard.close(); guard=self.guard()
        with self.assertRaises(DeliveryBlocked):
            guard.attempt(self.report,**self.scope,send=lambda _: self.fail('must not send'))
        guard.close()


if __name__ == '__main__':
    unittest.main()
