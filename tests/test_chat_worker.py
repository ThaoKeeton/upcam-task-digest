import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from upcam_app.smoke_data import make_smoke_service
from upcam_app.worker_api import WorkerApiError

try:
    from upcam_app.chat_worker import ChatWorker, send_chat_markdown
except ModuleNotFoundError:
    ChatWorker = None
    send_chat_markdown = None


def plan():
    return {'operation': 'tasks', 'search': None, 'task_id': None, 'start': None, 'end': None,
            'summarize': False, 'send_requested': False, 'reply': None}


class Planner:
    def plan(self, question, history): return plan()
    def summarize(self, question, source, history=None): return '虚构答案'


class Reader:
    def execute(self, requested):
        return {'kind': 'tasks', 'rows': [{'id': 'fake-task', 'content': '虚构内容'}], 'snapshot_guaranteed': False}


class ChatWorkerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ChatWorker, 'chat worker not implemented')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = make_smoke_service(Path(self.tmp.name))
        self.service.dispatch({'op': 'initialize'})
        self.sends = []
        self.controller = ChatWorker(self.service, planner_factory=lambda *_: Planner(),
                                     reader_factory=lambda *_: Reader(),
                                     sender=lambda *args: self.sends.append(args) or True)
        self.service._chat_worker = self.controller

    def call(self, op, payload=None):
        return self.service.dispatch({'op': op, 'payload': payload or {}})

    def test_chat_turn_queries_but_does_not_send_then_explicit_send_uses_exact_draft(self):
        result = self.call('chat_turn', {'message': '查询任务'})
        self.assertIn('虚构内容', result['answer'])
        self.assertEqual(self.sends, [])
        sent = self.call('chat_send', {'draft_id': result['draft_id'], 'request_id': str(uuid.uuid4())})
        self.assertEqual(sent['state'], 'confirmed')
        self.assertEqual(self.sends[0][2], result['answer'])
        self.assertNotIn('synthetic-only', sent['status'])
        self.assertEqual(self.call('chat_send', {'draft_id': result['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'not_sent')

    def test_reset_and_changed_config_invalidate_draft_without_sender(self):
        first = self.call('chat_turn', {'message': '查询'})
        self.call('chat_reset')
        self.assertEqual(self.call('chat_send', {'draft_id': first['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'not_sent')
        second = self.call('chat_turn', {'message': '查询'})
        secrets = self.service.secret_store.load(); secrets['dingtalk_signing_secret'] = 'changed-synthetic-secret'
        self.service.secret_store.save(secrets)
        self.assertEqual(self.call('chat_send', {'draft_id': second['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'not_sent')
        self.assertEqual(self.sends, [])

    def test_failed_new_turn_invalidates_old_draft_even_if_credentials_are_restored(self):
        old = self.call('chat_turn', {'message': '查询'})
        saved = self.service.secret_store.load()
        disabled = dict(saved); disabled.pop('teambition_mcp_authorized_identity')
        self.service.secret_store.save(disabled)
        with self.assertRaises(WorkerApiError): self.call('chat_turn', {'message': '新问题'})
        self.service.secret_store.save(saved)
        outcome = self.call('chat_send', {'draft_id': old['draft_id'], 'request_id': str(uuid.uuid4())})
        self.assertEqual(outcome['state'], 'not_sent')
        self.assertEqual(self.sends, [])

    def test_missing_mcp_authorization_blocks_before_planning(self):
        secrets = self.service.secret_store.load(); secrets.pop('teambition_mcp_authorized_identity')
        self.service.secret_store.save(secrets)
        with self.assertRaises(WorkerApiError): self.call('chat_turn', {'message': '查询'})
        self.assertEqual(self.sends, [])

    def test_unknown_provider_result_blocks_later_send_even_with_fresh_draft(self):
        self.controller.sender = lambda *_: False
        first = self.call('chat_turn', {'message': '查询'})
        sent = self.call('chat_send', {'draft_id': first['draft_id'], 'request_id': str(uuid.uuid4())})
        self.assertEqual(sent['state'], 'unknown')
        second = self.call('chat_turn', {'message': '再查'})
        self.assertEqual(self.call('chat_send', {'draft_id': second['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'unknown')

    def test_unexpected_guard_exception_after_dispatch_is_unknown_not_not_sent(self):
        result = self.call('chat_turn', {'message': '查询'})
        def interrupted(guard, request_id, destination, content, send):
            send(content)
            raise RuntimeError('synthetic interruption after dispatch')
        with patch('upcam_app.chat_worker.ChatDelivery.attempt', interrupted):
            outcome = self.call('chat_send', {'draft_id': result['draft_id'], 'request_id': str(uuid.uuid4())})
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(outcome['state'], 'unknown')

    def test_unknown_resolution_requires_explicit_confirmation_and_never_sends(self):
        self.controller.sender = lambda *args: self.sends.append(args) or False
        first = self.call('chat_turn', {'message': '查询'})
        self.assertEqual(self.call('chat_send', {'draft_id': first['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'unknown')
        preview = self.call('chat_resolve_preview')
        self.assertEqual(preview['state'], 'unknown')
        self.assertTrue(preview['request_id'])
        self.assertEqual(len(preview['content_hash']), 64)
        self.assertEqual(preview['content_label'], preview['content_hash'][:12])
        self.assertNotIn('虚构内容', json.dumps(preview, ensure_ascii=False))
        self.assertNotIn('synthetic-only', json.dumps(preview))
        self.assertEqual(self.call('chat_resolve_send', {'confirmed': False, 'preview_id': preview['preview_id']})['state'], 'not_resolved')
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.call('chat_resolve_send', {'confirmed': True, 'preview_id': preview['preview_id']})['state'], 'resolved')
        self.assertEqual(len(self.sends), 1)
        self.controller.sender = lambda *args: self.sends.append(args) or True
        next_result = self.call('chat_turn', {'message': '再查'})
        self.assertEqual(self.call('chat_send', {'draft_id': next_result['draft_id'], 'request_id': str(uuid.uuid4())})['state'], 'confirmed')
        self.assertEqual(len(self.sends), 2)

    def test_uncertain_reconciliation_commit_is_not_reported_as_not_resolved(self):
        from delivery_guard import DeliveryBlocked
        self.controller.sender = lambda *_: False
        result = self.call('chat_turn', {'message': '查询'})
        self.call('chat_send', {'draft_id': result['draft_id'], 'request_id': str(uuid.uuid4())})
        preview = self.call('chat_resolve_preview')
        with patch('upcam_app.chat_worker.ChatDelivery.resolve_unknown',
                   side_effect=DeliveryBlocked('synthetic', outcome='unknown')):
            outcome = self.call('chat_resolve_send', {'confirmed': True, 'preview_id': preview['preview_id']})
        self.assertEqual(outcome['state'], 'unknown')
        self.assertEqual(self.sends, [])

    def test_recovery_confirmation_is_bound_to_record_and_destination_snapshot(self):
        from upcam_app.chat_delivery import ChatDelivery
        self.controller.sender = lambda *_: False
        first = self.call('chat_turn', {'message': '查询'})
        first_id = str(uuid.uuid4())
        self.call('chat_send', {'draft_id': first['draft_id'], 'request_id': first_id})
        preview = self.call('chat_resolve_preview')
        credentials = self.service.secret_store.load()
        webhook = credentials['dingtalk_webhook']
        guard = ChatDelivery(self.service.data_dir / 'delivery_ledger.sqlite3',
                             credentials['delivery_ledger_id'], persistent_storage_verified=True)
        try:
            first_row = guard.peek_unresolved(webhook)
            self.assertEqual(guard.resolve_unknown(webhook, first_id, expected_content_hash=first_row['content_hash'],
                             expected_attempted_at_utc=first_row['attempted_at_utc']), 'resolved')
            second_id = str(uuid.uuid4())
            self.assertEqual(guard.attempt(second_id, webhook, '另一条虚构消息', lambda _: False), 'unknown')
        finally:
            guard.close()
        result = self.call('chat_resolve_send', {'confirmed': True, 'preview_id': preview['preview_id']})
        self.assertNotEqual(result['state'], 'resolved')
        current = self.call('chat_resolve_preview')
        self.assertEqual(current['request_id'], second_id)
        credentials['dingtalk_webhook'] = 'https://oapi.dingtalk.com/robot/send?access_token=changed-synthetic'
        self.service.secret_store.save(credentials)
        result = self.call('chat_resolve_send', {'confirmed': True, 'preview_id': current['preview_id']})
        self.assertNotEqual(result['state'], 'resolved')

    def test_recovery_rejects_changed_content_hash_after_preview(self):
        from upcam_app.chat_delivery import ChatDelivery
        self.controller.sender = lambda *_: False
        result = self.call('chat_turn', {'message': '查询'})
        self.call('chat_send', {'draft_id': result['draft_id'], 'request_id': str(uuid.uuid4())})
        preview = self.call('chat_resolve_preview')
        credentials = self.service.secret_store.load()
        guard = ChatDelivery(self.service.data_dir / 'delivery_ledger.sqlite3',
                             credentials['delivery_ledger_id'], persistent_storage_verified=True)
        try:
            row = guard.peek_unresolved(credentials['dingtalk_webhook'])
        finally:
            guard.close()
        row['content_hash'] = '0' * 64
        with patch('upcam_app.chat_worker.ChatDelivery.peek_unresolved', return_value=row), \
             patch('upcam_app.chat_worker.ChatDelivery.resolve_unknown') as resolve:
            outcome = self.call('chat_resolve_send', {'confirmed': True, 'preview_id': preview['preview_id']})
        self.assertEqual(outcome['state'], 'not_resolved')
        resolve.assert_not_called()

    def test_missing_or_forged_payload_cannot_send(self):
        self.call('chat_turn', {'message': '查询'})
        self.assertEqual(self.call('chat_send', {'draft_id': 'bad', 'request_id': str(uuid.uuid4())})['state'], 'not_sent')
        self.assertEqual(self.call('chat_send', {'draft_id': 'bad', 'request_id': str(uuid.uuid4()), 'content': 'forged'})['state'], 'not_sent')
        self.assertEqual(self.sends, [])

    def test_existing_webhook_transport_payload_has_no_automatic_mentions(self):
        calls = []
        def fake_transport(url, payload, timeout):
            calls.append(json.loads(payload)); return {'errcode': 0}
        outcome = send_chat_markdown('https://oapi.dingtalk.com/robot/send?access_token=synthetic',
                                     'synthetic-signature', '精确文本', transport=fake_transport)
        self.assertTrue(outcome)
        self.assertNotIn('at', calls[0])
        self.assertEqual(calls[0]['markdown']['text'], '精确文本')
        self.assertNotIn('日报', calls[0]['markdown']['title'])

    def test_legacy_workbench_cannot_access_standalone_chat_operations(self):
        self.service._set_pipeline_mode('legacy')
        for operation in ('chat_turn', 'chat_reset', 'chat_send', 'chat_resolve_preview', 'chat_resolve_send'):
            with self.subTest(operation=operation), self.assertRaises(WorkerApiError):
                self.call(operation, {'message': '虚构'})
        self.assertEqual(self.sends, [])

    def test_packaged_synthetic_chat_is_offline_by_default(self):
        standalone = make_smoke_service(Path(self.tmp.name) / 'smoke')
        standalone.dispatch({'op': 'initialize'})
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network prohibited')):
            result = standalone.dispatch({'op': 'chat_turn', 'payload': {'message': 'CI 虚构聊天'}})
        self.assertIn('CI 虚构聊天', result['answer'])

    def test_network_is_forbidden_during_fake_flow(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network prohibited')):
            result = self.call('chat_turn', {'message': '查询'})
            self.assertEqual(self.call('chat_send', {'draft_id': result['draft_id'],
                              'request_id': str(uuid.uuid4())})['state'], 'confirmed')

if __name__ == '__main__': unittest.main()
