"""Settings store for the self-hosted web-search + browsing stack.

Two moving parts are configured here:

* **search** — which engines turn a query into a list of result URLs: a list
  of provider entries (:class:`SearchProvider`), each an adapter from
  :mod:`app.ai.web_providers`. Primary entries are queried together and their
  lists fused; fallback entries run only when the primaries found nothing.
  The default is one self-hosted ``searxng`` (no API key, no third-party
  cloud). Before the list is saved once, it is derived from the legacy
  ``provider``/``fallback_provider`` fields.
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
import re

from pydantic import BaseModel, Field, field_validator

from app.ai import secret_box
from app.ai.web_providers import DEFAULT_ENDPOINTS, KINDS

_REDIS_KEY = "web_search_config"

SUPPORTED_PROVIDERS = KINDS

_DEFAULT_ENDPOINTS = DEFAULT_ENDPOINTS

_DEFAULT_BROWSER_URL = "http://web-browser:8093"

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class SearchProvider(BaseModel):
    """One search engine the agent may query (server-internal, key in plain)."""

    id: str
    kind: str
    enabled: bool = True
    # primary: queried together, lists fused; fallback: only when primaries found nothing.
    role: str = "primary"
    weight: float = Field(1.0, ge=0.0, le=10.0)
    endpoint: str | None = None
    api_key: str = ""
    engines: list[str] = Field(default_factory=list)
    # Country/market: SearXNG — unused, serper/jina gl, brave country, exa
    # userLocation, yandex searchType (ru, com, tr, kk, be, uz).
    region: str | None = None
    folder_id: str | None = None  # Yandex Cloud folder
    mcp_tool: str | None = None  # mcp_<server>_<tool> for kind=mcp
    # Calls per day; None — unlimited (self-hosted). Counted before the call.
    daily_quota: int | None = Field(default=None, ge=0)

    def resolved_endpoint(self) -> str | None:
        return self.endpoint or _DEFAULT_ENDPOINTS.get(self.kind)


class BrowserProxy(BaseModel):
    """W2: an upstream exit for the browser (residential, mobile, datacenter).

    A kept profile is bound to one proxy for good (see
    :func:`app.api.web_search.browser_profile_fields`), and the proxy's
    locale and timezone become the profile's, so IP, clock and language agree.
    """

    id: str
    server: str  # http://host:port
    username: str = ""
    password: str = ""  # plaintext, resolved from the encrypted store
    kind: str = "residential"  # residential | mobile | datacenter
    country: str | None = None
    locale: str | None = None  # ru-RU
    timezone: str | None = None  # Europe/Moscow
    enabled: bool = True


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
    # Saved provider list; empty → derived from the legacy fields above.
    providers: list[SearchProvider] = Field(default_factory=list)
    # Search language passed to engines that take one.
    language: str = "ru"
    # Polite pace: seconds between two page loads on one host (plus jitter).
    per_host_interval_s: float = 1.5
    search_cache_ttl_s: int = 6 * 3600
    page_cache_ttl_s: int = 24 * 3600
    # Pages research reads at the same time.
    research_concurrency: int = 4
    # W2: the browser's exits; empty → direct from the server's address.
    proxies: list[BrowserProxy] = Field(default_factory=list)
    # Persona of a profile without a proxy (or a proxy without its own).
    browser_locale: str = "ru-RU"
    browser_timezone: str = "Europe/Moscow"
    # W3: a work with no grant gets the read grant on its first browser action.
    browser_read_autogrant: bool = True

    def effective_providers(self) -> list[SearchProvider]:
        if self.providers:
            return list(self.providers)
        derived = [
            SearchProvider(
                id=self.provider or "searxng",
                kind=self.provider or "searxng",
                endpoint=self.endpoint,
                api_key=self.api_key,
                engines=self.searxng_engines if self.provider == "searxng" else [],
            )
        ]
        if self.fallback_provider:
            derived.append(
                SearchProvider(
                    id=f"{self.fallback_provider}-fallback",
                    kind=self.fallback_provider,
                    role="fallback",
                    endpoint=self.fallback_endpoint,
                    api_key=self.fallback_api_key,
                    engines=self.searxng_engines if self.fallback_provider == "searxng" else [],
                )
            )
        return derived

    def resolved_endpoint(self) -> str | None:
        return self.endpoint or _DEFAULT_ENDPOINTS.get(self.provider)

    def resolved_fallback_endpoint(self) -> str | None:
        if not self.fallback_provider:
            return None
        return self.fallback_endpoint or _DEFAULT_ENDPOINTS.get(self.fallback_provider)


class SearchProviderUpdate(BaseModel):
    """A provider entry from the GUI; ``api_key`` None or a mask keeps the stored key."""

    id: str
    kind: str
    enabled: bool = True
    role: str = "primary"
    weight: float = Field(1.0, ge=0.0, le=10.0)
    endpoint: str | None = None
    api_key: str | None = None
    engines: list[str] = Field(default_factory=list)
    region: str | None = None
    folder_id: str | None = None
    mcp_tool: str | None = None
    daily_quota: int | None = Field(default=None, ge=0)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        value = value.strip().lower()
        if not _ID.match(value):
            raise ValueError("id: латиница, цифры, - и _, до 40 символов")
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in KINDS:
            raise ValueError(f"kind: один из {', '.join(KINDS)}")
        return value

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if value not in ("primary", "fallback"):
            raise ValueError("role: primary или fallback")
        return value


class BrowserProxyUpdate(BaseModel):
    """A proxy from the GUI; ``password`` None or a mask keeps the stored one."""

    id: str
    server: str
    username: str = ""
    password: str | None = None
    kind: str = "residential"
    country: str | None = None
    locale: str | None = None
    timezone: str | None = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        value = value.strip().lower()
        if not _ID.match(value):
            raise ValueError("id: латиница, цифры, - и _, до 40 символов")
        return value

    @field_validator("server")
    @classmethod
    def _server(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^http://[^/\s:@]+:\d{2,5}/?$", value):
            raise ValueError("server: http://host:port (адрес HTTP-прокси)")
        return value.rstrip("/")

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in ("residential", "mobile", "datacenter"):
            raise ValueError("kind: residential, mobile или datacenter")
        return value


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
    providers: list[SearchProviderUpdate] | None = None
    language: str | None = None
    per_host_interval_s: float | None = Field(default=None, ge=0.0, le=60.0)
    search_cache_ttl_s: int | None = Field(default=None, ge=0, le=7 * 86400)
    page_cache_ttl_s: int | None = Field(default=None, ge=0, le=30 * 86400)
    research_concurrency: int | None = Field(default=None, ge=1, le=12)
    proxies: list[BrowserProxyUpdate] | None = None
    browser_locale: str | None = Field(default=None, max_length=20)
    browser_timezone: str | None = Field(default=None, max_length=60)
    browser_read_autogrant: bool | None = None


class BrowserProxyView(BaseModel):
    id: str
    server: str
    username: str
    password_mask: str
    password_set: bool
    kind: str
    country: str | None
    locale: str | None
    timezone: str | None
    enabled: bool


class SearchProviderView(BaseModel):
    id: str
    kind: str
    enabled: bool
    role: str
    weight: float
    endpoint: str | None
    endpoint_effective: str | None
    api_key_mask: str
    api_key_set: bool
    engines: list[str]
    region: str | None
    folder_id: str | None
    mcp_tool: str | None
    daily_quota: int | None
    used_today: int | None = None


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
    providers: list[SearchProviderView] = []
    providers_saved: bool = False
    language: str = "ru"
    per_host_interval_s: float = 1.5
    search_cache_ttl_s: int = 6 * 3600
    page_cache_ttl_s: int = 24 * 3600
    research_concurrency: int = 4
    proxies: list[BrowserProxyView] = []
    browser_locale: str = "ru-RU"
    browser_timezone: str = "Europe/Moscow"
    browser_read_autogrant: bool = True


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
        "fallback_provider": os.getenv("WEB_SEARCH_FALLBACK_PROVIDER", "").strip().lower() or None,
        "fallback_endpoint": os.getenv("WEB_SEARCH_FALLBACK_ENDPOINT", "").strip() or None,
        "_fallback_api_key_plain": os.getenv("WEB_SEARCH_FALLBACK_API_KEY", "").strip(),
        "browser_url": os.getenv("WEB_BROWSER_URL", "").strip() or _DEFAULT_BROWSER_URL,
        "searxng_engines": [
            e.strip() for e in os.getenv("WEB_SEARCH_SEARXNG_ENGINES", "").split(",") if e.strip()
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
    providers = [
        SearchProvider(
            **{k: v for k, v in entry.items() if k != "api_key_enc"},
            api_key=secret_box.decrypt(entry.get("api_key_enc")) or "",
        )
        for entry in stored.get("providers") or []
        if isinstance(entry, dict) and entry.get("kind") in KINDS
    ]
    proxies = [
        BrowserProxy(
            **{k: v for k, v in entry.items() if k != "password_enc"},
            password=secret_box.decrypt(entry.get("password_enc")) or "",
        )
        for entry in stored.get("proxies") or []
        if isinstance(entry, dict) and entry.get("server")
    ]
    tuning = {key: stored[key] for key in _TUNING_KEYS if stored.get(key) is not None}
    api_key = secret_box.decrypt(stored.get("api_key_enc")) or env["_api_key_plain"]
    fb_key = (
        secret_box.decrypt(stored.get("fallback_api_key_enc")) or env["_fallback_api_key_plain"]
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
        providers=providers,
        proxies=proxies,
        **tuning,
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
    if patch.providers is not None:
        stored["providers"] = _merge_providers(stored.get("providers") or [], patch.providers)
    if patch.proxies is not None:
        stored["proxies"] = _merge_proxies(stored.get("proxies") or [], patch.proxies)
    for key in _TUNING_KEYS:
        value = getattr(patch, key)
        if value is not None:
            if isinstance(value, str):
                # Only the search language is a lowercase code; "ru-RU" and
                # "Europe/Moscow" are case-sensitive.
                value = value.strip().lower() if key == "language" else value.strip()
            stored[key] = value

    _redis_set(stored)
    return get_config()


_TUNING_KEYS = (
    "language",
    "per_host_interval_s",
    "search_cache_ttl_s",
    "page_cache_ttl_s",
    "research_concurrency",
    "browser_locale",
    "browser_timezone",
    "browser_read_autogrant",
)


def _merge_proxies(previous: list[dict], entries: list[BrowserProxyUpdate]) -> list[dict]:
    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("id прокси должны быть разными")
    old = {e.get("id"): e.get("password_enc", "") for e in previous if isinstance(e, dict)}
    merged = []
    for entry in entries:
        data = entry.model_dump(exclude={"password"})
        password = entry.password
        if password is None or "…" in password or "•" in password:
            data["password_enc"] = old.get(entry.id, "")
        elif password == "":
            data["password_enc"] = ""
        else:
            data["password_enc"] = secret_box.encrypt(password)
        merged.append(data)
    return merged


def _merge_providers(previous: list[dict], entries: list[SearchProviderUpdate]) -> list[dict]:
    """New provider list; a key not re-entered stays encrypted as it was."""
    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("id провайдеров должны быть разными")
    old_keys = {e.get("id"): e.get("api_key_enc", "") for e in previous if isinstance(e, dict)}
    merged = []
    for entry in entries:
        data = entry.model_dump(exclude={"api_key"})
        data["engines"] = [e.strip() for e in entry.engines if e.strip()]
        data["endpoint"] = (entry.endpoint or "").strip() or None
        key = entry.api_key
        if key is None or "…" in key or "•" in key:
            data["api_key_enc"] = old_keys.get(entry.id, "")
        elif key == "":
            data["api_key_enc"] = ""
        else:
            data["api_key_enc"] = secret_box.encrypt(key.strip())
        merged.append(data)
    return merged


def to_view(cfg: WebSearchConfig, used_today: dict[str, int] | None = None) -> WebSearchConfigView:
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
        providers=[
            SearchProviderView(
                id=p.id,
                kind=p.kind,
                enabled=p.enabled,
                role=p.role,
                weight=p.weight,
                endpoint=p.endpoint,
                endpoint_effective=p.resolved_endpoint(),
                api_key_mask=secret_box.mask(p.api_key),
                api_key_set=bool(p.api_key),
                engines=p.engines,
                region=p.region,
                folder_id=p.folder_id,
                mcp_tool=p.mcp_tool,
                daily_quota=p.daily_quota,
                used_today=(used_today or {}).get(p.id),
            )
            for p in cfg.effective_providers()
        ],
        providers_saved=bool(cfg.providers),
        language=cfg.language,
        per_host_interval_s=cfg.per_host_interval_s,
        search_cache_ttl_s=cfg.search_cache_ttl_s,
        page_cache_ttl_s=cfg.page_cache_ttl_s,
        research_concurrency=cfg.research_concurrency,
        proxies=[
            BrowserProxyView(
                id=x.id,
                server=x.server,
                username=x.username,
                password_mask=secret_box.mask(x.password),
                password_set=bool(x.password),
                kind=x.kind,
                country=x.country,
                locale=x.locale,
                timezone=x.timezone,
                enabled=x.enabled,
            )
            for x in cfg.proxies
        ],
        browser_locale=cfg.browser_locale,
        browser_timezone=cfg.browser_timezone,
        browser_read_autogrant=cfg.browser_read_autogrant,
    )
