"""Fictional integration of existing collector semantics with MCP only."""

import unittest

from teambition_readonly import Client, CollectionError


class FakeAdapter:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def task_page(self, project_id, **kwargs):
        self.calls.append(('task_page', project_id, kwargs))
        return self.pages.pop(0)

    def activity_page(self, task_id, **kwargs):
        self.calls.append(('activity_page', task_id, kwargs))
        return self.pages.pop(0)

    def detail_envelope(self, task_id, **kwargs):
        self.calls.append(('detail', task_id, kwargs))
        return self.pages.pop(0)

    def projects(self, project_id=None):
        self.calls.append(('projects', project_id, {}))
        return self.pages.pop(0)


class McpClientBridgeTests(unittest.TestCase):
    def make_client(self, pages):
        adapter = FakeAdapter(pages)
        client = Client('fake-org', credentials=lambda: 'fake-token',
                        mcp_adapter_factory=lambda client: adapter)
        return client, adapter

    def test_page_queries_are_dispatched_to_mcp_only_with_same_scope(self):
        client, adapter = self.make_client([{'result': [], 'nextPageToken': ''}])
        result = list(client.pages('project.tasks', {
            'projectId': 'fake-project', 'pageSize': 10, 'includeArchived': False,
            'q': "updated >= '2026-01-02T00:00:00+08:00'"}))
        self.assertEqual(result, [])
        self.assertEqual(adapter.calls, [('task_page', 'fake-project', {
            'updated_since': '2026-01-02T00:00:00+08:00', 'include_archived': False,
            'page_size': 10, 'page_token': None})])

    def test_repeated_business_cursor_is_not_terminal(self):
        pages = [{'result': [{'id': 'a'}], 'nextPageToken': 'same'},
                 {'result': [{'id': 'b'}], 'nextPageToken': 'same'},
                 {'result': [{'id': 'c'}], 'nextPageToken': ''}]
        client, adapter = self.make_client(pages)
        rows = list(client.pages('task.activities', {'taskId': 'fake-task', 'pageSize': 10}))
        self.assertEqual([row['id'] for row in rows], ['a', 'b', 'c'])
        self.assertEqual([call[2]['page_token'] for call in adapter.calls], [None, 'same', 'same'])

    def test_cross_page_duplicate_bool_is_not_equal_to_number(self):
        pages = [{'result': [{'id': 'a', 'data': True}], 'nextPageToken': 'same'},
                 {'result': [{'id': 'a', 'data': 1}], 'nextPageToken': ''}]
        client, _ = self.make_client(pages)
        with self.assertRaises(CollectionError):
            list(client.pages('task.activities', {'taskId': 't1', 'pageSize': 10}))

    def test_unknown_internal_route_cannot_trigger_an_mcp_tool(self):
        client, adapter = self.make_client([])
        with self.assertRaises(CollectionError):
            client.get('task.write', {})
        self.assertEqual(adapter.calls, [])

    def test_project_picker_does_not_require_a_manually_entered_tenant(self):
        adapter = FakeAdapter([[{'id': 'p1', 'name': '项目一', 'organizationId': 'o1',
                                 'uniqueIdPrefix': 'UPCAM', 'isArchived': False,
                                 'description': 'not needed'}]])
        client = Client(None, credentials=lambda: 'fake-token', project_discovery=True,
                        mcp_adapter_factory=lambda owner: adapter)
        self.assertEqual(client.list_projects(), [{'id': 'p1', 'name': '项目一', 'organizationId': 'o1',
                                                   'uniqueIdPrefix': 'UPCAM'}])
        self.assertEqual(adapter.calls, [('projects', None, {})])

    def test_project_picker_does_not_guess_or_accept_invalid_prefix(self):
        for prefix in (None, '', 'bad prefix', 'x' * 33):
            adapter = FakeAdapter([[{'id': 'p1', 'name': '项目', 'organizationId': 'o1',
                                     'isArchived': False, 'uniqueIdPrefix': prefix}]])
            client = Client(None, credentials=lambda: 'fake-token', project_discovery=True,
                            mcp_adapter_factory=lambda owner: adapter)
            if prefix in (None, ''):
                self.assertNotIn('uniqueIdPrefix', client.list_projects()[0])
            else:
                with self.assertRaises(CollectionError):
                    client.list_projects()

    def test_batched_collector_reuses_fixed_mcp_adapter_not_app_routes(self):
        row = {'id': 'task-a', 'projectId': 'fake-project', 'isArchived': False,
               'updated': '2026-01-02T02:00:00+08:00',
               'executorId': 'u1', 'executorName': '虚构人员',
               'tfsId': 's1', 'tfsName': '虚构状态'}
        event = {'id': 'event-a', 'boundToObjectId': 'task-a', 'boundToObjectType': 'task',
                 'createTime': '2026-01-02T01:00:00+08:00', 'action': 'create'}
        adapter = FakeAdapter([{'result': [row], 'nextPageToken': ''}, {'result': row},
                               {'result': [event], 'nextPageToken': ''},
                               {'result': [row], 'nextPageToken': ''}])
        client = Client('fake-org', credentials=lambda: 'fake-token', batch_mode=True,
                        mcp_adapter_factory=lambda owner: adapter)
        result = client.collect('fake-project', day='2026-01-02')
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(result['records'], [{'task': row, 'activities': [event]}])
        self.assertEqual(result['metadata'], {'owner_names': {'u1': '虚构人员'},
                         'status_names': {'s1': '虚构状态'}, 'status_resolution_known': [],
                         'resolved_status_ids': []})
        self.assertEqual([call[0] for call in adapter.calls],
                         ['task_page', 'detail', 'activity_page', 'task_page'])
