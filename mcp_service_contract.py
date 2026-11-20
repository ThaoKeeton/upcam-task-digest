"""Narrow business response contracts observed in authorized MCP reads.

The supported names below were verified separately. Other tools are not
implicitly accepted. This module contains no credentials or business content.
Project-list termination and snapshot completeness are not
guaranteed; callers must make two consistent scans and disclose that limit.
"""


def _verified_paged_result(value):
    return (isinstance(value, dict) and type(value.get('code')) is int
            and value['code'] == 200 and type(value.get('errorCode')) is str
            and value['errorCode'] == '' and isinstance(value.get('result'), list)
            and type(value.get('nextPageToken')) is str
            and len(value['nextPageToken']) <= 8192)


def _verified_single_task(value):
    if (not isinstance(value, dict)
            or any(key in value for key in ('code', 'errorCode', 'nextPageToken'))):
        return False
    rows = value.get('result')
    return (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
            and type(rows[0].get('isArchived')) is bool)


def _verified_project_list(value):
    if (not isinstance(value, dict) or any(key in value for key in ('code', 'errorCode'))
            or not isinstance(value.get('requestId'), str) or not value['requestId']
            or not isinstance(value.get('result'), list)):
        return False
    if 'nextPageToken' not in value:
        return True
    token = value['nextPageToken']
    return type(token) is str and 0 < len(token) <= 8192


def task_archive_is_boolean(row):
    return isinstance(row, dict) and type(row.get('isArchived')) is bool


VERIFIED_VALIDATORS = {
    'teambition.project.query': _verified_project_list,
    'teambition.project.task-query': _verified_paged_result,
    'teambition.task.activity': _verified_paged_result,
    'teambition.task.query': _verified_single_task,
}
