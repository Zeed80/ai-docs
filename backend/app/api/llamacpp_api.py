"""llama.cpp server management API — status, config, model listing and download."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import AsyncIterator

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings

router = APIRouter()
logger = structlog.get_logger()

_REDIS_KEY = "llamacpp_config"
_MODELS_DIR = Path(os.environ.get("LLAMACPP_MODELS_DIR", "/llamacpp-models"))

# Well-known GGUF models — user can one-click download these
GGUF_CATALOG: list[dict] = [
    {
        "id": "qwen2.5-3b-q8_0",
        "name": "Qwen2.5 3B",
        "description": "Быстрый компактный, хорош для классификации и коротких задач",
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "filename": "qwen2.5-3b-instruct-q8_0.gguf",
        "size_human": "3.6 GB",
        "quant": "Q8_0",
        "params": "3B",
        "ctx": 32768,
        "tags": ["fast", "classification"],
    },
    {
        "id": "qwen2.5-7b-q8_0",
        "name": "Qwen2.5 7B",
        "description": "Сбалансированная модель — reasoning + русский язык, отличный выбор для старта",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q8_0.gguf",
        "size_human": "8.1 GB",
        "quant": "Q8_0",
        "params": "7B",
        "ctx": 32768,
        "tags": ["balanced", "russian", "recommended"],
    },
    {
        "id": "qwen2.5-14b-q4_k_m",
        "name": "Qwen2.5 14B Q4_K_M",
        "description": "Мощная модель с хорошим балансом качество/память",
        "repo": "Qwen/Qwen2.5-14B-Instruct-GGUF",
        "filename": "qwen2.5-14b-instruct-q4_k_m.gguf",
        "size_human": "9.0 GB",
        "quant": "Q4_K_M",
        "params": "14B",
        "ctx": 32768,
        "tags": ["powerful", "russian"],
    },
    {
        "id": "llama3.2-3b-q8_0",
        "name": "Llama 3.2 3B",
        "description": "Быстрый Llama, хорош для инструкций",
        "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "filename": "Llama-3.2-3B-Instruct-Q8_0.gguf",
        "size_human": "3.4 GB",
        "quant": "Q8_0",
        "params": "3B",
        "ctx": 131072,
        "tags": ["fast", "long-context"],
    },
    {
        "id": "llama3.1-8b-q8_0",
        "name": "Llama 3.1 8B",
        "description": "Популярная модель с длинным контекстом 128K",
        "repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "filename": "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf",
        "size_human": "8.5 GB",
        "quant": "Q8_0",
        "params": "8B",
        "ctx": 131072,
        "tags": ["popular", "long-context"],
    },
    {
        "id": "gemma2-2b-q8_0",
        "name": "Gemma 2 2B",
        "description": "Ультракомпактная модель Google, быстрый старт",
        "repo": "bartowski/gemma-2-2b-it-GGUF",
        "filename": "gemma-2-2b-it-Q8_0.gguf",
        "size_human": "2.8 GB",
        "quant": "Q8_0",
        "params": "2B",
        "ctx": 8192,
        "tags": ["tiny", "fast"],
    },
    {
        "id": "mistral-7b-q8_0",
        "name": "Mistral 7B v0.3",
        "description": "Классическая модель, хорошо протестирована",
        "repo": "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "filename": "Mistral-7B-Instruct-v0.3-Q8_0.gguf",
        "size_human": "8.1 GB",
        "quant": "Q8_0",
        "params": "7B",
        "ctx": 32768,
        "tags": ["classic", "reliable"],
    },
    {
        "id": "phi3.5-mini-q8_0",
        "name": "Phi-3.5 Mini 3.8B",
        "description": "Компактная модель Microsoft, хороший reasoning для своего размера",
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "filename": "Phi-3.5-mini-instruct-Q8_0.gguf",
        "size_human": "4.1 GB",
        "quant": "Q8_0",
        "params": "3.8B",
        "ctx": 131072,
        "tags": ["microsoft", "reasoning"],
    },
]

# Active downloads: model_id -> {progress, total, done, error}
_downloads: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_llamacpp_base_url() -> str:
    cfg = _load_config()
    return cfg.get("url", settings.llamacpp_url).rstrip("/")


def _load_config() -> dict:
    try:
        from app.utils.redis_client import get_sync_redis
        raw = get_sync_redis().get(_REDIS_KEY)
        if raw:
            return {**_default_config(), **json.loads(raw)}
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


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _hf_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


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
    kv_cache_type: str | None


class GgufModel(BaseModel):
    name: str
    path: str
    size_bytes: int
    size_human: str
    active: bool = False


class CatalogEntry(BaseModel):
    id: str
    name: str
    description: str
    repo: str
    filename: str
    size_human: str
    quant: str
    params: str
    ctx: int
    tags: list[str]
    downloaded: bool = False
    local_path: str | None = None


class DownloadRequest(BaseModel):
    model_id: str
    url: str | None = None  # override download URL


class DownloadStatus(BaseModel):
    model_id: str
    status: str  # pending | downloading | done | error
    progress_bytes: int
    total_bytes: int
    progress_pct: float
    error: str | None


# ── Status & Config endpoints ─────────────────────────────────────────────────

@router.get("/status", response_model=LlamaCppStatus, summary="llama.cpp server status")
async def get_llamacpp_status() -> LlamaCppStatus:
    base = _get_llamacpp_base_url()
    cfg = _load_config()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/health")
            r.raise_for_status()
            health = r.json()
    except Exception:
        return LlamaCppStatus(
            running=False, url=base, model_loaded=None, ctx_size=None,
            slots_idle=None, slots_processing=None, version=None,
            kv_cache_type=cfg.get("kv_cache_type"),
        )

    slots_idle = health.get("slots_idle")
    slots_processing = health.get("slots_processing")
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
                bi = props.get("build_info")
                version = bi.get("version") if isinstance(bi, dict) else None
    except Exception:
        pass

    return LlamaCppStatus(
        running=True, url=base, model_loaded=model_loaded, ctx_size=ctx_size,
        slots_idle=slots_idle, slots_processing=slots_processing, version=version,
        kv_cache_type=cfg.get("kv_cache_type"),
    )


@router.get("/config", response_model=LlamaCppConfig, summary="Current llama.cpp config")
async def get_llamacpp_config() -> LlamaCppConfig:
    return LlamaCppConfig(**_load_config())


@router.patch("/config", response_model=LlamaCppConfig, summary="Update llama.cpp config")
async def update_llamacpp_config(update: LlamaCppConfigUpdate) -> LlamaCppConfig:
    cfg = _load_config()
    for field, value in update.model_dump(exclude_none=True).items():
        cfg[field] = value
    _save_config(cfg)
    logger.info("llamacpp_config_updated", changes=update.model_dump(exclude_none=True))
    return LlamaCppConfig(**cfg)


# ── Model file management ─────────────────────────────────────────────────────

@router.get("/models", response_model=list[GgufModel], summary="List local GGUF models")
async def list_gguf_models() -> list[GgufModel]:
    active_path = _load_config().get("model", "")
    result: list[GgufModel] = []

    if _MODELS_DIR.exists():
        for p in sorted(_MODELS_DIR.rglob("*.gguf")):
            size = p.stat().st_size
            result.append(GgufModel(
                name=p.name,
                path=str(p),
                size_bytes=size,
                size_human=_human_size(size),
                active=(str(p) == active_path),
            ))

    return result


@router.delete("/models/{filename}", summary="Delete a local GGUF model")
async def delete_gguf_model(filename: str) -> dict:
    if not re.match(r'^[\w\-\.]+\.gguf$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = _MODELS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Model not found")
    path.unlink()
    logger.info("llamacpp_model_deleted", filename=filename)
    return {"deleted": filename}


@router.post("/models/activate", response_model=LlamaCppConfig, summary="Set active model")
async def activate_model(body: dict) -> LlamaCppConfig:
    path = body.get("path", "")
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    cfg = _load_config()
    cfg["model"] = path
    _save_config(cfg)
    logger.info("llamacpp_model_activated", path=path)
    return LlamaCppConfig(**cfg)


# ── Catalog & Download ────────────────────────────────────────────────────────

@router.get("/catalog", response_model=list[CatalogEntry], summary="GGUF model catalog")
async def get_catalog() -> list[CatalogEntry]:
    result = []
    for entry in GGUF_CATALOG:
        local_path = _MODELS_DIR / entry["filename"]
        result.append(CatalogEntry(
            **entry,
            downloaded=local_path.exists(),
            local_path=str(local_path) if local_path.exists() else None,
        ))
    return result


@router.post("/download", summary="Start downloading a GGUF model")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks) -> dict:
    model_id = req.model_id
    if model_id in _downloads and _downloads[model_id].get("status") == "downloading":
        return {"message": "Already downloading", "model_id": model_id}

    # Find catalog entry or use custom URL
    catalog_entry = next((e for e in GGUF_CATALOG if e["id"] == model_id), None)
    if catalog_entry:
        url = req.url or _hf_url(catalog_entry["repo"], catalog_entry["filename"])
        dest_name = catalog_entry["filename"]
    elif req.url:
        url = req.url
        dest_name = Path(req.url).name
        if not dest_name.endswith(".gguf"):
            dest_name += ".gguf"
    else:
        raise HTTPException(status_code=404, detail=f"Unknown model_id '{model_id}' and no URL provided")

    _downloads[model_id] = {"status": "pending", "progress_bytes": 0, "total_bytes": 0, "error": None}
    background_tasks.add_task(_download_model, model_id, url, dest_name)
    return {"message": "Download started", "model_id": model_id, "url": url, "dest": dest_name}


@router.delete("/download/{model_id}", summary="Cancel an active download")
async def cancel_download(model_id: str) -> dict:
    if model_id in _downloads:
        _downloads[model_id]["status"] = "cancelled"
    return {"cancelled": model_id}


@router.get("/download/{model_id}/status", response_model=DownloadStatus, summary="Download progress")
async def get_download_status(model_id: str) -> DownloadStatus:
    d = _downloads.get(model_id)
    if not d:
        raise HTTPException(status_code=404, detail="No download found for this model_id")
    total = d.get("total_bytes", 0)
    progress = d.get("progress_bytes", 0)
    pct = round(progress / total * 100, 1) if total > 0 else 0.0
    return DownloadStatus(
        model_id=model_id,
        status=d.get("status", "unknown"),
        progress_bytes=progress,
        total_bytes=total,
        progress_pct=pct,
        error=d.get("error"),
    )


@router.get("/download/{model_id}/stream", summary="SSE stream of download progress")
async def stream_download_progress(model_id: str):
    """Server-Sent Events stream: sends progress updates every 500ms until done."""
    async def _gen() -> AsyncIterator[str]:
        while True:
            d = _downloads.get(model_id)
            if not d:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break
            total = d.get("total_bytes", 0)
            progress = d.get("progress_bytes", 0)
            pct = round(progress / total * 100, 1) if total > 0 else 0.0
            payload = {
                "status": d.get("status"),
                "progress_bytes": progress,
                "total_bytes": total,
                "progress_pct": pct,
                "error": d.get("error"),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if d.get("status") in ("done", "error", "cancelled"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_gen(), media_type="text/event-stream")


async def _download_model(model_id: str, url: str, dest_name: str) -> None:
    """Background task: stream-download a GGUF file to _MODELS_DIR."""
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _MODELS_DIR / dest_name
    tmp = _MODELS_DIR / f"{dest_name}.tmp"

    _downloads[model_id] = {"status": "downloading", "progress_bytes": 0, "total_bytes": 0, "error": None}
    logger.info("llamacpp_download_start", model_id=model_id, url=url, dest=str(dest))

    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "llama-manager/1.0"}) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} from {url}")
                total = int(resp.headers.get("content-length", 0))
                _downloads[model_id]["total_bytes"] = total
                downloaded = 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                        if _downloads.get(model_id, {}).get("status") == "cancelled":
                            logger.info("llamacpp_download_cancelled", model_id=model_id)
                            tmp.unlink(missing_ok=True)
                            return
                        f.write(chunk)
                        downloaded += len(chunk)
                        _downloads[model_id]["progress_bytes"] = downloaded

        shutil.move(str(tmp), str(dest))
        _downloads[model_id]["status"] = "done"
        logger.info("llamacpp_download_done", model_id=model_id, path=str(dest))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        _downloads[model_id]["status"] = "error"
        _downloads[model_id]["error"] = str(e)
        logger.error("llamacpp_download_failed", model_id=model_id, error=str(e))


# ── Inference proxy / diagnostics ────────────────────────────────────────────

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


@router.get("/metrics", summary="llama.cpp Prometheus metrics")
async def get_llamacpp_metrics():
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/metrics")
            return PlainTextResponse(r.text, status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"llama-server unavailable: {e}")


@router.post("/test", summary="Quick test generation on running llama-server")
async def test_generation() -> dict:
    """Send a minimal prompt to verify the model is responding."""
    base = _get_llamacpp_base_url()
    payload = {
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{base}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            return {"ok": True, "response": text, "model": data.get("model")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
