"""Provider instance resolver — the single place that answers
"which endpoint (and API key) do I call for provider kind X / model Y?".

Multiple nodes per kind are supported (e.g. two Ollama servers on different
machines). Durable storage is the ``provider_instances`` DB table; the hot path
reads a Redis cache (key ``provider_instances``) refreshed by the
``/api/providers`` API and on startup. Resolution order for base_url/api_key:

    DB/Redis instance  →  YAML registry default  →  environment (.env)

so existing deployments work unchanged until a node is added in the UI.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import httpx
import structlog

from app.ai.secret_box import decrypt
from app.ai.schemas import ProviderKind
from app.config import settings

logger = structlog.get_logger()

_REDIS_KEY = "provider_instances"

# Env var that overrides the *default* node base_url for a local kind, so the
# correct Docker-internal hostname wins over whatever a browser session saved.
_ENV_URL_OVERRIDE = {
    ProviderKind.OLLAMA: "OLLAMA_URL",
    ProviderKind.VLLM: "VLLM_URL",
    ProviderKind.LLAMACPP: "LLAMACPP_URL",
}

# Static fallback base_urls (mirror model_registry.yaml providers section).
_YAML_DEFAULT_URL = {
    ProviderKind.OLLAMA: lambda: settings.ollama_url,
    ProviderKind.LLAMACPP: lambda: settings.llamacpp_url,
    ProviderKind.VLLM: lambda: os.environ.get("VLLM_URL", "http://localhost:8000"),
    ProviderKind.OPENAI_COMPATIBLE: lambda: "http://localhost:8080",
    ProviderKind.ANTHROPIC: lambda: "https://api.anthropic.com",
    ProviderKind.OPENROUTER: lambda: "https://openrouter.ai/api/v1",
    ProviderKind.DEEPSEEK: lambda: "https://api.deepseek.com/v1",
    ProviderKind.GEMINI: lambda: "https://generativelanguage.googleapis.com/v1beta/openai",
}

_API_KEY_ENV = {
    ProviderKind.ANTHROPIC: "ANTHROPIC_API_KEY",
    ProviderKind.OPENROUTER: "OPENROUTER_API_KEY",
    ProviderKind.DEEPSEEK: "DEEPSEEK_API_KEY",
    ProviderKind.GEMINI: "GOOGLE_API_KEY",
}

_LOCAL_KINDS = {
    ProviderKind.OLLAMA,
    ProviderKind.VLLM,
    ProviderKind.LLAMACPP,
    ProviderKind.OPENAI_COMPATIBLE,
    ProviderKind.LMSTUDIO,
}

# Lazily-loaded provider defaults from the YAML registry (base_url + api_key_env).
# Avoids duplicating the long cloud-provider list maintained in
# model_registry.yaml — any provider added there is resolvable here automatically.
_registry_providers_cache: dict | None = None


def _registry_providers() -> dict:
    global _registry_providers_cache
    if _registry_providers_cache is None:
        try:
            from app.ai.model_registry import ModelRegistry

            reg = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
            _registry_providers_cache = reg.providers
        except Exception:
            _registry_providers_cache = {}
    return _registry_providers_cache


@dataclass
class ResolvedProvider:
    """A concrete, ready-to-call provider endpoint."""

    kind: ProviderKind
    base_url: str
    is_local: bool
    api_key: str = ""
    instance_id: str | None = None
    name: str = ""
    extra: dict = field(default_factory=dict)


# ── Redis cache ─────────────────────────────────────────────────────────────


def _redis_get_instances() -> list[dict]:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_REDIS_KEY)
        return json.loads(raw) if raw else []
    except Exception:
        return []


def set_instances_cache(rows: list[dict]) -> None:
    """Replace the Redis cache. Called by the API/startup after DB writes."""
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(_REDIS_KEY, json.dumps(rows, ensure_ascii=False))
        _availability_cache.clear()
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider_instances_cache_write_failed", error=str(exc))


def _env_key_for(kind: ProviderKind) -> str:
    # Env var name: hardcoded map first, then the registry's api_key_env.
    env = _API_KEY_ENV.get(kind)
    if not env:
        cfg = _registry_providers().get(kind)
        env = getattr(cfg, "api_key_env", None) if cfg else None
    if not env:
        return ""
    # settings carries the typed mirrors for the common cloud providers
    return os.getenv(env, "") or {
        ProviderKind.ANTHROPIC: settings.anthropic_api_key,
        ProviderKind.OPENROUTER: settings.openrouter_api_key,
        ProviderKind.DEEPSEEK: settings.deepseek_api_key,
    }.get(kind, "")


def _default_instance(kind: ProviderKind) -> ResolvedProvider:
    """Synthesise the implicit default node from YAML/env when DB has no rows."""
    base = ""
    env_url = _ENV_URL_OVERRIDE.get(kind)
    if env_url and os.environ.get(env_url, "").strip():
        base = os.environ[env_url].strip()
    else:
        getter = _YAML_DEFAULT_URL.get(kind)
        if getter:
            base = getter()
        else:
            cfg = _registry_providers().get(kind)
            base = str(getattr(cfg, "base_url", "") or "") if cfg else ""
    return ResolvedProvider(
        kind=kind,
        base_url=str(base).rstrip("/"),
        is_local=kind in _LOCAL_KINDS,
        api_key=_env_key_for(kind),
        name=f"{kind.value} (default)",
    )


def _row_to_resolved(row: dict) -> ResolvedProvider | None:
    try:
        kind = ProviderKind(row["kind"])
    except Exception:
        return None
    base = (row.get("base_url") or "").strip()
    if not base:
        # Inherit the default base_url (incl. env override) for this kind.
        base = _default_instance(kind).base_url
    api_key = decrypt(row.get("api_key_encrypted")) or _env_key_for(kind)
    return ResolvedProvider(
        kind=kind,
        base_url=str(base).rstrip("/"),
        is_local=bool(row.get("is_local", kind in _LOCAL_KINDS)),
        api_key=api_key,
        instance_id=row.get("id"),
        name=row.get("name") or f"{kind.value}",
        extra=row.get("extra") or {},
    )


def list_instances(kind: ProviderKind) -> list[ResolvedProvider]:
    """Return all enabled nodes for ``kind`` (DB cache → YAML/env default)."""
    rows = [
        r
        for r in _redis_get_instances()
        if r.get("kind") == kind.value and r.get("enabled", True)
    ]
    resolved = [r for r in (_row_to_resolved(row) for row in rows) if r and r.base_url]
    if resolved:
        return resolved
    default = _default_instance(kind)
    return [default] if default.base_url else []


# ── Availability check (which node hosts a model) ───────────────────────────

_availability_cache: dict[str, tuple[float, set[str]]] = {}
_AVAIL_TTL = 30.0


def _models_on_node(node: ResolvedProvider) -> set[str]:
    """Return the set of model names served by a local node (cached ~30s)."""
    cache_key = node.base_url
    now = time.monotonic()
    cached = _availability_cache.get(cache_key)
    if cached and now - cached[0] < _AVAIL_TTL:
        return cached[1]
    names: set[str] = set()
    try:
        if node.kind == ProviderKind.OLLAMA:
            resp = httpx.get(f"{node.base_url}/api/tags", timeout=4.0)
            resp.raise_for_status()
            names = {m.get("name", "") for m in resp.json().get("models", [])}
            names |= {n.split(":")[0] for n in names if n}  # bare names too
        else:  # vLLM / llama.cpp / openai-compatible
            resp = httpx.get(f"{node.base_url}/v1/models", timeout=4.0)
            resp.raise_for_status()
            names = {m.get("id", "") for m in resp.json().get("data", [])}
    except Exception:
        # Treat as "unknown" rather than "empty" so we don't wrongly skip a node.
        return set()
    _availability_cache[cache_key] = (now, names)
    return names


def select_instance(
    kind: ProviderKind,
    provider_model: str | None = None,
    preferred_instance: str | None = None,
) -> ResolvedProvider:
    """Pick the node to call for ``kind``/``provider_model``.

    Priority: the model's pinned ``preferred_instance`` → first node that
    actually hosts the model → first enabled node. Cloud kinds (single logical
    endpoint) always return their one instance.
    """
    instances = list_instances(kind)
    if not instances:
        # Last resort: synth default even if base_url empty (provider will error clearly).
        return _default_instance(kind)

    if preferred_instance:
        for inst in instances:
            if inst.name == preferred_instance or inst.instance_id == preferred_instance:
                return inst

    if kind not in _LOCAL_KINDS or len(instances) == 1 or not provider_model:
        return instances[0]

    # Multiple local nodes: prefer one that hosts the model. If at least one
    # node answered with a non-empty model list and none host it, this is a
    # known-missing state, not unknown availability.
    saw_known_inventory = False
    for inst in instances:
        served = _models_on_node(inst)
        if not served:
            continue
        saw_known_inventory = True
        bare = provider_model.split(":")[0]
        if provider_model in served or bare in served:
            return inst
    if saw_known_inventory:
        raise RuntimeError(
            f"Model {provider_model} is not served by any enabled {kind.value} node"
        )
    return instances[0]


# ── DB → Redis sync (called from async API / startup) ───────────────────────


async def refresh_cache_from_db(session) -> list[dict]:
    """Load provider_instances from the DB into the Redis cache. Returns rows."""
    from sqlalchemy import select

    from app.db.models import ProviderInstance

    result = await session.execute(select(ProviderInstance))
    rows: list[dict] = []
    for inst in result.scalars().all():
        rows.append(
            {
                "id": str(inst.id),
                "kind": inst.kind,
                "name": inst.name,
                "base_url": inst.base_url,
                "enabled": inst.enabled,
                "is_local": inst.is_local,
                "api_key_encrypted": inst.api_key_encrypted,
                "extra": inst.extra or {},
            }
        )
    set_instances_cache(rows)
    return rows
