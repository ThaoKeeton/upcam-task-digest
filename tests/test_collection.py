"""All responses and credentials below are fictional; no network transport runs."""
import base64
import copy
import hashlib
import hmac
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from teambition_readonly import Client, CollectionError, NoRedirect, app_jwt

FIXTURES = Path(__file__).parent / 'fixtures'


class Response(io.BytesIO):
    status = 200


class FakeTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return Response(json.dumps(result).encode())


def page(rows=(), token=''):
    return {'code': 200, 'result': list(rows), 'nextPageToken': token}


def client(transport, **kw):
    return Client('demo-org', transport=transport, sleep=lambda seconds: None,
                  credentials=lambda: ('fictional-app', 'fictional-secret'), **kw)


class ClientTests(unittest.TestCase):
    def test_jwt_signature_claims_and_lifetime(self):
        token = app_jwt('fictional-app', 'fictional-secret', now=100, ttl=60)
        header, payload, signature = token.split('.')
        decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
        self.assertEqual(json.loads(decode(header)), {'alg': 'HS256', 'typ': 'JWT'})
        self.assertEqual(json.loads(decode(payload)), {'_appId': 'fictional-app', 'iat': 100, 'exp': 160})
        self.assertEqual(decode(signature), hmac.new(b'fictional-secret', (header+'.'+payload).encode(), hashlib.sha256).digest())
        with self.assertRaises(CollectionError):
            app_jwt('fictional-app', 'fictional-secret', ttl=301)
        with self.assertRaises(CollectionError):
            app_jwt(None, None)

    def test_collection_scans_old_tasks_and_all_activity_pages(self):
        task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        transport = FakeTransport(page([task], 'task-page-2'), page([task]),
            {'code': 200, 'result': task},
            page([{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'old-event','created':'2020-01-01T00:00:00Z'}], 'event-page-2'),
            page([{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'yesterday-comment','created':'2026-01-02T00:00:00Z'}]))
        result = client(transport)._collect_bounded_fixture('demo-project')
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(len(result['records'][0]['activities']), 2)
        self.assertEqual(len(transport.requests), 5)
        for req in transport.requests:
            self.assertEqual(req.method, 'GET')
            self.assertTrue(req.full_url.startswith('https://open.teambition.com/api/v3/'))
            self.assertIsNone(req.data)
            self.assertNotIn('fictional-secret', req.full_url)
        self.assertIn('includeArchived=false', transport.requests[0].full_url)
        self.assertIn('pageToken=task-page-2', transport.requests[1].full_url)
        self.assertIn('pageToken=event-page-2', transport.requests[-1].full_url)

    def test_default_collection_limit_accepts_more_than_the_previous_100_tasks(self):
        base_task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        tasks = [dict(base_task, id=f'demo-large-window-{index:03d}') for index in range(101)]
        responses = [page(tasks[:100], 'next-task-page'), page(tasks[100:])]
        for task in tasks:
            responses.extend([{'code': 200, 'result': task}, page()])

        transport = FakeTransport(*responses)
        collector = client(transport)
        result = collector.collect('demo-project', day='2026-01-02')

        self.assertEqual(len(result['records']), 101)
        self.assertEqual(collector.max_tasks, 300)
        self.assertEqual(collector.request_count, 204)
        self.assertTrue(result['pagination_complete'])

    def test_total_response_byte_budget_stops_paginated_reads(self):
        first = page([{'id': 'demo-event-1', 'padding': 'x' * 500}], 'next')
        second = page([{'id': 'demo-event-2', 'padding': 'y' * 500}])
        collector = client(FakeTransport(first, second), max_response_bytes=1024)

        with self.assertRaisesRegex(CollectionError, 'response byte budget'):
            list(collector.pages('/api/v3/task/demo-task/activity/list', {}))
        self.assertEqual(collector.request_count, 2)

    def test_fake_transport_to_existing_renderer_end_to_end(self):
        from digest_pipeline import build_digest
        collection=json.loads((FIXTURES/'collection.json').read_text())
        record=collection['records'][0]
        config=json.loads((FIXTURES/'config.json').read_text())
        t=FakeTransport(page([record['task']]), {'code':200,'result':record['task']},
                        page(record['activities'][:1], 'next'), page(record['activities'][1:]))
        report=build_digest(client(t)._collect_bounded_fixture('demo-project'), config, '2026-01-02')
        self.assertIn('评论 2 条', report['markdown'])
        self.assertIn('**📝 其他变更（1）**', report['markdown'])
        self.assertFalse(report['ready_to_send'])
        self.assertEqual(len(report['digest']['tasks']), 1)

    def test_empty_page_with_token_is_not_terminal(self):
        t = FakeTransport(page([], 'next'), page([{'id':'demo'}]))
        self.assertEqual(list(client(t).pages('/api/v3/task/demo/activity/list', {})), [{'id':'demo'}])

    def test_cycle_conflict_missing_envelope_and_page_limit_fail(self):
        cases = [
            [page([], 'a'), page([], 'a')],
            [page([], 'a'), page([], 'b'), page([], 'a')],
            [page([{'id':'a','value':1}], 'a'), page([{'id':'a','value':2}])],
            [{'code':200,'result':[]}],
            [{'code':200,'result':{},'nextPageToken':''}],
            [page([], 42)],
            [page([{}])],
            [page([{'taskId':'demo-task-without-event-id'}])],
            [page([], 'a'), page([], 'b'), page([], 'c')],
        ]
        for responses in cases:
            with self.subTest(responses=responses), self.assertRaises(CollectionError):
                list(client(FakeTransport(*responses), max_pages=3).pages('/api/v3/task/demo/activity/list', {}))

    def test_retry_rate_limits_and_redacted_failures(self):
        sleeps = []
        t = FakeTransport(HTTPError('https://example.com', 429, 'private-body', {'Retry-After':'4'}, None),
                          URLError('private-body'), page())
        c = client(t); c.sleep = sleeps.append
        self.assertEqual(c.get('/api/v3/task/query', {}), page())
        self.assertIn(4, sleeps)
        self.assertEqual(len(t.requests), 3)
        for status in (401, 403, 404, 302):
            t = FakeTransport(HTTPError('private-url', status, 'private-body', {}, None))
            with self.assertRaises(CollectionError) as ctx:
                client(t).get('/api/v3/task/query', {})
            self.assertNotIn('private', str(ctx.exception))
            self.assertEqual(len(t.requests), 1)
        with self.assertRaises(CollectionError):
            client(FakeTransport(*[TimeoutError()]*3)).get('/api/v3/task/query', {})

    def test_long_retry_after_aborts_instead_of_retrying_early(self):
        t = FakeTransport(HTTPError('private-url',429,'private',{'Retry-After':'120'},None))
        with self.assertRaises(CollectionError):
            client(t).get('/api/v3/task/query', {})
        self.assertEqual(len(t.requests), 1)

    def test_api_business_error_and_redirect_and_path_safety(self):
        with self.assertRaises(CollectionError):
            client(FakeTransport({'code':403,'errorMessage':'private'})).get('/api/v3/task/query', {})
        for path in ('https://example.com', '/api/v3/task/create', '/api/v3/task/../activity/list'):
            with self.assertRaises(CollectionError):
                client(FakeTransport()).get(path, {})
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://example.com'))

    def test_archive_explicit_and_detail_scope_validation(self):
        task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        task['isArchived'] = True
        t = FakeTransport(page([task]), {'code':200,'result':task})
        self.assertEqual(client(t)._collect_bounded_fixture('demo-project')['records'], [])
        t = FakeTransport(page([task]), {'code':200,'result':[task]}, page())
        self.assertEqual(len(client(t)._collect_bounded_fixture('demo-project',include_archived=True)['records']), 1)
        self.assertIn('includeArchived=true',t.requests[0].full_url)
        for field,value in [('id','other'),('projectId','other'),('isArchived',None)]:
            detail = copy.deepcopy(task); detail[field] = value
            with self.assertRaises(CollectionError):
                client(FakeTransport(page([task]), {'code':200,'result':detail}))._collect_bounded_fixture('demo-project')

    def test_missing_credentials_no_request(self):
        t = FakeTransport()
        c = client(t); c.credentials = lambda: (None,None)
        with self.assertRaises(CollectionError):
            c.get('/api/v3/task/query', {})
        self.assertEqual(t.requests, [])

    def test_malformed_json_and_response_size_limit(self):
        for body in (b'not json: fictional private content', b'x' * (8*1024*1024+1)):
            with self.subTest(size=len(body)):
                c=client(lambda request, timeout: Response(body))
                with self.assertRaises(CollectionError) as ctx:
                    c.get('/api/v3/task/query', {})
                self.assertNotIn('private', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
