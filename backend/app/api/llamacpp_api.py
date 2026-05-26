"""llama.cpp server management API — status, config, model listing."""

import json
import os
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter()
logger = structlog.get_logger()

_REDIS_KEY = "llamacpp_config"


def _get_llamacpp_base_url() -> str:
    """Return the configured llama-server base URL (runtime config overrides settings)."""
    cfg = _load_config()
    return cfg.get("url", settings.llamacpp_url).rstrip("/")


def _load_config() -> dict:
    try:
        from app.utils.redis_client import get_sync_redis
        raw = get_sync_redis().get(_REDIS_KEY)
        if raw:
            stored = json.loads(raw)
            return {**_default_config(), **stored}
    except Exception:
        pass
    return _default_config()


def _save_config(cfg: dict) -> None:
    try:
        from app.utils.redis_client import get_sync_redis
        get_sync_redis().set(_REDIS_KEY, json.dumps(cfg, ensure_ascii=False))
    except Exception as e:
        logger.warning("llamacpp_config_redis_write_failed", error=str(e))


def _default_config() -> dict:
    return {
        "url": settings.llamacpp_url,
        "model": settings.llamacpp_model,
        "ctx_size": settings.llamacpp_ctx_size,
        "kv_cache_type": settings.llamacpp_kv_cache_type,
        "n_gpu_layers": settings.llamacpp_n_gpu_layers,
        "parallel": settings.llamacpp_parallel,
        "flash_attn": settings.llamacpp_flash_attn,
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class LlamaCppConfig(BaseModel):
    url: str
    model: str
    ctx_size: int
    kv_cache_type: str
    n_gpu_layers: int
    parallel: int
    flash_attn: bool


class LlamaCppConfigUpdate(BaseModel):
    url: str | None = None
    model: str | None = None
    ctx_size: int | None = None
    kv_cache_type: str | None = None
    n_gpu_layers: int | None = None
    parallel: int | None = None
    flash_attn: bool | None = None


class LlamaCppStatus(BaseModel):
    running: bool
    url: str
    model_loaded: str | None
    ctx_size: int | None
    slots_idle: int | None
    slots_processing: int | None
    version: str | None


class GgufModel(BaseModel):
    name: str
    path: str
    size_bytes: int
    size_human: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", response_model=LlamaCppStatus, summary="llama.cpp server status")
async def get_llamacpp_status() -> LlamaCppStatus:
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/health")
            r.raise_for_status()
            health = r.json()
    except Exception:
        return LlamaCppStatus(
            running=False,
            url=base,
            model_loaded=None,
            ctx_size=None,
            slots_idle=None,
            slots_processing=None,
            version=None,
        )

    slots_idle = health.get("slots_idle")
    slots_processing = health.get("slots_processing")

    # /props gives model name + context size
    model_loaded = None
    ctx_size = None
    version = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            rp = await client.get(f"{base}/props")
            if rp.status_code == 200:
                props = rp.json()
                model_loaded = props.get("model_path") or props.get("model")
                ctx_size = props.get("n_ctx")
                version = props.get("build_info", {}).get("version") if isinstance(props.get("build_info"), dict) else None
    except Exception:
        pass

    return LlamaCppStatus(
        running=True,
        url=base,
        model_loaded=model_loaded,
        ctx_size=ctx_size,
        slots_idle=slots_idle,
        slots_processing=slots_processing,
        version=version,
    )


@router.get("/config", response_model=LlamaCppConfig, summary="llama.cpp configuration")
async def get_llamacpp_config() -> LlamaCppConfig:
    return LlamaCppConfig(**_load_config())


@router.patch("/config", response_model=LlamaCppConfig, summary="Update llama.cpp configuration")
async def update_llamacpp_config(update: LlamaCppConfigUpdate) -> LlamaCppConfig:
    cfg = _load_config()
    for field, value in update.model_dump(exclude_none=True).items():
        cfg[field] = value
    _save_config(cfg)
    logger.info("llamacpp_config_updated", changes=update.model_dump(exclude_none=True))
    return LlamaCppConfig(**cfg)


@router.get("/models", response_model=list[GgufModel], summary="List GGUF models in /models directory")
async def list_gguf_models() -> list[GgufModel]:
    """Scan /models directory (inside the llama-server container volume) for .gguf files.

    In Docker Compose, the backend container doesn't mount the llamacpp_models volume,
    so this endpoint falls back to listing model paths known to the config. In a setup
    where the models directory is shared (bind mount), this will enumerate all .gguf files.
    """
    models_dir = Path(os.environ.get("LLAMACPP_MODELS_DIR", "/models"))
    result: list[GgufModel] = []

    if models_dir.exists():
        for p in sorted(models_dir.rglob("*.gguf")):
            size = p.stat().st_size
            result.append(GgufModel(
                name=p.name,
                path=str(p),
                size_bytes=size,
                size_human=_human_size(size),
            ))

    if not result:
        # Fallback: show the currently configured model path if non-default
        cfg = _load_config()
        model_path = cfg.get("model", "")
        if model_path and model_path != "/models/model.gguf":
            result.append(GgufModel(
                name=Path(model_path).name,
                path=model_path,
                size_bytes=0,
                size_human="unknown",
            ))

    return result


@router.get("/slots", summary="llama.cpp active inference slots")
async def get_llamacpp_slots() -> dict:
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/slots")
            r.raise_for_status()
            return {"slots": r.json()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"llama-server unavailable: {e}")


@router.get("/metrics", summary="llama.cpp Prometheus metrics (raw text)")
async def get_llamacpp_metrics():
    """Proxy /metrics from llama-server (enabled with --metrics flag)."""
    from fastapi.responses import PlainTextResponse
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/metrics")
            return PlainTextResponse(r.text, status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"llama-server unavailable: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"
