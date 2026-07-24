"""Settings store for the self-hosted web-search + browsing stack.

Two moving parts are configured here:

* **search** — which engine turns a query into a list of result URLs. The
  default is the self-hosted ``searxng`` metasearch service (no API key, no
  third-party cloud). Paid adapters (``tavily``/``serper``/``brave``) remain
  available as an optional fallback for deployments that want them.
* **browsing** — the ``web-browser`` sidecar (headless Chromium with stealth
  patches) that renders a page like a human and returns readable text plus an
  optional screenshot.

Config lives in Redis so it can be edited from the GUI without a redeploy; the
process ``.env`` remains the fallback so a fresh install works out of the box.
The provider API key is encrypted at rest (:mod:`app.ai.secret_box`) and only a
mask is ever returned to the UI.
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel, Field

from app.ai import secret_box

_REDIS_KEY = "web_search_config"

SUPPORTED_PROVIDERS = ("searxng", "tavily", "serper", "brave", "custom")

_DEFAULT_ENDPOINTS = {
    "searxng": "http://searxng:8080/search",
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}

_DEFAULT_BROWSER_URL = "http://web-browser:8093"


class WebSearchConfig(BaseModel):
    """Resolved web-search + browsing configuration (server-internal)."""

    provider: str = "searxng"
    endpoint: str | None = None
    api_key: str = ""  # plaintext, resolved from encrypted store / env
    # Fallback engine used when the primary provider errors or returns nothing.
    fallback_provider: str | None = None
    fallback_endpoint: str | None = None
    fallback_api_key: str = ""
    # SearXNG engine allowlist (empty → SearXNG default set).
    searxng_engines: list[str] = Field(default_factory=list)
    # Human-like browsing sidecar.
    browser_url: str = _DEFAULT_BROWSER_URL
    browsing_enabled: bool = True

    def resolved_endpoint(self) -> str | None:
        return self.endpoint or _DEFAULT_ENDPOINTS.get(self.provider)

    def resolved_fallback_endpoint(self) -> str | None:
        if not self.fallback_provider:
            return None
        return self.fallback_endpoint or _DEFAULT_ENDPOINTS.get(self.fallback_provider)


class WebSearchConfigUpdate(BaseModel):
    """Patch payload from the GUI. Only provided fields are applied."""

    provider: str | None = None
    endpoint: str | None = None
    api_key: str | None = None  # "" clears, None leaves unchanged, "•••" ignored
    fallback_provider: str | None = None
    fallback_endpoint: str | None = None
    fallback_api_key: str | None = None
    searxng_engines: list[str] | None = None
    browser_url: str | None = None
    browsing_enabled: bool | None = None


class WebSearchConfigView(BaseModel):
    """Display-safe view for the GUI: secrets are masked, never returned raw."""

    provider: str
    endpoint: str | None
    endpoint_effective: str | None
    api_key_mask: str
    api_key_set: bool
    fallback_provider: str | None
    fallback_endpoint: str | None
    fallback_endpoint_effective: str | None
    fallback_api_key_mask: str
    fallback_api_key_set: bool
    searxng_engines: list[str]
    browser_url: str
    browsing_enabled: bool
    supported_providers: list[str]
    default_endpoints: dict[str, str]


def _redis_get() -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — Redis is a soft dependency for config
        return None


def _redis_set(data: dict) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(_REDIS_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _env_config() -> dict:
    """Legacy/bootstrap configuration from environment variables."""
    provider = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower() or "searxng"
    return {
        "provider": provider,
        "endpoint": os.getenv("WEB_SEARCH_ENDPOINT", "").strip() or None,
        # Env key is plaintext; it is not persisted back encrypted unless saved.
        "_api_key_plain": os.getenv("WEB_SEARCH_API_KEY", "").strip(),
        "fallback_provider": os.getenv("WEB_SEARCH_FALLBACK_PROVIDER", "").strip().lower()
        or None,
        "fallback_endpoint": os.getenv("WEB_SEARCH_FALLBACK_ENDPOINT", "").strip() or None,
        "_fallback_api_key_plain": os.getenv("WEB_SEARCH_FALLBACK_API_KEY", "").strip(),
        "browser_url": os.getenv("WEB_BROWSER_URL", "").strip() or _DEFAULT_BROWSER_URL,
        "searxng_engines": [
            e.strip()
            for e in os.getenv("WEB_SEARCH_SEARXNG_ENGINES", "").split(",")
            if e.strip()
        ],
    }


def get_config() -> WebSearchConfig:
    """Resolve effective config: Redis (with encrypted secrets) → env fallback."""
    env = _env_config()
    stored = _redis_get()
    if not stored:
        return WebSearchConfig(
            provider=env["provider"],
            endpoint=env["endpoint"],
            api_key=env["_api_key_plain"],
            fallback_provider=env["fallback_provider"],
            fallback_endpoint=env["fallback_endpoint"],
            fallback_api_key=env["_fallback_api_key_plain"],
            searxng_engines=env["searxng_engines"],
            browser_url=env["browser_url"],
        )
    api_key = secret_box.decrypt(stored.get("api_key_enc")) or env["_api_key_plain"]
    fb_key = (
        secret_box.decrypt(stored.get("fallback_api_key_enc"))
        or env["_fallback_api_key_plain"]
    )
    return WebSearchConfig(
        provider=stored.get("provider") or env["provider"],
        endpoint=stored.get("endpoint") if stored.get("endpoint") is not None else env["endpoint"],
        api_key=api_key,
        fallback_provider=stored.get("fallback_provider", env["fallback_provider"]),
        fallback_endpoint=stored.get("fallback_endpoint", env["fallback_endpoint"]),
        fallback_api_key=fb_key,
        searxng_engines=stored.get("searxng_engines") or env["searxng_engines"],
        browser_url=stored.get("browser_url") or env["browser_url"],
        browsing_enabled=stored.get("browsing_enabled", True),
    )


# Sentinel the GUI echoes back for an unchanged (masked) secret; ignore it.
_MASK_SENTINELS = {"", "•••", "…", "********"}


def update_config(patch: WebSearchConfigUpdate) -> WebSearchConfig:
    stored = _redis_get() or {}

    def _secret_field(new: str | None, enc_key: str) -> None:
        if new is None:
            return
        if new == "":
            stored[enc_key] = ""  # explicit clear
        elif "…" in new or "•" in new:
            return  # a mask echoed back — leave the stored secret intact
        else:
            stored[enc_key] = secret_box.encrypt(new)

    if patch.provider is not None:
        stored["provider"] = patch.provider.strip().lower()
    if patch.endpoint is not None:
        stored["endpoint"] = patch.endpoint.strip() or None
    _secret_field(patch.api_key, "api_key_enc")
    if patch.fallback_provider is not None:
        stored["fallback_provider"] = patch.fallback_provider.strip().lower() or None
    if patch.fallback_endpoint is not None:
        stored["fallback_endpoint"] = patch.fallback_endpoint.strip() or None
    _secret_field(patch.fallback_api_key, "fallback_api_key_enc")
    if patch.searxng_engines is not None:
        stored["searxng_engines"] = [e.strip() for e in patch.searxng_engines if e.strip()]
    if patch.browser_url is not None:
        stored["browser_url"] = patch.browser_url.strip() or _DEFAULT_BROWSER_URL
    if patch.browsing_enabled is not None:
        stored["browsing_enabled"] = bool(patch.browsing_enabled)

    _redis_set(stored)
    return get_config()


def to_view(cfg: WebSearchConfig) -> WebSearchConfigView:
    return WebSearchConfigView(
        provider=cfg.provider,
        endpoint=cfg.endpoint,
        endpoint_effective=cfg.resolved_endpoint(),
        api_key_mask=secret_box.mask(cfg.api_key),
        api_key_set=bool(cfg.api_key),
        fallback_provider=cfg.fallback_provider,
        fallback_endpoint=cfg.fallback_endpoint,
        fallback_endpoint_effective=cfg.resolved_fallback_endpoint(),
        fallback_api_key_mask=secret_box.mask(cfg.fallback_api_key),
        fallback_api_key_set=bool(cfg.fallback_api_key),
        searxng_engines=cfg.searxng_engines,
        browser_url=cfg.browser_url,
        browsing_enabled=cfg.browsing_enabled,
        supported_providers=list(SUPPORTED_PROVIDERS),
        default_endpoints=dict(_DEFAULT_ENDPOINTS),
    )
