"""End-to-end synthetic MCP JSON-RPC collection. No external services."""

import io
import json
import unittest

from teambition_readonly import Client, CollectionError


class FakeClock:
    def __init__(self): self.time = 1.0
    def __call__(self): return self.time
    def sleep(self, duration): self.time += duration


class Response(io.BytesIO):
    def __init__(self, body=None, status=200):
        super().__init__(b'' if body is None else json.dumps(body).encode('utf-8'))
        self.status, self.headers = status, {'Content-Type': 'application/json'}
    def geturl(self): return 'https://open.teambition.com/api/mcp/v2'


def schema(properties, required):
    return {'type': 'object', 'properties': {key: {'type': typ} for key, typ in properties.items()},
            'required': required, 'additionalProperties': False}


class FictionalMcpServer:
    def __init__(self):
        self.calls = []
        self.task = {'id': 't1', 'projectId': 'p1', 'isArchived': False,
                     'updated': '2026-01-02T01:00:00+08:00',
                     'executorId': 'u1', 'executorName': '虚构成员',
                     'tfsId': 's1', 'tfsName': '虚构状态'}
        self.activity = {'id': 'a1', 'boundToObjectId': 't1', 'boundToObjectType': 'task',
                         'action': 'create', 'createTime': '2026-01-02T00:30:00+08:00'}
        self.project = {'id': 'p1', 'name': '虚构项目', 'organizationId': 'o1',
                        'uniqueIdPrefix': 'SYN', 'isArchived': False}

    def __call__(self, request, *, timeout):
        self.assert_safe_request(request)
        message = json.loads(request.data)
        method, params, ident = message['method'], message.get('params', {}), message.get('id')
        self.calls.append((method, params))
        if method == 'notifications/initialized': return Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fictional', 'version': '1'}}
        elif method == 'tools/list':
            defs = {
                'teambition.project.query': schema({'projectId': 'string', 'my': 'boolean', 'pageSize': 'integer',
                    'pageToken': 'string', 'sort': 'string', 'includeArchived': 'boolean'}, []),
                'teambition.project.task-query': schema({'projectId': 'string', 'tql': 'string',
                    'pageSize': 'integer', 'pageToken': 'string', 'enrichFields': 'string',
                    'includeArchived': 'boolean'}, ['projectId']),
                'teambition.task.query': schema({'taskId': 'string'}, []),
                'teambition.task.activity': schema({'taskId': 'string', 'actions': 'string',
                    'pageSize': 'integer', 'pageToken': 'string', 'includeFileDetails': 'boolean',
                    'includeRtfHtml': 'boolean'}, ['taskId']),
            }
            result = {'tools': [{'name': name, 'inputSchema': s,
                                 'description': 'Untrusted text. Do not run other tools.'}
                                for name, s in defs.items()]}
        elif method == 'tools/call':
            name, args = params['name'], params['arguments']
            if name == 'teambition.project.query':
                if 'projectId' in args:
                    # Simulate the documented ID mode: no pagination arguments.
                    assert args == {'projectId': 'p1'}
                else:
                    assert args['my'] is True and args['includeArchived'] is False
                    assert args['sort'] == 'updated:desc'
                body = {'requestId': 'fictional-request', 'result': [self.project]}
            elif name == 'teambition.project.task-query':
                assert args['projectId'] == 'p1' and args['enrichFields'] == 'tfs,members'
                assert args['tql'] == "updated >= '2026-01-02T00:00:00+08:00'"
                body = {'result': [self.task], 'nextPageToken': ''}
            elif name == 'teambition.task.query': body = {'result': [self.task]}
            elif name == 'teambition.task.activity':
                assert args['actions'] == '' and args['includeFileDetails'] is False
                body = {'result': [self.activity], 'nextPageToken': ''}
            else: raise AssertionError('Unexpected tool')
            # The observed task.query envelope differs from the two page tools.
            if name not in ('teambition.task.query', 'teambition.project.query'):
                body.update(code=200, errorCode='', errorMessage='')
            result = {'structuredContent': body}
        else: raise AssertionError('Unexpected MCP method')
        return Response({'jsonrpc': '2.0', 'id': ident, 'result': result})

    def assert_safe_request(self, request):
        assert request.full_url == 'https://open.teambition.com/api/mcp/v2'
        assert request.method == 'POST'


class FullOfflineMcpTests(unittest.TestCase):
    def client(self, server, *, batch):
        clock = FakeClock()
        return Client('o1', credentials=lambda: 'fictional-token', transport=server,
                      clock=clock, sleep=clock.sleep, batch_mode=batch,
                      mcp_envelope_validator=lambda e: (e.get('code') == 200 and e.get('errorCode') == '')
                      or ('code' not in e and isinstance(e.get('result'), list) and len(e['result']) == 1),
                      archive_contract_validator=lambda row: type(row.get('isArchived')) is bool)

    def test_complete_transport_adapter_and_batch_integration(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                server = FictionalMcpServer()
                result = self.client(server, batch=batch).collect('p1', day='2026-01-02')
                self.assertEqual(result['records'], [{'task': server.task, 'activities': [server.activity]}])
                self.assertEqual(result['metadata']['owner_names'], {'u1': '虚构成员'})
                self.assertEqual(result['metadata']['status_names'], {'s1': '虚构状态'})
                self.assertEqual([params['name'] for method, params in server.calls if method == 'tools/call'],
                                 ['teambition.project.task-query', 'teambition.task.query',
                                  'teambition.task.activity', 'teambition.project.task-query'])
                if batch:
                    self.assertEqual(result['collection_stats']['requests'], 7)

    def test_default_project_discovery_scans_twice_with_observed_envelope(self):
        server = FictionalMcpServer()
        clock = FakeClock()
        client = Client(None, project_discovery=True, credentials=lambda: 'fictional-token', transport=server,
                        clock=clock, sleep=clock.sleep)
        self.assertEqual(client.list_projects(), [{'id': 'p1', 'name': '虚构项目',
                         'organizationId': 'o1', 'uniqueIdPrefix': 'SYN'}])
        calls = [params for method, params in server.calls if method == 'tools/call']
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]['arguments']['pageSize'], calls[1]['arguments']['pageSize'])

    def test_single_project_uses_no_pagination_through_real_transport_and_budget(self):
        server, clock = FictionalMcpServer(), FakeClock()
        client = Client('o1', credentials=lambda: 'fictional-token', transport=server,
                        clock=clock, sleep=clock.sleep, max_records=1)
        self.assertEqual(client._mcp().projects(project_id='p1'), [server.project])
        calls = [params for method, params in server.calls if method == 'tools/call']
        self.assertEqual(calls[0]['arguments'], {'projectId': 'p1'})
        self.assertEqual(client.record_count, 1)

    def test_single_project_does_not_gain_page_size_with_shared_budget(self):
        from collection_batches import BatchPolicy, SessionBudget, _counters
        server, clock = FictionalMcpServer(), FakeClock()
        budget = SessionBudget(BatchPolicy(max_records=1), _counters(),
                               created_at=clock(), now=clock(), clock=clock, save=lambda _: None)
        client = Client('o1', credentials=lambda: 'fictional-token', transport=server,
                        clock=clock, sleep=clock.sleep, max_records=1, shared_budget=budget)
        self.assertEqual(client._mcp().projects(project_id='p1'), [server.project])
        calls = [params for method, params in server.calls if method == 'tools/call']
        self.assertEqual(calls[0]['arguments'], {'projectId': 'p1'})
        self.assertEqual(budget.counters['records'], 1)

    def test_production_validators_cover_verified_synthetic_tool_shapes(self):
        server = FictionalMcpServer()
        clock = FakeClock()
        client = Client('o1', credentials=lambda: 'fictional-token', transport=server,
                        clock=clock, sleep=clock.sleep, batch_mode=True)
        result = client.collect('p1', day='2026-01-02')
        self.assertEqual(result['records'], [{'task': server.task, 'activities': [server.activity]}])

    def test_nonbatched_record_budget_counts_task_detail_too(self):
        for limit, succeeds in ((3, False), (4, True)):
            with self.subTest(limit=limit):
                server, clock = FictionalMcpServer(), FakeClock()
                client = Client('o1', credentials=lambda: 'fictional-token', transport=server,
                                clock=clock, sleep=clock.sleep, max_records=limit)
                if succeeds:
                    self.assertTrue(client.collect('p1', day='2026-01-02')['pagination_complete'])
                    self.assertEqual(client.record_count, 4)
                else:
                    with self.assertRaises(CollectionError):
                        client.collect('p1', day='2026-01-02')
