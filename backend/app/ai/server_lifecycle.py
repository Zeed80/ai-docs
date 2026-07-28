"""On-demand lifecycle for container-bound model servers (vLLM, llama.cpp).

Unlike Ollama (which loads/unloads models dynamically via ``keep_alive``), vLLM
and llama.cpp pin a single model in VRAM for the whole container lifetime. On a
single-GPU box this wastes VRAM whenever those servers idle.

Policy (see model_lifecycle for the Ollama side):
  - The agent orchestrator stays resident in Ollama; nothing here touches it.
  - vLLM / llama.cpp servers are started on demand when a request routes to them
    (``ensure_running``) and stopped after an idle period (``stop_idle_servers``,
    driven by a background loop in the backend, which has the Docker socket).

Server control goes through the Docker socket (``/var/run/docker.sock``), the
same mechanism the manual ``/server/{action}`` endpoint uses. ``mark_used``
records the last-use timestamp in Redis so the idle sweep is shared across
processes.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import structlog

logger = structlog.get_logger()

# Providers managed here (container-bound, single resident model).
MANAGED_PROVIDERS = ("vllm", "llamacpp")

_DOCKER_SOCK = "/var/run/docker.sock"
_LAST_USED_KEY = "model_server:last_used"  # Redis hash provider -> epoch seconds

# Docker compose service name per provider (override via env).
SERVICE_NAMES = {
    "llamacpp": os.environ.get("LLAMACPP_SERVICE_NAME", "llama-server"),
    "vllm": os.environ.get("VLLM_SERVICE_NAME", "vllm-server"),
    "ollama": os.environ.get("OLLAMA_SERVICE_NAME", "ollama"),
}


def idle_timeout_seconds() -> float:
    """Idle period after which a managed server is stopped."""
    try:
        return float(os.environ.get("MODEL_SERVER_IDLE_TIMEOUT_SECONDS", "600"))
    except ValueError:
        return 600.0


def on_demand_enabled() -> bool:
    return os.environ.get("MODEL_SERVER_ON_DEMAND", "true").strip().lower() != "false"


def assigned_providers() -> set[str]:
    """Provider kinds that some task is actually routed to.

    A vLLM or llama.cpp container pins its model in VRAM for its whole lifetime,
    so one that nothing is assigned to is pure loss: on a single-GPU box the
    llama.cpp server alone held 22 of 24 GB while no routing referred to it.

    Only PRIMARY assignments count. A fallback entry means "if the assigned
    model fails, try this one" — it is not a decision to keep a second model
    resident, and treating it as one would keep both servers up for ever.

    Returns an empty set only when the routing store cannot be read at all; the
    caller must treat that as "unknown" rather than "nothing is assigned", or a
    Redis hiccup would stop the servers a running system depends on.
    """
    from app.ai.model_registry import ModelRegistry
    from app.ai.task_routing import get_task_routing

    registry = ModelRegistry.from_yaml("app/ai/config/model_registry.yaml")
    providers: set[str] = set()
    for routing in get_task_routing().values():
        key = routing.primary
        if not key:
            continue
        capability = registry.models.get(key)
        if capability is not None:
            providers.add(capability.provider.value)
    return providers


def _assignment_state() -> set[str] | None:
    """``assigned_providers()`` but None when it cannot be determined."""
    try:
        return assigned_providers()
    except Exception as exc:  # noqa: BLE001 — unknown is not "nothing"
        logger.warning("model_assignments_unreadable", error=str(exc)[:200])
        return None


def _health_url(provider: str) -> str | None:
    from app.config import settings

    if provider == "vllm":
        base = os.environ.get("VLLM_URL", "").strip() or "http://vllm-server:8000"
        return f"{base.rstrip('/').removesuffix('/v1')}/health"
    if provider == "llamacpp":
        return f"{str(settings.llamacpp_url).rstrip('/').removesuffix('/v1')}/health"
    return None


# ---------------------------------------------------------------------------
# Redis last-use tracking
# ---------------------------------------------------------------------------

def mark_used(provider: str) -> None:
    if provider not in MANAGED_PROVIDERS:
        return
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().hset(_LAST_USED_KEY, provider, str(time.time()))
    except Exception:
        pass


def last_used(provider: str) -> float | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().hget(_LAST_USED_KEY, provider)
        return float(raw) if raw else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Docker socket control
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    return os.path.exists(_DOCKER_SOCK)


async def _find_container(service: str) -> dict | None:
    filters = json.dumps({"label": [f"com.docker.compose.service={service}"]})
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK), base_url="http://docker"
    ) as client:
        r = await client.get("/containers/json", params={"filters": filters, "all": "true"})
        r.raise_for_status()
        containers = r.json()
        return containers[0] if containers else None


async def is_running(provider: str) -> bool:
    service = SERVICE_NAMES.get(provider)
    if not service or not _docker_available():
        return False
    try:
        c = await _find_container(service)
        return bool(c and c.get("State") == "running")
    except Exception as exc:
        logger.debug("server_state_check_failed", provider=provider, error=str(exc))
        return False


async def _docker_action(provider: str, action: str) -> bool:
    """start | stop | restart the provider's container. Returns success."""
    service = SERVICE_NAMES.get(provider)
    if not service or not _docker_available():
        return False
    try:
        c = await _find_container(service)
        if not c:
            logger.warning("server_container_not_found", provider=provider, service=service)
            return False
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK), base_url="http://docker"
        ) as client:
            resp = await client.post(f"/containers/{c['Id']}/{action}", params={"t": "10"})
            # 204 = done, 304 = already in target state.
            return resp.status_code in (204, 304)
    except Exception as exc:
        logger.warning("server_docker_action_failed", provider=provider, action=action, error=str(exc))
        return False


async def _wait_healthy(provider: str, timeout: float = 240.0) -> bool:
    url = _health_url(provider)
    if not url:
        return True
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            import asyncio

            await asyncio.sleep(3.0)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ensure_running(provider: str, *, wait: bool = True) -> bool:
    """Make sure a managed server is up before routing a request to it.

    Records the use timestamp regardless so the idle sweep doesn't stop a server
    that is actively being driven. No-op for unmanaged providers (e.g. ollama).
    """
    if provider not in MANAGED_PROVIDERS:
        return True
    # A request routed here means something is assigned to this provider — but
    # check anyway: a stale fallback chain or a hand-picked model can address a
    # server nobody assigned, and starting it costs the whole GPU.
    assigned = _assignment_state()
    if assigned is not None and provider not in assigned:
        logger.info("model_server_not_assigned", provider=provider)
        return False
    mark_used(provider)
    if not on_demand_enabled():
        return True
    if not _docker_available():
        # No socket here (e.g. a worker without the mount) — assume the server is
        # managed elsewhere; the request will fail and fall back if it is down.
        return False
    if await is_running(provider):
        return True
    logger.info("model_server_starting", provider=provider)
    if not await _docker_action(provider, "start"):
        return False
    ok = await _wait_healthy(provider) if wait else True
    logger.info("model_server_started", provider=provider, healthy=ok)
    mark_used(provider)
    return ok


async def _is_healthy(provider: str) -> bool:
    """One-shot /health probe (no polling)."""
    url = _health_url(provider)
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
            return r.status_code == 200
    except Exception:
        return False


async def stop_server(provider: str) -> bool:
    if provider not in MANAGED_PROVIDERS:
        return False
    logger.info("model_server_stopping", provider=provider)
    return await _docker_action(provider, "stop")


async def stop_unassigned_servers() -> list[str]:
    """Stop managed servers nothing is routed to, without waiting for idle.

    ``docker compose up`` starts every service in the active profile, so both
    model servers come up whether or not any task is assigned to them — and
    then hold VRAM until the idle sweep gets round to them. A server nobody
    assigned is not idle, it is unwanted, and waiting ten minutes to say so
    means ten minutes of a GPU that the assigned models needed.

    Called at startup and on every sweep, so a model UNASSIGNED in the settings
    frees its server on the next pass rather than at the next restart.
    """
    if not _docker_available():
        return []
    assigned = _assignment_state()
    if assigned is None:
        return []
    stopped: list[str] = []
    for provider in MANAGED_PROVIDERS:
        if provider in assigned:
            continue
        if not await is_running(provider):
            continue
        logger.info("unassigned_model_server_stopping", provider=provider)
        if await stop_server(provider):
            stopped.append(provider)
    if stopped:
        logger.info("unassigned_model_servers_stopped", servers=stopped)
    return stopped


async def stop_idle_servers(threshold_seconds: float | None = None) -> list[str]:
    """Stop managed servers idle longer than the threshold. Returns stopped ones."""
    if not _docker_available():
        return []
    # An unassigned server goes regardless of the idle clock and regardless of
    # whether on-demand starting is switched off: nothing is routed to it.
    stopped: list[str] = list(await stop_unassigned_servers())
    if not on_demand_enabled():
        return stopped
    threshold = threshold_seconds if threshold_seconds is not None else idle_timeout_seconds()
    now = time.time()
    for provider in MANAGED_PROVIDERS:
        if provider in stopped:
            continue
        if not await is_running(provider):
            continue
        used = last_used(provider)
        # No recorded use → treat "now" as first sighting so we don't kill a
        # server mid-startup; record and skip this round.
        if used is None:
            mark_used(provider)
            continue
        if now - used >= threshold:
            # A container in "running" state may still be LOADING its model (a big
            # vision model takes minutes and bumps no last_used until it serves a
            # request). Reaping it then SIGKILLs the load (exit 137). Only reap a
            # server that is actually serving (healthy); a not-yet-healthy one is
            # starting up, so reset its clock and leave it alone this round.
            if not await _is_healthy(provider):
                mark_used(provider)
                continue
            if await stop_server(provider):
                stopped.append(provider)
    if stopped:
        logger.info("idle_model_servers_stopped", servers=stopped)
    return stopped
