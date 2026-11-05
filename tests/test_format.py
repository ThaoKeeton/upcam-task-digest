import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from render_digest import render_digest


ROOT = Path(__file__).resolve().parents[1]


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/digest.json").read_text(encoding="utf-8"))

    def test_full_approved_snapshot_and_cli(self):
        expected = (ROOT / "examples/digest.md").read_text(encoding="utf-8")
        self.assertEqual(render_digest(self.data), expected)
        result = subprocess.run(
            [sys.executable, str(ROOT / "render_digest.py"), str(ROOT / "examples/digest.json")],
            cwd=ROOT.parent, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertEqual(result.stdout, expected)

    def test_category_order_counts_and_spacing(self):
        self.data["tasks"].reverse()
        output = render_digest(self.data)
        headings = ["**✅ 已解决（1）**", "**💬 工作进展（2）**", "**🆕 新建任务（1）**", "**📝 其他变更（1）**"]
        positions = [output.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        for heading in headings:
            self.assertEqual(output.count(heading), 1)
            self.assertIn(f"\n\n\u3000\n\n{heading}  \n1\\. [", output)
        self.assertIn("📊 更新 5 项：已解决 1 项、工作进展 2 项、新建任务 1 项、其他变更 1 项\n\n\u3000\n\n", output)
        self.assertNotIn("\n---", output)
        self.assertNotIn("\n\n\n", output)

    def test_task_lines_use_hard_breaks_without_indentation(self):
        output = render_digest(self.data)
        for index, task in enumerate(self.data["tasks"]):
            number = f"{index}\\. " if task["category"] == "progress" else "1\\. "
            title = task["title"].replace("-", "\\-")
            summary_line = f'{task["summary"]}  \n' if task["summary"] else ''
            block = (f'{number}[{title}](<{task["url"]}>)  \n'
                     f'{summary_line}'
                     f'当前：{task["status"]}｜负责人：{task["owner"]}')
            self.assertIn(block, output)
        self.assertIn("\n\n\u3000\n\n2\\. [", output)

    def test_empty_and_single_item_categories(self):
        self.data["tasks"] = [self.data["tasks"][1]]
        output = render_digest(self.data)
        self.assertIn("**💬 工作进展（1）**", output)
        self.assertIn("  \n1\\. [", output)
        self.assertNotIn("\n\n\u3000\n\n2\\. [", output)
        for absent in ("已解决", "新建任务", "其他变更"):
            self.assertNotIn(absent, output)
        self.data["tasks"] = []
        self.assertEqual(render_digest(self.data), "**Teambition｜UPCAM 项目任务动态日报**  \n🕒 时间范围：2026-01-01 00:00 — 2026-01-02 00:00  \n📊 更新 0 项\n")

    def test_empty_custom_windows_keep_only_title_range_and_zero_count(self):
        self.data.update(date="2026-01-02", tasks=[])
        for start, end, displayed in (
            ("2026-01-01T16:50:00+08:00", "2026-01-02T08:50:00+08:00",
             "2026-01-01 16:50 — 2026-01-02 08:50"),
            ("2026-01-02T00:00:00+08:00", "2026-01-02T16:50:00+08:00",
             "2026-01-02 00:00 — 16:50"),
        ):
            with self.subTest(start=start, end=end):
                self.data["time_range"] = {
                    "start": start, "end_exclusive": end, "timezone": "Asia/Shanghai",
                }
                self.assertEqual(render_digest(self.data),
                    "**Teambition｜UPCAM 项目任务动态日报**  \n"
                    f"🕒 时间范围：{displayed}  \n📊 更新 0 项\n")

    def test_spacer_paragraphs_and_literal_numbering(self):
        output = render_digest(self.data)
        self.assertTrue(output.startswith(
            "**Teambition｜UPCAM 项目任务动态日报**  \n🕒 时间范围：2026-01-01 00:00 — 2026-01-02 00:00  \n📊 更新 5 项："
        ))
        self.assertNotIn("📅", output)
        self.assertEqual(output.count("🕒"), 1)
        self.assertEqual(output.count("📊"), 1)
        blocks = output.rstrip("\n").split("\n\n\u3000\n\n")
        # One compact header block + five tasks (category headings join first tasks).
        self.assertEqual(len(blocks), 6)
        self.assertEqual(output.count("\n\n\u3000\n\n"), 5)
        self.assertNotRegex(output, r"(?m)^\d+\. ")
        self.assertNotRegex(output, r"(?m)^[ \t]+\S")
        for block in blocks:
            if "https://example.com/tasks/" in block:
                if block.startswith("**"):
                    heading, block = block.split("  \n", 1)
                    self.assertTrue(heading.endswith("**"))
                self.assertRegex(block, r"^\d+\\\. \[")
                task = next(task for task in self.data['tasks'] if task['url'] in block)
                lines = 3 if task['summary'] else 2
                self.assertEqual(len(block.splitlines()), lines)
                self.assertEqual(block.count("  \n"), lines - 1)

    def test_duplicate_task_rejected_across_categories(self):
        duplicate = copy.deepcopy(self.data["tasks"][0])
        duplicate["category"] = "other"
        self.data["tasks"].append(duplicate)
        with self.assertRaises(ValueError):
            render_digest(self.data)

    def test_each_category_starts_at_one_even_with_single_task(self):
        for category in ("resolved", "progress", "new", "other"):
            with self.subTest(category=category):
                data = copy.deepcopy(self.data)
                task = data["tasks"][0]
                task["category"] = category
                data["tasks"] = [task]
                output = render_digest(data)
                self.assertEqual(output.count("  \n1\\. ["), 1)
                self.assertNotIn("\n\n\u3000\n\n2\\. [", output)
        output = render_digest(self.data)
        self.assertEqual(output.count("  \n1\\. ["), 4)
        self.assertEqual(output.count("\n\n\u3000\n\n2\\. ["), 1)

    def test_invalid_fields_do_not_break_layout(self):
        for field, value in (("category", "unknown"), ("title", "first\nsecond"),
                             ("summary", None), ("summary", "  "), ("url", "javascript:alert(1)")):
            with self.subTest(field=field):
                data = copy.deepcopy(self.data)
                data["tasks"][0][field] = value
                with self.assertRaises(ValueError):
                    render_digest(data)

    def test_no_supplement_omits_summary_line_but_keeps_owner_and_status(self):
        task = self.data['tasks'][0]
        task['summary'] = ''
        self.data['tasks'] = [task]
        output = render_digest(self.data)
        self.assertIn(f'](<{task["url"]}>)  \n当前：{task["status"]}｜负责人：{task["owner"]}', output)
        for forbidden in ('北京时间', '采集截止'):
            self.assertNotIn(forbidden, output)
        for key in ('owner', 'status'):
            bad = copy.deepcopy(self.data)
            bad['tasks'][0][key] = ''
            with self.assertRaises(ValueError): render_digest(bad)

    def test_editorial_sample_keeps_material_facts_without_redundant_summary(self):
        output = render_digest(self.data)
        for fact in ('复查通过', '焦点丢失风险', '下一步补充回归', '验收受阻', 'demoabc1'):
            self.assertIn(fact, output)
        for task in self.data['tasks']:
            self.assertIn(f'当前：{task["status"]}｜负责人：{task["owner"]}', output)
            self.assertNotIn(task['status'], task['summary'])
        for omitted in ('北京时间', '采集截止', '新建筛选需求', '自动加参与者', '收到'):
            self.assertNotIn(omitted, output)
        self.assertIn('🕒 时间范围：2026-01-01 00:00 — 2026-01-02 00:00', output)

    def test_markdown_title_and_parenthesized_direct_link(self):
        self.data["tasks"] = [self.data["tasks"][0]]
        self.data["tasks"][0]["title"] = "示例 [标题] *原文*"
        self.data["tasks"][0]["url"] = "https://example.com/tasks/(sample)"
        self.assertIn(r"[示例 \[标题\] \*原文\*](<https://example.com/tasks/(sample)>)", render_digest(self.data))


if __name__ == "__main__":
    unittest.main()
