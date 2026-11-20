"""Per-tool acceptance limited to types observed in authorized read-only tests."""

import unittest

from mcp_service_contract import VERIFIED_VALIDATORS, task_archive_is_boolean


class McpVerifiedContractTests(unittest.TestCase):
    def test_task_and_activity_pages_require_observed_success_and_cursor_types(self):
        for name in ('teambition.project.task-query', 'teambition.task.activity'):
            check = VERIFIED_VALIDATORS[name]
            for rows, token in (([], ''), ([{'id': 'synthetic'}], 'next'), ([{'id': 'synthetic'}], '')):
                self.assertTrue(check({'code': 200, 'errorCode': '', 'result': rows, 'nextPageToken': token}))
            for changes in ({'code': True}, {'code': '200'}, {'errorCode': None},
                            {'nextPageToken': None}, {'result': {}}, {'errorCode': 'denied'}):
                value = {'code': 200, 'errorCode': '', 'result': [], 'nextPageToken': ''}
                value.update(changes)
                self.assertFalse(check(value))
            for missing in ('code', 'errorCode', 'result', 'nextPageToken'):
                value = {'code': 200, 'errorCode': '', 'result': [], 'nextPageToken': ''}
                del value[missing]
                self.assertFalse(check(value))

    def test_single_task_detail_has_distinct_observed_envelope(self):
        check = VERIFIED_VALIDATORS['teambition.task.query']
        self.assertTrue(check({'result': [{'id': 't1', 'isArchived': False}]}))
        for value in ({'result': []}, {'result': {}}, {'result': [{'isArchived': 0}]},
                      {'result': [{'isArchived': False}], 'code': 200},
                      {'result': [{'isArchived': False}], 'nextPageToken': ''}):
            self.assertFalse(check(value))

    def test_project_list_uses_its_own_observed_envelope(self):
        check = VERIFIED_VALIDATORS['teambition.project.query']
        self.assertTrue(check({'requestId': 'fictional', 'result': []}))
        self.assertTrue(check({'requestId': 'fictional', 'result': [{}], 'nextPageToken': 'next'}))
        for value in ({'result': []}, {'requestId': 1, 'result': []},
                      {'requestId': 'x', 'result': {},},
                      {'requestId': 'x', 'result': [], 'nextPageToken': ''},
                      {'requestId': 'x', 'result': [], 'nextPageToken': None},
                      {'requestId': 'x', 'result': [], 'code': 200}):
            self.assertFalse(check(value))

    def test_member_query_is_not_guessed(self):
        self.assertNotIn('teambition.member.query', VERIFIED_VALIDATORS)

    def test_archive_flag_requires_boolean_not_truthiness(self):
        self.assertTrue(task_archive_is_boolean({'isArchived': True}))
        self.assertTrue(task_archive_is_boolean({'isArchived': False}))
        for value in (None, 0, 1, '', 'false'):
            self.assertFalse(task_archive_is_boolean({'isArchived': value}))
