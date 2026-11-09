"""Fail-closed, local-only checks for known credentials in report material.

Inspection never rewrites evidence, logs content, reads files or calls a service.
Known full values (including credential-bearing URL components) and bounded
URL/JSON/HTML encodings are checked. This is not a general secret detector and
does not claim to recognize arbitrary encryption or partial/unknown secrets.
"""

from __future__ import annotations

import html
import math
from urllib.parse import unquote, unquote_plus

from .detailed_diagnostic import _json_unescape_view, _known_pattern


MAX_DEPTH = 32
MAX_NODES = 16_000_000
MAX_BYTES = 128 * 1024 * 1024
MAX_INSPECTION_BYTES = 512 * 1024 * 1024
MAX_STRING_BYTES = 1024 * 1024
MAX_ENCODING_LAYERS = 8
MAX_INSPECTION_VIEWS = 64
MAX_CACHED_TEXTS = 4096
MAX_CACHE_BYTES = 1024 * 1024
_BLOCKED = "本机凭据安全检查未通过；本次已阻断，未发送钉钉。请检查本机设置或重新只读采集。"


class CredentialExposureBlocked(RuntimeError):
    """Neither matching text nor internal detection errors may be disclosed."""

    def __init__(self):
        super().__init__(_BLOCKED)


class ReportCredentialGuard:
    def __init__(self, credentials: dict[str, str]):
        try:
            if (type(credentials) is not dict
                    or any(type(key) is not str or type(value) is not str
                           for key, value in credentials.items())):
                raise CredentialExposureBlocked()
            self._known, _ = _known_pattern(credentials)
        except Exception:
            raise CredentialExposureBlocked() from None

    def require_safe(self, value) -> None:
        """Inspect all keys/values, not just fields today's renderer consumes.

        Unknown types, cycles, malformed encodings and exhausted bounds block
        the run. Do not stringify arbitrary objects or return a cleaned copy.
        """
        try:
            nodes, total_bytes, inspection_bytes, cache_bytes = 0, 0, 0, 0
            active = set()
            safe_text = set()

            def charge(text, *, inspection=False):
                nonlocal total_bytes, inspection_bytes
                size = len(text.encode("utf-8"))
                if inspection:
                    inspection_bytes += size
                else:
                    total_bytes += size
                if (size > MAX_STRING_BYTES or total_bytes > MAX_BYTES
                        or inspection_bytes > MAX_INSPECTION_BYTES):
                    raise CredentialExposureBlocked()
                return size

            def inspect_text(text):
                nonlocal cache_bytes
                size = charge(text)
                # Repeated field names and unchanged comments are common in
                # large histories. Cache only fully checked, immutable strings,
                # with strict bounds and only for this one inspection call.
                if text in safe_text:
                    return
                pending = [(text, 0)]
                seen = {text}
                while pending:
                    current, layer = pending.pop()
                    if layer:
                        charge(current, inspection=True)
                    if self._known is not None and self._known.search(current):
                        raise CredentialExposureBlocked()
                    decoders = []
                    if "%" in current:
                        decoders.append(unquote)
                    if "+" in current:
                        decoders.append(unquote_plus)
                    if "\\" in current:
                        decoders.append(_json_unescape_view)
                    if "&" in current:
                        decoders.append(html.unescape)
                    for decode in decoders:
                        decoded = (decode(current, errors="strict")
                                   if decode in (unquote, unquote_plus) else decode(current))
                        # JSON surrogate pairs represent one character; isolated
                        # surrogates are malformed and must fail closed.
                        if any(0xD800 <= ord(char) <= 0xDFFF for char in decoded):
                            decoded = decoded.encode("utf-16", "surrogatepass").decode("utf-16")
                        if decoded not in seen:
                            if layer >= MAX_ENCODING_LAYERS or len(seen) >= MAX_INSPECTION_VIEWS:
                                raise CredentialExposureBlocked()
                            seen.add(decoded)
                            pending.append((decoded, layer + 1))
                if len(safe_text) < MAX_CACHED_TEXTS and cache_bytes + size <= MAX_CACHE_BYTES:
                    safe_text.add(text)
                    cache_bytes += size

            def walk(item, depth=0):
                nonlocal nodes
                nodes += 1
                if nodes > MAX_NODES or depth > MAX_DEPTH:
                    raise CredentialExposureBlocked()
                kind = type(item)
                if kind is str:
                    inspect_text(item)
                elif item is None or kind is bool:
                    return
                elif kind in (int, float):
                    if kind is float and not math.isfinite(item):
                        raise CredentialExposureBlocked()
                    inspect_text(str(item))
                elif kind in (dict, list, tuple):
                    marker = id(item)
                    if marker in active:
                        raise CredentialExposureBlocked()
                    active.add(marker)
                    try:
                        if kind is dict:
                            for key, entry in item.items():
                                if type(key) is not str:
                                    raise CredentialExposureBlocked()
                                walk(key, depth + 1)
                                walk(entry, depth + 1)
                        else:
                            for entry in item:
                                walk(entry, depth + 1)
                    finally:
                        active.remove(marker)
                else:
                    raise CredentialExposureBlocked()

            walk(value)
        except Exception:
            raise CredentialExposureBlocked() from None


class GuardedSummarizer:
    """Check actual model-bound content and all returned summary material."""

    def __init__(self, client, guard: ReportCredentialGuard):
        self._client = client
        self._guard = guard

    def summarize(self, api_key, model, effort, rows, rules, service_tier="default"):
        # The API Key authenticates only its intended service. Never include the
        # local credential dictionary or the comparison set in model content.
        self._guard.require_safe((model, effort, rows, rules, service_tier))
        try:
            result = self._client.summarize(
                api_key, model, effort, rows, rules, service_tier=service_tier)
        except Exception as exc:
            self._guard.require_safe(str(exc))
            raise
        self._guard.require_safe(result)
        return result
