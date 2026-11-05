"""Synthetic project content only; the detailed export never performs IO."""

import copy
import importlib.util
import json
import socket
import unittest
from unittest.mock import patch
from urllib.parse import quote, quote_plus


if importlib.util.find_spec("upcam_app.detailed_diagnostic"):
    from upcam_app import detailed_diagnostic as detailed
else:
    detailed = None


class DetailedDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(detailed, "The detailed diagnostic implementation is missing")

    def metadata(self):
        return {
            "id": "synthetic-diagnostic", "created_at_utc": "2026-01-02T02:00:00Z",
            "application_version": "synthetic-version", "ruleset_revision": "synthetic-rules",
            "capabilities": ["synthetic-capability"],
            "report_window": {"start": "2026-01-01T16:00:00Z",
                              "end_exclusive": "2026-01-02T16:00:00Z", "timezone": "Asia/Shanghai"},
        }

    def event(self, body=None, **extra):
        return {"id": "synthetic-event", "action": "future.synthetic.action",
                "boundToObjectId": "synthetic-task", "boundToObjectType": "task",
                "createTime": "2026-01-02T01:00:00Z",
                "content": json.dumps(body if body is not None else {"comment": "合成业务正文"},
                                      ensure_ascii=False), **extra}

    def collection(self, events=None):
        return {
            "tenant_id": "synthetic-tenant", "project_id": "synthetic-project",
            "include_archived": False, "pagination_complete": True,
            "batch_id": "synthetic-batch", "collected_at": "2026-01-02T02:00:00Z",
            "selection": {"mode": "v3-project-updated-half-open", "date": "2026-01-02",
                          "start": "2026-01-01T16:00:00Z", "end_exclusive": "2026-01-02T16:00:00Z"},
            "records": [{"task": {"id": "synthetic-task", "projectId": "synthetic-project",
                                   "content": "合成任务标题", "executorId": "synthetic-new-owner",
                                   "tfsId": "synthetic-new-status", "isDone": True,
                                   "isArchived": False, "updated": "2026-01-02T01:00:00Z",
                                   "tasklistId": "synthetic-list"},
                         "activities": events if events is not None else [self.event()]}],
        }

    def config(self):
        return {
            "tenant_id": "synthetic-tenant", "project_id": "synthetic-project",
            "include_archived": False, "api_contract_verified": True,
            "activity_fields": {"time": "createTime", "action": "action"},
            "view": {"verified": True, "evidence": "合成范围核验", "task_equals": {"tasklistId": "synthetic-list"}},
            "owner_names": {"synthetic-new-owner": "合成新执行人", "synthetic-old-owner": "合成旧执行人",
                            "synthetic-unused-owner": "未引用的合成姓名"},
            "status_names": {"synthetic-new-status": "合成已完成", "synthetic-old-status": "合成进行中",
                             "synthetic-unused-status": "未引用的合成状态"},
            "resolved_status_ids": ["synthetic-new-status"],
        }

    def build(self, events=None, *, collection=None, config=None, metadata=None, credentials=None):
        return detailed.build_detailed_snapshot(
            collection if collection is not None else self.collection(events),
            config if config is not None else self.config(),
            metadata if metadata is not None else self.metadata(),
            credentials=credentials if credentials is not None else {})

    def body(self, report, index=0):
        return json.loads(report["records"][0]["activities"][index]["content"])

    def test_preserves_unknown_action_original_body_references_and_business_fields(self):
        body = {"comment": "合成正文和姓名", "secretLevel": "business-high",
                "commit": "1234567890abcdef1234567890abcdef12345678",
                "files": ["0123456789abcdef01234567"], "futureField": {"original": [True, 3, None]}}
        event = self.event(body, payload={"fromAutomation": False},
                           hrefPreview=[{"href": "https://synthetic.invalid/file", "info": None}])
        report = self.build([event])
        self.assertEqual(report["format"], "upcam-teambition-detailed-diagnostic-v1")
        self.assertTrue(report["contains_project_content"])
        self.assertFalse(report["sendable"])
        self.assertTrue(report["data_altered"])
        self.assertEqual(self.body(report), body)
        exported = report["records"][0]["activities"][0]
        self.assertEqual(exported["action"], "future.synthetic.action")
        self.assertEqual(exported["payload"], {"fromAutomation": False})
        self.assertEqual(exported["hrefPreview"], event["hrefPreview"])
        self.assertFalse(report["credential_redaction"]["full_text_anonymized"])

    def test_scope_task_and_configuration_are_explicit_allowlists(self):
        source = self.collection()
        source["response"] = {"content": "MUST-NOT-EXPORT-RESPONSE"}
        source["headers"] = {"X-Other": "MUST-NOT-EXPORT-HEADER"}
        source["selection"]["extra"] = "MUST-NOT-EXPORT-SELECTION"
        source["records"][0]["task"]["description"] = "MUST-NOT-EXPORT-TASK-EXTRA"
        config = self.config()
        config["openai_api_key"] = "MUST-NOT-EXPORT-CONFIG"
        config["settings"] = {"other": "MUST-NOT-EXPORT-SETTINGS"}
        metadata = self.metadata()
        metadata["response"] = "MUST-NOT-EXPORT-METADATA"
        report = self.build(collection=source, config=config, metadata=metadata)
        self.assertEqual(report["scope"]["tenant_id"], "synthetic-tenant")
        self.assertEqual(report["records"][0]["task"]["content"], "合成任务标题")
        self.assertEqual(report["diagnostic"]["report_window"], self.metadata()["report_window"])
        self.assertNotIn("MUST-NOT-EXPORT", json.dumps(report, ensure_ascii=False))

    def test_task_creation_and_activity_creator_update_evidence_are_preserved(self):
        source = self.collection([self.event(creatorId="synthetic-creator", updateTime="2026-01-02T01:30:00Z")])
        source["records"][0]["task"]["created"] = "2025-12-31T01:00:00Z"
        report = self.build(collection=source)
        record = report["records"][0]
        self.assertEqual(record["task"]["created"], "2025-12-31T01:00:00Z")
        self.assertEqual(record["activities"][0]["creatorId"], "synthetic-creator")
        self.assertEqual(record["activities"][0]["updateTime"], "2026-01-02T01:30:00Z")

    def test_nested_credential_keys_use_nfkc_casefold_and_suffixes(self):
        body = {"secretLevel": "keep-business", "nested": {
            "AuThOrIzAtIoN": "synthetic-unconfigured-auth", "Set-Cookie": "synthetic-cookie",
            "ＡＰＩ＿ＫＥＹ": "synthetic-api", "service.access_token": "synthetic-access",
            "client-SECRET": "synthetic-client", "x-PRIVATE_key": "synthetic-private",
            "headers": {"Other": "synthetic-header"}, "credentials": {"other": "synthetic-cred"},
            "dingtalk_signing_secret": "synthetic-signing", "business": "keep-nested"}}
        report = self.build([self.event(body)])
        self.assertEqual(self.body(report), {"secretLevel": "keep-business", "nested": {"business": "keep-nested"}})
        self.assertGreater(report["credential_redaction"]["removed_fields"], 0)

    def test_known_credentials_are_removed_from_every_exported_string_and_key(self):
        secret = 'synthetic/a+b?=秘密"value'
        variants = [secret, quote(secret, safe=""), quote_plus(secret),
                    json.dumps(secret, ensure_ascii=True)[1:-1],
                    quote(quote(secret, safe=""), safe="")]
        event = self.event({"comment": " | ".join(variants), "prefix-" + secret: "private-key-name",
                            "quote": json.dumps({"content": "引用 " + secret})},
                           action="future." + secret, payload={"name": secret})
        source = self.collection([event])
        source["records"][0]["task"]["content"] += secret
        report = self.build(collection=source, credentials={"teambition_app_secret": secret})
        output = json.dumps(report, ensure_ascii=False)
        for value in variants:
            self.assertNotIn(value, output)
        self.assertNotIn("private-key-name", output)
        self.assertIsInstance(report["records"][0]["activities"][0]["content"], str)

    def test_dingtalk_webhook_embedded_token_is_a_known_secret(self):
        token = "synthetic-hook-token-09284"
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=" + token
        report = self.build([self.event({"comment": "body " + token, "url": webhook})],
                            credentials={"dingtalk_webhook": webhook})
        encoded = json.dumps(report)
        self.assertNotIn(token, encoded)
        self.assertNotIn("oapi.dingtalk.com", encoded)

    def test_multiple_percent_encoding_layers_cannot_hide_configured_values(self):
        secret = "synthetic/private?opaque=31337"
        encoded = secret
        for _ in range(3):
            encoded = quote(encoded, safe="")
        report = self.build([self.event({"comment": "prefix " + encoded})], credentials={"app_secret": secret})
        self.assertNotIn(encoded, json.dumps(report))
        self.assertNotIn(secret, json.dumps(report))
        for _ in range(4):
            encoded = quote(encoded, safe="")
        with self.assertRaises(detailed.DetailedDiagnosticError):
            self.build([self.event({"comment": encoded})], credentials={"app_secret": secret})

    def test_three_percent_layers_of_plus_secret_are_removed_without_any_auth_label(self):
        secret = "SYNTHETIC+KNOWN+SECRET"
        value = "prefix SYNTHETIC%25252BKNOWN%25252BSECRET"
        report = self.build([self.event(content=value)], credentials={"app_secret": secret})
        self.assertNotIn("SYNTHETIC", report["records"][0]["activities"][0]["content"])

    def test_quoted_or_space_separated_url_auth_values_leave_no_tail(self):
        values = ['https://synthetic.invalid/?access_token="SYNTHETIC-URL-SECRET"',
                  'https://synthetic.invalid/?access_token=SYNTHETIC FIRST-SECRET-TAIL',
                  'prefix https://synthetic.invalid/?api_key=FIRST-SECRET SECOND-SECRET-TAIL']
        for index, value in enumerate(values):
            with self.subTest(index=index):
                report = self.build([self.event(content=value)])
                exported = report["records"][0]["activities"][0]["content"]
                self.assertNotIn("SECRET", exported)

    def test_embedded_escaped_json_auth_labels_are_omitted(self):
        values = [r'prefix {\"password\":\"SYNTHETIC-PASSWORD\"}',
                  r'prefix {\\\"password\\\":\\\"SYNTHETIC-PASSWORD\\\"}',
                  r'prefix {\"\u0070assword\":\"SYNTHETIC-PASSWORD\"}']
        for index, value in enumerate(values):
            with self.subTest(index=index):
                report = self.build([self.event(content=value)])
                self.assertNotIn("SYNTHETIC-PASSWORD", json.dumps(report))

    def test_custom_auth_headers_are_removed_without_dropping_author(self):
        report = self.build([self.event({"X-Auth": "SYNTHETIC-XAUTH", "X-Custom-Auth": "SYNTHETIC-CUSTOM",
                                          "author": {"name": "合成作者"}, "secretLevel": "business"})])
        self.assertEqual(self.body(report), {"author": {"name": "合成作者"}, "secretLevel": "business"})

    def test_credential_keys_are_decoded_in_key_context_before_name_checks(self):
        body = {r'\u0070assword': "SYNTHETIC-ESCAPED-KEY-PASSWORD",
                r'api\u005fkey': "SYNTHETIC-ESCAPED-APIKEY",
                r'api\\u005fkey': "SYNTHETIC-DOUBLE-ESCAPED-APIKEY",
                r'api%255Cu005fkey': "SYNTHETIC-MIXED-ESCAPED-APIKEY",
                r'\u0061uthor': "Synthetic business author", "secretLevel": "business"}
        report = self.build([self.event(body)])
        self.assertEqual(self.body(report), {r'\u0061uthor': "Synthetic business author", "secretLevel": "business"})

    def test_recognizable_private_key_and_api_token_formats_are_removed(self):
        values = ["-----BEGIN RSA PRIVATE KEY-----\nfake-private-material\n-----END RSA PRIVATE KEY-----",
                  "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD", "github_pat_1234567890abcdefghijklmnopqrstuvwxyz",
                  "xoxb-123456789012-abcdefghijklmnopqrstuvwx", "sk_live_1234567890abcdefghijklmnopqrstuvwxyz",
                  "SEC0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"]
        for index, value in enumerate(values):
            with self.subTest(index=index):
                report = self.build([self.event({"comment": value})])
                self.assertNotIn(value, self.body(report)["comment"])

    def test_removes_url_userinfo_auth_query_and_authentication_formats(self):
        body = {"url": "https://fake-user:fake-password@synthetic.invalid/p?file=abc&access_token=fake-query&signature=fake-signature#title",
                "comment": "Bearer fakeBearer_123456789. Basic ZmFrZTpmYWtl api_key=fake-labelled-key",
                "another": "sk-proj-0123456789abcdefghijklmnopqrstuvwxyz",
                "webhook": "https://oapi.dingtalk.com/robot/send?access_token=fake-hook"}
        report = self.build([self.event(body)])
        exported = self.body(report)
        self.assertEqual(exported["url"], "https://synthetic.invalid/p?file=abc#title")
        encoded = json.dumps(report)
        for value in ("fake-user", "fake-password", "fake-query", "fake-signature", "fakeBearer",
                      "ZmFrZTpmYWtl", "fake-labelled-key", "sk-proj-0123", "fake-hook"):
            self.assertNotIn(value, encoded)

    def test_encoded_json_and_double_encoded_quotes_are_recursively_scrubbed(self):
        nested = json.dumps(json.dumps({"content": "保留引用", "AcCeSs_ToKeN": "fake-nested-token"}))
        encoded = quote(json.dumps({"password": "fake-percent-password", "business": "keep-encoded"}))
        report = self.build([self.event({"quote": nested, "future": encoded})])
        body = self.body(report)
        self.assertEqual(json.loads(json.loads(body["quote"])), {"content": "保留引用"})
        self.assertNotIn("fake-nested-token", json.dumps(report))
        self.assertNotIn("fake-percent-password", json.dumps(report))
        self.assertIn("keep-encoded", json.dumps(report))

    def test_only_window_and_unknown_time_bodies_are_included(self):
        times = ["2026-01-01T15:59:59Z", "2026-01-01T16:00:00Z", "2026-01-02T15:59:59Z",
                 "2026-01-02T16:00:00Z", "2026-01-02T01:00:00", "not-a-time", None]
        events = [self.event({"comment": "window-body-" + str(i)}, id="event-" + str(i), createTime=value)
                  for i, value in enumerate(times)]
        report = self.build(events)
        bodies = {json.loads(e["content"])["comment"] for e in report["records"][0]["activities"]}
        self.assertEqual(bodies, {"window-body-1", "window-body-2", "window-body-4", "window-body-5", "window-body-6"})
        self.assertEqual(report["counts"]["out_of_window_activities"], 2)
        self.assertEqual(report["counts"]["invalid_time_activities"], 3)
        self.assertNotIn("window-body-0", json.dumps(report))
        self.assertNotIn("window-body-3", json.dumps(report))

    def test_mapping_subset_contains_current_and_window_old_new_references(self):
        events = [self.event({"_oldExecutorId": "synthetic-old-owner", "_executorId": "synthetic-new-owner",
                              "oldTfsId": "synthetic-old-status", "tfsId": "synthetic-new-status"}),
                  self.event({"_oldExecutorId": "synthetic-unused-owner"}, id="outside",
                             createTime="2026-01-02T16:00:00Z")]
        report = self.build(events)
        runtime = report["runtime_config"]
        self.assertEqual(set(runtime["owner_names"]), {"synthetic-old-owner", "synthetic-new-owner"})
        self.assertEqual(set(runtime["status_names"]), {"synthetic-old-status", "synthetic-new-status"})
        self.assertEqual(runtime["resolved_status_ids"], ["synthetic-new-status"])
        self.assertNotIn("未引用", json.dumps(report, ensure_ascii=False))

    def test_preserves_resolved_mapping_validation_evidence_including_invalid_ids(self):
        config = self.config()
        config["resolved_status_ids"] = ["synthetic-new-status", "synthetic-unused-status",
                                         "synthetic-unmapped-status", "synthetic-unmapped-status"]
        report = self.build(config=config)
        runtime = report["runtime_config"]
        self.assertEqual(runtime["resolved_status_ids"], ["synthetic-new-status", "synthetic-unused-status",
                                                        "synthetic-unmapped-status", "synthetic-unmapped-status"])
        self.assertEqual(runtime["status_names"]["synthetic-unused-status"], "未引用的合成状态")
        self.assertNotIn("synthetic-unmapped-status", runtime["status_names"])

    def test_plain_non_json_text_and_business_identifiers_are_preserved(self):
        text = "literal {not-json} secretLevel=high 1234567890abcdef1234567890abcdef12345678"
        report = self.build([self.event(content=text, quote="普通非 JSON 引用", files=["0123456789abcdef01234567"])])
        event = report["records"][0]["activities"][0]
        self.assertEqual(event["content"], text)
        self.assertEqual(event["quote"], "普通非 JSON 引用")
        self.assertEqual(event["files"], ["0123456789abcdef01234567"])

    def test_business_percent_encoded_urls_keep_original_bytes(self):
        value = "https://synthetic.invalid/file%2Fname?q=some%20business%2Bvalue"
        report = self.build([self.event({"url": value, "comment": "普通业务说明"})])
        self.assertEqual(self.body(report)["url"], value)

    def test_authentication_scheme_with_unverifiable_suffix_drops_whole_string(self):
        for value in ("Authorization: Digest username=unknown secret words", "Bearer odd@private:value trailing-private",
                      "Basic opaque==:tail-private", '<token>xml-private</token>'):
            with self.subTest(syntax=value.split()[0]):
                report = self.build([self.event({"comment": value})])
                self.assertNotIn("private", self.body(report)["comment"])
                self.assertNotIn("unknown", self.body(report)["comment"])

    def test_unparsed_suffixed_credential_assignments_and_markup_are_omitted(self):
        values = ["myPassword=unconfigured-private-value", "service.authToken: unconfigured-private-value",
                  "<service_token>unconfigured-private-value</service_token>"]
        for index, value in enumerate(values):
            with self.subTest(index=index):
                report = self.build([self.event({"comment": value, "secretLevel": "public-business"})])
                self.assertNotIn("unconfigured-private-value", json.dumps(report))
                self.assertEqual(self.body(report)["secretLevel"], "public-business")

    def test_unknown_malformed_auth_structure_fails_with_value_free_error(self):
        with self.assertRaises(detailed.DetailedDiagnosticError) as caught:
            self.build([self.event(content='{"nested":{"X-Api-Key":"fake-private", broken')])
        self.assertEqual(str(caught.exception), "详细诊断无法安全生成，请重新运行只读诊断。")

    def test_source_task_and_event_count_limits_fail_before_expanding_content(self):
        source = self.collection()
        source["records"] *= 3
        with patch.object(detailed, "MAX_TASKS", 2):
            with self.assertRaises(detailed.DetailedDiagnosticError):
                self.build(collection=source)
        source = self.collection([self.event()] * 4)
        with patch.object(detailed, "MAX_ACTIVITIES", 3):
            with self.assertRaises(detailed.DetailedDiagnosticError):
                self.build(collection=source)

    def test_aggregate_unit_bytes_are_bounded_before_copying_large_payload(self):
        event = self.event(payload={"business": ["safe business text " * 30 for _ in range(10)]})
        with patch.object(detailed, "MAX_UNIT_BYTES", 2048):
            with self.assertRaises(detailed.DetailedDiagnosticError):
                self.build([event])

    def test_no_body_or_credential_path_is_copied_into_omission_metadata(self):
        events = [self.event({"comment": "UNIQUE-PRIVATE-BODY-" + "x" * 3000,
                               "some-private-api_key": "UNIQUE-PRIVATE-CREDENTIAL"}, id="e-" + str(i))
                  for i in range(10)]
        with patch.object(detailed, "MAX_DETAILED_BYTES", 9000):
            report = self.build(events)
        self.assertGreater(report["omissions"]["activities"], 0)
        stats = json.dumps({"counts": report["counts"], "omissions": report["omissions"],
                            "credential_redaction": report["credential_redaction"]})
        for value in ("UNIQUE-PRIVATE", "some-private", "comment", "e-1"):
            self.assertNotIn(value, stats)

    def test_unicode_escape_case_variants_of_configured_values_are_removed_in_plain_text(self):
        secret = "synthetic/密钥é"
        escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
        escaped = escaped.replace("\\u00e9", "\\u00E9").replace("\\u5bc6", "\\u5BC6")
        report = self.build([self.event(content="text " + escaped)], credentials={"app_secret": secret})
        self.assertNotIn(escaped, report["records"][0]["activities"][0]["content"])

    def test_unsupported_configured_credential_types_fail_closed(self):
        with self.assertRaises(detailed.DetailedDiagnosticError):
            self.build(credentials={"app_secret": 123456789})

    def test_duplicate_keys_non_finite_and_invalid_json_fail_without_input_echo(self):
        inputs = ['{"comment":"fake-private","comment":"other"}', '{"x":NaN}',
                  '{"x":1e999}', '{"authorization":"fake-private"',
                  json.dumps('{"token":"fake-private","token":"other"}')]
        for value in inputs:
            with self.subTest(value_index=inputs.index(value)):
                with self.assertRaises(detailed.DetailedDiagnosticError) as caught:
                    self.build([self.event(content=value)])
                self.assertNotIn("fake-private", str(caught.exception))
                self.assertEqual(str(caught.exception), "详细诊断无法安全生成，请重新运行只读诊断。")

    def test_deep_json_oversized_string_invalid_utf8_and_cycles_fail_closed(self):
        cycle = {}
        cycle["child"] = cycle
        inputs = ["[" * 80 + "0" + "]" * 80, "x" * (detailed.MAX_STRING_BYTES + 1), "\ud800", cycle]
        for index, value in enumerate(inputs):
            with self.subTest(index=index):
                with self.assertRaises(detailed.DetailedDiagnosticError):
                    self.build([self.event(content=value)])

    def test_window_is_required_valid_and_timezone_aware(self):
        for value in (None, {}, {"start": "2026-01-01T16:00:00", "end_exclusive": "2026-01-02T16:00:00Z"},
                      {"start": "2026-01-02T16:00:00Z", "end_exclusive": "2026-01-01T16:00:00Z"}):
            metadata = self.metadata()
            metadata["report_window"] = value
            with self.subTest(window_type=type(value).__name__):
                with self.assertRaises(detailed.DetailedDiagnosticError):
                    self.build(metadata=metadata)

    def test_total_budget_omits_complete_events_and_preserves_unknown_first(self):
        events = [self.event({"comment": "合成常规正文" * 300}, id="known-" + str(i), action="comment")
                  for i in range(20)]
        events.append(self.event({"futureField": "保留定位失败的实际动作"}, id="unknown-last"))
        with patch.object(detailed, "MAX_DETAILED_BYTES", 12 * 1024):
            report = self.build(events)
        encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        self.assertLessEqual(len(encoded), 12 * 1024)
        exported = report["records"][0]["activities"]
        self.assertIn("unknown-last", [event["id"] for event in exported])
        self.assertGreater(report["omissions"]["activities"], 0)
        self.assertEqual(len(exported) + report["omissions"]["activities"], 21)
        for event in exported:
            self.assertIsInstance(json.loads(event["content"]), dict)

    def test_builder_is_pure_and_never_uses_network_or_environment(self):
        source, config, metadata, credentials = self.collection(), self.config(), self.metadata(), {"app_secret": "fake-private-secret"}
        originals = copy.deepcopy((source, config, metadata, credentials))
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                patch("os.getenv", side_effect=AssertionError("environment forbidden")), \
                patch("builtins.open", side_effect=AssertionError("filesystem forbidden")):
            report = detailed.build_detailed_snapshot(source, config, metadata, credentials=credentials)
        self.assertEqual((source, config, metadata, credentials), originals)
        self.assertEqual(report, self.build(collection=source, config=config, metadata=metadata, credentials=credentials))


if __name__ == "__main__":
    unittest.main()
