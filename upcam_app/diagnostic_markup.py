"""Bounded, value-free lexical hints about angle brackets and code delimiters.

This is neither an HTML/CommonMark parser nor a renderer.  A public tag spelling,
balanced delimiters, or a renderMode value proves nothing about rendered content
or an author's meaning.  Consumers must retain their existing review gates.

Only literal built-in strings are inspected.  UTF-8 validation precedes scanning:
inputs over 65536 characters are rejected without encoding, and the remaining
bounded encoding must fit 65536 bytes.  At most 32768 characters, 64 fragments,
and 32 lexical nesting levels are examined.  Counts describe observed prefixes
when scan_complete is false.  scanned_character_count covers code/control checks;
fragment_scanned_character_count can be smaller when the fragment/depth budget
stops angle scanning.  Unknown names, attributes, text and URLs are never returned;
name_length and length count Unicode characters, not encoded bytes.

Code contexts recognize only simple fences and inline backtick pairs within a
line.  Containers, indentation, escapes, raw HTML text, unmatched delimiters and
other ambiguities produce explicit unknown contexts and fixed limitation codes.
Even an unlimited scan is only a lexical observation, never syntax validation.
"""

from __future__ import annotations

import re
import unicodedata


_INPUT_BYTES = 65536
_SCAN_CHARACTERS = 32768
_MAX_FRAGMENTS = 64
_MAX_DEPTH = 32
_MODE_BYTES = 64
_PUBLIC_TAGS = frozenset({
    "a", "abbr", "area", "b", "base", "blockquote", "br", "code", "col", "dd",
    "del", "details", "div", "dl", "dt", "em", "embed", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "input", "li", "link", "meta", "ol", "p",
    "param", "pre", "s", "script", "source", "span", "strong", "style", "sub",
    "summary", "sup", "table", "tbody", "td", "textarea", "th", "thead", "tr",
    "track", "u", "ul", "wbr",
})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_RAW_TEXT_TAGS = frozenset({"pre", "script", "style", "textarea"})
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9:_-]*\Z")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)\Z")
_CONTAINER = re.compile(r"^(?: {4}|\t| {0,3}(?:>|(?:[-+*]|[0-9]{1,9}[.)])[ \t]))")


def _type_name(value):
    # Identity checks also avoid invoking hooks on arbitrary metaclasses.
    for kind, label in ((type(None), "null"), (bool, "boolean"), (int, "integer"),
                        (float, "number"), (str, "string"), (dict, "object"), (list, "array")):
        if type(value) is kind:
            return label
    return "unsupported"


def _mode_category(value):
    if value is None:
        return "absent"
    if type(value) is not str:
        return "invalid"
    if len(value) > _MODE_BYTES:
        return "limited"
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return "invalid"
    if len(encoded) > _MODE_BYTES:
        return "limited"
    if not value:
        return "empty"
    return value if value in ("text", "markdown", "html") else "other"


def _empty_code():
    return {"backtick_count": 0, "fence_marker_count": 0, "fenced_block_count": 0,
            "inline_span_count": 0, "unclosed_fence": False, "unclosed_inline": False,
            "context_limited": False}


def _code_contexts(text):
    """Recognize a deliberately small lexical subset in linear bounded work."""
    contexts = ["text-or-unknown"] * len(text)
    code, limitations = _empty_code(), set()
    code["backtick_count"] = text.count("`")
    fence_char, fence_length, offset = "", 0, 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        candidate = _FENCE.fullmatch(body)
        if fence_char:
            contexts[offset:offset + len(line)] = ["fenced"] * len(line)
            if (candidate and candidate[1][0] == fence_char
                    and len(candidate[1]) >= fence_length and not candidate[2].strip(" \t")):
                code["fence_marker_count"] += 1
                fence_char, fence_length = "", 0
            offset += len(line)
            continue
        if _CONTAINER.match(body):
            limitations.add("complex_markdown_container")
        if candidate:
            if candidate[1][0] == "`" and "`" in candidate[2]:
                limitations.add("noncanonical_fence")
            else:
                fence_char, fence_length = candidate[1][0], len(candidate[1])
                code["fence_marker_count"] += 1
                code["fenced_block_count"] += 1
            offset += len(line)
            continue
        cursor = 0
        while cursor < len(body):
            if body[cursor:cursor + 2] == "\\`":
                limitations.add("escaped_backtick")
                cursor += 2
                continue
            if body[cursor] != "`":
                cursor += 1
                continue
            opening_end = cursor + 1
            while opening_end < len(body) and body[opening_end] == "`":
                opening_end += 1
            run_length, search, closing_end = opening_end - cursor, opening_end, -1
            while search < len(body):
                found = body.find("`", search)
                if found < 0:
                    break
                end = found + 1
                while end < len(body) and body[end] == "`":
                    end += 1
                if end - found == run_length:
                    closing_end = end
                    break
                search = end
            if closing_end < 0:
                code["unclosed_inline"] = True
                limitations.add("unclosed_inline")
                break
            contexts[offset + cursor:offset + closing_end] = ["inline"] * (closing_end - cursor)
            code["inline_span_count"] += 1
            cursor = closing_end
        offset += len(line)
    if fence_char:
        code["unclosed_fence"] = True
        limitations.add("unclosed_fence")
    if "\u2028" in text or "\u2029" in text:
        limitations.add("nonstandard_line_separator")
    code["context_limited"] = bool(limitations)
    return contexts, code, limitations


def _fragment_end(text, start):
    """Return a delimiter boundary, never claim HTML or XML validity."""
    if text.startswith("<!--", start):
        end = text.find("-->", start + 4)
        return (len(text) if end < 0 else end + 3), end >= 0, "html_comment", 1, set()
    if text.startswith("<![CDATA[", start):
        end = text.find("]]>", start + 9)
        return (len(text) if end < 0 else end + 3), end >= 0, "cdata", 1, set()
    if text.startswith("<?", start):
        end = text.find("?>", start + 2)
        return (len(text) if end < 0 else end + 2), end >= 0, "processing_instruction", 1, set()
    kind = "html_declaration" if text.startswith("<!", start) else "other_angle"
    quote, depth, maximum = "", 1, 1
    limitations = set()
    for cursor in range(start + 1, len(text)):
        char = text[cursor]
        if quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "<":
            limitations.add("nested_angles")
            if depth >= _MAX_DEPTH:
                limitations.add("angle_depth_limit")
                return cursor + 1, False, kind, maximum, limitations
            depth += 1
            maximum = max(maximum, depth)
        elif char == ">":
            depth -= 1
            if depth == 0:
                return cursor + 1, True, kind, maximum, limitations
        elif kind == "html_declaration" and char == "[":
            limitations.add("complex_declaration")
    return len(text), False, kind, maximum, limitations


def _tag_header(text, start, end):
    closing = text.startswith("</", start)
    beginning = start + (2 if closing else 1)
    cursor = beginning
    while cursor < end and text[cursor] not in " \t\r\n/<>=\"'":
        cursor += 1
    name = text[beginning:cursor]
    return name.lower(), len(name), closing, _NAME.fullmatch(name) is not None


def markup_shape(value, render_mode=None) -> dict:
    """Return only fixed categories, public tag labels, counts and booleans.

    render_mode is opaque protocol metadata reduced to a fixed category.  It
    never selects a renderer or grants trust.  scan_complete describes coverage,
    not valid markup; analysis_limited describes additional lexical ambiguity.
    """
    result = {
        "format": "upcam-safe-markup-shape-v1", "lexical_only": True,
        "rendering_verified": False, "commonmark_verified": False,
        "input_type": _type_name(value), "input_length": len(value) if type(value) is str else 0,
        "utf8_check": "not_string", "input_byte_count": 0,
        "input_byte_count_complete": False, "input_within_byte_limit": False,
        "scanned_character_count": 0, "fragment_scanned_character_count": 0,
        "scan_complete": False, "analysis_limited": False,
        "limitations": [], "render_mode": _mode_category(render_mode),
        "angle_fragment_count": 0, "exact_br_count": 0, "other_angle_count": 0,
        "max_observed_depth": 0, "tag_counts": {}, "fragments": [],
        "code": _empty_code(), "control_categories": [],
    }
    limitations = set()
    if result["render_mode"] in ("invalid", "limited"):
        limitations.add("invalid_render_mode" if result["render_mode"] == "invalid" else "render_mode_limit")
    if type(value) is not str:
        limitations.add("non_string_input")
    elif len(value) > _INPUT_BYTES:
        result["utf8_check"] = "limited"
        limitations.add("input_byte_limit")
    else:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            result["utf8_check"] = "invalid"
            limitations.add("invalid_utf8")
        else:
            result["utf8_check"] = "valid"
            result["input_byte_count"] = len(encoded)
            result["input_byte_count_complete"] = True
            result["input_within_byte_limit"] = len(encoded) <= _INPUT_BYTES
            if len(encoded) > _INPUT_BYTES:
                limitations.add("input_byte_limit")
    if not result["input_within_byte_limit"]:
        result["analysis_limited"] = True
        result["limitations"] = sorted(limitations)
        result["code"]["context_limited"] = True
        return result

    text = value[:_SCAN_CHARACTERS]
    result["scanned_character_count"] = len(text)
    result["scan_complete"] = len(text) == len(value)
    if not result["scan_complete"]:
        limitations.add("scan_character_limit")
    contexts, result["code"], code_limits = _code_contexts(text)
    limitations.update(code_limits)
    controls = set()
    for char in text:
        if char in "\r\n\t":
            continue
        category = unicodedata.category(char)
        if category == "Cc":
            controls.add("ascii_control" if ord(char) < 128 else "unicode_control")
        elif category == "Cf":
            controls.add("unicode_format")
    result["control_categories"] = sorted(controls)
    if controls:
        limitations.add("unsafe_controls")

    stack, cursor = [], 0
    while cursor < len(text):
        start = text.find("<", cursor)
        if start < 0:
            cursor = len(text)
            break
        if len(result["fragments"]) >= _MAX_FRAGMENTS:
            limitations.add("fragment_limit")
            result["scan_complete"] = False
            cursor = start
            break
        end, closed, kind, angle_depth, fragment_limits = _fragment_end(text, start)
        limitations.update(fragment_limits)
        result["max_observed_depth"] = max(result["max_observed_depth"], angle_depth)
        context = contexts[start]
        if any(item != context for item in contexts[start:end]):
            limitations.add("mixed_lexical_context")
            context = "unknown"
        fragment = {"kind": kind, "name_length": 0, "length": end - start,
                    "closed": closed, "closing": False, "self_closing": False,
                    "exact_br": closed and text[start:end] == "<br>", "context": context}
        if kind == "other_angle":
            name, name_length, closing, tag_like = _tag_header(text, start, end)
            fragment["name_length"] = name_length
            fragment["closing"] = closing
            fragment["self_closing"] = closed and text[max(start, end - 2):end] == "/>"
            if tag_like and name in _PUBLIC_TAGS:
                fragment["kind"] = "allowlisted_tag_like"
                fragment["tag"] = name
                result["tag_counts"][name] = result["tag_counts"].get(name, 0) + 1
            else:
                result["other_angle_count"] += 1
            if context not in ("fenced", "inline"):
                if tag_like and name in _RAW_TEXT_TAGS:
                    limitations.add("raw_html_text")
                if closed and tag_like and not fragment_limits:
                    if closing:
                        if stack and stack[-1] == name:
                            stack.pop()
                        else:
                            limitations.add("lexical_nesting_mismatch")
                    elif not fragment["self_closing"] and name not in _VOID_TAGS:
                        if len(stack) >= _MAX_DEPTH:
                            limitations.add("tag_depth_limit")
                            result["scan_complete"] = False
                        else:
                            stack.append(name)
                            result["max_observed_depth"] = max(result["max_observed_depth"], len(stack))
        if not closed:
            limitations.add("unclosed_angle_fragment")
        result["fragments"].append(fragment)
        result["exact_br_count"] += int(fragment["exact_br"])
        cursor = end
        if "angle_depth_limit" in fragment_limits or "tag_depth_limit" in limitations:
            result["scan_complete"] = False
            break
    if stack:
        limitations.add("unclosed_tag_like")
    result["fragment_scanned_character_count"] = cursor
    result["angle_fragment_count"] = len(result["fragments"])
    result["analysis_limited"] = bool(limitations)
    result["limitations"] = sorted(limitations)
    if limitations:
        result["code"]["context_limited"] = True
        for fragment in result["fragments"]:
            fragment["context"] = "unknown"
    return result
