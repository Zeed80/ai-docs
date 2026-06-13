"""GPU VRAM Budget Manager.

Tracks memory usage across all local AI providers (Ollama, llama.cpp, vLLM)
and prevents conflicting model loads that would exceed available VRAM.

RTX 3090 default: 24 GB. Override via env GPU_TOTAL_VRAM_GB.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
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


@dataclass
class GPUTelemetry:
    """Live GPU telemetry for the UI status bar (all fields optional)."""

    name: str | None = None
    driver_version: str | None = None
    utilization_pct: float | None = None
    temp_gpu_c: float | None = None
    temp_mem_c: float | None = None            # nvidia-smi temperature.memory (N/A on GeForce)
    temp_mem_junction_c: float | None = None   # gpu-temp-helper sidecar (gddr6 BAR read)
    power_draw_w: float | None = None
    power_limit_w: float | None = None
    power_limit_min_w: float | None = None     # NVML constraints (sidecar only)
    power_limit_max_w: float | None = None
    power_limit_default_w: float | None = None
    fan_pct: float | None = None
    vram_total_gb: float | None = None
    vram_used_gb: float | None = None
    vram_free_gb: float | None = None
    clock_sm_mhz: float | None = None
    clock_mem_mhz: float | None = None
    ts: float = 0.0
    source: str = "none"                       # sidecar | docker-exec | local


@dataclass
class CPUTelemetry:
    """Host CPU telemetry from the gpu-temp-helper sidecar (sysfs/procfs)."""

    model: str | None = None
    threads: int | None = None
    utilization_pct: float | None = None
    temp_c: float | None = None
    power_draw_w: float | None = None          # RAPL package energy delta
    freq_mhz: float | None = None              # average current frequency
    freq_limit_mhz: float | None = None        # scaling_max_freq cap
    freq_hw_min_mhz: float | None = None
    freq_hw_max_mhz: float | None = None
    boost: bool | None = None
    fan_rpm: int | None = None
    fan_pct: float | None = None
    ts: float = 0.0


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


async def _docker_exec_in_gpu_container(cmd: list[str]) -> str | None:
    """Run a command inside a GPU-enabled container via Docker Engine API exec."""
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
                "Cmd": cmd,
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

            return output if output.strip() else None
    except Exception as exc:
        logger.debug("docker_exec_in_gpu_container_failed", error=str(exc))
        return None


async def _query_nvidia_smi_via_docker() -> GPUStats | None:
    """Run nvidia-smi inside a GPU-enabled container via Docker Engine API exec."""
    output = await _docker_exec_in_gpu_container(_NVIDIA_SMI_CMD)
    return _parse_nvidia_smi_output(output) if output else None


def _query_nvidia_smi() -> GPUStats | None:
    """Local subprocess nvidia-smi (run_in_executor path)."""
    return _query_nvidia_smi_local()


# ---------------------------------------------------------------------------
# GPU telemetry for the UI status bar.
# Primary source: gpu-temp-helper sidecar (NVML + GDDR6X junction temp).
# Fallback: extended nvidia-smi query (local or docker exec) without junction.
# ---------------------------------------------------------------------------

GPU_TEMP_HELPER_URL = os.environ.get("GPU_TEMP_HELPER_URL", "")
GPU_TELEMETRY_TTL_S = float(os.environ.get("GPU_TELEMETRY_TTL_S", "3.0"))

_NVIDIA_SMI_TELEMETRY_CMD = [
    "nvidia-smi",
    "--query-gpu=name,driver_version,utilization.gpu,temperature.gpu,"
    "temperature.memory,power.draw,power.limit,fan.speed,"
    "memory.total,memory.used,memory.free,clocks.sm,clocks.mem",
    "--format=csv,noheader,nounits",
]


def _opt_float(value: object) -> float | None:
    """Parse an nvidia-smi/sidecar value; '[N/A]' / 'N/A' / '' -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or "N/A" in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _mb_to_gb(value: float | None) -> float | None:
    return round(value / 1024, 2) if value is not None else None


def _parse_nvidia_smi_telemetry(output: str, source: str) -> GPUTelemetry | None:
    line = output.strip().split("\n")[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 13:
        return None
    return GPUTelemetry(
        name=parts[0] or None,
        driver_version=parts[1] or None,
        utilization_pct=_opt_float(parts[2]),
        temp_gpu_c=_opt_float(parts[3]),
        temp_mem_c=_opt_float(parts[4]),
        power_draw_w=_opt_float(parts[5]),
        power_limit_w=_opt_float(parts[6]),
        fan_pct=_opt_float(parts[7]),
        vram_total_gb=_mb_to_gb(_opt_float(parts[8])),
        vram_used_gb=_mb_to_gb(_opt_float(parts[9])),
        vram_free_gb=_mb_to_gb(_opt_float(parts[10])),
        clock_sm_mhz=_opt_float(parts[11]),
        clock_mem_mhz=_opt_float(parts[12]),
        ts=time.time(),
        source=source,
    )


def _parse_sidecar_cpu(data: dict) -> CPUTelemetry | None:
    c = data.get("cpu")
    if not c:
        return None
    threads = c.get("threads")
    return CPUTelemetry(
        model=c.get("model"),
        threads=int(threads) if threads is not None else None,
        utilization_pct=_opt_float(c.get("utilization_pct")),
        temp_c=_opt_float(c.get("temp_c")),
        power_draw_w=_opt_float(c.get("power_draw_w")),
        freq_mhz=_opt_float(c.get("freq_mhz")),
        freq_limit_mhz=_opt_float(c.get("freq_limit_mhz")),
        freq_hw_min_mhz=_opt_float(c.get("freq_hw_min_mhz")),
        freq_hw_max_mhz=_opt_float(c.get("freq_hw_max_mhz")),
        boost=c.get("boost"),
        fan_rpm=c.get("fan_rpm"),
        fan_pct=_opt_float(c.get("fan_pct")),
        ts=time.time(),
    )


async def _query_sidecar_telemetry() -> tuple[GPUTelemetry | None, CPUTelemetry | None]:
    if not GPU_TEMP_HELPER_URL:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{GPU_TEMP_HELPER_URL.rstrip('/')}/telemetry")
            if r.status_code != 200:
                return None, None
            data = r.json()
    except Exception as exc:
        logger.debug("gpu_temp_helper_unreachable", error=str(exc))
        return None, None
    cpu = _parse_sidecar_cpu(data)
    gpus = data.get("gpus") or []
    if not gpus:
        return None, cpu
    g = gpus[0]
    gpu = GPUTelemetry(
        name=g.get("name"),
        driver_version=g.get("driver_version"),
        utilization_pct=_opt_float(g.get("utilization_pct")),
        temp_gpu_c=_opt_float(g.get("temp_gpu_c")),
        temp_mem_junction_c=_opt_float(g.get("temp_mem_junction_c")),
        power_draw_w=_opt_float(g.get("power_draw_w")),
        power_limit_w=_opt_float(g.get("power_limit_w")),
        power_limit_min_w=_opt_float(g.get("power_limit_min_w")),
        power_limit_max_w=_opt_float(g.get("power_limit_max_w")),
        power_limit_default_w=_opt_float(g.get("power_limit_default_w")),
        fan_pct=_opt_float(g.get("fan_pct")),
        vram_total_gb=_mb_to_gb(_opt_float(g.get("vram_total_mb"))),
        vram_used_gb=_mb_to_gb(_opt_float(g.get("vram_used_mb"))),
        vram_free_gb=_mb_to_gb(_opt_float(g.get("vram_free_mb"))),
        clock_sm_mhz=_opt_float(g.get("clock_sm_mhz")),
        clock_mem_mhz=_opt_float(g.get("clock_mem_mhz")),
        ts=time.time(),
        source="sidecar",
    )
    return gpu, cpu


def _update_gpu_prometheus(t: GPUTelemetry) -> None:
    try:
        from app.core import metrics

        if t.utilization_pct is not None:
            metrics.gpu_utilization_percent.set(t.utilization_pct)
        if t.temp_gpu_c is not None:
            metrics.gpu_temperature_celsius.labels(sensor="gpu").set(t.temp_gpu_c)
        mem_temp = t.temp_mem_junction_c if t.temp_mem_junction_c is not None else t.temp_mem_c
        if mem_temp is not None:
            metrics.gpu_temperature_celsius.labels(sensor="mem_junction").set(mem_temp)
        if t.power_draw_w is not None:
            metrics.gpu_power_watts.labels(kind="draw").set(t.power_draw_w)
        if t.power_limit_w is not None:
            metrics.gpu_power_watts.labels(kind="limit").set(t.power_limit_w)
        if t.vram_used_gb is not None:
            metrics.gpu_vram_bytes.labels(kind="used").set(t.vram_used_gb * 1024**3)
        if t.vram_total_gb is not None:
            metrics.gpu_vram_bytes.labels(kind="total").set(t.vram_total_gb * 1024**3)
        if t.fan_pct is not None:
            metrics.gpu_fan_percent.set(t.fan_pct)
    except Exception:
        pass


def _update_cpu_prometheus(c: CPUTelemetry) -> None:
    try:
        from app.core import metrics

        if c.utilization_pct is not None:
            metrics.cpu_utilization_percent.set(c.utilization_pct)
        if c.temp_c is not None:
            metrics.cpu_temperature_celsius.set(c.temp_c)
        if c.power_draw_w is not None:
            metrics.cpu_power_watts.set(c.power_draw_w)
        if c.freq_mhz is not None:
            metrics.cpu_frequency_mhz.labels(kind="current").set(c.freq_mhz)
        if c.freq_limit_mhz is not None:
            metrics.cpu_frequency_mhz.labels(kind="limit").set(c.freq_limit_mhz)
    except Exception:
        pass


_telemetry_cache: tuple[float, GPUTelemetry | None, CPUTelemetry | None] | None = None
_telemetry_lock = asyncio.Lock()


async def get_hw_telemetry() -> tuple[GPUTelemetry | None, CPUTelemetry | None]:
    """Cached, single-flight GPU+CPU telemetry: sidecar first, nvidia-smi fallback."""
    global _telemetry_cache
    async with _telemetry_lock:
        now = time.monotonic()
        if _telemetry_cache is not None and now - _telemetry_cache[0] < GPU_TELEMETRY_TTL_S:
            return _telemetry_cache[1], _telemetry_cache[2]

        gpu, cpu = await _query_sidecar_telemetry()
        if gpu is None:
            loop = asyncio.get_running_loop()
            output = await loop.run_in_executor(
                None, _run_nvidia_smi_local_raw, _NVIDIA_SMI_TELEMETRY_CMD
            )
            if output:
                gpu = _parse_nvidia_smi_telemetry(output, source="local")
        if gpu is None:
            output = await _docker_exec_in_gpu_container(_NVIDIA_SMI_TELEMETRY_CMD)
            if output:
                gpu = _parse_nvidia_smi_telemetry(output, source="docker-exec")

        if gpu is not None:
            _update_gpu_prometheus(gpu)
        if cpu is not None:
            _update_cpu_prometheus(cpu)
        _telemetry_cache = (now, gpu, cpu)
        return gpu, cpu


async def get_gpu_telemetry() -> GPUTelemetry | None:
    """Back-compat wrapper: GPU part of the combined telemetry."""
    gpu, _cpu = await get_hw_telemetry()
    return gpu


def _run_nvidia_smi_local_raw(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


async def set_gpu_power_limit(watts: float) -> dict:
    """Set the GPU power limit via the gpu-temp-helper sidecar.

    Returns the sidecar response ({ok, power_limit_w, clamped, min_w, max_w}).
    Raises RuntimeError when the sidecar is unavailable or refuses.
    """
    global _telemetry_cache
    if not GPU_TEMP_HELPER_URL:
        raise RuntimeError("GPU_TEMP_HELPER_URL is not configured")
    from app.config import settings

    headers = (
        {"X-Power-Limit-Token": settings.agent_service_key}
        if settings.agent_service_key
        else {}
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{GPU_TEMP_HELPER_URL.rstrip('/')}/power-limit",
                json={"watts": watts},
                headers=headers,
            )
            data = r.json()
    except Exception as exc:
        raise RuntimeError(f"gpu-temp-helper unreachable: {exc}") from exc
    if r.status_code != 200 or not data.get("ok"):
        raise RuntimeError(str(data.get("error") or f"helper returned {r.status_code}"))
    logger.info(
        "gpu_power_limit_set",
        requested_w=watts,
        applied_w=data.get("power_limit_w"),
        clamped=data.get("clamped"),
    )
    async with _telemetry_lock:
        _telemetry_cache = None  # force fresh telemetry with the new limit
    return data


async def set_cpu_limit(max_freq_mhz: float | None, boost: bool | None) -> dict:
    """Cap the host CPU frequency / toggle boost via the gpu-temp-helper sidecar.

    Desktop Ryzen exposes no userspace PPT limit, so frequency capping is the
    supported way to bound CPU power draw. Returns the sidecar response
    ({ok, max_freq_mhz, boost, clamped, hw_min_mhz, hw_max_mhz}).
    """
    global _telemetry_cache
    if not GPU_TEMP_HELPER_URL:
        raise RuntimeError("GPU_TEMP_HELPER_URL is not configured")
    from app.config import settings

    headers = (
        {"X-Power-Limit-Token": settings.agent_service_key}
        if settings.agent_service_key
        else {}
    )
    payload: dict = {}
    if max_freq_mhz is not None:
        payload["max_freq_mhz"] = max_freq_mhz
    if boost is not None:
        payload["boost"] = boost
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{GPU_TEMP_HELPER_URL.rstrip('/')}/cpu-limit",
                json=payload,
                headers=headers,
            )
            data = r.json()
    except Exception as exc:
        raise RuntimeError(f"gpu-temp-helper unreachable: {exc}") from exc
    if r.status_code != 200 or not data.get("ok"):
        raise RuntimeError(str(data.get("error") or f"helper returned {r.status_code}"))
    logger.info(
        "cpu_limit_set",
        requested_mhz=max_freq_mhz,
        applied_mhz=data.get("max_freq_mhz"),
        boost=data.get("boost"),
        clamped=data.get("clamped"),
    )
    async with _telemetry_lock:
        _telemetry_cache = None  # force fresh telemetry with the new limit
    return data


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


async def unload_all_ollama_models(
    ollama_url: str | None = None, *, exclude_pinned: bool = True
) -> list[str]:
    """Unload loaded Ollama models from VRAM. Returns unloaded model names.

    By default the pinned orchestrator model is preserved so the agent keeps an
    instant response. Pass exclude_pinned=False to force-unload everything.
    """
    url = (ollama_url or str(settings.ollama_url).rstrip("/"))
    pinned: set[str] = set()
    if exclude_pinned:
        try:
            from app.ai.model_lifecycle import pinned_ollama_models
            pinned = pinned_ollama_models()
        except Exception:
            pinned = set()

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
        if not name:
            continue
        # Match against pinned by exact name or base (strip :tag) to be safe.
        base = name.split(":")[0]
        if name in pinned or base in {p.split(":")[0] for p in pinned}:
            logger.debug("ollama_keep_pinned", model=name)
            continue
        if await unload_ollama_model(name, url):
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
