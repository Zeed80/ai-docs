"""GPU VRAM Budget Manager.

Tracks memory usage across all local AI providers (Ollama, llama.cpp, vLLM)
and prevents conflicting model loads that would exceed available VRAM.

RTX 3090 default: 24 GB. Override via env GPU_TOTAL_VRAM_GB.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()

TOTAL_VRAM_GB: float = float(os.environ.get("GPU_TOTAL_VRAM_GB", "24.0"))
SAFETY_MARGIN_GB: float = 1.0   # reserve 1 GB for OS / driver overhead


@dataclass
class LoadedModel:
    name: str
    vram_gb: float
    provider: str


@dataclass
class ProviderAllocation:
    provider: str
    running: bool = False
    models: list[LoadedModel] = field(default_factory=list)
    vram_used_gb: float = 0.0
    vram_limit_gb: float | None = None   # soft limit set by user; None = unlimited
    url: str = ""


@dataclass
class GPUStats:
    total_gb: float
    used_gb: float
    free_gb: float
    driver_version: str | None = None


# ---------------------------------------------------------------------------
# nvidia-smi helpers — three strategies in priority order:
#   1. Local subprocess (works if backend has GPU device access)
#   2. Docker exec into a GPU-enabled container (Ollama, llama-server, vllm-server)
#   3. Parse Ollama /api/ps size_vram as a fallback estimate
# ---------------------------------------------------------------------------

_DOCKER_SOCK = "/var/run/docker.sock"
_GPU_CONTAINER_LABELS = [
    "com.docker.compose.service=ollama",
    "com.docker.compose.service=llama-server",
    "com.docker.compose.service=vllm-server",
]
_NVIDIA_SMI_CMD = [
    "nvidia-smi",
    "--query-gpu=memory.total,memory.used,memory.free,driver_version",
    "--format=csv,noheader,nounits",
]


def _parse_nvidia_smi_output(output: str) -> GPUStats | None:
    line = output.strip().split("\n")[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    try:
        total_mb, used_mb, free_mb = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    driver = parts[3] if len(parts) > 3 else None
    return GPUStats(
        total_gb=round(total_mb / 1024, 2),
        used_gb=round(used_mb / 1024, 2),
        free_gb=round(free_mb / 1024, 2),
        driver_version=driver,
    )


def _query_nvidia_smi_local() -> GPUStats | None:
    """Try subprocess nvidia-smi directly (works when backend has GPU device access)."""
    try:
        result = subprocess.run(
            _NVIDIA_SMI_CMD,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return _parse_nvidia_smi_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


async def _query_nvidia_smi_via_docker() -> GPUStats | None:
    """Run nvidia-smi inside a GPU-enabled container via Docker Engine API exec."""
    if not os.path.exists(_DOCKER_SOCK):
        return None
    try:
        transport = httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=8.0
        ) as client:
            # Find a GPU-enabled container
            container_id = None
            for label in _GPU_CONTAINER_LABELS:
                filters = __import__("json").dumps({"label": [label], "status": ["running"]})
                r = await client.get("/containers/json", params={"filters": filters})
                if r.status_code == 200 and r.json():
                    container_id = r.json()[0]["Id"]
                    break
            if not container_id:
                return None

            # Create exec
            exec_body = {
                "AttachStdout": True,
                "AttachStderr": True,
                "Cmd": _NVIDIA_SMI_CMD,
            }
            ec = await client.post(f"/containers/{container_id}/exec", json=exec_body)
            if ec.status_code != 201:
                return None
            exec_id = ec.json()["Id"]

            # Start exec and capture output
            start_r = await client.post(
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                headers={"Content-Type": "application/json"},
            )
            if start_r.status_code not in (200, 204):
                return None

            # Docker multiplexes stdout/stderr with an 8-byte header per frame
            raw = start_r.content
            output = ""
            i = 0
            while i + 8 <= len(raw):
                _stream_type = raw[i]  # 1=stdout, 2=stderr
                size = int.from_bytes(raw[i + 4:i + 8], "big")
                chunk = raw[i + 8:i + 8 + size].decode("utf-8", errors="replace")
                if _stream_type == 1:
                    output += chunk
                i += 8 + size

            return _parse_nvidia_smi_output(output) if output.strip() else None
    except Exception as exc:
        logger.debug("nvidia_smi_docker_exec_failed", error=str(exc))
        return None


def _query_nvidia_smi() -> GPUStats | None:
    """Local subprocess nvidia-smi (run_in_executor path)."""
    return _query_nvidia_smi_local()


# ---------------------------------------------------------------------------
# Per-provider status queries
# ---------------------------------------------------------------------------

async def _query_ollama(base_url: str, timeout: float = 5.0) -> ProviderAllocation:
    alloc = ProviderAllocation(provider="ollama", url=base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/ps")
            resp.raise_for_status()
            data = resp.json()
        alloc.running = True
        for m in data.get("models", []):
            name = m.get("name", "unknown")
            # Ollama returns size_vram in bytes
            vram_bytes = m.get("size_vram", 0)
            vram_gb = round(vram_bytes / (1024 ** 3), 2)
            alloc.models.append(LoadedModel(name=name, vram_gb=vram_gb, provider="ollama"))
            alloc.vram_used_gb += vram_gb
    except Exception as exc:
        logger.debug("ollama_ps_failed", error=str(exc))
    return alloc


async def _query_llamacpp(base_url: str, timeout: float = 5.0) -> ProviderAllocation:
    alloc = ProviderAllocation(provider="llamacpp", url=base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/props")
            resp.raise_for_status()
            props = resp.json()
        alloc.running = True
        model_path = props.get("default_generation_settings", {}).get("model", "")
        model_name = os.path.basename(model_path) if model_path else "unknown"

        # Estimate VRAM from model file size (rough heuristic for GGUF)
        vram_gb = 0.0
        models_dir = os.environ.get("LLAMACPP_MODELS_DIR", "/llamacpp-models")
        active_file = os.path.join(models_dir, ".active_model")
        if os.path.exists(active_file):
            with open(active_file) as f:
                active_path = f.read().strip()
            # Map server path (/models/...) to host path (/llamacpp-models/...)
            host_path = active_path.replace("/models/", f"{models_dir}/", 1)
            if os.path.exists(host_path):
                size_bytes = os.path.getsize(host_path)
                vram_gb = round(size_bytes / (1024 ** 3) * 0.95, 2)  # 95% of file goes to GPU

        alloc.models = [LoadedModel(name=model_name, vram_gb=vram_gb, provider="llamacpp")]
        alloc.vram_used_gb = vram_gb
    except Exception as exc:
        logger.debug("llamacpp_props_failed", error=str(exc))
    return alloc


async def _query_vllm(base_url: str, timeout: float = 5.0) -> ProviderAllocation:
    alloc = ProviderAllocation(provider="vllm", url=base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/v1/models")
            resp.raise_for_status()
            data = resp.json()
        alloc.running = True
        # vLLM doesn't expose exact VRAM — use gpu_memory_utilization from config
        gpu_util = float(os.environ.get("VLLM_GPU_MEMORY_UTILIZATION", "0.85"))
        vram_estimate = round(TOTAL_VRAM_GB * gpu_util, 2)
        for m in data.get("data", []):
            alloc.models.append(LoadedModel(
                name=m.get("id", "unknown"),
                vram_gb=vram_estimate,
                provider="vllm",
            ))
            alloc.vram_used_gb += vram_estimate
    except Exception as exc:
        logger.debug("vllm_models_failed", error=str(exc))
    return alloc


# ---------------------------------------------------------------------------
# Redis soft-limit storage
# ---------------------------------------------------------------------------

def _load_vram_limits() -> dict[str, float]:
    try:
        import json
        from app.utils.redis_client import get_sync_redis
        raw = get_sync_redis().get("gpu_vram_limits")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def save_vram_limits(limits: dict[str, float]) -> None:
    try:
        import json
        from app.utils.redis_client import get_sync_redis
        get_sync_redis().set("gpu_vram_limits", json.dumps(limits))
    except Exception as exc:
        logger.warning("gpu_vram_limits_save_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_allocations() -> dict[str, ProviderAllocation]:
    """Query all providers concurrently and return VRAM allocations."""
    ollama_url = str(settings.ollama_url).rstrip("/")
    llamacpp_url = str(settings.llamacpp_url).rstrip("/")
    vllm_url = os.environ.get("VLLM_URL", "").strip() or "http://vllm-server:8000"

    results = await asyncio.gather(
        _query_ollama(ollama_url),
        _query_llamacpp(llamacpp_url),
        _query_vllm(vllm_url),
        return_exceptions=True,
    )

    limits = _load_vram_limits()
    allocs: dict[str, ProviderAllocation] = {}
    for result in results:
        if isinstance(result, ProviderAllocation):
            result.vram_limit_gb = limits.get(result.provider)
            allocs[result.provider] = result

    return allocs


async def get_gpu_stats() -> GPUStats | None:
    """Return real GPU stats.

    Strategy:
    1. subprocess nvidia-smi (if GPU device is mounted into backend container)
    2. Docker exec nvidia-smi inside a GPU-enabled container (Ollama, llama-server…)
    """
    # Try local first (fast, synchronous)
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(None, _query_nvidia_smi_local)
    if stats:
        return stats
    # Fallback: Docker exec into GPU container
    return await _query_nvidia_smi_via_docker()


async def check_can_load(provider: str, model_vram_gb: float) -> tuple[bool, str]:
    """Check if a model can be loaded without exceeding the VRAM budget.

    Returns (can_load, reason_message).
    """
    allocs = await get_allocations()
    gpu = await get_gpu_stats()

    total = TOTAL_VRAM_GB
    if gpu:
        total = gpu.total_gb

    current_used = sum(a.vram_used_gb for a in allocs.values())
    available = total - current_used - SAFETY_MARGIN_GB

    if model_vram_gb <= 0:
        return True, ""

    if available < model_vram_gb:
        # Build advice
        largest_provider = max(allocs.values(), key=lambda a: a.vram_used_gb, default=None)
        advice = ""
        if largest_provider and largest_provider.provider != provider and largest_provider.vram_used_gb > 0:
            advice = (
                f" Выгрузите модель из {largest_provider.provider} "
                f"(~{largest_provider.vram_used_gb:.1f} GB) чтобы освободить место."
            )
        msg = (
            f"Недостаточно VRAM: занято {current_used:.1f}/{total:.0f} GB, "
            f"доступно {available:.1f} GB, нужно {model_vram_gb:.1f} GB.{advice}"
        )
        return False, msg

    # Also check provider soft limit
    alloc = allocs.get(provider)
    if alloc and alloc.vram_limit_gb is not None:
        provider_after = alloc.vram_used_gb + model_vram_gb
        if provider_after > alloc.vram_limit_gb:
            msg = (
                f"Превышен лимит VRAM для {provider}: "
                f"{provider_after:.1f} GB > {alloc.vram_limit_gb:.1f} GB (лимит)."
            )
            return False, msg

    return True, ""


# ---------------------------------------------------------------------------
# Ollama auto-management
# ---------------------------------------------------------------------------

async def unload_ollama_model(model_name: str, ollama_url: str | None = None) -> bool:
    """Unload a specific model from Ollama VRAM via keep_alive=0 API call."""
    url = (ollama_url or str(settings.ollama_url).rstrip("/"))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{url}/api/generate", json={
                "model": model_name,
                "keep_alive": 0,
                "prompt": "",
            })
            return r.status_code in (200, 204)
    except Exception as exc:
        logger.debug("ollama_unload_failed", model=model_name, error=str(exc))
        return False


async def unload_all_ollama_models(ollama_url: str | None = None) -> list[str]:
    """Unload ALL loaded Ollama models from VRAM. Returns list of unloaded model names."""
    url = (ollama_url or str(settings.ollama_url).rstrip("/"))
    unloaded: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{url}/api/ps")
            if r.status_code != 200:
                return []
            models = r.json().get("models", [])
    except Exception as exc:
        logger.debug("ollama_ps_failed_for_unload", error=str(exc))
        return []

    for m in models:
        name = m.get("name", "")
        if name and await unload_ollama_model(name, url):
            unloaded.append(name)
            logger.info("ollama_model_unloaded", model=name)

    return unloaded


async def ensure_vram_for(provider: str, model_vram_gb: float, auto_free: bool = True) -> tuple[bool, str]:
    """Check VRAM; if auto_free=True, auto-unload Ollama models to make room.

    Returns (ok, message). When auto_free triggers unloading, waits for GPU
    memory to be released before re-checking.
    """
    can_load, reason = await check_can_load(provider, model_vram_gb)
    if can_load:
        return True, ""

    if not auto_free:
        return False, reason

    # Try to free Ollama VRAM first
    allocs = await get_allocations()
    ollama = allocs.get("ollama")
    if ollama and ollama.vram_used_gb > 0:
        logger.info("auto_freeing_ollama_vram", vram_gb=ollama.vram_used_gb, for_provider=provider)
        unloaded = await unload_all_ollama_models()
        if unloaded:
            await asyncio.sleep(2.0)
            can_load2, reason2 = await check_can_load(provider, model_vram_gb)
            if can_load2:
                return True, f"auto_freed_ollama:{','.join(unloaded)}"
            return False, reason2

    return False, reason
