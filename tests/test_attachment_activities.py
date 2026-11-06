"""Synthetic attachment references; no uploaded content or production identities."""

from copy import deepcopy
import json
import unittest

from attachment_activities import attachment_change


TASK = "t" * 24
FIELD = "f" * 24
FILE = "a" * 24
OTHER_FILE = "b" * 24
NAME = "虚构附件甲.pdf"
OTHER_NAME = "虚构附件乙.pdf"


def upload_work(file_id=FILE, name=NAME, *, field_id=FIELD, task_id=TASK):
    return {"_id": file_id, "fileName": name,
            "meta": {"boundToObjectType": "file", "payload": {
                "boundToObjectType": "customfield", "boundToObjectId": field_id,
                "scope": "task", "scopeId": task_id}}}


def upload_payload():
    return {"customfield": {"type": "work"}, "works": [upload_work()]}


def customfield_payload():
    return {"customfieldId": FIELD, "customfieldName": "虚构附件字段",
            "oldValue": [], "oldValueString": "", "value": [{
                "_id": FILE, "fileName": NAME, "fileSize": 12,
                "thumbnail": None, "thumbnailUrl": None,
                "fileCategory": "application", "title": NAME, "source": "awos",
                "meta": {"boundToObjectType": "file"}, "secretLevel": 0}],
            "valueString": NAME, "secretLevel": 0}


def event(action, payload):
    return {"id": "synthetic-attachment-event", "action": action,
            "content": json.dumps(payload, ensure_ascii=False),
            "boundToObjectId": TASK, "boundToObjectType": "task",
            "createTime": "2026-01-02T01:00:00Z"}


class AttachmentActivityTests(unittest.TestCase):
    def parse(self, raw, config=None):
        return attachment_change(raw, config)

    def test_upload_returns_references_without_progress_or_completion_claims(self):
        payload = upload_payload()
        raw = event("upload.customfield.work", payload)
        original = deepcopy(raw)
        parsed = self.parse(raw)
        self.assertEqual(parsed["pattern"], "customfield-file-reference")
        self.assertEqual(parsed["field_id"], FIELD)
        self.assertEqual(parsed["file_ids"], [FILE])
        self.assertEqual(parsed["file_names"], {FILE: NAME})
        self.assertEqual(parsed["parsed_payload"], payload)
        self.assertIs(parsed["completion_evidence"], False)
        self.assertIs(parsed["needs_review"], False)
        self.assertEqual(raw, original)
        self.assertNotIn("summary", parsed)
        self.assertNotIn("file_contents", parsed)

    def test_v2_retains_complete_opaque_metadata_for_local_audit(self):
        payload = customfield_payload()
        parsed = self.parse(event("customfield.create.v2", payload))
        self.assertEqual(parsed["field_id"], FIELD)
        self.assertEqual(parsed["file_ids"], [FILE])
        self.assertEqual(parsed["file_names"], {FILE: NAME})
        self.assertEqual(parsed["parsed_payload"], payload)
        self.assertIs(parsed["needs_review"], False)
        self.assertIs(parsed["completion_evidence"], False)

    def test_same_file_events_both_survive_with_one_reference_identity(self):
        raws = [event("upload.customfield.work", upload_payload()),
                event("customfield.create.v2", customfield_payload())]
        raws[1]["id"] = "synthetic-attachment-event-2"
        parsed = [self.parse(raw) for raw in raws]
        self.assertEqual(len(parsed), 2)
        self.assertEqual([item["file_ids"] for item in parsed], [[FILE], [FILE]])
        self.assertEqual([item["field_id"] for item in parsed], [FIELD, FIELD])
        self.assertEqual([item["file_names"] for item in parsed], [{FILE: NAME}, {FILE: NAME}])
        self.assertEqual([json.loads(raw["content"]) for raw in raws],
                         [item["parsed_payload"] for item in parsed])

    def test_upload_supports_multiple_distinct_references_in_one_field(self):
        payload = upload_payload()
        payload["works"].append(upload_work(OTHER_FILE, OTHER_NAME))
        parsed = self.parse(event("upload.customfield.work", payload))
        self.assertEqual(parsed["file_ids"], [FILE, OTHER_FILE])
        self.assertEqual(parsed["file_names"], {FILE: NAME, OTHER_FILE: OTHER_NAME})
        self.assertEqual(parsed["parsed_payload"], payload)

    def test_v2_multifile_string_semantics_are_unverified_and_rejected(self):
        for rendered in (NAME, NAME + "," + OTHER_NAME, [NAME, OTHER_NAME], ""):
            payload = customfield_payload()
            other = deepcopy(payload["value"][0])
            other.update(_id=OTHER_FILE, fileName=OTHER_NAME, title=OTHER_NAME)
            payload["value"].append(other)
            payload["valueString"] = rendered
            with self.subTest(rendered=type(rendered).__name__):
                self.assertIsNone(self.parse(event("customfield.create.v2", payload)))

    def test_duplicate_upload_file_id_is_rejected_even_with_identical_metadata(self):
        for name in (NAME, OTHER_NAME):
            payload = upload_payload()
            payload["works"].append(upload_work(FILE, name))
            with self.subTest(conflicting_name=name != NAME):
                self.assertIsNone(self.parse(event("upload.customfield.work", payload)))

    def test_upload_rejects_cross_task_or_cross_field_references(self):
        for changed in (upload_work(OTHER_FILE, OTHER_NAME, task_id="u" * 24),
                        upload_work(OTHER_FILE, OTHER_NAME, field_id="g" * 24)):
            payload = upload_payload()
            payload["works"].append(changed)
            self.assertIsNone(self.parse(event("upload.customfield.work", payload)))

    def test_upload_rejects_nested_binding_enum_changes(self):
        cases = []
        for path, replacement in ((["customfield", "type"], "text"),
                                  (["works", 0, "meta", "boundToObjectType"], "task"),
                                  (["works", 0, "meta", "payload", "boundToObjectType"], "task"),
                                  (["works", 0, "meta", "payload", "scope"], "project")):
            payload = upload_payload()
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            cases.append(payload)
        for payload in cases:
            self.assertIsNone(self.parse(event("upload.customfield.work", payload)))

    def test_outer_task_binding_missing_mismatched_or_path_is_rejected(self):
        for action, payload in (("upload.customfield.work", upload_payload()),
                                ("customfield.create.v2", customfield_payload())):
            for key, value in (("boundToObjectId", None), ("boundToObjectId", "../task"),
                               ("boundToObjectType", "project"), ("taskId", "u" * 24)):
                raw = event(action, payload)
                raw[key] = value
                with self.subTest(action=action, key=key):
                    self.assertIsNone(self.parse(raw))
            raw = event(action, payload)
            del raw["boundToObjectType"]
            self.assertIsNone(self.parse(raw))

    def test_upload_scope_must_equal_outer_task(self):
        raw = event("upload.customfield.work", upload_payload())
        raw["boundToObjectId"] = "u" * 24
        self.assertIsNone(self.parse(raw))

    def test_same_outer_task_id_alias_is_allowed(self):
        raw = event("upload.customfield.work", upload_payload())
        raw["taskId"] = TASK
        self.assertEqual(self.parse(raw)["file_ids"], [FILE])

    def test_ordinary_customfield_values_and_other_actions_remain_unknown(self):
        for action, payload in (("customfield.create.v2", {
                "customfieldId": FIELD, "customfieldName": "虚构文本字段",
                "oldValue": [], "oldValueString": "", "value": "普通文本",
                "valueString": "普通文本", "secretLevel": 0}),
                ("customfield.update.v2", customfield_payload()),
                ("upload.customfield.work.v2", upload_payload()),
                ("customfield.create.v2", upload_payload()),
                ("upload.customfield.work", customfield_payload())):
            with self.subTest(action=action):
                self.assertIsNone(self.parse(event(action, payload)))

    def test_unknown_payload_fields_at_every_level_are_rejected(self):
        shapes = (("upload.customfield.work", upload_payload,
                   ([], ["customfield"], ["works", 0], ["works", 0, "meta"],
                    ["works", 0, "meta", "payload"])),
                  ("customfield.create.v2", customfield_payload,
                   ([], ["value", 0], ["value", 0, "meta"])))
        for action, builder, paths in shapes:
            for path in paths:
                payload = builder()
                target = payload
                for key in path:
                    target = target[key]
                target["unverified"] = "opaque synthetic value"
                with self.subTest(action=action, depth=len(path)):
                    self.assertIsNone(self.parse(event(action, payload)))

    def test_outer_attachment_or_preview_side_channels_remain_unsupported(self):
        for action, payload in (("upload.customfield.work", upload_payload()),
                                ("customfield.create.v2", customfield_payload())):
            for key, value in (("payload", {}), ("attachments", []), ("files", []),
                               ("dingFiles", []), ("images", []), ("data", None),
                               ("hrefPreview", []), ("thumbnail", None),
                               ("thumbnailUrl", "https://example.invalid/preview"),
                               ("customfieldId", "g" * 24), ("field_id", "g" * 24),
                               ("fileName", OTHER_NAME), ("file_ids", [OTHER_FILE]),
                               ("works", []), ("value", []), ("parsed_payload", {})):
                raw = event(action, payload)
                raw[key] = value
                with self.subTest(action=action, key=key):
                    self.assertIsNone(self.parse(raw))

    def test_required_payload_fields_at_every_level_cannot_be_omitted(self):
        shapes = (("upload.customfield.work", upload_payload,
                   ([], ["customfield"], ["works", 0], ["works", 0, "meta"],
                    ["works", 0, "meta", "payload"])),
                  ("customfield.create.v2", customfield_payload,
                   ([], ["value", 0], ["value", 0, "meta"])))
        for action, builder, paths in shapes:
            for path in paths:
                original = builder()
                target = original
                for key in path:
                    target = target[key]
                for missing in target:
                    payload = deepcopy(original)
                    node = payload
                    for key in path:
                        node = node[key]
                    del node[missing]
                    with self.subTest(action=action, depth=len(path), missing=missing):
                        self.assertIsNone(self.parse(event(action, payload)))

    def test_wrong_upload_container_types_and_empty_references_are_rejected(self):
        for key, value in (("customfield", "work"), ("works", []), ("works", {}),
                           ("works", None), ("works", [None]), ("works", [FILE])):
            payload = upload_payload()
            payload[key] = value
            self.assertIsNone(self.parse(event("upload.customfield.work", payload)))

    def test_ids_require_exact_safe_24_character_file_and_field_tokens(self):
        for invalid in ("a" * 23, "a" * 25, "a" * 23 + "/", "a" * 23 + "\\",
                        "a" * 23 + "\n", "../" + "a" * 21, "甲" * 24, None, 123):
            for action, builder, path in (
                    ("upload.customfield.work", upload_payload, ["works", 0, "_id"]),
                    ("upload.customfield.work", upload_payload,
                     ["works", 0, "meta", "payload", "boundToObjectId"]),
                    ("customfield.create.v2", customfield_payload, ["customfieldId"]),
                    ("customfield.create.v2", customfield_payload, ["value", 0, "_id"])):
                payload = builder()
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid
                with self.subTest(action=action, depth=len(path), invalid_type=type(invalid).__name__):
                    self.assertIsNone(self.parse(event(action, payload)))

    def test_paths_controls_and_non_utf8_filenames_are_rejected(self):
        for invalid in ("", " ", "../fictional.pdf", "/fictional.pdf", r"C:\fictional.pdf",
                        "C:fictional.pdf", "fictional/notes.pdf", "fictional\\notes.pdf",
                        "fictional%2fnotes.pdf", "fictional%255cnotes.pdf", ".", "..",
                        " leading.pdf", "trailing.pdf ", "fictional\nnotes.pdf",
                        "fictional\x00notes.pdf", "fictional\u202enotes.pdf", "\ud800.pdf"):
            for action, payload in (("upload.customfield.work", upload_payload()),
                                    ("customfield.create.v2", customfield_payload())):
                if action == "upload.customfield.work":
                    payload["works"][0]["fileName"] = invalid
                else:
                    payload["value"][0].update(fileName=invalid, title=invalid)
                    payload["valueString"] = invalid
                raw = event(action, payload) if "\ud800" not in invalid else {
                    **event(action, {}), "content": json.dumps(payload)}
                with self.subTest(action=action, invalid_length=len(invalid)):
                    self.assertIsNone(self.parse(raw))

    def test_v2_file_fields_require_only_observed_types_and_values(self):
        invalid = {"fileSize": (-1, True, 1.5, "12", None),
                   "thumbnail": ("https://example.invalid/preview", [], {}),
                   "thumbnailUrl": ("https://example.invalid/preview", False),
                   "fileCategory": ("image", None), "source": ("other", None),
                   "secretLevel": (True, "0", 1.5, None),
                   "title": (OTHER_NAME, "", None), "meta": ({"boundToObjectType": "task"},)}
        for key, values in invalid.items():
            for value in values:
                payload = customfield_payload()
                payload["value"][0][key] = value
                with self.subTest(key=key, value_type=type(value).__name__):
                    self.assertIsNone(self.parse(event("customfield.create.v2", payload)))

    def test_v2_envelope_requires_observed_empty_previous_value_and_exact_filename(self):
        invalid = {"oldValue": ([FILE], None, {}), "oldValueString": (NAME, None),
                   "value": ([], {}, [FILE], [None]),
                   "valueString": (OTHER_NAME, "", None, [NAME]),
                   "secretLevel": (True, "0", None, 1.5),
                   "customfieldName": ("", "  ", "field\nname", "field\x00name", None)}
        for key, values in invalid.items():
            for value in values:
                payload = customfield_payload()
                payload[key] = value
                with self.subTest(key=key, value_type=type(value).__name__):
                    self.assertIsNone(self.parse(event("customfield.create.v2", payload)))

    def test_secret_level_metadata_is_opaque_integer_without_invented_range_or_equality(self):
        payload = customfield_payload()
        payload["secretLevel"] = -2
        payload["value"][0]["secretLevel"] = 9
        parsed = self.parse(event("customfield.create.v2", payload))
        self.assertEqual(parsed["parsed_payload"]["secretLevel"], -2)
        self.assertEqual(parsed["parsed_payload"]["value"][0]["secretLevel"], 9)

    def test_structured_content_is_not_accepted_as_an_already_parsed_object(self):
        raw = event("upload.customfield.work", upload_payload())
        for content in (upload_payload(), [], None, 123, b"{}", "null", "[]", '"{}"'):
            raw["content"] = content
            with self.subTest(content_type=type(content).__name__):
                self.assertIsNone(self.parse(raw))

    def test_duplicate_keys_nonstandard_numbers_trailing_data_and_deep_json_are_rejected(self):
        raw = event("upload.customfield.work", upload_payload())
        valid = raw["content"]
        cases = [valid.replace('"works":', '"works": [], "works":', 1),
                 valid.replace('"fileName":', '"fileName": "other", "fileName":', 1),
                 valid.replace('"type": "work"', '"type": NaN'),
                 valid.replace('"type": "work"', '"type": Infinity'),
                 valid + " {}", "{" * 1000, "[" * 1100 + "]" * 1100,
                 "{invalid", valid + "\ud800"]
        for content in cases:
            raw["content"] = content
            with self.subTest(length=len(content)):
                self.assertIsNone(self.parse(raw))

    def test_content_byte_budget_blocks_even_when_character_count_is_small(self):
        payload = customfield_payload()
        payload["customfieldName"] = "甲" * 22000
        raw = event("customfield.create.v2", payload)
        self.assertLess(len(raw["content"]), 65536)
        self.assertGreater(len(raw["content"].encode("utf-8")), 65536)
        self.assertIsNone(self.parse(raw))

    def test_malformed_outer_objects_and_actions_return_none(self):
        for raw in (None, [], "{}", {}, {"action": []}, {"action": {}}):
            with self.subTest(raw_type=type(raw).__name__):
                self.assertIsNone(self.parse(raw))

    def test_config_cannot_expand_supported_actions_or_file_shapes(self):
        raw = event("customfield.update.v2", customfield_payload())
        self.assertIsNone(self.parse(raw, {"attachment_actions": ["customfield.update.v2"]}))
        valid = event("customfield.create.v2", customfield_payload())
        self.assertEqual(self.parse(valid, {"ignored_synthetic_mapping": True})["file_ids"], [FILE])


if __name__ == "__main__":
    unittest.main()
