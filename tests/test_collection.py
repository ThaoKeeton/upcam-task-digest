"""All responses and credentials below are fictional; no network transport runs."""
import copy
import io
import json
from pathlib import Path
import unittest
from urllib.error import HTTPError, URLError

from teambition_readonly import Client, CollectionError
from teambition_mcp import ENDPOINT, NoRedirect

FIXTURES = Path(__file__).parent / 'fixtures'


def arguments(request):
    return json.loads(request.data)['params']['arguments']


def tool_name(request):
    return json.loads(request.data)['params']['name']


class Response(io.BytesIO):
    def __init__(self, value=b'', *, status=200):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Type': 'application/json'}
        self.raw = body

    def geturl(self):
        return ENDPOINT


class FakeTransport:
    """Fictional JSON-RPC service, exercising the production MCP stack.

    requests contains business calls for traversal assertions; wire_requests
    includes handshake and discovery so no network budget cost is hidden.
    """
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.wire_requests = []
        self.response_sizes = []

    def business_response(self, name, params):
        try:
            result = next(self.responses)
        except StopIteration:
            raise AssertionError('Unexpected fictional MCP business request') from None
        if isinstance(result, BaseException):
            raise result
        return result

    def __call__(self, request, timeout):
        assert request.method == 'POST' and request.full_url == ENDPOINT
        self.wire_requests.append(request)
        message = json.loads(request.data)
        method = message['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fictional-tests', 'version': '1'}}
        elif method == 'notifications/initialized':
            response = Response(status=202)
            self.response_sizes.append(0)
            return response
        elif method == 'tools/list':
            definitions = {
                'teambition.project.task-query': {'projectId': 'string', 'tql': 'string',
                    'pageSize': 'integer', 'pageToken': 'string', 'includeArchived': 'boolean',
                    'enrichFields': 'string'},
                'teambition.task.query': {'taskId': 'string'},
                'teambition.task.activity': {'taskId': 'string', 'pageSize': 'integer',
                    'pageToken': 'string', 'actions': 'string', 'includeFileDetails': 'boolean',
                    'includeRtfHtml': 'boolean'},
            }
            result = {'tools': [{'name': name, 'inputSchema': {'type': 'object',
                      'properties': {key: {'type': kind} for key, kind in fields.items()},
                      'additionalProperties': False}} for name, fields in definitions.items()]}
        elif method == 'tools/call':
            self.requests.append(request)
            params = message['params']
            body = self.business_response(params['name'], params['arguments'])
            if isinstance(body, Response):
                self.response_sizes.append(len(body.raw))
                return body
            result = {'structuredContent': body}
        else:
            raise AssertionError('Unexpected fictional MCP method')
        response = Response({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        self.response_sizes.append(len(response.raw))
        return response


def page(rows=(), token=''):
    return {'code': 200, 'result': list(rows), 'nextPageToken': token}


def activity(event_id='demo-event', task_id='demo', **fields):
    return dict({'id': event_id, 'boundToObjectId': task_id, 'boundToObjectType': 'task',
                 'createTime': '2026-01-02T00:00:00Z'}, **fields)


def client(transport, **kw):
    options = {'sleep': lambda seconds: None, 'credentials': lambda: 'fictional-user-token',
               'mcp_envelope_validator': lambda value: value.get('code') == 200,
               'archive_contract_validator': lambda row: type(row.get('isArchived')) is bool}
    options.update(kw)
    return Client('demo-org', transport=transport, **options)


class ClientTests(unittest.TestCase):
    def test_mcp_uses_user_bearer_token_and_no_app_jwt(self):
        transport = FakeTransport(page())
        collector = client(transport)
        self.assertEqual(collector.get('project.tasks', {'projectId': 'demo-project'}), page())
        self.assertEqual([json.loads(request.data)['method'] for request in transport.wire_requests],
                         ['initialize', 'notifications/initialized', 'tools/list', 'tools/call'])
        self.assertEqual(collector.request_count, 4)
        for request in transport.wire_requests:
            self.assertEqual(request.get_header('Authorization'), 'Bearer fictional-user-token')
            self.assertNotIn('fictional-user-token', request.full_url)
            self.assertNotIn('fictional-user-token', request.data.decode())

    def test_handshake_and_discovery_consume_request_budget_before_business_calls(self):
        for budget in (1, 2, 3):
            transport = FakeTransport(page())
            collector = client(transport, max_requests=budget)
            with self.subTest(budget=budget), self.assertRaisesRegex(CollectionError, 'request budget'):
                collector.get('project.tasks', {'projectId': 'demo-project'})
            self.assertEqual(len(transport.wire_requests), budget)
            self.assertEqual(transport.requests, [])
            self.assertEqual(collector.request_count, budget)

    def test_token_identity_change_never_reuses_an_existing_mcp_session(self):
        token = ['fictional-first-user']
        transport = FakeTransport(page(), page())
        collector = client(transport, credentials=lambda: token[0])
        collector.get('project.tasks', {'projectId': 'demo-project'})
        token[0] = 'fictional-other-user'
        with self.assertRaisesRegex(CollectionError, 'authorization expired or denied'):
            collector.get('project.tasks', {'projectId': 'demo-project'})
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(transport.wire_requests), 4)

    def test_collection_scans_old_tasks_and_all_activity_pages(self):
        task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        transport = FakeTransport(page([task], 'task-page-2'), page([task]),
            {'code': 200, 'result': task},
            page([{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'old-event','createTime':'2020-01-01T00:00:00Z'}], 'event-page-2'),
            page([{'boundToObjectId':'demo-old-task','boundToObjectType':'task','id':'yesterday-comment','createTime':'2026-01-02T00:00:00Z'}]))
        result = client(transport)._collect_bounded_fixture('demo-project')
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(len(result['records'][0]['activities']), 2)
        self.assertEqual(len(transport.requests), 5)
        for req in transport.requests:
            self.assertEqual(req.method, 'POST')
            self.assertEqual(req.full_url, ENDPOINT)
            self.assertEqual(json.loads(req.data)['method'], 'tools/call')
            self.assertNotIn('fictional-user-token', req.full_url)
        self.assertFalse(arguments(transport.requests[0])['includeArchived'])
        self.assertEqual(arguments(transport.requests[1])['pageToken'], 'task-page-2')
        self.assertEqual(arguments(transport.requests[-1])['pageToken'], 'event-page-2')

    def test_default_collection_limit_accepts_more_than_the_previous_100_tasks(self):
        base_task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        tasks = [dict(base_task, id=f'demo-large-window-{index:03d}') for index in range(101)]
        responses = [page(tasks[:100], 'next-task-page'), page(tasks[100:])]
        for task in tasks:
            responses.extend([{'code': 200, 'result': task}, page()])
        responses.extend([page(tasks[:100], 'closing-task-page'), page(tasks[100:])])

        transport = FakeTransport(*responses)
        collector = client(transport)
        result = collector.collect('demo-project', day='2026-01-02')

        self.assertEqual(len(result['records']), 101)
        self.assertEqual(collector.max_tasks, 300)
        self.assertEqual(collector.request_count, 209)
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(arguments(transport.requests[-1])['pageToken'], 'closing-task-page')

    def test_total_response_byte_budget_stops_paginated_reads(self):
        first = page([activity('demo-event-1', 'demo-task', padding='x' * 500)], 'next')
        second = page([activity('demo-event-2', 'demo-task', padding='y' * 500)])
        pilot = FakeTransport(first, second)
        list(client(pilot).pages('task.activities', {'taskId': 'demo-task'}))
        budget = sum(pilot.response_sizes) - 1
        collector = client(FakeTransport(first, second), max_response_bytes=budget)
        with self.assertRaisesRegex(CollectionError, 'response byte budget'):
            list(collector.pages('task.activities', {'taskId': 'demo-task'}))
        self.assertEqual(collector.request_count, 5)  # handshake + first page + bounded final read

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
        row = activity('demo')
        t = FakeTransport(page([], 'next'), page([row]))
        self.assertEqual(list(client(t).pages('task.activities', {'taskId': 'demo'})), [row])
        self.assertEqual(arguments(t.requests[-1])['pageToken'], 'next')

    def test_conflicts_missing_envelopes_and_nonterminal_page_limits_fail(self):
        cases = [
            [page([], 'a'), page([], 'a'), page([], 'a')],
            [page([], 'a'), page([], 'b'), page([], 'a')],
            [page([activity('a', value=1)], 'a'), page([activity('a', value=2)])],
            [{'code':200,'result':[]}],
            [{'code':200,'result':{},'nextPageToken':''}],
            [page([], 42)],
            [page([{}])],
            [page([{'taskId':'demo-task-without-event-id'}])],
            [page([], 'a'), page([], 'b'), page([], 'c')],
        ]
        for responses in cases:
            with self.subTest(responses=responses), self.assertRaises(CollectionError):
                list(client(FakeTransport(*responses), max_pages=3).pages('task.activities', {'taskId': 'demo'}))

    def test_repeated_business_cursor_continues_until_explicit_empty(self):
        rows = [activity(str(i)) for i in range(3)]
        t = FakeTransport(page(rows[:1], 'same'), page(rows[1:2], 'same'), page(rows[2:]))
        self.assertEqual(list(client(t).pages('task.activities', {'taskId': 'demo'})), rows)
        self.assertEqual([arguments(request).get('pageToken') for request in t.requests],
                         [None, 'same', 'same'])

    def test_retry_rate_limits_and_redacted_failures(self):
        sleeps = []
        t = FakeTransport(HTTPError('https://example.com', 429, 'private-body', {'Retry-After':'4'}, None),
                          HTTPError('https://example.com', 503, 'private-body', {}, None), page())
        c = client(t); c.sleep = sleeps.append
        self.assertEqual(c.get('project.tasks', {'projectId': 'demo-project'}), page())
        self.assertIn(4, sleeps)
        self.assertEqual(len(t.requests), 3)
        for status in (401, 403, 404, 302):
            t = FakeTransport(HTTPError('private-url', status, 'private-body', {}, None))
            with self.assertRaises(CollectionError) as ctx:
                client(t).get('project.tasks', {'projectId': 'demo-project'})
            self.assertNotIn('private', str(ctx.exception))
            self.assertEqual(len(t.requests), 1)
        for failure in (URLError('private-body'), TimeoutError('private-body')):
            t = FakeTransport(failure, page())
            with self.assertRaises(CollectionError) as caught:
                client(t).get('project.tasks', {'projectId': 'demo-project'})
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(len(t.requests), 1)  # uncertain requests are not retried

    def test_long_retry_after_aborts_instead_of_retrying_early(self):
        t = FakeTransport(HTTPError('private-url',429,'private',{'Retry-After':'120'},None))
        with self.assertRaises(CollectionError):
            client(t).get('project.tasks', {'projectId': 'demo-project'})
        self.assertEqual(len(t.requests), 1)

    def test_api_business_error_and_redirect_and_path_safety(self):
        with self.assertRaises(CollectionError):
            client(FakeTransport({'code':403,'errorMessage':'private'})).get('project.tasks', {'projectId': 'demo-project'})
        for path in ('https://example.com', '/api/v3/task/query', '/api/v3/task/create',
                     '/api/v3/task/../activity/list', 'task.write'):
            transport = FakeTransport()
            with self.assertRaises(CollectionError):
                client(transport).get(path, {})
            self.assertEqual(transport.wire_requests, [])
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://example.com'))

    def test_archive_explicit_and_detail_scope_validation(self):
        task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        task['isArchived'] = True
        t = FakeTransport(page([task]), {'code':200,'result':task})
        with self.assertRaises(CollectionError):
            client(t)._collect_bounded_fixture('demo-project')
        self.assertEqual(len(t.requests), 1)
        t = FakeTransport(page([task]), {'code':200,'result':[task]}, page())
        self.assertEqual(len(client(t)._collect_bounded_fixture('demo-project',include_archived=True)['records']), 1)
        self.assertTrue(arguments(t.requests[0])['includeArchived'])
        for field,value in [('id','other'),('projectId','other'),('isArchived',None)]:
            detail = copy.deepcopy(task); detail[field] = value
            with self.assertRaises(CollectionError):
                client(FakeTransport(page([task]), {'code':200,'result':detail}))._collect_bounded_fixture('demo-project', include_archived=True)

    def test_missing_credentials_no_request(self):
        t = FakeTransport()
        c = client(t); c.credentials = lambda: None
        with self.assertRaises(CollectionError):
            c.get('project.tasks', {'projectId': 'demo-project'})
        self.assertEqual(t.requests, [])
        self.assertEqual(t.wire_requests, [])

    def test_malformed_json_and_response_size_limit(self):
        for body in (b'not json: fictional private content', b'x' * (8*1024*1024+1)):
            with self.subTest(size=len(body)):
                c=client(lambda request, timeout: Response(body))
                with self.assertRaises(CollectionError) as ctx:
                    c.get('project.tasks', {'projectId': 'demo-project'})
                self.assertNotIn('private', str(ctx.exception))


class ProjectScanCompletionTests(unittest.TestCase):
    def setUp(self):
        self.task = json.loads((FIXTURES/'collection.json').read_text())['records'][0]['task']
        self.day = '2026-01-02'

    def prefix(self, task=None, activities=()):
        task = self.task if task is None else task
        return [page([task]), {'code': 200, 'result': task}, page(activities)]

    def test_archive_option_survives_initial_and_closing_project_scans(self):
        archived = dict(self.task, id='fictional-archived-task', isArchived=True)
        for include_archived, tasks in ((False, [self.task]), (True, [self.task, archived])):
            responses = [page(tasks)]
            for task in tasks:
                responses.extend([{'code': 200, 'result': task}, page()])
            responses.append(page(tasks))
            transport = FakeTransport(*responses)
            with self.subTest(include_archived=include_archived):
                collection = client(transport).collect('demo-project', day=self.day,
                                                       include_archived=include_archived)
                self.assertIs(collection['include_archived'], include_archived)
                self.assertEqual([record['task']['id'] for record in collection['records']],
                                 [task['id'] for task in tasks])
                for request in (transport.requests[0], transport.requests[-1]):
                    query = arguments(request)
                    self.assertIs(query['includeArchived'], include_archived)
                    self.assertEqual(query['tql'], "updated >= '2026-01-02T00:00:00+08:00'")

    def test_archive_option_requires_bool_before_any_request(self):
        for invalid in (None, 0, 1, 'false', [], {}):
            transport = FakeTransport()
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(CollectionError):
                client(transport).collect('demo-project', day=self.day, include_archived=invalid)
            self.assertEqual(transport.requests, [])

    def test_candidate_archive_scope_and_detail_archive_drift_block_completion(self):
        for invalid in (True, None, 0):
            transport = FakeTransport(page([dict(self.task, isArchived=invalid)]))
            with self.subTest(candidate_archive=invalid), self.assertRaises(CollectionError):
                client(transport).collect('demo-project', day=self.day)
            self.assertEqual(len(transport.requests), 1)
        transport = FakeTransport(page([self.task]),
                                  {'code': 200, 'result': dict(self.task, isArchived=True)})
        with self.assertRaisesRegex(CollectionError, 'MCP request failed'):
            client(transport).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 2)

    def test_closing_scan_must_read_past_empty_nonterminal_pages(self):
        transport = FakeTransport(*self.prefix(), page([], 'closing-next'), page([self.task]))
        collection = client(transport).collect('demo-project', day=self.day)
        self.assertTrue(collection['pagination_complete'])
        self.assertEqual(len(collection['records']), 1)
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(arguments(transport.requests[-1])['pageToken'], 'closing-next')

    def test_closing_missing_terminal_envelope_cannot_return_a_complete_empty_digest(self):
        incomplete = {'code': 200, 'result': [self.task]}
        cases = (([*self.prefix(), incomplete], 4),
                 ([*self.prefix(), page([], 'closing-next'), incomplete], 5),
                 ([page(), {'code': 200, 'result': []}], 2))
        for responses, requests in cases:
            transport = FakeTransport(*responses)
            with self.subTest(requests=requests), self.assertRaisesRegex(CollectionError, 'MCP request failed'):
                client(transport).collect('demo-project', day=self.day)
            self.assertEqual(len(transport.requests), requests)

    def test_closing_pagination_limit_blocks_even_if_seen_tasks_already_match(self):
        transport = FakeTransport(*self.prefix(), page([self.task], 'unread-closing-page'))
        with self.assertRaisesRegex(CollectionError, 'incomplete collection'):
            client(transport, max_pages=1).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 4)

    def test_closing_conflicting_duplicate_fields_are_rejected_across_pages(self):
        changed = dict(self.task, title='虚构：结束分页出现冲突')
        transport = FakeTransport(*self.prefix(), page([self.task], 'closing-next'), page([changed]))
        with self.assertRaisesRegex(CollectionError, 'Conflicting duplicate'):
            client(transport).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 5)

    def test_identical_closing_duplicates_are_deduplicated_after_all_pages(self):
        transport = FakeTransport(*self.prefix(), page([self.task], 'closing-next'), page([self.task]))
        collection = client(transport).collect('demo-project', day=self.day)
        self.assertTrue(collection['pagination_complete'])
        self.assertEqual(len(collection['records']), 1)
        self.assertEqual(len(transport.requests), 5)
        self.assertFalse(collection['selection']['snapshot_guaranteed'])

    def test_closing_row_change_with_same_updated_timestamp_is_not_a_stable_scan(self):
        changed = dict(self.task, content='虚构：同版本时间仍观察到字段差异')
        transport = FakeTransport(*self.prefix(), page([changed]))
        with self.assertRaisesRegex(CollectionError, 'changed during collection'):
            client(transport).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 4)

    def test_closing_task_limit_still_blocks_after_all_activity_reads(self):
        extra = dict(self.task, id='fictional-extra-closing-task')
        transport = FakeTransport(*self.prefix(), page([self.task, extra]))
        with self.assertRaisesRegex(CollectionError, 'Task selection exceeds'):
            client(transport, max_tasks=1).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 4)

    def test_empty_project_cannot_skip_closing_request_to_fit_request_budget(self):
        transport = FakeTransport(page(), page())
        with self.assertRaisesRegex(CollectionError, 'request budget'):
            client(transport, max_requests=4).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 1)

    def test_request_budget_interrupts_history_and_closing_without_returning_zero_activity(self):
        old_event = {'id': 'fictional-outside-window', 'action': 'create',
                     'boundToObjectId': self.task['id'], 'boundToObjectType': 'task',
                     'createTime': '2020-01-01T00:00:00Z'}
        responses = [page([self.task]), {'code': 200, 'result': self.task},
                     page([old_event], 'activity-next'), page(),
                     page([], 'closing-next'), page([self.task])]
        for budget in (1, 2, 3, 4, 5):
            transport = FakeTransport(*responses)
            with self.subTest(budget=budget), self.assertRaisesRegex(CollectionError, 'request budget'):
                client(transport, max_requests=budget + 3).collect('demo-project', day=self.day)
            self.assertEqual(len(transport.requests), budget)
        transport = FakeTransport(*responses)
        result = client(transport, max_requests=9).collect('demo-project', day=self.day)
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(result['records'][0]['activities'], [old_event])
        self.assertEqual(len(transport.requests), 6)

    def test_record_budget_interrupts_history_or_closing_instead_of_completing(self):
        activity = {'id': 'fictional-activity-row', 'boundToObjectId': self.task['id'],
                    'boundToObjectType': 'task', 'createTime': '2020-01-01T00:00:00Z'}
        # Details, activity rows and both project scans all count toward the cap.
        for budget, requests in ((1, 1), (2, 2), (3, 3)):
            transport = FakeTransport(*self.prefix(activities=[activity]), page([self.task]))
            with self.subTest(budget=budget), self.assertRaisesRegex(CollectionError, 'record budget'):
                client(transport, max_records=budget).collect('demo-project', day=self.day)
            self.assertEqual(len(transport.requests), requests)

    def test_closing_response_byte_budget_failure_does_not_return_partial_collection(self):
        task = dict(self.task, padding='x' * 600)
        prefix = self.prefix(task)
        pilot = FakeTransport(*prefix, page([task]))
        client(pilot).collect('demo-project', day=self.day)
        budget = sum(pilot.response_sizes) - 1
        self.assertGreaterEqual(budget, 1024)
        transport = FakeTransport(*prefix, page([task]))
        with self.assertRaisesRegex(CollectionError, 'response byte budget'):
            client(transport, max_response_bytes=budget).collect('demo-project', day=self.day)
        self.assertEqual(len(transport.requests), 4)  # closing read is bounded and fails closed

    def test_time_budget_expiring_during_closing_read_cannot_mark_collection_complete(self):
        now = [0.0]
        fake = FakeTransport(*self.prefix(), page([self.task]))

        def closing_timeout(request, timeout):
            response = fake(request, timeout)
            if len(fake.requests) == 4:
                now[0] = 2.0
            return response

        with self.assertRaisesRegex(CollectionError, 'time budget'):
            client(closing_timeout, max_seconds=1, clock=lambda: now[0]).collect(
                'demo-project', day=self.day)
        self.assertEqual(len(fake.requests), 4)


if __name__ == '__main__':
    unittest.main()
