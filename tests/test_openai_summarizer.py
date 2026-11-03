import json
import unittest

from openai_summarizer import OpenAIResponsesClient, SummaryError, build_request


class OpenAIRequestTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"task_key": "task-1", "title": "虚构任务标题",
                      "comment_bodies": ["虚构评论正文"]}]
        self.rules = ["只按来源总结"]

    def test_standard_tier_is_explicit_and_request_contains_only_minimal_fields(self):
        body = json.loads(build_request("gpt-6-luna", "max", self.rows, self.rules))
        self.assertEqual(body["service_tier"], "default")
        self.assertEqual(body["reasoning"], {"effort": "max"})
        self.assertFalse(body["store"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        user = json.loads(body["input"][1]["content"][0]["text"])
        self.assertEqual(set(user["tasks"][0]), {"task_key", "title", "comment_bodies"})
        self.assertNotIn("task-id", json.dumps(body))

    def test_fast_service_tier_is_opt_in_and_verified_for_gpt_6_luna(self):
        body = json.loads(build_request("gpt-6-luna", "max", self.rows, self.rules, "fast"))
        self.assertEqual(body["service_tier"], "fast")
        with self.assertRaises(SummaryError):
            build_request("new-unverified-model", "default", self.rows, self.rules, "fast")

    def test_input_identifiers_or_unexpected_payload_fields_are_rejected(self):
        row = dict(self.rows[0], task_id="private-id")
        with self.assertRaises(SummaryError):
            build_request("gpt-6-luna", "max", [row], self.rules)
        with self.assertRaises(SummaryError):
            build_request("gpt-6-luna", "max", [dict(self.rows[0], task_key="private-task-id")], self.rules)

    def test_empty_input_does_not_call_transport(self):
        client = OpenAIResponsesClient(transport=lambda *_: self.fail("transport must not be used"))
        self.assertEqual(client.summarize("synthetic-key", "gpt-6-luna", "max", [], self.rules), {})

    def test_response_coverage_is_checked_and_key_is_not_in_body(self):
        captured = {}
        payload = json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps({"items": [
                {"task_key": "task-1", "summary": "虚构摘要"}]}, ensure_ascii=False)}]}]}, ensure_ascii=False).encode()

        def transport(api_key, body, timeout):
            captured.update(api_key=api_key, body=body, timeout=timeout)
            return payload

        client = OpenAIResponsesClient(transport=transport)
        self.assertEqual(client.summarize("synthetic-key", "gpt-6-luna", "max", self.rows, self.rules),
                         {"task-1": "虚构摘要"})
        self.assertNotIn(b"synthetic-key", captured["body"])
        self.assertEqual(captured["timeout"], 90)

    def test_incomplete_refusal_and_malformed_responses_block(self):
        refusal = json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "synthetic"}]}]}).encode()
        incomplete = json.dumps({"status": "incomplete", "output": []}).encode()
        missing_coverage = json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"items":[]}'}]}]}).encode()
        for response in (refusal, incomplete, missing_coverage, b"not-json"):
            client = OpenAIResponsesClient(transport=lambda *_args, response=response: response)
            with self.assertRaises(SummaryError):
                client.summarize("synthetic-key", "gpt-6-luna", "max", self.rows, self.rules)


if __name__ == "__main__":
    unittest.main()
