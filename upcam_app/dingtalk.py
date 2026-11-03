"""DingTalk webhook transport adapter for the automatic delivery guard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote_plus
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1024 * 1024


class DingTalkConfigError(ValueError):
    """The webhook does not match the supported DingTalk HTTPS form."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signed_webhook(webhook: str, secret: str, timestamp_ms: int) -> str:
    parsed = urlsplit(webhook)
    if (parsed.scheme != "https" or parsed.hostname != "oapi.dingtalk.com"
            or parsed.username or parsed.password or parsed.fragment
            or parsed.path != "/robot/send" or not parsed.query):
        raise DingTalkConfigError("Use a DingTalk HTTPS robot webhook URL")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    if sum(1 for key, _ in pairs if key == "access_token") != 1:
        raise DingTalkConfigError("The DingTalk webhook URL must contain one access token")
    query = dict(pairs)
    if not query.get("access_token") or not secret:
        raise DingTalkConfigError("The DingTalk webhook or signing secret is missing")
    message = f"{timestamp_ms}\n{secret}".encode("utf-8")
    signature = base64.b64encode(hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()).decode("ascii")
    query["timestamp"] = str(timestamp_ms)
    query["sign"] = quote_plus(signature)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class DingTalkSender:
    """Send only when invoked as DeliveryGuard's callback.

    This adapter returns True only for a bounded, valid DingTalk success
    envelope. Every other result returns False so DeliveryGuard persists an
    unknown state and prevents an automatic retry.
    """

    def __init__(self, transport=None, clock_ms=None, timeout: int = 15):
        self.transport = transport or self._http_transport
        self.clock_ms = clock_ms or (lambda: __import__("time").time_ns() // 1_000_000)
        self.timeout = timeout

    def send_markdown(self, webhook: str, secret: str, markdown: str) -> bool:
        if not isinstance(markdown, str) or not markdown.strip():
            return False
        destination = signed_webhook(webhook, secret, self.clock_ms())
        result = self.transport(destination, _markdown_payload("UPCAM 站会日报", markdown), self.timeout)
        return isinstance(result, dict) and type(result.get("errcode")) is int and result["errcode"] == 0

    @staticmethod
    def _http_transport(url: str, payload: bytes, timeout: int):
        request = Request(url, data=payload,
                          headers={"Content-Type": "application/json; charset=utf-8",
                                   "Accept": "application/json"}, method="POST")
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _markdown_payload(title: str, markdown: str) -> bytes:
    return json.dumps({"msgtype": "markdown", "markdown": {"title": title, "text": markdown}},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
