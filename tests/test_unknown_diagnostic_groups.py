"""Fictional source failures remain blocked while local diagnostics stay useful."""

from copy import deepcopy
import json
import re
import unittest

from automated_digest import (
    AutomatedDigestBlocked, build_automated_digest, diagnose_automatic_source,
)
from report_contract import updated_selection


class ForbiddenSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Hard source failures must prevent every model request")


class UnknownDiagnosticGroupsTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "tenant_id": "synthetic-tenant", "project_id": "synthetic-project",
            "include_archived": False, "api_contract_verified": True,
            "view": {"verified": True, "evidence": "fictional verification", "task_equals": {}},
            "owner_names": {"synthetic-person": "虚构成员"},
            "status_names": {"synthetic-open": "虚构待处理"},
            "resolved_status_ids": [],
        }

    def event(self, task_id, index, action, content=None):
        return {
            "id": f"synthetic-event-{index}", "action": action,
            "boundToObjectId": task_id, "boundToObjectType": "task",
            "createTime": "2026-01-02T08:00:00Z",
            "content": (json.dumps(content, ensure_ascii=False) if content is not None
                        else '{"PRIVATE_UNKNOWN_BODY":"PRIVATE_VALUE"}'),
        }

    def record(self, task_id, actions):
        return {
            "task": {
                "id": task_id, "projectId": "synthetic-project", "isArchived": False,
                "content": "PRIVATE_TASK_TITLE", "executorId": "synthetic-person",
                "tfsId": "synthetic-open", "created": "2020-01-01T00:00:00Z",
                "updated": "2026-01-02T08:00:00Z", "isDone": False,
            },
            "activities": [self.event(task_id, i, action) for i, action in enumerate(actions)],
        }

    def blocked_message(self, records):
        collection = {
            "tenant_id": "synthetic-tenant", "project_id": "synthetic-project",
            "include_archived": False, "pagination_complete": True,
            "selection": updated_selection("2026-01-02"), "records": records,
        }
        diagnostic = diagnose_automatic_source(collection, self.config, "2026-01-02")
        self.assertFalse(diagnostic["ok"])
        client = ForbiddenSummarizer()
        with self.assertRaises(AutomatedDigestBlocked) as raised:
            build_automated_digest(collection, self.config, "2026-01-02", "synthetic-key",
                                   "gpt-6-sol", "max", client=client)
        self.assertEqual(client.calls, 0)
        self.assertEqual(str(raised.exception), diagnostic["message"])
        self.assertIn("未调用 OpenAI", diagnostic["message"])
        self.assertIn("未发送钉钉", diagnostic["message"])
        return diagnostic["message"]

    def test_many_tasks_share_one_type_without_hiding_a_later_type(self):
        # Counting task/action pairs or events would overcount the two types.
        records = [self.record(f"synthetic-task-{i:03d}", ["future.synthetic.alpha"] * 2)
                   for i in range(67)]
        records[-1]["activities"].append(self.event("synthetic-task-066", "late-beta", "future.synthetic.beta"))
        records += [self.record("zz-late-task-1", ["future.synthetic.beta"]),
                    self.record("zz-late-task-2", ["future.synthetic.beta"])]

        message = self.blocked_message(records)

        self.assertIn("共 2 类未识别动态类型/类别", message)
        self.assertIn("影响 69 个任务", message)
        self.assertEqual(message.count("future.synthetic.alpha"), 1)
        self.assertEqual(message.count("future.synthetic.beta"), 1)
        self.assertIn("future.synthetic.alpha：影响 67 个任务", message)
        self.assertIn("future.synthetic.beta：影响 3 个任务", message)
        alpha = next(line for line in message.splitlines() if "future.synthetic.alpha" in line)
        self.assertIn("synthetic-task-000, synthetic-task-001, synthetic-task-002", alpha)
        self.assertNotIn("synthetic-task-003", alpha)
        self.assertIn("先复制", message)
        self.assertIn("类型行", message)
        self.assertIn("导出安全结构", message)
        self.assertEqual(message, self.blocked_message(list(reversed(records))))

    def test_other_hard_failures_remain_visible_with_many_unknown_tasks(self):
        records = [self.record(f"synthetic-task-{i:03d}", ["future.synthetic.alpha"])
                   for i in range(120)]
        for record in records:
            task_id = record["task"]["id"]
            record["activities"].append(self.event(task_id, "bad-comment", "comment", {
                "comment": "PRIVATE_COMMENT_BODY", "PRIVATE_FIELD": "PRIVATE_COMMENT_VALUE",
            }))
        record = self.record("zz-other-failures", [])
        record["task"].update(executorId="synthetic-unmapped-owner", tfsId="synthetic-unmapped-status")
        record["activities"] = [
            self.event("zz-other-failures", "commit", "standard", {
                "title": "关联了提交: PRIVATE_COMMIT", "customCreator": {"name": "PRIVATE_SOURCE"},
            }),
            self.event("zz-other-failures", "other-standard", "standard", {
                "title": "PRIVATE_OTHER_TITLE", "customCreator": {"name": "PRIVATE_OTHER_SOURCE"},
            }),
            self.event("zz-other-failures", "executor", "update_executor", {}),
        ]
        records.append(record)

        message = self.blocked_message(records)

        for reason in ("未识别动态类型", "评论动态结构未通过本地校验", "尚未识别的评论字段",
                       "关联提交动态仅有标题模式", "standard 动态尚未符合已核验结构或标题规则",
                       "执行人变更动态结构或来源标记无法核实", "缺少成员姓名映射", "缺少状态名称映射"):
            self.assertIn(reason, message)
        self.assertIn("synthetic-unmapped-owner", message)
        self.assertIn("synthetic-unmapped-status", message)
        self.assertEqual(message.count("future.synthetic.alpha"), 1)
        self.assertNotIn("PRIVATE_", message)

    def test_unsafe_and_non_string_actions_share_a_hidden_category(self):
        actions = [
            None, True, 123, {"PRIVATE_ACTION_KEY": "PRIVATE_ACTION_VALUE"},
            ["PRIVATE_LIST_ACTION"], b"PRIVATE_BYTES_ACTION", "",
            "x" * 81, "https://example.invalid/PRIVATE_URL_ACTION",
            "PRIVATE_ACTION WITH SPACES", "PRIVATE_ACTION\nINJECTED", "PRIVATE_ACTION\u202e",
            "PRIVATE_敏感动作", "A", "B" * 80, "safe_token-._19",
        ]
        records = [self.record(f"synthetic-action-task-{i:02d}", [action, deepcopy(action)])
                   for i, action in enumerate(actions)]

        message = self.blocked_message(records)

        self.assertIn("共 4 类未识别动态类型/类别", message)
        self.assertEqual(message.count("原值已隐藏"), 1)
        self.assertIn("（格式无效，原值已隐藏）：影响 13 个任务", message)
        self.assertIn("未识别动态类型 A：影响 1 个任务", message)
        self.assertIn("未识别动态类型 " + "B" * 80 + "：影响 1 个任务", message)
        self.assertIn("未识别动态类型 safe_token-._19：影响 1 个任务", message)
        for unsafe in ("PRIVATE_", "INJECTED", "example.invalid", "x" * 81, "敏感动作", "\u202e"):
            self.assertNotIn(unsafe, message)

    def test_type_limit_reports_the_number_of_omitted_categories(self):
        record = self.record("synthetic-many-types", [f"future.synthetic.kind{i:02d}" for i in range(40)])

        message = self.blocked_message([record])

        rows = re.findall(r"^未识别动态类型 (future\.synthetic\.kind[0-9]+)：影响 1 个任务", message, re.M)
        self.assertIn(len(rows), (24, 32))
        self.assertEqual(len(rows), len(set(rows)))
        self.assertIn("共 40 类未识别动态类型/类别", message)
        self.assertIn(f"另有 {40 - len(rows)} 类未识别动态类型/类别未显示", message)
        self.assertIn("导出安全结构", message)
        self.assertLessEqual(len(message), 12000)

    def test_display_overflow_is_explicit_and_keeps_other_failure_categories(self):
        records = [self.record(f"synthetic-{i:02d}-" + "a" * 450, [f"future.synthetic.kind{i:02d}"])
                   for i in range(32)]
        other = self.record("zz-important-failures", [])
        other["task"]["executorId"] = "synthetic-unmapped-owner"
        other["activities"] = [
            self.event("zz-important-failures", "comment", "comment", {
                "comment": "PRIVATE_COMMENT", "PRIVATE_FIELD": True,
            }),
            self.event("zz-important-failures", "commit", "standard", {
                "title": "关联了提交: PRIVATE_COMMIT", "customCreator": {"name": "PRIVATE_SOURCE"},
            }),
        ]
        records.append(other)

        message = self.blocked_message(records)

        self.assertLessEqual(len(message), 12000)
        self.assertIn("诊断显示已截断", message)
        self.assertIn("导出安全结构", message)
        self.assertIn("评论动态结构未通过本地校验", message)
        self.assertIn("关联提交动态仅有标题模式", message)
        self.assertIn("缺少成员姓名映射", message)
        self.assertNotIn("PRIVATE_", message)


if __name__ == "__main__":
    unittest.main()
