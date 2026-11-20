import unittest

try:
    from upcam_app.chat_query import ChatReadService, ChatQueryError
except ModuleNotFoundError:
    ChatReadService = None
    ChatQueryError = ValueError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.tasks = [
            {'id': 't1', 'projectId': 'p1', 'isArchived': False, 'updated': '2026-11-10T10:00:00+08:00', 'content': '虚构任务甲'},
            {'id': 't2', 'projectId': 'p1', 'isArchived': False, 'updated': '2026-11-10T10:00:00+08:00', 'content': '其他事项'},
        ]
        self.activities = [{'id': 'e1', 'boundToObjectId': 't1', 'boundToObjectType': 'task',
                            'createTime': '2026-11-10T09:00:00+08:00', 'content': '虚构动态'}]
    def pages(self, operation, params, **kwargs):
        self.calls.append((operation, dict(params)))
        if operation == 'project.tasks': return iter(self.tasks)
        if operation == 'task.activities': return iter(self.activities)
        raise AssertionError('unexpected operation')
    def get(self, operation, params):
        self.calls.append((operation, dict(params)))
        row = next(row for row in self.tasks if row['id'] == params['taskId'])
        return {'result': [row.copy()]}


def plan(op, *, search=None, task_id=None, start=None, end=None):
    return {'operation': op, 'search': search, 'task_id': task_id, 'start': start, 'end': end}


class ChatReadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ChatReadService, 'chat query service is not implemented')
        self.client = FakeClient()
        self.service = ChatReadService(self.client, project_id='p1', include_archived=False,
                                       list_projects=lambda: [{'id': 'p1', 'name': '虚构项目'}])

    def test_task_query_is_scoped_and_filters_locally(self):
        result = self.service.execute(plan('tasks', search='任务甲'))
        self.assertEqual([r['id'] for r in result['rows']], ['t1'])
        self.assertEqual(self.client.calls[0][1]['projectId'], 'p1')
        self.assertNotIn('q', self.client.calls[0][1])

    def test_read_activity_verifies_task_binding_and_time_window(self):
        result = self.service.execute(plan('activities', task_id='t1',
                                           start='2026-11-10T08:00:00+08:00',
                                           end='2026-11-10T10:00:00+08:00'))
        self.assertEqual(result['rows'][0]['activities'][0]['id'], 'e1')
        self.assertEqual(self.client.calls[0][0], 'task.details')
        self.assertTrue(all(args.get('projectId') == 'p1' for _, args in self.client.calls))

    def test_unapproved_tool_and_extra_arguments_never_reach_client(self):
        for requested in ({**plan('delete_task')}, {**plan('tasks'), 'tql': 'evil'},
                          {**plan('task', task_id='../x')}, {**plan('activities', start='yesterday')}):
            with self.subTest(requested=requested), self.assertRaises(ChatQueryError):
                self.service.execute(requested)
        self.assertEqual(self.client.calls, [])

    def test_title_search_rejects_control_characters_before_query(self):
        with self.assertRaises(ChatQueryError): self.service.execute(plan('tasks', search='x\ny'))
        self.assertEqual(self.client.calls, [])

    def test_changed_task_or_wrong_activity_binding_fails_closed(self):
        self.client.activities[0]['boundToObjectId'] = 'different'
        with self.assertRaises(ChatQueryError):
            self.service.execute(plan('activities', task_id='t1'))

    def test_conflicting_nested_project_and_alias_block_source(self):
        for mutation in ('nested', 'alias'):
            with self.subTest(mutation=mutation):
                self.client.tasks[0].pop('project', None); self.client.tasks[0].pop('taskId', None)
                if mutation == 'nested': self.client.tasks[0]['project'] = {'_id': 'other-project'}
                else: self.client.tasks[0]['taskId'] = 'other-task'
                with self.assertRaises(ChatQueryError): self.service.execute(plan('tasks'))

    def test_activity_explicitly_bound_to_another_project_is_rejected(self):
        self.client.activities[0]['projectId'] = 'other-project'
        with self.assertRaises(ChatQueryError): self.service.execute(plan('activities', task_id='t1'))

    def test_ambiguous_task_name_is_returned_for_user_to_choose(self):
        self.client.tasks[1]['content'] = '虚构任务甲二'
        result = self.service.execute(plan('task', search='虚构任务甲'))
        self.assertEqual(result['kind'], 'choose_task')
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual([x[0] for x in self.client.calls], ['project.tasks'])

    def test_search_scans_large_project_before_applying_narrow_filter(self):
        self.client.tasks = [dict(self.client.tasks[0], id=f't{i}', content='其他') for i in range(101)]
        self.client.tasks.append(dict(self.client.tasks[0], id='selected', content='唯一命中'))
        result = self.service.execute(plan('tasks', search='唯一命中'))
        self.assertEqual([row['id'] for row in result['rows']], ['selected'])

    def test_project_activity_candidate_filter_discloses_coverage_limit(self):
        self.client.tasks[1]['updated'] = '2026-11-09T10:00:00+08:00'
        result = self.service.execute(plan('activities', start='2026-11-10T08:00:00+08:00',
                                           end='2026-11-11T08:00:00+08:00'))
        self.assertEqual([row['task']['id'] for row in result['rows']], ['t1'])
        self.assertIn('可能遗漏', result['coverage'])

    def test_task_and_project_results_disclose_current_visibility_limit(self):
        self.assertIn('可见', self.service.execute(plan('tasks'))['coverage'])
        self.assertIn('可见', self.service.execute(plan('projects'))['coverage'])

    def test_completed_source_does_not_claim_snapshot_guarantee(self):
        result = self.service.execute(plan('tasks'))
        self.assertFalse(result['snapshot_guaranteed'])

if __name__ == '__main__':
    unittest.main()
