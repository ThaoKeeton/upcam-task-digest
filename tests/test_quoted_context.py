"""Fictional quote envelopes and source gates; no credentials or network."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from activity_adapter import comment_input
from automated_digest import AutomatedDigestBlocked, build_automated_digest, diagnose_automatic_source, validate_automated_report
from openai_summarizer import SummaryError, build_request
from report_contract import updated_selection
from upcam_app.diagnostic_export import safe_structure_export
from teambition_readonly import CollectionError

REFERENCE = "a" * 24
ROLE = "quoted-context-not-author-opinion"


class RecordingSummary:
    def __init__(self):
        self.calls = []

    def summarize(self, _key, _model, _effort, rows, _rules, service_tier="default"):
        self.calls.append(deepcopy(rows))
        return {row["task_key"]: "虚构：验证尚未结束。" for row in rows}


class QuotedContextTests(unittest.TestCase):
    def parse(self, raw_quote, **outer):
        return comment_input({"content": json.dumps({"comment": "当前作者：验证尚未结束。", "quote": raw_quote}),
                              **outer}, "synthetic-comment", None)

    def fixture(self, quote):
        directory = Path(__file__).parent / "fixtures"
        collection = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        collection["selection"] = updated_selection("2026-01-02")
        config.update(api_contract_verified=True, view={"verified": True, "evidence": "synthetic", "task_equals": {}})
        event = collection["records"][0]["activities"][0]
        event["content"] = json.dumps({"comment": "当前作者：验证尚未结束。", "quote": quote}, ensure_ascii=False)
        return collection, config

    def test_exact_single_layer_quote_preserves_identity_and_separates_author_text(self):
        raw = json.dumps({"_id": REFERENCE, "content": "历史引用：曾经完成初稿。"}, ensure_ascii=False)
        parsed = self.parse(raw)
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertEqual(parsed["text"], "当前作者：验证尚未结束。")
        self.assertEqual(parsed["quote_evidence"], {"content": raw})
        self.assertIn("quote_contexts", parsed)
        self.assertEqual(parsed["quote_contexts"]["content"], {
            "format": "id-content-json", "reference_id": REFERENCE,
            "text": "历史引用：曾经完成初稿。", "role": ROLE,
        })
        self.assertEqual(parsed["parsed_payload"]["quote"], raw)

    def test_plain_quote_compatibility_and_acknowledgement_is_not_suppressed(self):
        parsed = self.parse("历史引用：[文字](https://example.com/reference)")
        self.assertFalse(parsed["extraction_needs_review"])
        self.assertIn("quote_contexts", parsed)
        self.assertIsNone(parsed["quote_contexts"]["content"]["reference_id"])
        raw = {"content": json.dumps({"comment": "收到", "quote": json.dumps({"_id": REFERENCE, "content": "历史引用"})})}
        self.assertFalse(comment_input(raw, "synthetic", None)["omit_from_summary"])

    def test_bad_envelopes_are_not_decoded_stripped_or_partially_accepted(self):
        good = {"_id": REFERENCE, "content": "历史引用"}
        cases = [
            "{}", json.dumps({"content": "历史引用"}),
            json.dumps(dict(good, extra="unverified")), json.dumps(dict(good, _id="short")),
            json.dumps(dict(good, _id="bad id".ljust(24))), json.dumps(dict(good, content="")),
            json.dumps(dict(good, content={"text": "nested"})),
            json.dumps(dict(good, content='<br>unknown markup')), json.dumps(dict(good, content="bad\u202e")),
            json.dumps(dict(good, content="bad\x00")), json.dumps([good]), json.dumps(json.dumps(good)),
            json.dumps(json.dumps(json.dumps(dict(good, private_metadata="SYNTHETIC_PRIVATE")))),
            json.dumps(json.dumps(json.dumps(json.dumps(good)))),
            json.dumps(dict(good, content="\ud800")), '["malformed',
            '{"_id":"' + REFERENCE + '","content":"one","content":"two"}',
            '{"_id":"' + REFERENCE + '","content":NaN}',
            '{"_id":"' + REFERENCE + '","content":1e999}',
        ]
        for quote in cases:
            with self.subTest(quote_type=type(quote).__name__):
                parsed = self.parse(quote)
                self.assertTrue(parsed["extraction_needs_review"])
                self.assertEqual(parsed["quote_evidence"]["content"], quote)

    def test_conflicting_structured_quote_sources_block(self):
        inner = json.dumps({"_id": REFERENCE, "content": "历史引用一"})
        outer = json.dumps({"_id": "b" * 24, "content": "历史引用二"})
        self.assertTrue(self.parse(inner, quote=outer)["extraction_needs_review"])

    def test_summary_receives_only_role_labelled_context_without_reference_id(self):
        quote = json.dumps({"_id": REFERENCE, "content": "历史引用：曾经完成初稿。"}, ensure_ascii=False)
        collection, config = self.fixture(quote)
        self.assertTrue(diagnose_automatic_source(collection, config, "2026-01-02")["ok"])
        summary = RecordingSummary()
        report = build_automated_digest(collection, config, "2026-01-02", "synthetic-key", "gpt-6-luna", "max", client=summary)
        row = next(row for row in summary.calls[0] if "quoted_contexts" in row)
        self.assertEqual(row["comment_bodies"], ["当前作者：验证尚未结束。"])
        self.assertEqual(row["quoted_contexts"], [{"comment_index": 0, "role": ROLE, "text": "历史引用：曾经完成初稿。"}])
        encoded = json.dumps(summary.calls)
        for private in (REFERENCE, "demo-old-task", "demo-person", "demo-event-1"):
            self.assertNotIn(private, encoded)
        self.assertNotIn("历史引用：曾经完成初稿。", report["markdown"])
        validate_automated_report(report, collection, config, "gpt-6-luna", "max")

    def test_quote_adapter_does_not_bypass_standard_unknown_mapping_or_completeness(self):
        quote = json.dumps({"_id": REFERENCE, "content": "历史引用"})
        collection, config = self.fixture(quote)
        variants = []
        unknown = deepcopy(collection)
        unknown["records"][0]["activities"][0]["action"] = "synthetic.unknown"
        variants.append((unknown, config))
        standard = deepcopy(collection)
        standard["records"][0]["activities"][0].update(action="standard", content=json.dumps({"title": "关联了提交: synthetic", "customCreator": {"name": "synthetic"}}))
        variants.append((standard, config))
        missing = deepcopy(config); missing["status_names"].pop("demo-open")
        variants.append((collection, missing))
        incomplete = deepcopy(collection); incomplete["pagination_complete"] = False
        variants.append((incomplete, config))
        for source, settings in variants:
            with self.subTest():
                client = RecordingSummary()
                with self.assertRaises((AutomatedDigestBlocked, CollectionError, ValueError)):
                    build_automated_digest(source, settings, "2026-01-02", "synthetic-key", "gpt-6-luna", "max", client=client)
                self.assertEqual(client.calls, [])

    def test_request_validates_context_role_index_and_no_raw_identity(self):
        row = {"task_key": "task-1", "title": "虚构任务", "comment_bodies": ["仍待验证"],
               "quoted_contexts": [{"comment_index": 0, "role": ROLE, "text": "历史引用"}]}
        try:
            body = json.loads(build_request("gpt-6-luna", "max", [row], ["只按来源总结"]))
        except SummaryError:
            self.fail("The verified role-labelled quote context must pass request validation")
        prompt = body["input"][0]["content"][0]["text"]
        self.assertIn("quoted_contexts", prompt)
        for patch in ({"reference_id": REFERENCE}, {"comment_index": True}, {"comment_index": 1},
                      {"role": "author-opinion"}, {"text": "<b>rich</b>"}):
            altered = deepcopy(row); altered["quoted_contexts"][0].update(patch)
            with self.subTest(patch_keys=list(patch)), self.assertRaises(SummaryError):
                build_request("gpt-6-luna", "max", [altered], ["只按来源总结"])

    def test_safe_export_acceptance_check_never_exposes_quote_identity_or_body(self):
        collection, config = self.fixture(json.dumps({"_id": REFERENCE, "content": "SYNTHETIC_PRIVATE_QUOTE"}))
        report = safe_structure_export(collection, config, "2026-01-02")
        comment = report["tasks"][0]["activities"][0]["comment_check"]
        self.assertFalse(comment["needs_review"])
        self.assertTrue(comment["quote_checks"]["content"]["adapter_supported"])
        encoded = json.dumps(report)
        self.assertNotIn(REFERENCE, encoded)
        self.assertNotIn("SYNTHETIC_PRIVATE_QUOTE", encoded)
