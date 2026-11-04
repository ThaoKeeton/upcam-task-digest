"""Synthetic file references and unavailable previews; no live services or credentials."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from activity_adapter import MAX_CONTENT, comment_input
from automated_digest import (
    AutomatedDigestBlocked, build_automated_digest, diagnose_automatic_source,
    requires_openai_summary, validate_automated_report,
)
from delivery_guard import DeliveryBlocked, DeliveryGuard, initialize_ledger
from openai_summarizer import OpenAIResponsesClient
from report_contract import updated_selection
from teambition_readonly import CollectionError


FILES = ["a" * 24, "b" * 24]
HREF = "https://example.com/reference"
BODY = "虚构：请核对 " + HREF + " ，验证仍未完成。"


def attachment_payload():
    return {"comment": "", "attachments": [], "dingFiles": [], "files": list(FILES),
            "renderMode": "text", "isOnlyNotifyMentions": False}


def unavailable_preview():
    return [{"href": HREF, "type": "linkcard", "info": {
        "describe": None, "icon": None, "title": None,
        "extra": {"contentType": None, "errorMsg": "SYNTHETIC_UNAVAILABLE_ERROR"},
    }}]


class RecordingSummary:
    def __init__(self):
        self.calls = []

    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        self.calls.append(deepcopy(rows))
        return {row["task_key"]: "虚构：验证仍未完成。" for row in rows}


class CommentEvidenceTests(unittest.TestCase):
    def parse(self, payload, **outer):
        return comment_input({"content": json.dumps(payload, ensure_ascii=False), **outer},
                             "synthetic-comment", None)

    def fixture(self, payload, **outer):
        directory = Path(__file__).parent / "fixtures"
        collection = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        collection.update(selection=updated_selection("2026-01-02"),
                          batch_id="00000000-0000-4000-8000-000000000001",
                          collected_at="2026-01-03T01:00:00+00:00")
        config.update(api_contract_verified=True,
                      view={"verified": True, "evidence": "synthetic verification", "task_equals": {}})
        event = collection["records"][0]["activities"][0]
        event.update(content=json.dumps(payload, ensure_ascii=False), **outer)
        collection["records"][0]["activities"] = [event]
        return collection, config

    def build(self, collection, config, client):
        return build_automated_digest(collection, config, "2026-01-02", "synthetic-key",
                                      "gpt-6-luna", "max", client=client)

    def assert_blocked_before_model(self, collection, config):
        client = RecordingSummary()
        with self.assertRaises((AutomatedDigestBlocked, CollectionError, ValueError, TypeError)):
            self.build(collection, config, client)
        self.assertEqual(client.calls, [])

    def test_exact_attachment_comment_has_count_and_retains_raw_ids_locally(self):
        for mode in ("text", "markdown"):
            for notify in (False, True):
                payload = dict(attachment_payload(), renderMode=mode, isOnlyNotifyMentions=notify)
                with self.subTest(mode=mode, notify=notify):
                    parsed = self.parse(payload)
                    self.assertFalse(parsed["extraction_needs_review"])
                    self.assertTrue(parsed["attachment_only"])
                    self.assertEqual(parsed["file_count"], 2)
                    self.assertEqual(parsed["payload_evidence"]["files"], FILES)
                    self.assertEqual(parsed["parsed_payload"], payload)
                    self.assertEqual(json.loads(parsed["raw_content"]), payload)
                    self.assertIsNone(parsed["text"])
                    self.assertFalse(parsed["semantic_review_required"])

    def test_empty_body_requires_the_entire_evidenced_attachment_shape(self):
        cases = []
        for key in ("comment", "attachments", "dingFiles", "files", "renderMode", "isOnlyNotifyMentions"):
            payload = attachment_payload()
            payload.pop(key)
            cases.append(payload)
        for body in (None, False, 0, {}, [], " ", "\n", "<br>"):
            cases.append(dict(attachment_payload(), comment=body))
        for patch_values in ({"files": []}, {"renderMode": None}, {"renderMode": "html"},
                             {"isOnlyNotifyMentions": 1}, {"isOnlyNotifyMentions": None},
                             {"mentions": {"synthetic-person": "opaque"}}, {"quote": "引用正文"}):
            cases.append(dict(attachment_payload(), **patch_values))
        for payload in cases:
            with self.subTest(keys=list(payload)):
                parsed = self.parse(payload)
                self.assertTrue(parsed["extraction_needs_review"])
                collection, config = self.fixture(payload)
                self.assert_blocked_before_model(collection, config)

    def test_malformed_files_block_even_with_substantive_plain_body(self):
        for files in (None, "a" * 24, {}, True, [None], [True], [1], ["short"],
                      ["a" * 25], ["a " * 12], [{"id": FILES[0]}],
                      [FILES[0], {"id": FILES[1]}], [FILES[0], FILES[0]], ["a" * 23 + "\u202e"]):
            for body in ("", "虚构：验证仍未完成。"):
                with self.subTest(files_type=type(files).__name__, has_body=bool(body)):
                    payload = dict(attachment_payload(), comment=body, files=files)
                    self.assertTrue(self.parse(payload)["extraction_needs_review"])
                    self.assert_blocked_before_model(*self.fixture(payload))

    def test_populated_unsupported_attachment_data_and_images_block(self):
        for source in ("content", "event"):
            for key in ("attachments", "dingFiles", "data", "images"):
                for value in ([{"id": "synthetic-unverified"}], {"text": "SYNTHETIC_HIDDEN"}, "hidden"):
                    for body in ("", "虚构：验证仍未完成。"):
                        payload = dict(attachment_payload(), comment=body)
                        outer = {}
                        (payload if source == "content" else outer)[key] = value
                        with self.subTest(source=source, key=key, value_type=type(value).__name__, has_body=bool(body)):
                            self.assertTrue(self.parse(payload, **outer)["extraction_needs_review"])
                            self.assert_blocked_before_model(*self.fixture(payload, **outer))

    def test_mirrored_file_ids_are_counted_once_and_conflicts_block(self):
        payload = attachment_payload()
        parsed = self.parse(payload, files=list(FILES), attachments=[], dingFiles=[],
                            renderMode="text", isOnlyNotifyMentions=False)
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertEqual(parsed["file_count"], 2)
        self.assertEqual(parsed["extra_evidence"]["files"], FILES)
        for outer in ({"files": []}, {"files": ["c" * 24]}, {"files": [{"id": FILES[0]}]},
                      {"renderMode": "markdown"}, {"isOnlyNotifyMentions": True}):
            with self.subTest(outer_keys=list(outer)):
                self.assertTrue(self.parse(payload, **outer)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload, **outer))

    def test_attachment_only_report_needs_no_model_or_download_and_is_activity(self):
        collection, config = self.fixture(attachment_payload())
        client = RecordingSummary()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No downloads")):
            self.assertFalse(requires_openai_summary(collection, config, "2026-01-02"))
            self.assertTrue(diagnose_automatic_source(collection, config, "2026-01-02")["ok"])
            report = self.build(collection, config, client)
        self.assertEqual(client.calls, [])
        self.assertTrue(report["ready_to_send"])
        self.assertEqual(len(report["digest"]["tasks"]), 1)
        summary = report["digest"]["tasks"][0]["summary"]
        self.assertIn("评论 1 条", summary)
        self.assertIn("仅附件评论 1 条", summary)
        self.assertIn("评论附有 2 个文件", summary)
        self.assertIn("未读取文件内容", summary)
        self.assertNotIn("未发现任务动态", report["markdown"])
        self.assertNotIn("已完成", summary)
        self.assertNotIn("待复核", summary)
        for file_id in FILES:
            self.assertNotIn(file_id, report["markdown"])
        self.assertEqual(report["summary_inputs"][0]["semantic_summary"], "")
        validate_automated_report(report, collection, config, "gpt-6-luna", "max")

    def test_attachment_counts_accumulate_without_merging_comment_identity(self):
        collection, config = self.fixture(attachment_payload())
        second = deepcopy(collection["records"][0]["activities"][0])
        second.update(id="synthetic-second-comment", createTime="2026-01-02T01:00:00Z",
                      content=json.dumps(dict(attachment_payload(), files=["c" * 24])))
        collection["records"][0]["activities"].append(second)
        client = RecordingSummary()
        report = self.build(collection, config, client)
        summary = report["digest"]["tasks"][0]["summary"]
        self.assertIn("评论 2 条", summary)
        self.assertIn("仅附件评论 2 条", summary)
        self.assertIn("评论附有 3 个文件", summary)
        self.assertEqual(len(report["audit"][0]["event_ids"]), 2)
        self.assertEqual(client.calls, [])

    def test_same_file_referenced_by_multiple_comments_is_counted_once_per_task(self):
        collection, config = self.fixture(attachment_payload())
        second = deepcopy(collection["records"][0]["activities"][0])
        second.update(id="synthetic-repeated-reference", createTime="2026-01-02T01:00:00Z",
                      content=json.dumps(dict(attachment_payload(), files=[FILES[0]])))
        collection["records"][0]["activities"].append(second)
        client = RecordingSummary()
        report = self.build(collection, config, client)
        summary = report["digest"]["tasks"][0]["summary"]
        self.assertIn("仅附件评论 2 条", summary)
        self.assertIn("评论附有 2 个文件", summary)
        self.assertNotIn("评论附有 3 个文件", summary)
        self.assertEqual(len(report["audit"][0]["event_ids"]), 2)
        self.assertEqual(client.calls, [])

    def test_outer_images_and_data_allow_only_absent_null_or_empty_containers(self):
        for key in ("images", "data"):
            for value in (False, True, 0, 1, "", "hidden"):
                for payload in (attachment_payload(), {"comment": "虚构：验证仍未完成。"}):
                    with self.subTest(key=key, value_type=type(value).__name__, has_body=bool(payload["comment"])):
                        self.assertTrue(self.parse(payload, **{key: value})["extraction_needs_review"])
                        self.assert_blocked_before_model(*self.fixture(payload, **{key: value}))
            for value in (None, [], {}):
                with self.subTest(key=key, empty_type=type(value).__name__):
                    self.assertFalse(self.parse(attachment_payload(), **{key: value})["extraction_needs_review"])

    def test_fake_sender_runs_only_after_provenance_and_duplicate_guards(self):
        collection, config = self.fixture(attachment_payload())
        report = self.build(collection, config, RecordingSummary())
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "delivery.sqlite3"
            identity = initialize_ledger(ledger)
            guard = DeliveryGuard(ledger, identity, persistent_storage_verified=True)
            calls = []
            scope = {"tenant": "demo-org", "project": "demo-project", "destination": "synthetic",
                     "current_collection": collection, "current_config": config,
                     "current_model": "gpt-6-luna", "current_effort": "max"}
            changed = deepcopy(collection)
            changed["records"][0]["activities"][0]["content"] = json.dumps(
                dict(attachment_payload(), files=["c" * 24]))
            try:
                with self.assertRaises(DeliveryBlocked):
                    guard.attempt(report, **dict(scope, current_collection=changed),
                                  send=lambda body: calls.append(body) or True)
                self.assertEqual(calls, [])
                self.assertEqual(guard.attempt(report, **scope, send=lambda body: calls.append(body) or True),
                                 "confirmed")
                with self.assertRaises(DeliveryBlocked):
                    guard.attempt(report, **scope, send=lambda body: calls.append(body) or True)
                self.assertEqual(len(calls), 1)
            finally:
                guard.close()

    def test_exact_unavailable_preview_preserves_raw_and_only_body_needs_semantics(self):
        preview = unavailable_preview()
        payload = {"comment": BODY, "renderMode": "text", "hrefPreview": preview}
        parsed = self.parse(payload)
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertEqual(parsed["text"], BODY)
        self.assertEqual(parsed["parsed_payload"], payload)
        self.assertEqual(parsed["preview_evidence"], {"content": preview})
        self.assertTrue(parsed["semantic_review_required"])
        self.assertFalse(parsed["omit_from_summary"])
        collection, config = self.fixture(payload)
        self.assertTrue(diagnose_automatic_source(collection, config, "2026-01-02")["ok"])

    def test_matching_event_preview_is_local_and_conflicting_preview_blocks(self):
        preview = unavailable_preview()
        payload = {"comment": BODY, "hrefPreview": preview}
        parsed = self.parse(payload, hrefPreview=deepcopy(preview))
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertEqual(parsed["preview_evidence"], {"content": preview, "event": preview})
        changed = deepcopy(preview)
        changed[0]["info"]["extra"]["errorMsg"] = "DIFFERENT_SYNTHETIC_ERROR"
        self.assertTrue(self.parse(payload, hrefPreview=changed)["extraction_needs_review"])
        self.assert_blocked_before_model(*self.fixture(payload, hrefPreview=changed))

    def test_event_only_preview_is_checked_and_never_added_to_model_input(self):
        preview = unavailable_preview()
        payload = {"comment": BODY}
        parsed = self.parse(payload, hrefPreview=preview)
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertEqual(parsed["preview_evidence"], {"event": preview})
        client = RecordingSummary()
        report = self.build(*self.fixture(payload, hrefPreview=preview), client)
        self.assertEqual(client.calls, [[{"task_key": "task-1", "title": "虚构旧任务",
                                          "comment_bodies": [BODY]}]])
        self.assertNotIn("SYNTHETIC_UNAVAILABLE_ERROR", report["markdown"])
        for bad in ([], [None], {"href": HREF}):
            with self.subTest(preview_type=type(bad).__name__):
                self.assertTrue(self.parse(payload, hrefPreview=bad)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload, hrefPreview=bad))

    def test_body_and_strict_metadata_send_only_minimal_body_request(self):
        payload = dict(attachment_payload(), comment=BODY, hrefPreview=unavailable_preview())
        collection, config = self.fixture(payload)
        captured = []

        def transport(_key, body, _timeout):
            request = json.loads(body)
            captured.append(request)
            return json.dumps({"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps({"items": [
                    {"task_key": "task-1", "summary": "虚构：验证仍未完成。"}]}, ensure_ascii=False)}]}]}).encode()

        report = self.build(collection, config, OpenAIResponsesClient(transport=transport))
        self.assertEqual(len(captured), 1)
        user = json.loads(captured[0]["input"][1]["content"][0]["text"])
        self.assertEqual(user["tasks"], [{"task_key": "task-1", "title": "虚构旧任务",
                                           "comment_bodies": [BODY]}])
        encoded = json.dumps(user, ensure_ascii=False)
        for private in (*FILES, "SYNTHETIC_UNAVAILABLE_ERROR", "hrefPreview", "linkcard",
                        "demo-old-task", "demo-event-1", "demo-person"):
            self.assertNotIn(private, encoded)
        self.assertNotIn("SYNTHETIC_UNAVAILABLE_ERROR", report["markdown"])
        self.assertIn("评论附有 2 个文件", report["digest"]["tasks"][0]["summary"])
        validate_automated_report(report, collection, config, "gpt-6-luna", "max")

    def test_opaque_preview_errors_with_markup_or_json_stay_local_and_out_of_model_request(self):
        for error in ('Synthetic failure: {"status":503}', '<html>503 Service Unavailable</html>'):
            preview = unavailable_preview()
            preview[0]["info"]["extra"]["errorMsg"] = error
            payload = {"comment": BODY, "hrefPreview": preview}
            with self.subTest(error_format="html" if error.startswith("<") else "json"):
                parsed = self.parse(payload)
                self.assertFalse(parsed["extraction_needs_review"])
                self.assertEqual(parsed["preview_evidence"], {"content": preview})
                self.assertEqual(parsed["parsed_payload"], payload)
                self.assertEqual(json.loads(parsed["raw_content"]), payload)
                captured = []

                def transport(_key, body, _timeout):
                    captured.append(json.loads(body))
                    return json.dumps({"status": "completed", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps({"items": [
                            {"task_key": "task-1", "summary": "虚构：验证仍未完成。"}]}, ensure_ascii=False)}]}]}).encode()

                collection, config = self.fixture(payload)
                report = self.build(collection, config, OpenAIResponsesClient(transport=transport))
                self.assertEqual(len(captured), 1)
                user = json.loads(captured[0]["input"][1]["content"][0]["text"])
                self.assertEqual(user["tasks"], [{"task_key": "task-1", "title": "虚构旧任务",
                                                   "comment_bodies": [BODY]}])
                for message in captured[0]["input"]:
                    for item in message["content"]:
                        self.assertNotIn(error, item.get("text", ""))
                self.assertNotIn(error, report["markdown"])
                validate_automated_report(report, collection, config, "gpt-6-luna", "max")

    def test_unknown_populated_or_incomplete_previews_block_before_model(self):
        cases = [None, {}, [], [None], unavailable_preview() * 2]
        patches = [
            ((), "unknown", None), ((), "type", None), ((), "type", "short"),
            ((), "href", None), (("info",), "describe", "SYNTHETIC_DESCRIPTION"),
            (("info",), "icon", "https://example.com/icon"), (("info",), "title", "SYNTHETIC_TITLE"),
            (("info",), "unknown", None), (("info", "extra"), "contentType", "text/html"),
            (("info", "extra"), "errorMsg", None), (("info", "extra"), "errorMsg", {}),
            (("info", "extra"), "unknown", None), (("info", "extra"), "errorMsg", "bad\x00error"),
        ]
        for path, key, value in patches:
            preview = unavailable_preview()
            target = preview[0]
            for part in path:
                target = target[part]
            target[key] = value
            cases.append(preview)
        for path, key in (((), "href"), ((), "type"), ((), "info"),
                          (("info",), "title"), (("info",), "extra"),
                          (("info", "extra"), "contentType"), (("info", "extra"), "errorMsg")):
            preview = unavailable_preview()
            target = preview[0]
            for part in path:
                target = target[part]
            target.pop(key)
            cases.append(preview)
        for preview in cases:
            payload = {"comment": BODY, "hrefPreview": preview}
            with self.subTest(preview_type=type(preview).__name__):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))

    def test_preview_href_requires_safe_http_and_exact_body_url_binding(self):
        for href in ("javascript:alert(1)", "file:///tmp/private", "//example.com/reference",
                     "https://user:password@example.com/reference", "https://@example.com/reference",
                     "https://example.com\\reference", "https://example.com:invalid/reference",
                     "https://example.com:99999/reference", "https://example.com/%0aheader",
                     "https://example.com/\u202ereference", "https:///reference"):
            preview = unavailable_preview()
            preview[0]["href"] = href
            payload = {"comment": "虚构：核对 " + href + " ，尚未验证。", "hrefPreview": preview}
            with self.subTest(href=href):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))
        for body in ("虚构：没有提供链接。", HREF + "/different", HREF + "?different=1", HREF + "#different",
                     "https://example.com/?next=" + HREF):
            payload = {"comment": body, "quote": HREF, "hrefPreview": unavailable_preview()}
            with self.subTest(body=body):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))
        payload = {"comment": "[虚构参考](" + HREF + ")，验证仍未完成。",
                   "renderMode": "markdown", "hrefPreview": unavailable_preview()}
        self.assertFalse(self.parse(payload)["extraction_needs_review"])

    def test_parenthesized_url_suffix_is_not_an_exact_preview_url_match(self):
        for body in (HREF + "(different)", HREF + "?next=(different)",
                     "[虚构参考](" + HREF + "(different))"):
            payload = {"comment": body, "hrefPreview": unavailable_preview()}
            with self.subTest(body=body):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))

    def test_exact_br_is_normalized_for_preview_url_binding_with_raw_evidence_retained(self):
        body = "虚构：核对<br>" + HREF + "<br>验证仍未完成。"
        normalized_body = "虚构：核对\n" + HREF + "\n验证仍未完成。"
        expected_text = "虚构：核对 " + HREF + " 验证仍未完成。"
        for source in ("content", "event"):
            payload = {"comment": body, "renderMode": "markdown"}
            outer = {}
            (payload if source == "content" else outer)["hrefPreview"] = unavailable_preview()
            with self.subTest(preview_source=source):
                parsed = self.parse(payload, **outer)
                self.assertFalse(parsed["extraction_needs_review"])
                self.assertEqual(parsed["body"], body)
                self.assertEqual(parsed["parsed_payload"], payload)
                self.assertEqual(json.loads(parsed["raw_content"]), payload)
                self.assertEqual(parsed["normalized_body"], normalized_body)
                self.assertEqual(parsed["text"], expected_text)
                collection, config = self.fixture(payload, **outer)
                client = RecordingSummary()
                report = self.build(collection, config, client)
                self.assertEqual(client.calls, [[{"task_key": "task-1", "title": "虚构旧任务",
                                                  "comment_bodies": [expected_text]}]])
                validate_automated_report(report, collection, config, "gpt-6-luna", "max")
        for tag in ("<BR>", "<br/>", "<br >", '<br class="synthetic">', "<p>"):
            payload = {"comment": "虚构：核对" + tag + HREF, "hrefPreview": unavailable_preview()}
            with self.subTest(unsupported_tag=tag):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))
        payload = {"comment": "虚构：核对 https://example.com/re<br>ference",
                   "hrefPreview": unavailable_preview()}
        self.assertTrue(self.parse(payload)["extraction_needs_review"])
        self.assert_blocked_before_model(*self.fixture(payload))

    def test_rich_controls_and_oversize_evidence_still_block(self):
        for body in ("<p>" + BODY + "</p>", "<span>" + BODY, BODY + "\x00", BODY + "\u202e",
                     "<BR>" + BODY, "<br/>" + BODY):
            payload = {"comment": body, "hrefPreview": unavailable_preview()}
            with self.subTest(body_suffix=body[-8:]):
                self.assertTrue(self.parse(payload)["extraction_needs_review"])
                self.assert_blocked_before_model(*self.fixture(payload))
        huge = unavailable_preview()
        huge[0]["info"]["extra"]["errorMsg"] = "x" * MAX_CONTENT
        with self.assertRaises(CollectionError):
            self.parse({"comment": BODY, "hrefPreview": huge})
        self.assert_blocked_before_model(*self.fixture({"comment": BODY, "hrefPreview": huge}))
        duplicate = '{"comment":"' + BODY + '","files":[],"files":["' + FILES[0] + '"]}'
        self.assertTrue(comment_input({"content": duplicate}, "synthetic-comment", None)["extraction_needs_review"])

    def test_both_adaptations_retain_source_mapping_pagination_and_window_gates(self):
        for payload in (attachment_payload(), {"comment": BODY, "hrefPreview": unavailable_preview()}):
            collection, config = self.fixture(payload)
            variants = []
            for key, value in (("pagination_complete", False), ("project_id", "synthetic-other-project"),
                               ("selection", {"mode": "offline-fixture"}), ("include_archived", True)):
                changed = deepcopy(collection)
                changed[key] = value
                variants.append((changed, config))
            for field in ("owner_names", "status_names"):
                changed = deepcopy(config)
                changed[field] = {}
                variants.append((collection, changed))
            changed = deepcopy(config)
            changed["view"]["verified"] = False
            variants.append((collection, changed))
            changed = deepcopy(config)
            changed["api_contract_verified"] = False
            variants.append((collection, changed))
            for field, value in (("boundToObjectId", "synthetic-other-task"),
                                 ("createTime", "2026-01-02T01:00:00"), ("action", "unknown.action")):
                changed = deepcopy(collection)
                changed["records"][0]["activities"][0][field] = value
                variants.append((changed, config))
            changed = deepcopy(collection)
            changed["records"][0]["task"]["updated"] = "2026-01-04T01:00:00Z"
            variants.append((changed, config))
            for source, settings in variants:
                with self.subTest(payload_keys=list(payload)):
                    self.assert_blocked_before_model(source, settings)


if __name__ == "__main__":
    unittest.main()
