"""OpenAI model-ID refresh plus a deliberately conservative capability catalog.

GET /v1/models is API-key scoped but only returns basic model metadata. It does
not report endpoint compatibility or reasoning-effort values. Those values are
therefore shown only for model IDs explicitly documented in this app release.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import atomic_write


MODELS_URL = "https://api.openai.com/v1/models"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CACHE_SCHEMA = 2

# Source checked against the official GPT-6 Luna model reference during this
# release. The API's /models payload does not include these capabilities.
KNOWN_REASONING_EFFORTS = {
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
}


class ModelCatalogError(RuntimeError):
    """A model catalog could not be refreshed or validated."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAIModelCatalog:
    def __init__(self, cache_path: Path, transport=None, clock=None):
        self.cache_path = Path(cache_path)
        self.transport = transport or self._http_transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def load(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return self._validate_cache(cache)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ModelCatalogError("The local model cache is damaged; the saved file was not changed") from exc

    def refresh(self, api_key: str) -> dict:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelCatalogError("Enter or save an OpenAI API key before refreshing models")
        try:
            payload = self.transport(api_key, 20)
            result = self._parse_response(payload)
        except ModelCatalogError:
            raise
        except Exception:
            # Do not surface exception text; HTTP libraries can include headers or URLs.
            raise ModelCatalogError("OpenAI model refresh failed; the previous cache is unchanged") from None
        cache = {
            "schema_version": CACHE_SCHEMA,
            "fetched_at_utc": self.clock().astimezone(timezone.utc).isoformat(timespec="seconds"),
            "source": MODELS_URL,
            "credential_fingerprint": credential_fingerprint(api_key),
            "models": result,
        }
        self._validate_cache(cache)
        atomic_write(self.cache_path, (json.dumps(cache, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return cache

    @staticmethod
    def _parse_response(payload) -> list[dict]:
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_RESPONSE_BYTES:
            raise ModelCatalogError("OpenAI returned an invalid or oversized model list")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelCatalogError("OpenAI returned an invalid model list") from None
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            raise ModelCatalogError("OpenAI returned an invalid model list")
        models = []
        seen = set()
        for item in document["data"]:
            if not isinstance(item, dict):
                raise ModelCatalogError("OpenAI returned an invalid model list")
            model_id = item.get("id")
            if (not isinstance(model_id, str) or not model_id or len(model_id) > 256
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", model_id)
                    or model_id in seen):
                raise ModelCatalogError("OpenAI returned an invalid model identifier")
            seen.add(model_id)
            owned_by = item.get("owned_by")
            shutdown_date = item.get("shutdown_date")
            if owned_by is not None and (not isinstance(owned_by, str) or len(owned_by) > 128):
                raise ModelCatalogError("OpenAI returned invalid model metadata")
            if shutdown_date is not None and (not isinstance(shutdown_date, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", shutdown_date)):
                raise ModelCatalogError("OpenAI returned invalid model metadata")
            models.append({"id": model_id, "owned_by": owned_by, "shutdown_date": shutdown_date})
        if not models:
            raise ModelCatalogError("OpenAI returned no models; the previous cache is unchanged")
        return sorted(models, key=lambda item: item["id"].casefold())

    @staticmethod
    def _validate_cache(cache: object) -> dict:
        if (not isinstance(cache, dict) or cache.get("schema_version") != CACHE_SCHEMA
                or not isinstance(cache.get("fetched_at_utc"), str)
                or cache.get("source") != MODELS_URL
                or not isinstance(cache.get("credential_fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", cache["credential_fingerprint"])):
            raise ModelCatalogError("The local model cache has an unsupported format")
        models = OpenAIModelCatalog._parse_cached_models(cache.get("models"))
        try:
            datetime.fromisoformat(cache["fetched_at_utc"].replace("Z", "+00:00"))
        except ValueError:
            raise ModelCatalogError("The local model cache has an invalid timestamp") from None
        return {**cache, "models": models}

    @staticmethod
    def _parse_cached_models(items: object) -> list[dict]:
        if not isinstance(items, list):
            raise ModelCatalogError("The local model cache is invalid")
        # Reuse the same strict identifier rules without accepting any API payload fields.
        payload = json.dumps({"data": items}, ensure_ascii=True).encode("ascii")
        return OpenAIModelCatalog._parse_response(payload)

    @staticmethod
    def _http_transport(api_key: str, timeout: int) -> bytes:
        request = Request(MODELS_URL, headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
        }, method="GET")
        try:
            opener = build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise ModelCatalogError(f"OpenAI model list returned HTTP {response.status}")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Never surface response bodies; they may contain sensitive diagnostics.
            raise ModelCatalogError(f"OpenAI model list returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ModelCatalogError("Could not reach the OpenAI model list; the previous cache is unchanged") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ModelCatalogError("OpenAI returned an oversized model list")
        return payload


def reasoning_efforts(model_id: str) -> tuple[str, ...]:
    """Return only release-verified values; empty means use model default."""
    return KNOWN_REASONING_EFFORTS.get(model_id, ())


def credential_fingerprint(api_key: str) -> str:
    """Bind the local model cache to the key used for refresh without storing it."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ModelCatalogError("OpenAI API key is missing")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def cache_matches_key(cache: dict | None, api_key: str) -> bool:
    if not isinstance(cache, dict):
        return False
    try:
        expected = credential_fingerprint(api_key)
    except ModelCatalogError:
        return False
    return hmac.compare_digest(cache.get("credential_fingerprint", ""), expected)
