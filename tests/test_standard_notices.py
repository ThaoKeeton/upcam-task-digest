"""Synthetic display notices never establish a code object or its source."""

import copy
import json
import unittest

from standard_notices import standard_association_notification


class StandardNoticeTests(unittest.TestCase):
    def parse(self, raw):
        return standard_association_notification(raw)

    def event(self, title="关联了提交 「fictional change」", name="虚构通知来源"):
        return {"action": "standard", "content": json.dumps(
            {"title": title, "customCreator": {"name": name}}, ensure_ascii=False)}

    def test_observed_wrappers_return_only_explicit_display_evidence(self):
        for title, kind in (("关联了提交 「fictional change」", "commit"),
                            ("关联了合并请求 「fictional request」", "merge-request")):
            with self.subTest(kind=kind):
                raw = self.event(title)
                self.assertEqual(self.parse(raw), {
                    "pattern": "association-display-notice", "association_kind": kind,
                    "evidence_scope": "teambition-display-notice",
                    "source_verified": False, "object_verified": False,
                    "needs_review": False, "title": title,
                    "source_creator_name": "虚构通知来源",
                    "parsed_payload": {"title": title, "customCreator": {"name": "虚构通知来源"}},
                })

    def test_multiline_quoted_title_is_preserved_without_normalization(self):
        for title in ("关联了提交 「fictional first\nfictional second」",
                      "关联了合并请求 「fictional first\r\nfictional second」"):
            with self.subTest(title=title):
                self.assertEqual(self.parse(self.event(title))["title"], title)

    def test_creator_display_name_never_authenticates_a_provider(self):
        parsed = self.parse(self.event(name="Fictional Git Provider"))
        self.assertEqual(parsed["source_creator_name"], "Fictional Git Provider")
        self.assertIs(parsed["source_verified"], False)
        self.assertIs(parsed["object_verified"], False)
        self.assertNotIn("provider", parsed)
        self.assertNotIn("commit_id", parsed)
        self.assertNotIn("resolved", parsed)

    def test_action_must_be_explicitly_standard(self):
        for action in (None, "comment", "standard ", "STANDARD", [], True):
            with self.subTest(action=action):
                raw = self.event()
                raw["action"] = action
                self.assertIsNone(self.parse(raw))
        raw = self.event()
        del raw["action"]
        self.assertIsNone(self.parse(raw))

    def test_non_object_events_and_non_string_content_are_rejected(self):
        for raw in (None, [], "standard", 3):
            with self.subTest(raw=raw):
                self.assertIsNone(self.parse(raw))
        for content in (None, {}, [], b"{}", 3, True):
            with self.subTest(content_type=type(content).__name__):
                self.assertIsNone(self.parse({"action": "standard", "content": content}))

    def test_title_wrapper_does_not_accept_legacy_or_broader_phrases(self):
        titles = ("关联了提交", "关联了提交: fictional change", "关联了提交 fictional change",
                  "关联了提交「fictional change」", "关联了提交  「fictional change」",
                  "关联了提交\t「fictional change」", "关联了提交 \"fictional change\"",
                  "关联了提交 『fictional change』", "未关联了提交 「fictional change」",
                  "取消关联了提交 「fictional change」", "fictional 关联了提交 「fictional change」",
                  "关联了提交失败 「fictional change」", "关联了合并请求: fictional request",
                  "关联了提交 「fictional change」 suffix", "关联了提交 「fictional change」\n")
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(self.parse(self.event(title)))

    def test_empty_quoted_title_is_not_a_supported_notice(self):
        for title in ("关联了提交 「」", "关联了提交 「  」", "关联了合并请求 「\r\n」"):
            with self.subTest(title=title):
                self.assertIsNone(self.parse(self.event(title)))

    def test_payload_and_creator_keys_must_match_the_observed_shape(self):
        base = json.loads(self.event()["content"])
        variants = ({}, {"title": base["title"]},
                    {"customCreator": base["customCreator"]},
                    dict(base, source="fictional source"),
                    dict(base, commit={"sha": "fictional-sha"}),
                    dict(base, payload={}), dict(base, source_verified=True),
                    dict(base, customCreator={}),
                    dict(base, customCreator={"name": "虚构通知来源", "id": "fictional-actor"}),
                    dict(base, customCreator={"name": "虚构通知来源", "verified": True}))
        for payload in variants:
            with self.subTest(payload=payload):
                self.assertIsNone(self.parse({"action": "standard", "content": json.dumps(payload)}))

    def test_nested_or_wrongly_typed_text_fields_are_rejected(self):
        for value in (None, True, 3, [], {"text": "fictional text"}):
            with self.subTest(value=value):
                self.assertIsNone(self.parse(self.event(title=value)))
                self.assertIsNone(self.parse(self.event(name=value)))
        for creator in (None, [], "fictional creator", 3):
            payload = {"title": "关联了提交 「fictional change」", "customCreator": creator}
            self.assertIsNone(self.parse({"action": "standard", "content": json.dumps(payload)}))

    def test_blank_creator_name_is_rejected(self):
        for name in ("", " ", "\r\n\t"):
            with self.subTest(name=name):
                self.assertIsNone(self.parse(self.event(name=name)))

    def test_duplicate_json_keys_are_rejected_at_each_observed_level(self):
        contents = (
            '{"title":"关联了提交 「fictional change」","title":"关联了提交 「fictional change」",'
            '"customCreator":{"name":"fictional source"}}',
            '{"title":"关联了提交 「fictional change」","customCreator":'
            '{"name":"fictional source","name":"fictional source"}}',
            '{"title":"关联了提交 「fictional change」","customCreator":{"name":"first"},'
            '"customCreator":{"name":"second"}}',
        )
        for content in contents:
            self.assertIsNone(self.parse({"action": "standard", "content": content}))

    def test_invalid_json_and_nonstandard_constants_are_rejected(self):
        for content in ("", "{", "[]", "null", "NaN", "Infinity", "-Infinity",
                        json.dumps(self.event()["content"]),
                        '{"title":"关联了提交 「fictional change」","customCreator":{"name":NaN}}'):
            with self.subTest(content=content):
                self.assertIsNone(self.parse({"action": "standard", "content": content}))

    def test_invalid_utf8_is_rejected_before_or_after_json_decoding(self):
        raw = self.event()
        raw["content"] += "\ud800"
        self.assertIsNone(self.parse(raw))
        for title, name in (("关联了提交 「fictional\ud800」", "fictional source"),
                            ("关联了提交 「fictional change」", "fictional\udfff")):
            content = json.dumps({"title": title, "customCreator": {"name": name}}, ensure_ascii=True)
            self.assertIsNone(self.parse({"action": "standard", "content": content}))

    def test_control_and_format_characters_are_rejected_in_local_evidence(self):
        for char in ("\x00", "\x1b", "\x7f", "\u0085", "\u200b", "\u202e", "\ufeff"):
            with self.subTest(codepoint=ord(char)):
                self.assertIsNone(self.parse(self.event(title="关联了提交 「fictional" + char + "」")))
                self.assertIsNone(self.parse(self.event(name="fictional" + char)))

    def test_only_lf_or_crlf_line_breaks_are_accepted_inside_the_title(self):
        for title in ("关联了提交 「fictional\rchange」", "关联了提交 「fictional\tchange」"):
            self.assertIsNone(self.parse(self.event(title)))
        for name in ("fictional\nsource", "fictional\r\nsource", "fictional\tsource"):
            self.assertIsNone(self.parse(self.event(name=name)))

    def test_unsupported_markup_is_rejected_without_stripping_or_decoding(self):
        for text in ("<b>fictional</b>", "<script", '{"text":"fictional"}', "<!--fictional-->"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(self.event(title="关联了提交 「" + text + "」")))
                self.assertIsNone(self.parse(self.event(name=text)))

    def test_known_outer_provenance_or_alternate_payload_cannot_override_the_notice(self):
        fields = ("provider", "Provider", "providerId", "source", "source_id", "source-id", "appId", "integration",
                  "integrationId", "repository", "repositoryId", "repo", "commit", "commitId",
                  "sha", "hash", "url", "html_url", "objectLink", "payload", "customCreator",
                  "title", "source_creator_name", "commitHash", "commitUrl", "mergeRequestUrl",
                  "pullRequestUrl", "repository_url", "source_verified", "object_verified", "association_kind",
                  "evidence_scope", "parsed_payload", "pattern", "needs_review")
        for field in fields:
            for value in (None, {}, "fictional metadata"):
                with self.subTest(field=field, value=value):
                    raw = self.event()
                    raw[field] = value
                    self.assertIsNone(self.parse(raw))

    def test_ordinary_activity_fields_do_not_authenticate_the_source(self):
        raw = self.event()
        raw.update(id="fictional-event", creatorId="fictional-creator", boundToObjectId="fictional-task",
                   boundToObjectType="task", createTime="2026-01-02T01:00:00Z",
                   updateTime="2026-01-02T01:00:00Z", sequence=2)
        parsed = self.parse(raw)
        self.assertEqual(parsed["association_kind"], "commit")
        self.assertIs(parsed["source_verified"], False)

    def test_content_budget_is_measured_in_utf8_bytes(self):
        raw = self.event()
        raw["content"] += " " * (65536 - len(raw["content"].encode("utf-8")))
        self.assertIsNotNone(self.parse(raw))
        raw["content"] += " "
        self.assertIsNone(self.parse(raw))
        self.assertIsNone(self.parse(self.event(title="关联了提交 「" + "虚" * 22000 + "」")))

    def test_excessively_nested_json_fails_closed(self):
        content = '{"title":' + "[" * 2000 + '"fictional"' + "]" * 2000 + ',"customCreator":{"name":"fictional"}}'
        self.assertIsNone(self.parse({"action": "standard", "content": content}))

    def test_parser_preserves_input_and_does_not_deduplicate_matching_titles(self):
        first = self.event()
        first["id"] = "fictional-first"
        second = dict(first, id="fictional-second")
        original = copy.deepcopy((first, second))
        self.assertIsNotNone(self.parse(first))
        self.assertIsNotNone(self.parse(second))
        self.assertEqual((first, second), original)


if __name__ == "__main__":
    unittest.main()
