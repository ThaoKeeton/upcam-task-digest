"""Task display uses confirmed metadata and synthetic identifiers only."""

from copy import deepcopy
import unittest

from task_display import (TaskDisplayError, format_task_reference,
                          normalize_task_link_view, validate_task_prefix)


PROJECT = "a1" * 12
TASK = "b2" * 12
VIEW = "c3" * 12
OTHER = "d4" * 12


def sample_task(**updates):
    return {"id": TASK, "projectId": PROJECT, "content": "登录：修复 v2.0 / 状态",
            "uniqueId": 7, **updates}


def sample_config(**updates):
    return {"project_id": PROJECT, "task_number_prefix": "DEMO", **updates}


class PrefixTests(unittest.TestCase):
    def test_confirmed_ascii_prefix_is_preserved(self):
        for prefix in ("DEMO", "D", "D2_mo", "D" * 32):
            with self.subTest(prefix=prefix):
                self.assertEqual(validate_task_prefix(prefix), prefix)

    def test_empty_settings_prefix_is_allowed_until_display_needs_it(self):
        self.assertEqual(validate_task_prefix(""), "")
        with self.assertRaises(TaskDisplayError):
            validate_task_prefix("", allow_empty=False)

    def test_invalid_prefix_is_rejected_without_normalizing_it(self):
        for value in (None, True, 7, [], {}, b"DEMO", " DEMO", "DEMO ", "DE-MO",
                      "DEMO-", "2DEMO", "_DEMO", "ＤＥＭＯ", "演示", "DE\u200bMO",
                      "DEMO\n", "DEMO\t", "D" * 33, "DEMO/OTHER", "DEMO:7"):
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                validate_task_prefix(value)


class ViewLinkTests(unittest.TestCase):
    def test_empty_view_and_plain_hex_view_id(self):
        self.assertEqual(normalize_task_link_view("", PROJECT), "")
        self.assertEqual(normalize_task_link_view(VIEW, PROJECT), VIEW)
        self.assertEqual(normalize_task_link_view(VIEW.upper(), PROJECT), VIEW.upper())

    def test_matching_project_view_urls_return_only_view_id(self):
        urls = (f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}",
                f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}/task/{TASK}")
        for value in urls:
            with self.subTest(value=value):
                self.assertEqual(normalize_task_link_view(value, PROJECT), VIEW)

    def test_view_id_requires_exactly_24_ascii_hex_characters(self):
        for value in (None, True, 24, [], {}, b"c3", "c" * 23, "c" * 25, "g" * 24,
                      "ｃ" * 24, " " + VIEW, VIEW + "\n", VIEW + "/"):
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                normalize_task_link_view(value, PROJECT)

    def test_view_url_rejects_wrong_authority_project_and_path_attacks(self):
        base = f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}"
        values = (
            base.replace("https://", "http://"),
            base.replace("www.teambition.com", "teambition.com"),
            base.replace("www.teambition.com", "www.teambition.com.evil.invalid"),
            base.replace("www.teambition.com", "user@www.teambition.com"),
            base.replace("www.teambition.com", "www.teambition.com:443"),
            base.replace(PROJECT, OTHER), base + "?x=1", base + "?", base + "#x", base + "#",
            base + "/", base + "/task/", base + f"/task/{TASK}/other", base + "/task/../other",
            base.replace("/tasks/", "/%74asks/"), base.replace("/tasks/", "/tasks//"),
            base.replace("/tasks/", "/tasks/../tasks/"), base.replace("/project/", "/project%2f"),
            base + "%3Fhidden", base + "\\task\\" + TASK, "\n" + base,
            base.replace("/tasks/", "/ta\u200bsks/"), base + f"/task/{TASK}?x=1",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                normalize_task_link_view(value, PROJECT)

    def test_url_cannot_match_an_invalid_configured_project(self):
        value = f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}"
        for project in (None, "", True, [], PROJECT + "/other", "演示"):
            with self.subTest(project=project), self.assertRaises(TaskDisplayError):
                normalize_task_link_view(value, project)


class TaskReferenceTests(unittest.TestCase):
    def test_number_precedes_unmodified_title_and_uses_standalone_link_by_default(self):
        self.assertEqual(format_task_reference(sample_task(), sample_config()), {
            "number": "DEMO-7", "title": "DEMO-7 登录：修复 v2.0 / 状态",
            "url": f"https://www.teambition.com/task/{TASK}",
        })

    def test_string_number_padding_is_preserved(self):
        result = format_task_reference(sample_task(uniqueId="0007"), sample_config())
        self.assertEqual(result["number"], "DEMO-0007")
        self.assertEqual(result["title"], "DEMO-0007 登录：修复 v2.0 / 状态")

    def test_maximum_18_digit_numbers_are_supported(self):
        for value in (999999999999999999, "999999999999999999", "000000000000000001"):
            with self.subTest(value=value):
                self.assertEqual(format_task_reference(sample_task(uniqueId=value), sample_config())["number"],
                                 "DEMO-" + str(value))

    def test_invalid_or_unconfirmed_numbers_are_never_guessed(self):
        values = (None, True, False, 0, -1, 1.0, [], {}, b"7", "", "0", "0000", "-7", "+7",
                  " 7", "7 ", "7\n", "７", "٧", "7.0", "7e1", "DEMO-7",
                  10 ** 18, "1" * 19, "0" * 18 + "1")
        for value in values:
            task = sample_task(uniqueId=value, content="DEMO-7 标题里和提交里的编号不能作为字段来源")
            with self.subTest(value=value), self.assertRaises(TaskDisplayError) as caught:
                format_task_reference(task, sample_config())
            self.assertIn("任务编号", str(caught.exception))

    def test_missing_number_does_not_fall_back_to_task_identity(self):
        task = sample_task()
        del task["uniqueId"]
        with self.assertRaises(TaskDisplayError):
            format_task_reference(task, sample_config())

    def test_confirmed_nested_project_prefix_can_supply_the_prefix(self):
        task = sample_task(project={"_id": PROJECT, "id": PROJECT, "uniqueIdPrefix": "DEMO"})
        self.assertEqual(format_task_reference(task, {"project_id": PROJECT})["number"], "DEMO-7")

    def test_matching_nested_and_saved_prefixes_are_accepted(self):
        task = sample_task(project={"uniqueIdPrefix": "DEMO"})
        self.assertEqual(format_task_reference(task, sample_config())["number"], "DEMO-7")

    def test_nested_project_without_prefix_uses_saved_confirmation(self):
        self.assertEqual(format_task_reference(sample_task(project={"id": PROJECT}), sample_config())["number"],
                         "DEMO-7")

    def test_unavailable_nested_prefix_uses_confirmed_saved_prefix(self):
        for prefix in (None, ""):
            with self.subTest(prefix=prefix):
                result = format_task_reference(sample_task(project={"uniqueIdPrefix": prefix}), sample_config())
                self.assertEqual(result["number"], "DEMO-7")

    def test_unavailable_nested_prefix_requires_nonempty_saved_confirmation(self):
        for prefix in (None, ""):
            for config in ({"project_id": PROJECT}, sample_config(task_number_prefix="")):
                with self.subTest(prefix=prefix, config=config), self.assertRaises(TaskDisplayError):
                    format_task_reference(sample_task(project={"uniqueIdPrefix": prefix}), config)

    def test_missing_prefix_is_not_filled_with_an_assumed_project_name(self):
        for config in ({"project_id": PROJECT}, sample_config(task_number_prefix="")):
            with self.subTest(config=config), self.assertRaises(TaskDisplayError) as caught:
                format_task_reference(sample_task(), config)
            self.assertIn("前缀", str(caught.exception))

    def test_conflicting_prefix_sources_block_display(self):
        task = sample_task(project={"uniqueIdPrefix": "OTHER"})
        with self.assertRaises(TaskDisplayError):
            format_task_reference(task, sample_config())

    def test_present_invalid_prefixes_are_not_ignored(self):
        for prefix in ([], {}, False, 0, " ", "ＤＥＭＯ", "DEMO-"):
            with self.subTest(prefix=prefix), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(project={"uniqueIdPrefix": prefix}), sample_config())
        with self.assertRaises(TaskDisplayError):
            format_task_reference(sample_task(project={"uniqueIdPrefix": "DEMO"}),
                                  sample_config(task_number_prefix=[]))

    def test_only_exact_existing_number_and_space_avoids_double_prefix(self):
        cases = (("DEMO-7 登录：修复", "DEMO-7 登录：修复"),
                 ("DEMO-7：登录", "DEMO-7 DEMO-7：登录"),
                 ("#DEMO-7 登录", "DEMO-7 #DEMO-7 登录"),
                 ("DEMO-70 登录", "DEMO-7 DEMO-70 登录"))
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(format_task_reference(sample_task(content=title), sample_config())["title"], expected)

    def test_configured_view_uses_the_confirmed_full_route(self):
        result = format_task_reference(sample_task(), sample_config(task_link_view_id=VIEW))
        self.assertEqual(result["url"],
                         f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}/task/{TASK}")

    def test_empty_view_uses_documented_standalone_task_route(self):
        result = format_task_reference(sample_task(), sample_config(task_link_view_id=""))
        self.assertEqual(result["url"], f"https://www.teambition.com/task/{TASK}")

    def test_saved_view_must_be_an_id_after_settings_normalization(self):
        for value in (None, [], "g" * 24,
                      f"https://www.teambition.com/project/{PROJECT}/tasks/view/{VIEW}"):
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(), sample_config(task_link_view_id=value))

    def test_unverified_server_url_is_not_used(self):
        result = format_task_reference(sample_task(url="https://evil.invalid/private"), sample_config())
        self.assertEqual(result["url"], f"https://www.teambition.com/task/{TASK}")

    def test_all_supplied_task_identity_aliases_must_agree(self):
        task = sample_task(_id=TASK, taskId=TASK)
        self.assertEqual(format_task_reference(task, sample_config())["number"], "DEMO-7")
        for updates in ({"_id": OTHER}, {"taskId": OTHER}, {"_id": ""}, {"taskId": None}):
            with self.subTest(updates=updates), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(**updates), sample_config())

    def test_record_identity_fallbacks_are_accepted_when_unambiguous(self):
        for key in ("_id", "taskId"):
            task = sample_task()
            del task["id"]
            task[key] = TASK
            with self.subTest(key=key):
                self.assertEqual(format_task_reference(task, sample_config())["url"],
                                 f"https://www.teambition.com/task/{TASK}")

    def test_task_identity_must_be_present_and_safe(self):
        for value in (None, "", True, [], "中文", TASK + "/other", TASK + "?x=1", TASK + "\n"):
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(id=value), sample_config())

    def test_task_and_configured_project_must_match(self):
        with self.assertRaises(TaskDisplayError):
            format_task_reference(sample_task(projectId=OTHER), sample_config())
        for value in (None, "", True, [], "中文", PROJECT + "/other"):
            with self.subTest(value=value), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(), sample_config(project_id=value))

    def test_display_metadata_scope_accepts_only_the_current_tenant_and_project(self):
        tenant = "e5" * 12
        config = sample_config(tenant_id=tenant, task_display_scope={"tenant_id": tenant, "project_id": PROJECT})
        self.assertEqual(format_task_reference(sample_task(), config)["number"], "DEMO-7")
        self.assertEqual(format_task_reference(sample_task(), sample_config(task_display_scope=None))["number"],
                         "DEMO-7")

    def test_stale_or_malformed_display_metadata_scope_blocks_formatting(self):
        tenant = "e5" * 12
        scopes = ([], "PRIVATE_SCOPE", True, {},
                  {"tenant_id": OTHER, "project_id": PROJECT},
                  {"tenant_id": tenant, "project_id": OTHER},
                  {"tenant_id": tenant, "project_id": PROJECT, "extra": "PRIVATE_SCOPE"},
                  {"tenant_id": tenant}, {"project_id": PROJECT})
        for scope in scopes:
            with self.subTest(scope=scope):
                with self.assertRaises(TaskDisplayError) as caught:
                    format_task_reference(sample_task(), sample_config(tenant_id=tenant, task_display_scope=scope))
                self.assertIn("标识", str(caught.exception))
                for private in (tenant, PROJECT, OTHER, "PRIVATE_SCOPE"):
                    self.assertNotIn(private, str(caught.exception))

    def test_nested_project_id_aliases_must_match_current_project(self):
        for project in ({"id": OTHER}, {"_id": OTHER}, {"id": PROJECT, "_id": OTHER},
                        {"id": ""}, {"_id": None}, None, [], "DEMO"):
            with self.subTest(project=project), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(project=project), sample_config())

    def test_title_rejects_missing_text_and_invisible_controls(self):
        for value in (None, "", " ", 7, [], {}, "line\nbreak", "tab\ttext", "\u200f标题",
                      "标题\x7f", "标题\u2028换行", "标题\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(TaskDisplayError):
                format_task_reference(sample_task(content=value), sample_config())

    def test_metadata_errors_are_fixed_chinese_and_do_not_echo_private_values(self):
        messages = []
        for secret in ("PRIVATE_TITLE_ONE", "PRIVATE_TITLE_TWO"):
            task = sample_task(uniqueId=secret, content=secret)
            with self.assertRaises(TaskDisplayError) as caught:
                format_task_reference(task, sample_config())
            message = str(caught.exception)
            messages.append(message)
            self.assertRegex(message, "[\u4e00-\u9fff]")
            for private in (secret, TASK, PROJECT, "uniqueId", "Traceback"):
                self.assertNotIn(private, message)
        self.assertEqual(messages[0], messages[1])

    def test_bad_argument_types_raise_safe_display_errors(self):
        for task, config in ((None, sample_config()), ([], sample_config()),
                             (sample_task(), None), (sample_task(), [])):
            with self.subTest(task=task, config=config), self.assertRaises(TaskDisplayError):
                format_task_reference(task, config)

    def test_formatting_does_not_mutate_task_or_configuration(self):
        task = sample_task(project={"id": PROJECT, "uniqueIdPrefix": "DEMO"})
        config = sample_config(task_link_view_id=VIEW)
        original = deepcopy((task, config))
        format_task_reference(task, config)
        self.assertEqual((task, config), original)


if __name__ == "__main__":
    unittest.main()
