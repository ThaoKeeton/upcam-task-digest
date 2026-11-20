"""Fictional MCP envelopes only. No live service, credential, or model calls."""

import copy
from types import MappingProxyType
import unittest

import mcp_collection_adapter
from mcp_collection_adapter import MCPCollectionAdapter
from teambition_readonly import CollectionError


def fictional_success(envelope):
    # This test-only value is NOT a claim about production code/errorCode values.
    return envelope.get('fictionalSuccess') is True


def fictional_archive_contract(row):
    return type(row.get('isArchived')) is bool


def page(rows=(), token=''):
    return {'fictionalSuccess': True, 'code': 'TEST_ONLY_SUCCESS', 'errorCode': None,
            'result': list(rows), 'nextPageToken': token}


def project(**changes):
    return dict({'id': 'fictional-project', 'name': '虚构项目',
                 'organizationId': 'fictional-org', 'uniqueIdPrefix': 'FIC',
                 'isArchived': False}, **changes)


def task(**changes):
    return dict({'id': 'fictional-task', 'projectId': 'fictional-project',
                 'updated': '2026-01-03T10:00:00+08:00', 'isArchived': False,
                 'executorId': 'fictional-owner', 'executorName': '虚构甲',
                 'tfsId': 'fictional-status', 'tfsName': '已解决',
                 'isDone': True, 'uninterpreted': {'keep': 'verbatim'}}, **changes)


def activity(**changes):
    return dict({'id': 'fictional-event', 'boundToObjectId': 'fictional-task',
                 'boundToObjectType': 'task', 'createTime': '2026-01-02T10:00:00+08:00',
                 'action': 'comment', 'content': 'fictional text'}, **changes)


class FictionalTransport:
    """Only replace the external MCP boundary; validation remains real."""

    def __init__(self, *responses):
        self.responses = list(copy.deepcopy(responses))
        self.calls = []

    def call_tool(self, name, arguments, *, validate_envelope):
        self.calls.append((name, copy.deepcopy(arguments)))
        if not self.responses:
            raise AssertionError('Unexpected extra MCP request')
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if validate_envelope(result) is not True:
            raise CollectionError('Fictional transport rejected envelope')
        return result


class MCPCollectionAdapterTests(unittest.TestCase):
    def make(self, *responses, **options):
        transport = FictionalTransport(*responses)
        options.setdefault('validate_envelope', fictional_success)
        options.setdefault('validate_task_archive', fictional_archive_contract)
        options.setdefault('organization_id', 'fictional-org')
        return MCPCollectionAdapter(transport, **options), transport

    def test_projects_read_array_with_my_filter_and_complete_paging(self):
        client, transport = self.make(page([], 'next'), page([project()]), page([project()]))
        self.assertEqual(client.projects(page_size=25), [project()])
        self.assertEqual(transport.calls, [
            ('teambition.project.query', {'my': True, 'pageSize': 25, 'includeArchived': False,
                                          'sort': 'updated:desc'}),
            ('teambition.project.query', {'my': True, 'pageSize': 25, 'pageToken': 'next',
                                          'includeArchived': False, 'sort': 'updated:desc'}),
            ('teambition.project.query', {'my': True, 'pageSize': 100, 'includeArchived': False,
                                          'sort': 'updated:desc'})])

    def test_project_page_keeps_missing_terminal_cursor_without_fabrication(self):
        response = {'requestId': 'fictional-request', 'result': [project()]}
        client, _ = self.make(response, validate_envelope=lambda row:
                              set(row) == {'requestId', 'result'})
        self.assertEqual(client.project_page(), response)

    def test_two_project_scans_agree_across_distinct_page_sizes_and_orders(self):
        first = project(id='fictional-first')
        second = project(id='fictional-second')
        ending = {'fictionalSuccess': True, 'result': [second]}
        client, transport = self.make(page([first], 'next'), ending,
                                      {'fictionalSuccess': True, 'result': [second, first]})
        self.assertEqual(client.projects(page_size=1), [first, second])
        self.assertEqual([arguments['pageSize'] for _, arguments in transport.calls], [1, 1, 100])

    def test_default_project_recheck_uses_second_distinct_page_size(self):
        client, transport = self.make(page([project()]), page([project()]))
        self.assertEqual(client.projects(), [project()])
        self.assertEqual([arguments['pageSize'] for _, arguments in transport.calls], [100, 50])

    def test_two_project_scans_reject_set_or_content_changes(self):
        for changed in ([], [project(id='fictional-other')], [project(name='changed')],
                        [project(), project(id='fictional-added')]):
            client, _ = self.make(page([project()]), page(changed))
            with self.subTest(changed=changed), self.assertRaises(CollectionError):
                client.projects()

    def test_project_scan_rejects_identical_ids_within_or_across_pages(self):
        responses = (
            (page([project(), project()]),),
            (page([project()], 'next'), page([project()])),
            (page([project()]), page([project()], 'next'), page([project()])))
        for sequence in responses:
            client, _ = self.make(*sequence)
            with self.subTest(pages=len(sequence)), self.assertRaises(CollectionError):
                client.projects()

    def test_my_project_listing_rejects_archived_projects(self):
        client, _ = self.make(page([project(isArchived=True)]))
        with self.assertRaises(CollectionError):
            client.projects()

    def test_project_identifier_lookup_scans_once_and_allows_missing_cursor(self):
        response = {'fictionalSuccess': True, 'result': [project()]}
        client, transport = self.make(response)
        self.assertEqual(client.projects(project_id='fictional-project'), [project()])
        self.assertEqual(transport.calls, [('teambition.project.query', {
            'projectId': 'fictional-project'})])

    def test_specific_project_request_omits_all_pagination_arguments(self):
        response = {'fictionalSuccess': True, 'result': [project()]}
        client, transport = self.make(response)
        self.assertEqual(client.project_page(project_id='fictional-project', page_size=25), response)
        self.assertEqual(transport.calls, [('teambition.project.query', {'projectId': 'fictional-project'})])

    def test_specific_project_response_cannot_contain_multiple_rows(self):
        for rows in ([project(), project()], [project(), project(id='fictional-other')]):
            with self.subTest(rows=len(rows)):
                client, transport = self.make(page(rows))
                with self.assertRaisesRegex(CollectionError, 'Invalid or incomplete MCP page envelope'):
                    client.project_page(project_id='fictional-project', page_size=100)
                self.assertEqual(len(transport.calls), 1)

    def test_specific_project_rejects_explicit_cursor_before_network(self):
        for cursor in ('', 'opaque'):
            with self.subTest(cursor=cursor):
                client, transport = self.make(page([project()]))
                with self.assertRaises(CollectionError):
                    client.project_page(project_id='fictional-project', page_token=cursor)
                self.assertEqual(transport.calls, [])

    def test_specific_project_unexpected_cursor_is_not_silently_followed(self):
        client, transport = self.make(page([project()], 'unexpected'))
        with self.assertRaises(CollectionError):
            client.projects(project_id='fictional-project')
        self.assertEqual(len(transport.calls), 1)

    def test_unresolved_project_prefix_is_preserved_without_guessing(self):
        missing = project(); missing.pop('uniqueIdPrefix')
        for row in (missing, project(uniqueIdPrefix=None), project(uniqueIdPrefix='')):
            client, _ = self.make(page([row]))
            with self.subTest(prefix=row.get('uniqueIdPrefix')):
                self.assertEqual(client.projects(project_id='fictional-project'), [row])

    def test_project_rescan_cannot_bypass_page_or_raw_row_limits(self):
        client, transport = self.make(page([project()]), page([], 'next'), max_pages=1)
        with self.assertRaises(CollectionError):
            client.projects()
        self.assertEqual(len(transport.calls), 2)
        client, _ = self.make(page([project()]),
                              page([project(), project(id='fictional-other')]), max_records=1)
        with self.assertRaises(CollectionError):
            client.projects()

    def test_project_scans_can_finish_at_exact_shared_record_limit(self):
        response = {'fictionalSuccess': True, 'result': [project()]}
        client, _ = self.make(response, response, max_pages=1, max_records=2)
        self.assertEqual(client.projects(), [project()])

    def test_project_two_scans_share_the_raw_record_budget(self):
        client, transport = self.make(page([project()]), page([project()]), max_records=1)
        with self.assertRaises(CollectionError):
            client.projects()
        self.assertEqual(len(transport.calls), 2)

    def test_empty_project_listing_still_requires_second_scan(self):
        client, transport = self.make(page(), page())
        self.assertEqual(client.projects(), [])
        self.assertEqual(len(transport.calls), 2)
        client, _ = self.make(page(), page([project()]))
        with self.assertRaises(CollectionError):
            client.projects()

    def test_project_missing_cursor_exception_does_not_apply_to_tasks_or_activities(self):
        client, _ = self.make({'fictionalSuccess': True, 'result': []},
                              {'fictionalSuccess': True, 'result': []})
        with self.assertRaises(CollectionError):
            client.task_page('fictional-project')
        with self.assertRaises(CollectionError):
            client.activity_page('fictional-task')

    def test_specific_project_is_checked_without_my_filter(self):
        client, transport = self.make(page([project()]))
        self.assertEqual(client.project_page(project_id='fictional-project'), page([project()]))
        self.assertEqual(transport.calls, [('teambition.project.query', {
            'projectId': 'fictional-project'})])

    def test_tasks_use_lower_bound_only_and_enriched_readonly_tool(self):
        client, transport = self.make(page([task()]))
        result = client.tasks('fictional-project', updated_since='2026-01-01T00:00:00+08:00',
                              include_archived=True, page_size=20)
        self.assertEqual(result, [task()])
        self.assertEqual(transport.calls, [('teambition.project.task-query', {
            'projectId': 'fictional-project', 'tql': "updated >= '2026-01-01T00:00:00+08:00'",
            'includeArchived': True, 'pageSize': 20, 'enrichFields': 'tfs,members'})])

    def test_full_project_page_omits_tql_and_preserves_cursor(self):
        client, transport = self.make(page())
        client.task_page('fictional-project', updated_since=None, page_token='opaque')
        self.assertEqual(transport.calls[0], ('teambition.project.task-query', {
            'projectId': 'fictional-project', 'includeArchived': False,
            'pageSize': 100, 'pageToken': 'opaque', 'enrichFields': 'tfs,members'}))

    def test_activities_read_every_page_with_no_action_or_time_filter(self):
        older = activity(id='fictional-older', createTime='2020-01-01T00:00:00Z')
        client, transport = self.make(page([older], 'next'), page([], 'last'), page([activity()]))
        self.assertEqual(client.activities('fictional-task', page_size=30), [older, activity()])
        self.assertEqual(transport.calls, [
            ('teambition.task.activity', dict({'taskId': 'fictional-task', 'actions': '',
              'includeFileDetails': False, 'includeRtfHtml': False, 'pageSize': 30}, **extra))
            for extra in ({}, {'pageToken': 'next'}, {'pageToken': 'last'})])

    def test_repeated_nonempty_cursor_continues_with_id_deduplication(self):
        second = activity(id='fictional-second')
        client, transport = self.make(page([activity()], 'same'), page([activity()], 'same'),
                                      page([second], ''))
        self.assertEqual(client.activities('fictional-task'), [activity(), second])
        self.assertEqual(len(transport.calls), 3)

    def test_conflicting_duplicate_record_rejected(self):
        client, _ = self.make(page([activity()], 'same'), page([activity(content='different')]))
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')

    def test_unknown_missing_or_nonempty_invalid_cursor_never_means_done(self):
        invalid = [dict(fictionalSuccess=True, result=[]), page([], False), page([], 0),
                   page([], []), page([], 'x' * 8193)]
        for response in invalid:
            with self.subTest(response=response):
                client, _ = self.make(response)
                with self.assertRaises(CollectionError):
                    client.activity_page('fictional-task')

    def test_none_cursor_is_explicit_terminal(self):
        client, transport = self.make(page([activity()], None))
        self.assertEqual(client.activities('fictional-task'), [activity()])
        self.assertEqual(len(transport.calls), 1)

    def test_page_budget_exhaustion_is_not_empty_success(self):
        client, transport = self.make(page([], 'same'), page([], 'same'), max_pages=2)
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')
        self.assertEqual(len(transport.calls), 2)

    def test_record_budget_counts_duplicate_rows_before_deduplication(self):
        client, _ = self.make(page([activity()], 'same'), page([activity()]), max_records=1)
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')

    def test_exact_record_and_page_budgets_succeed_only_at_terminal(self):
        client, _ = self.make(page([activity()]), max_records=1, max_pages=1)
        self.assertEqual(client.activities('fictional-task'), [activity()])

    def test_success_values_cannot_be_guessed_from_code_or_error_code(self):
        for response in ({'code': 200, 'result': [], 'nextPageToken': ''},
                         {'errorCode': 0, 'result': [], 'nextPageToken': ''},
                         dict(page(), fictionalSuccess=False)):
            with self.subTest(response=response):
                client, _ = self.make(response)
                with self.assertRaises(CollectionError):
                    client.activities('fictional-task')

    def test_missing_success_validator_blocks_before_transport(self):
        client, transport = self.make(page(), validate_envelope=None)
        with self.assertRaises(CollectionError):
            client.projects()
        self.assertEqual(transport.calls, [])

    def mapped(self, validators, *responses):
        try:
            return self.make(*responses, validate_envelope=validators)
        except CollectionError:
            self.fail('Valid tool-specific envelope validator mapping was rejected')

    def test_tool_validator_mapping_handles_distinct_page_and_detail_envelopes(self):
        # Fictional rows using the parent-supplied outer response shapes.
        def paginated(envelope):
            return (type(envelope.get('code')) is int and envelope['code'] == 200
                    and envelope.get('errorCode') == ''
                    and isinstance(envelope.get('result'), list)
                    and isinstance(envelope.get('nextPageToken'), str))

        def detail_only(envelope):
            return (set(envelope) == {'result'} and isinstance(envelope['result'], list)
                    and len(envelope['result']) == 1)

        validators = {
            'teambition.project.task-query': paginated,
            'teambition.task.activity': paginated,
            'teambition.task.query': detail_only}
        task_response = {'code': 200, 'errorCode': '', 'result': [task()], 'nextPageToken': ''}
        activity_response = {'code': 200, 'errorCode': '', 'result': [activity()], 'nextPageToken': ''}
        detail_response = {'result': [task()]}
        client, transport = self.mapped(validators, task_response, activity_response, detail_response)
        self.assertEqual(client.tasks('fictional-project'), [task()])
        self.assertEqual(client.activities('fictional-task'), [activity()])
        self.assertEqual(client.task_detail('fictional-task', project_id='fictional-project'), task())
        self.assertEqual([name for name, _ in transport.calls], [
            'teambition.project.task-query', 'teambition.task.activity', 'teambition.task.query'])

    def test_mapped_tool_cannot_borrow_another_tools_success_policy(self):
        validators = {'teambition.project.task-query': lambda _: False,
                      'teambition.task.activity': lambda _: True}
        client, _ = self.mapped(validators, page(), page())
        with self.assertRaises(CollectionError):
            client.task_page('fictional-project')
        self.assertEqual(client.activities('fictional-task'), [])

    def test_unlisted_project_tool_is_blocked_before_transport(self):
        for validators in ({}, {'teambition.task.activity': fictional_success}):
            with self.subTest(validators=tuple(validators)):
                client, transport = self.mapped(validators, page([project()]))
                with self.assertRaises(CollectionError):
                    client.projects()
                with self.assertRaises(CollectionError):
                    client.projects(project_id='fictional-project')
                self.assertEqual(transport.calls, [])

    def test_validator_mapping_is_copied_without_later_expansion(self):
        validators = {'teambition.task.activity': lambda _: False}
        client, transport = self.mapped(validators, page())
        validators['teambition.task.activity'] = lambda _: True
        validators['teambition.project.query'] = lambda _: True
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')
        with self.assertRaises(CollectionError):
            client.projects()
        self.assertEqual(len(transport.calls), 1)

    def test_readonly_mapping_is_supported(self):
        validators = MappingProxyType({'teambition.task.activity': fictional_success})
        client, _ = self.mapped(validators, page([activity()]))
        self.assertEqual(client.activities('fictional-task'), [activity()])

    def test_mapping_rejects_unknown_tool_names_and_noncallable_policies(self):
        for validators in ({'teambition.task.update': fictional_success},
                           {'teambition.project.task-query': None},
                           {'teambition.task.activity': True},
                           {None: fictional_success},
                           {'*': fictional_success}):
            with self.subTest(keys=tuple(validators)), self.assertRaises(CollectionError):
                self.make(validate_envelope=validators)

    def test_unknown_tool_never_reaches_transport_even_with_callable_validator(self):
        client, transport = self.make(page())
        with self.assertRaises(CollectionError):
            client._call('teambition.task.update', {'taskId': 'fictional-task'}, lambda _: None)
        self.assertEqual(transport.calls, [])

    def test_mapped_validator_requires_exact_true_and_sanitizes_exceptions(self):
        def raises_private_message(_):
            raise ValueError('FICTIONAL_PRIVATE_RESPONSE')

        for validator in (lambda _: 1, lambda _: 'yes', lambda _: None, raises_private_message):
            client, _ = self.mapped({'teambition.task.activity': validator}, page())
            with self.subTest(validator=validator), self.assertRaises(CollectionError) as caught:
                client.activities('fictional-task')
            self.assertNotIn('FICTIONAL_PRIVATE_RESPONSE', str(caught.exception))

    def test_detail_contract_does_not_accept_page_envelope_and_reverse(self):
        validators = {
            'teambition.project.task-query': lambda row: row.get('code') == 200,
            'teambition.task.query': lambda row: set(row) == {'result'}}
        client, _ = self.mapped(validators, {'result': [task()]},
                               {'code': 200, 'errorCode': '', 'result': [task()]})
        with self.assertRaises(CollectionError):
            client.task_page('fictional-project')
        with self.assertRaises(CollectionError):
            client.task_detail('fictional-task', project_id='fictional-project')

    def test_missing_archive_contract_blocks_task_read_before_transport(self):
        client, transport = self.make(page([task()]), validate_task_archive=None)
        with self.assertRaises(CollectionError):
            client.tasks('fictional-project')
        self.assertEqual(transport.calls, [])

    def test_permission_failure_propagates_instead_of_empty_activities(self):
        client, _ = self.make(CollectionError('Fictional permission denied'))
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')

    def test_project_identity_organization_archive_and_name_validated(self):
        bad = [project(id='other'), project(organizationId='other'), project(isArchived='false'),
               project(name=''), project(_id='other'), project(uniqueIdPrefix=12)]
        for row in bad:
            with self.subTest(row=row):
                client, _ = self.make(page([row]))
                with self.assertRaises(CollectionError):
                    client.projects(project_id='fictional-project')

    def test_task_identity_time_archive_scope_and_candidate_lower_bound_validated(self):
        bad = [task(id='invalid id'), task(_id='other'), task(taskId='other'),
               task(projectId='other'), task(updated=None), task(updated='2026-01-02T12:00:00'),
               task(updated='2020-01-01T00:00:00Z'), task(isArchived='false'), task(isArchived=True),
               task(created='not-a-time')]
        missing_archive = task(); missing_archive.pop('isArchived'); bad.append(missing_archive)
        for row in bad:
            with self.subTest(row=row):
                client, _ = self.make(page([row]))
                with self.assertRaises(CollectionError):
                    client.tasks('fictional-project', updated_since='2026-01-01T00:00:00Z')

    def test_activity_identity_time_binding_and_optional_project_validated(self):
        bad = [activity(id=None), activity(_id='other'), activity(boundToObjectId='other'),
               activity(boundToObjectType='project'), activity(taskId='other'),
               activity(createTime=None), activity(createTime='2026-01-02T10:00:00'),
               activity(projectId='other')]
        for row in bad:
            with self.subTest(row=row):
                client, _ = self.make(page([row]))
                with self.assertRaises(CollectionError):
                    client.activities('fictional-task', project_id='fictional-project')

    def test_no_fallback_from_task_id_to_missing_activity_id(self):
        row = activity(taskId='fictional-task'); row.pop('id')
        client, _ = self.make(page([row]))
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')

    def test_detail_uses_only_task_read_tool_and_validates_identity_version(self):
        response = {'fictionalSuccess': True, 'result': task()}
        client, transport = self.make(response)
        self.assertEqual(client.task_detail('fictional-task', project_id='fictional-project',
                                           expected_updated='2026-01-03T10:00:00+08:00'), task())
        self.assertEqual(transport.calls, [('teambition.task.query', {'taskId': 'fictional-task'})])
        for row in (task(id='other'), task(projectId='other'), task(updated='2026-01-04T00:00:00Z')):
            client, _ = self.make({'fictionalSuccess': True, 'result': row})
            with self.assertRaises(CollectionError):
                client.task_detail('fictional-task', project_id='fictional-project',
                                   expected_updated='2026-01-03T10:00:00+08:00')

    def test_single_detail_array_preserves_envelope_and_unwraps_exactly_one_task(self):
        # A supported shape, not a claim that real MCP detail has been verified.
        response = dict(page([task()]))
        client, _ = self.make(response, response)
        self.assertEqual(client.detail_envelope('fictional-task', project_id='fictional-project'), response)
        self.assertEqual(client.task_detail('fictional-task', project_id='fictional-project'), task())

    def test_detail_array_missing_ambiguous_or_mismatched_identity_rejected(self):
        for rows in ([], [task(), task()], [task(id='other')]):
            client, _ = self.make(page(rows))
            with self.subTest(rows=rows), self.assertRaises(CollectionError):
                client.task_detail('fictional-task', project_id='fictional-project')

    def test_conflicting_same_page_records_rejected_at_page_boundary(self):
        client, _ = self.make(page([activity(), activity(content='different')]))
        with self.assertRaises(CollectionError):
            client.activity_page('fictional-task')

    def test_enriched_names_are_session_local_and_do_not_prove_resolution(self):
        client, _ = self.make(page([task()]))
        client.tasks('fictional-project')
        self.assertEqual(client.metadata, {
            'owner_names': {'fictional-owner': '虚构甲'}, 'status_names': {'fictional-status': '已解决'},
            'status_resolution_known': [], 'resolved_status_ids': []})
        client.metadata['owner_names']['fictional-owner'] = 'mutated'
        self.assertEqual(client.metadata['owner_names'], {'fictional-owner': '虚构甲'})
        fresh, _ = self.make(page())
        self.assertEqual(fresh.metadata['owner_names'], {})

    def test_metadata_from_final_tasks_does_not_depend_on_adapter_lifetime(self):
        helper = getattr(mcp_collection_adapter, 'metadata_from_tasks', None)
        self.assertTrue(callable(helper), 'Pure task metadata aggregation is not implemented')
        raw = [task(), task(id='fictional-second', executorId='fictional-other', executorName='虚构乙')]
        original = copy.deepcopy(raw)
        self.assertEqual(helper(raw), {
            'owner_names': {'fictional-owner': '虚构甲', 'fictional-other': '虚构乙'},
            'status_names': {'fictional-status': '已解决'},
            'status_resolution_known': [], 'resolved_status_ids': []})
        self.assertEqual(raw, original)
        with self.assertRaises(CollectionError):
            helper([task(), task(executorName='冲突')])

    def test_duplicate_conflict_does_not_collapse_boolean_and_number(self):
        client, _ = self.make(page([activity(extra=1)], 'next'), page([activity(extra=True)]))
        with self.assertRaises(CollectionError):
            client.activities('fictional-task')

    def test_conflicting_name_evidence_fails_without_partial_metadata_commit(self):
        for key in ('executorName', 'tfsName'):
            client, _ = self.make(page([task(), task(id='fictional-second', **{key: '冲突名称'})]))
            with self.assertRaises(CollectionError):
                client.tasks('fictional-project')
            self.assertEqual(client.metadata['owner_names'], {})
            self.assertEqual(client.metadata['status_names'], {})

    def test_missing_names_are_not_fabricated_or_inferred_from_done(self):
        row = task(); row.pop('executorName'); row.pop('tfsName')
        client, _ = self.make(page([row]))
        client.tasks('fictional-project')
        self.assertEqual(client.metadata, {'owner_names': {}, 'status_names': {},
                                           'status_resolution_known': [], 'resolved_status_ids': []})

    def test_name_without_identity_and_invalid_names_fail(self):
        for row in (task(executorId=None), task(executorName=' name '), task(tfsName='bad\nname')):
            client, _ = self.make(page([row]))
            with self.assertRaises(CollectionError):
                client.task_page('fictional-project')

    def test_bad_arguments_fail_before_any_tool_call(self):
        for values in ({'project_id': 'bad id'}, {'project_id': 'fictional-project', 'page_size': True},
                       {'project_id': 'fictional-project', 'include_archived': 'false'},
                       {'project_id': 'fictional-project', 'updated_since': '2026-01-01'},
                       {'project_id': 'fictional-project', 'updated_since': "2026-01-01T00:00:00Z' or 1=1"},
                       {'project_id': 'fictional-project', 'page_token': 0}):
            client, transport = self.make(page())
            with self.subTest(values=values), self.assertRaises(CollectionError):
                client.task_page(**values)
            self.assertEqual(transport.calls, [])


if __name__ == '__main__':
    unittest.main()
