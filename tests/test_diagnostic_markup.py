"""Fictional markup evidence must stay bounded and contain no source values."""

import json
import unittest

try:
    from upcam_app.diagnostic_markup import markup_shape
except ModuleNotFoundError as error:
    if error.name != "upcam_app.diagnostic_markup":
        raise
    markup_shape = None


class DiagnosticMarkupTests(unittest.TestCase):
    def shape(self, value, render_mode=None):
        self.assertIsNotNone(markup_shape, "The independent safe markup diagnostic is missing")
        return markup_shape(value, render_mode)

    def test_protocol_html_reports_public_tags_without_private_source_values(self):
        event = {
            "action": "comment", "boundToObjectType": "task",
            "content": json.dumps({
                "comment": '<p private-attribute="PRIVATE_ATTRIBUTE_VALUE">PRIVATE_BODY'
                           '<strong>PRIVATE_NAME</strong><br><BR>'
                           '<a href="https://private.example/PRIVATE_LINK">PRIVATE_LINK_TEXT</a>'
                           '<img src="PRIVATE_IMAGE" /></p>',
                "attachments": [], "dingFiles": [], "files": [], "mentions": {},
                "renderMode": "markdown", "isOnlyNotifyMentions": False,
            }),
        }
        payload = json.loads(event["content"])
        result = self.shape(payload["comment"], payload["renderMode"])
        self.assertEqual(result["tag_counts"], {"p": 2, "strong": 2, "br": 2, "a": 2, "img": 1})
        self.assertEqual(result["exact_br_count"], 1)
        self.assertEqual(result["angle_fragment_count"], 9)
        self.assertEqual(result["render_mode"], "markdown")
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["analysis_limited"])
        self.assertTrue(all(fragment["closed"] for fragment in result["fragments"]))
        self.assertTrue(all(fragment["context"] == "text-or-unknown" for fragment in result["fragments"]))
        self.assertFalse(result["rendering_verified"])
        self.assertFalse(result["commonmark_verified"])
        encoded = json.dumps(result)
        for private in ("private-attribute", "PRIVATE_ATTRIBUTE_VALUE", "PRIVATE_BODY", "PRIVATE_NAME",
                        "private.example", "PRIVATE_LINK", "PRIVATE_LINK_TEXT", "PRIVATE_IMAGE"):
            self.assertNotIn(private, encoded)

    def test_private_tag_names_attributes_and_text_are_counts_only(self):
        result = self.shape('<private-secret private_attr="PRIVATE_TOKEN">PRIVATE_TEXT</private-secret>')
        self.assertEqual(result["tag_counts"], {})
        self.assertEqual(result["other_angle_count"], 2)
        self.assertEqual([item["name_length"] for item in result["fragments"]], [14, 14])
        self.assertTrue(all(item["kind"] == "other_angle" for item in result["fragments"]))
        self.assertTrue(result["fragments"][1]["closing"])
        encoded = json.dumps(result)
        for private in ("private-secret", "private_attr", "PRIVATE_TOKEN", "PRIVATE_TEXT"):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.shape('<private-a secret-a="value-a">text-a</private-a>'),
                         self.shape('<private-b secret-b="value-b">text-b</private-b>'))

    def test_cpp_types_comparisons_and_links_do_not_establish_html_rendering(self):
        result = self.shape('a < b && c > d; std::vector<PrivateType>; '
                            '<https://private.example/PRIVATE_LINK>')
        self.assertEqual(result["angle_fragment_count"], 3)
        self.assertEqual(result["other_angle_count"], 3)
        self.assertEqual(result["tag_counts"], {})
        self.assertEqual(result["fragments"][1]["name_length"], 11)
        self.assertFalse(result["rendering_verified"])
        self.assertFalse(result["commonmark_verified"])
        encoded = json.dumps(result)
        for private in ("PrivateType", "private.example", "PRIVATE_LINK"):
            self.assertNotIn(private, encoded)

    def test_fenced_inline_and_plain_fragments_are_only_lexical_hints(self):
        result = self.shape('```cpp\nstd::vector<PrivateType> value;\n```\n`<span>` and <br>', "markdown")
        self.assertEqual([item["context"] for item in result["fragments"]],
                         ["fenced", "inline", "text-or-unknown"])
        self.assertEqual(result["code"]["backtick_count"], 8)
        self.assertEqual(result["code"]["fence_marker_count"], 2)
        self.assertEqual(result["code"]["fenced_block_count"], 1)
        self.assertEqual(result["code"]["inline_span_count"], 1)
        self.assertFalse(result["code"]["context_limited"])
        self.assertFalse(result["rendering_verified"])
        self.assertNotIn("PrivateType", json.dumps(result))

    def test_tilde_fences_crlf_and_matching_multiple_backticks_are_bounded_hints(self):
        values = [
            ("~~~private-language\n<p>\n~~~~\n", "fenced", 0, 2),
            ("```\r\n<p>\r\n```\r\n", "fenced", 6, 2),
            ("`` <p> ` PRIVATE_INLINE ``", "inline", 5, 0),
        ]
        for value, context, ticks, fences in values:
            with self.subTest(context=context, ticks=ticks):
                result = self.shape(value, "markdown")
                self.assertEqual(result["fragments"][0]["context"], context)
                self.assertEqual(result["code"]["backtick_count"], ticks)
                self.assertEqual(result["code"]["fence_marker_count"], fences)
                self.assertFalse(result["code"]["context_limited"])
                self.assertNotIn("private-language", json.dumps(result))
                self.assertNotIn("PRIVATE_INLINE", json.dumps(result))

    def test_unclosed_or_escaped_code_delimiters_are_explicitly_unknown(self):
        for value, flag in (("```\n<p>", "unclosed_fence"), ("`<p>", "unclosed_inline"),
                            ("`<p>``", "unclosed_inline")):
            with self.subTest(flag=flag, length=len(value)):
                result = self.shape(value, "markdown")
                self.assertTrue(result["code"][flag])
                self.assertTrue(result["code"]["context_limited"])
                self.assertTrue(result["analysis_limited"])
                self.assertEqual(result["fragments"][0]["context"], "unknown")
        escaped = self.shape(r"\`<p>\`", "markdown")
        self.assertTrue(escaped["code"]["context_limited"])
        self.assertEqual(escaped["code"]["inline_span_count"], 0)
        self.assertEqual(escaped["fragments"][0]["context"], "unknown")

    def test_complex_markdown_containers_and_raw_html_are_explicitly_unknown(self):
        values = ["> ```\n> <p>\n> ```", "    <p>", "\t<p>", "- `<p>`",
                  "```bad`language\n<p>\n```", "<script>`PRIVATE_SCRIPT`<p></script>"]
        for value in values:
            with self.subTest(length=len(value)):
                result = self.shape(value, "markdown")
                self.assertTrue(result["analysis_limited"])
                self.assertTrue(result["code"]["context_limited"])
                self.assertTrue(all(item["context"] == "unknown" for item in result["fragments"]))
                self.assertFalse(result["commonmark_verified"])

    def test_comments_declarations_processing_and_cdata_have_fixed_categories(self):
        result = self.shape('<!-- PRIVATE_NOTE <private-name> --> <!DOCTYPE private-doc> '
                            '<?PRIVATE_PI token="PRIVATE_PI_TOKEN"?> <![CDATA[PRIVATE_CDATA]]>')
        self.assertEqual([item["kind"] for item in result["fragments"]],
                         ["html_comment", "html_declaration", "processing_instruction", "cdata"])
        self.assertTrue(all(item["closed"] for item in result["fragments"]))
        self.assertEqual(result["tag_counts"], {})
        self.assertTrue(all(item["name_length"] == 0 for item in result["fragments"]))
        encoded = json.dumps(result)
        for private in ("PRIVATE_NOTE", "private-name", "private-doc", "PRIVATE_PI",
                        "PRIVATE_PI_TOKEN", "PRIVATE_CDATA", "DOCTYPE"):
            self.assertNotIn(private, encoded)

    def test_closure_ignores_greater_than_inside_quoted_attributes(self):
        value = '<a private_attr="PRIVATE_A>PRIVATE_B" other=\'PRIVATE_C>PRIVATE_D\'>PRIVATE_TEXT</a>'
        result = self.shape(value)
        self.assertEqual(result["angle_fragment_count"], 2)
        self.assertEqual(result["fragments"][0]["length"], 66)
        self.assertTrue(result["fragments"][0]["closed"])
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["analysis_limited"])
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_only_exact_lowercase_br_token_is_marked_exact(self):
        result = self.shape('<br><BR><br/><br /><br private_attr="PRIVATE_VALUE">')
        self.assertEqual(result["tag_counts"], {"br": 5})
        self.assertEqual(result["exact_br_count"], 1)
        self.assertEqual([item["exact_br"] for item in result["fragments"]],
                         [True, False, False, False, False])
        self.assertEqual([item["self_closing"] for item in result["fragments"]],
                         [False, False, True, True, False])

    def test_malformed_and_incomplete_fragments_never_claim_complete_markup(self):
        for value in ('<p', '<p private="PRIVATE_VALUE', '<!-- PRIVATE_NOTE >',
                      '<?PRIVATE_PI>', '<![CDATA[PRIVATE_CDATA>'):
            with self.subTest(length=len(value)):
                result = self.shape(value)
                self.assertFalse(result["fragments"][0]["closed"])
                self.assertTrue(result["analysis_limited"])
                self.assertEqual(result["fragments"][0]["context"], "unknown")
                self.assertFalse(result["rendering_verified"])
        mismatch = self.shape('<p><strong>PRIVATE_TEXT</p></strong>')
        self.assertTrue(mismatch["analysis_limited"])
        self.assertIn("lexical_nesting_mismatch", mismatch["limitations"])
        self.assertTrue(all(item["context"] == "unknown" for item in mismatch["fragments"]))

    def test_nested_angles_are_limited_instead_of_proving_html_or_cpp(self):
        result = self.shape('std::vector<std::pair<PRIVATE_LEFT, PRIVATE_RIGHT>>')
        self.assertEqual(result["angle_fragment_count"], 1)
        self.assertTrue(result["analysis_limited"])
        self.assertIn("nested_angles", result["limitations"])
        self.assertEqual(result["fragments"][0]["context"], "unknown")
        self.assertNotIn("PRIVATE_LEFT", json.dumps(result))
        self.assertNotIn("PRIVATE_RIGHT", json.dumps(result))

    def test_unsafe_control_categories_do_not_emit_codepoints_or_source(self):
        result = self.shape('<p>PRIVATE_TEXT\x00\u0085\u202e</p>')
        self.assertEqual(result["control_categories"],
                         ["ascii_control", "unicode_control", "unicode_format"])
        self.assertTrue(result["analysis_limited"])
        self.assertNotIn("PRIVATE_TEXT", json.dumps(result))
        self.assertNotIn("202e", json.dumps(result))
        safe = self.shape('PRIVATE_LINE\nPRIVATE_NEXT\tPRIVATE_TAB\r\n')
        self.assertEqual(safe["control_categories"], [])

    def test_surrogates_are_rejected_before_scanning_without_echoing_input(self):
        for value in ('<p>PRIVATE_BEFORE\ud800</p>', '\udfffPRIVATE_AFTER'):
            with self.subTest(length=len(value)):
                result = self.shape(value)
                self.assertEqual(result["utf8_check"], "invalid")
                self.assertEqual(result["fragments"], [])
                self.assertEqual(result["scanned_character_count"], 0)
                self.assertFalse(result["scan_complete"])
                self.assertTrue(result["analysis_limited"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_nonstring_values_and_objects_are_not_coerced_or_walked(self):
        class PrivateObject:
            def __str__(self):
                raise AssertionError("Never stringify a private input")

        class PrivateString(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("Never execute a subclass override")

        values = [(None, "null"), (True, "boolean"), (9, "integer"), (1.5, "number"),
                  ({"PRIVATE_KEY": "PRIVATE_VALUE"}, "object"), (["PRIVATE_TEXT"], "array"),
                  (b"PRIVATE_BYTES", "unsupported"), (PrivateObject(), "unsupported"),
                  (PrivateString("PRIVATE_SUBCLASS"), "unsupported")]
        for value, kind in values:
            with self.subTest(kind=kind):
                result = self.shape(value)
                self.assertEqual(result["input_type"], kind)
                self.assertEqual(result["utf8_check"], "not_string")
                self.assertEqual(result["fragments"], [])
                self.assertFalse(result["scan_complete"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_unknown_object_types_cannot_execute_metaclass_hash_hooks(self):
        class PrivateMeta(type):
            def __hash__(cls):
                raise AssertionError("Unknown types must not execute private hooks")

        class PrivateObject(metaclass=PrivateMeta):
            pass

        result = self.shape(PrivateObject())
        self.assertEqual(result["input_type"], "unsupported")
        self.assertEqual(result["fragments"], [])
        self.assertFalse(result["scan_complete"])

    def test_render_modes_are_bounded_fixed_categories_without_arbitrary_values(self):
        values = [(None, "absent"), ("", "empty"), ("text", "text"),
                  ("markdown", "markdown"), ("html", "html"),
                  ("PRIVATE_MODE", "other"), ({"PRIVATE_KEY": "PRIVATE_VALUE"}, "invalid"),
                  ("\ud800PRIVATE_MODE", "invalid"), ("PRIVATE_MODE" * 10000, "limited")]
        for value, kind in values:
            with self.subTest(kind=kind):
                result = self.shape("<br>", value)
                self.assertEqual(result["render_mode"], kind)
                self.assertFalse(result["rendering_verified"])
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_huge_character_input_is_not_encoded_or_scanned_wholesale(self):
        result = self.shape("PRIVATE_HUGE" * 100000)
        self.assertEqual(result["utf8_check"], "limited")
        self.assertFalse(result["input_byte_count_complete"])
        self.assertFalse(result["input_within_byte_limit"])
        self.assertEqual(result["scanned_character_count"], 0)
        self.assertEqual(result["fragments"], [])
        self.assertFalse(result["scan_complete"])
        self.assertIn("input_byte_limit", result["limitations"])
        self.assertLess(len(json.dumps(result)), 2000)

    def test_utf8_byte_budget_precedes_markup_scanning_for_multibyte_text(self):
        result = self.shape("虚" * 21846)  # 65538 UTF-8 bytes, fewer than 65536 characters.
        self.assertEqual(result["utf8_check"], "valid")
        self.assertEqual(result["input_byte_count"], 65538)
        self.assertTrue(result["input_byte_count_complete"])
        self.assertFalse(result["input_within_byte_limit"])
        self.assertEqual(result["scanned_character_count"], 0)
        self.assertFalse(result["scan_complete"])

    def test_scan_character_limit_is_inclusive_and_does_not_hide_truncation(self):
        within = self.shape("x" * 32764 + "<br>")
        self.assertTrue(within["scan_complete"])
        self.assertEqual(within["exact_br_count"], 1)
        result = self.shape("x" * 32764 + "<br>PRIVATE_TAIL")
        self.assertEqual(result["scanned_character_count"], 32768)
        self.assertEqual(result["exact_br_count"], 1)
        self.assertFalse(result["scan_complete"])
        self.assertTrue(result["analysis_limited"])
        self.assertIn("scan_character_limit", result["limitations"])
        self.assertNotIn("PRIVATE_TAIL", json.dumps(result))

    def test_fragment_budget_is_inclusive_and_counts_are_observed_prefix_only(self):
        within = self.shape("<br>" * 64)
        self.assertEqual(within["angle_fragment_count"], 64)
        self.assertTrue(within["scan_complete"])
        result = self.shape("<br>" * 65 + "PRIVATE_TAIL")
        self.assertEqual(len(result["fragments"]), 64)
        self.assertEqual(result["angle_fragment_count"], 64)
        self.assertEqual(result["exact_br_count"], 64)
        self.assertEqual(result["tag_counts"], {"br": 64})
        self.assertFalse(result["scan_complete"])
        self.assertIn("fragment_limit", result["limitations"])
        self.assertLess(len(json.dumps(result)), 25000)

    def test_code_and_fragment_coverage_are_distinct_when_fragment_budget_stops_early(self):
        result = self.shape("<br>" * 65 + "`PRIVATE_TAIL`")
        self.assertEqual(result["scanned_character_count"], 274)
        self.assertEqual(result["fragment_scanned_character_count"], 256)
        self.assertEqual(result["code"]["backtick_count"], 2)
        self.assertEqual(result["exact_br_count"], 64)
        self.assertFalse(result["scan_complete"])
        self.assertNotIn("PRIVATE_TAIL", json.dumps(result))

    def test_tag_nesting_and_nested_angle_depth_are_bounded_without_recursion(self):
        within = self.shape("<div>" * 32 + "</div>" * 32)
        self.assertTrue(within["scan_complete"])
        self.assertEqual(within["max_observed_depth"], 32)
        result = self.shape("<div>" * 33 + "</div>" * 33)
        self.assertFalse(result["scan_complete"])
        self.assertIn("tag_depth_limit", result["limitations"])
        self.assertLessEqual(result["max_observed_depth"], 32)
        self.assertLessEqual(len(result["fragments"]), 64)
        angles = self.shape("<" * 33 + "PRIVATE_TOKEN" + ">" * 33)
        self.assertFalse(angles["scan_complete"])
        self.assertIn("angle_depth_limit", angles["limitations"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(angles))

    def test_public_tag_name_prefixes_do_not_whitelist_private_identifiers(self):
        result = self.shape('<p-private>PRIVATE_TEXT</p-private><p@private.example>')
        self.assertEqual(result["tag_counts"], {})
        self.assertEqual(result["other_angle_count"], 3)
        self.assertNotIn("tag", result["fragments"][0])
        self.assertNotIn("tag", result["fragments"][2])
        self.assertNotIn("private.example", json.dumps(result))

    def test_empty_text_is_a_complete_empty_lexical_scan(self):
        result = self.shape("")
        self.assertEqual(result["input_length"], 0)
        self.assertEqual(result["input_byte_count"], 0)
        self.assertEqual(result["utf8_check"], "valid")
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["analysis_limited"])
        self.assertEqual(result["fragments"], [])


if __name__ == "__main__":
    unittest.main()
