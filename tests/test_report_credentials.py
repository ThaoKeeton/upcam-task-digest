"""Synthetic-only report boundary checks; no credential store or network access."""

from copy import deepcopy
import html
import json
import unittest
from unittest.mock import patch
from urllib.parse import quote, quote_plus

from upcam_app.report_credentials import CredentialExposureBlocked, ReportCredentialGuard


class ReportCredentialGuardTests(unittest.TestCase):
    def setUp(self):
        self.token = "fictional-Guard/令牌+7&<>"
        self.guard = ReportCredentialGuard({"teambition_mcp_token": self.token})

    def test_nested_plain_credentials_in_keys_or_values_are_blocked(self):
        for value in (self.token, {self.token: "text"}, [{"nested": ("prefix " + self.token,)}]):
            with self.subTest(shape=type(value).__name__):
                with self.assertRaises(CredentialExposureBlocked) as raised:
                    self.guard.require_safe(value)
                self.assertNotIn(self.token, str(raised.exception))

    def test_url_json_html_and_mixed_encoded_full_credentials_are_blocked(self):
        encoded = [
            quote(self.token, safe=""), quote_plus(self.token),
            json.dumps(self.token, ensure_ascii=True)[1:-1],
            json.dumps(json.dumps(self.token, ensure_ascii=True)),
            "".join("\\u%04x" % ord(char) for char in self.token),
            html.escape(self.token),
            "".join("&#%d;" % ord(char) for char in self.token),
            quote(html.escape(json.dumps(self.token, ensure_ascii=True)), safe=""),
            "".join("%{:02x}".format(byte) for byte in self.token.encode()),
        ]
        for index, value in enumerate(encoded):
            with self.subTest(encoding=index):
                with self.assertRaises(CredentialExposureBlocked):
                    self.guard.require_safe("prefix " + value + " suffix")

    def test_decoded_query_and_url_credentials_are_also_independent_secrets(self):
        guard = ReportCredentialGuard({
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=fictional%2Brobot%2Fkey",
            "other": "https://fictional-user:fictional-password@example.invalid/",
        })
        for token in ("fictional+robot/key", "fictional-user", "fictional-password"):
            with self.subTest(component=token.split("-")[-1]):
                with self.assertRaises(CredentialExposureBlocked):
                    guard.require_safe(token)

    def test_nonsecret_evidence_is_left_exactly_unchanged(self):
        evidence = {"text": '虚构 <div>普通正文</div> "quoted" 进度 90% 😀',
                    "comment": json.dumps({"text": "合法 emoji 😀 和斜杠 /"}),
                    "number": [1, 0.5, False, None], "url": "https://example.invalid/a%20b"}
        before = deepcopy(evidence)
        self.assertIsNone(self.guard.require_safe(evidence))
        self.assertEqual(evidence, before)

    def test_non_bmp_credential_is_detected_in_decoded_json_surrogate_pairs(self):
        guard = ReportCredentialGuard({"token": "fictional-😀-credential"})
        with self.assertRaises(CredentialExposureBlocked):
            guard.require_safe(r"fictional-\uD83d\uDE00-credential")

    def test_unknown_objects_are_not_stringified(self):
        class MustNotInspect:
            def __str__(self):
                raise AssertionError("arbitrary string conversion must not occur")

            __repr__ = __str__

        with self.assertRaises(CredentialExposureBlocked):
            self.guard.require_safe({"nested": MustNotInspect()})

    def test_cycles_bad_keys_bytes_and_nonfinite_numbers_block(self):
        cycle = []
        cycle.append(cycle)
        for value in (cycle, {1: "not a JSON key"}, b"raw bytes", float("nan"), float("inf")):
            with self.assertRaises(CredentialExposureBlocked):
                self.guard.require_safe(value)

    def test_malformed_unicode_or_percent_encoding_blocks_without_echo(self):
        for value in ("\ud800", "%ff", r"escaped \ud800"):
            with self.assertRaises(CredentialExposureBlocked) as raised:
                self.guard.require_safe(value)
            self.assertNotIn(value, str(raised.exception))

    def test_inspection_limits_do_not_allow_partial_scans(self):
        limits_and_inputs = (
            ("MAX_BYTES", 5, ["plain", "more"]),
            ("MAX_STRING_BYTES", 5, "long text"),
            ("MAX_NODES", 2, [None, None]),
            ("MAX_DEPTH", 1, [[None]]),
            ("MAX_INSPECTION_BYTES", 3, "%41%41%41%41"),
            ("MAX_ENCODING_LAYERS", 1, "%252541"),
            ("MAX_INSPECTION_VIEWS", 1, "%41"),
        )
        for name, limit, evidence in limits_and_inputs:
            with self.subTest(limit=name), patch("upcam_app.report_credentials." + name, limit):
                with self.assertRaises(CredentialExposureBlocked):
                    self.guard.require_safe(evidence)

    def test_more_than_two_million_small_nodes_can_fit_collection_budget(self):
        # About 10 MiB of JSON, far below collection's 128 MiB retained budget.
        # An older two-million-node guard stopped valid large histories early.
        self.guard.require_safe([None] * 2_000_000)

    def test_decoded_inspection_views_do_not_consume_source_byte_budget(self):
        with patch("upcam_app.report_credentials.MAX_BYTES", 13):
            self.guard.require_safe("%41%41%41%41")

    def test_cached_text_is_still_charged_against_total_source_bytes(self):
        with patch("upcam_app.report_credentials.MAX_BYTES", 8):
            with self.assertRaises(CredentialExposureBlocked):
                self.guard.require_safe(["plain", "plain"])

    def test_full_or_disabled_cache_does_not_skip_later_credentials(self):
        for limit in (0, 1):
            with patch("upcam_app.report_credentials.MAX_CACHED_TEXTS", limit):
                with self.assertRaises(CredentialExposureBlocked):
                    self.guard.require_safe(["ordinary", "ordinary", self.token])

    def test_credential_pattern_failure_never_exposes_exception_or_value(self):
        with patch("upcam_app.report_credentials._known_pattern", side_effect=ValueError(self.token)):
            with self.assertRaises(CredentialExposureBlocked) as raised:
                ReportCredentialGuard({"token": self.token})
        self.assertNotIn(self.token, str(raised.exception))

    def test_malformed_credential_dictionary_fails_closed(self):
        for value in (None, [], {"key": None}, {"key": [self.token]}, {1: self.token}):
            with self.assertRaises(CredentialExposureBlocked):
                ReportCredentialGuard(value)


if __name__ == "__main__":
    unittest.main()
