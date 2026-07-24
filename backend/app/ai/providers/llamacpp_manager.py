"""llama.cpp server manager — status, config, model search (HF/ModelScope), download.

Internal provider module (no public router). Exposed to the rest of the app only
through ``app.api.local_models_api``, which imports these helpers directly.
Symmetric with ``app.ai.providers.vllm_manager``.
"""

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
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings

logger = structlog.get_logger()

_REDIS_KEY = "llamacpp_config"
_TOKENS_KEY = "llamacpp_tokens"
_MODELS_DIR = Path(os.environ.get("LLAMACPP_MODELS_DIR", "/llamacpp-models"))
# File written by activate_model so llama-server picks up the new model on restart.
# Uses /models/ path (inside llama-server container), not the backend's /llamacpp-models/.
_ACTIVE_MODEL_FILE = _MODELS_DIR / ".active_model"
# Path prefix inside the llama-server container (mapped from the same llamacpp_models volume).
_SERVER_MODELS_PREFIX = "/models"
_DOCKER_SOCK = "/var/run/docker.sock"

HF_API = "https://huggingface.co/api"
MS_API = "https://modelscope.cn/api/v1"

# Active downloads: download_id -> progress dict
_downloads: dict[str, dict] = {}


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_get(key: str) -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis
        raw = get_sync_redis().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set(key: str, value: dict) -> None:
    try:
        from app.utils.redis_client import get_sync_redis
        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False))
    except Exception as e:
        logger.warning("redis_write_failed", key=key, error=str(e))


def _load_config() -> dict:
    stored = _redis_get(_REDIS_KEY)
    return {**_default_config(), **(stored or {})}


def _save_config(cfg: dict) -> None:
    _redis_set(_REDIS_KEY, cfg)


def _load_tokens() -> dict:
    return _redis_get(_TOKENS_KEY) or {}


def _save_tokens(tokens: dict) -> None:
    _redis_set(_TOKENS_KEY, tokens)


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


def _get_llamacpp_base_url() -> str:
    return _load_config().get("url", settings.llamacpp_url).rstrip("/")


def _hf_headers() -> dict:
    token = _load_tokens().get("huggingface", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _ms_headers() -> dict:
    token = _load_tokens().get("modelscope", "")
    h = {"User-Agent": "llama-manager/1.0"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


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
    vision: bool = False          # True when mmproj is loaded and vision=true in /props
    mmproj_path: str | None = None  # path of the loaded mmproj file (from /props)


class GgufModel(BaseModel):
    name: str
    path: str
    size_bytes: int
    size_human: str
    active: bool = False
    is_mmproj: bool = False   # True for mmproj-*.gguf vision projector files


class TokensUpdate(BaseModel):
    huggingface: str | None = None
    modelscope: str | None = None


class TokensStatus(BaseModel):
    huggingface_set: bool
    modelscope_set: bool


class HFFile(BaseModel):
    filename: str
    size_bytes: int
    size_human: str
    quant: str                # detected quant type (Q4_K_M, Q8_0, F16, …)
    is_split: bool            # multi-part file
    split_group: str | None = None   # e.g. "q8_0" — all parts share same group
    part_index: int | None = None    # 1-based part number
    total_parts: int | None = None


class HFModelResult(BaseModel):
    repo_id: str
    author: str
    model_name: str
    downloads: int
    likes: int
    tags: list[str]
    gated: bool
    files: list[HFFile] = []


class MSModelResult(BaseModel):
    repo_id: str
    name: str
    downloads: int
    stars: int
    tags: list[str]
    files: list[HFFile] = []


class DownloadRequest(BaseModel):
    repo_id: str
    filename: str
    source: str = "huggingface"   # huggingface | modelscope | url
    url: str | None = None        # override download URL


class DownloadStatus(BaseModel):
    download_id: str
    repo_id: str
    filename: str
    status: str
    progress_bytes: int
    total_bytes: int
    progress_pct: float
    error: str | None


class ActivateModelResponse(BaseModel):
    status: str           # "ok" | "restarting" | "no_docker" | "error"
    model: str            # backend path that was activated
    server_path: str      # path as seen inside llama-server container
    message: str
    server_running: bool = False


# ── Docker helpers ────────────────────────────────────────────────────────────

def _backend_to_server_path(backend_path: str) -> str:
    """Translate /llamacpp-models/foo.gguf → /models/foo.gguf (llama-server view)."""
    p = Path(backend_path)
    try:
        rel = p.relative_to(_MODELS_DIR)
    except ValueError:
        rel = Path(p.name)
    return str(Path(_SERVER_MODELS_PREFIX) / rel)


async def _docker_find_container(service_name: str) -> str | None:
    """Return container ID for the given compose service name, or None."""
    if not Path(_DOCKER_SOCK).exists():
        return None
    try:
        transport = httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK)
        filters = json.dumps({"label": [f"com.docker.compose.service={service_name}"]})
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=5.0
        ) as client:
            # all=true is required: a profile-gated ML server is usually STOPPED
            # between activations, and Docker's default /containers/json lists
            # only running containers. Without it activate could not restart a
            # stopped llama-server — it would report the service as "not found".
            r = await client.get(
                "/containers/json", params={"filters": filters, "all": "true"}
            )
            if r.status_code == 200:
                containers = r.json()
                if containers:
                    return containers[0]["Id"]
    except Exception as exc:
        logger.warning("docker_find_container_failed", service=service_name, error=str(exc))
    return None


async def _docker_restart_container(container_id: str, stop_timeout: int = 5) -> None:
    """Gracefully stop then start a container via Docker Engine API."""
    transport = httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", timeout=30.0
    ) as client:
        r = await client.post(
            f"/containers/{container_id}/restart",
            params={"t": str(stop_timeout)},
        )
        r.raise_for_status()


async def _wait_llama_healthy(url: str, timeout: int = 120) -> bool:
    """Poll /health until 200 or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{url}/health")
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2.0)
    return False


async def ensure_server_running() -> dict:
    """Idempotent auto-start used when a llama.cpp model is assigned as a provider.

    Starts llama-server (with its currently-active GGUF, chosen via the model
    management activate flow) only when it isn't already healthy. No-op if the
    server is already up, so re-applying an assignment never interrupts it.
    """
    base = _get_llamacpp_base_url().rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/health")
            if r.status_code == 200:
                return {"status": "already_running"}
    except Exception:
        pass
    if not Path(_DOCKER_SOCK).exists():
        return {"status": "no_docker"}
    service_name = os.environ.get("LLAMACPP_SERVICE_NAME", "llama-server")
    container_id = await _docker_find_container(service_name)
    if not container_id:
        return {"status": "not_found"}
    await _docker_restart_container(container_id)
    healthy = await _wait_llama_healthy(base)
    return {"status": "started" if healthy else "start_timeout"}


# ── Status & Config ───────────────────────────────────────────────────────────

async def get_llamacpp_status() -> LlamaCppStatus:
    base = _get_llamacpp_base_url()
    cfg = _load_config()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/health")
            r.raise_for_status()
            health = r.json()
    except Exception:
        return LlamaCppStatus(running=False, url=base, model_loaded=None, ctx_size=None,
                              slots_idle=None, slots_processing=None, version=None,
                              kv_cache_type=cfg.get("kv_cache_type"))

    model_loaded = None
    ctx_size = None
    version = None
    slots_idle = None
    slots_processing = None
    vision = False
    mmproj_path = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            # props: model path, vision flag, mmproj path
            rp = await client.get(f"{base}/props")
            if rp.status_code == 200:
                props = rp.json()
                model_loaded = props.get("model_path") or props.get("model_alias")
                bi = props.get("build_info")
                version = bi.get("version") if isinstance(bi, dict) else None
                # newer llama.cpp: modalities.vision; older: top-level vision bool
                modalities = props.get("modalities") or {}
                vision = bool(modalities.get("vision") or props.get("vision"))
                mmproj_path = props.get("mmproj_path") or None

            # v1/models: n_ctx from meta
            rm = await client.get(f"{base}/v1/models")
            if rm.status_code == 200:
                models_data = (rm.json().get("data") or [])
                if models_data:
                    ctx_size = (models_data[0].get("meta") or {}).get("n_ctx")

            # slots: idle/processing count
            rs = await client.get(f"{base}/slots")
            if rs.status_code == 200:
                slots = rs.json() or []
                slots_idle = sum(1 for s in slots if not s.get("is_processing"))
                slots_processing = sum(1 for s in slots if s.get("is_processing"))
    except Exception:
        pass

    return LlamaCppStatus(running=True, url=base, model_loaded=model_loaded, ctx_size=ctx_size,
                          slots_idle=slots_idle, slots_processing=slots_processing,
                          version=version, kv_cache_type=cfg.get("kv_cache_type"),
                          vision=vision, mmproj_path=mmproj_path)


async def get_llamacpp_config() -> LlamaCppConfig:
    return LlamaCppConfig(**_load_config())


async def update_llamacpp_config(update: LlamaCppConfigUpdate) -> LlamaCppConfig:
    cfg = _load_config()
    for field, value in update.model_dump(exclude_none=True).items():
        cfg[field] = value
    _save_config(cfg)
    return LlamaCppConfig(**cfg)


# ── Token management ──────────────────────────────────────────────────────────

async def get_tokens_status() -> TokensStatus:
    t = _load_tokens()
    return TokensStatus(huggingface_set=bool(t.get("huggingface")),
                        modelscope_set=bool(t.get("modelscope")))


async def update_tokens(update: TokensUpdate) -> TokensStatus:
    t = _load_tokens()
    if update.huggingface is not None:
        t["huggingface"] = update.huggingface.strip()
    if update.modelscope is not None:
        t["modelscope"] = update.modelscope.strip()
    _save_tokens(t)
    return TokensStatus(huggingface_set=bool(t.get("huggingface")),
                        modelscope_set=bool(t.get("modelscope")))


async def delete_token(provider: str) -> TokensStatus:
    if provider not in ("huggingface", "modelscope"):
        raise HTTPException(status_code=400, detail="Unknown provider")
    t = _load_tokens()
    t.pop(provider, None)
    _save_tokens(t)
    return TokensStatus(huggingface_set=bool(t.get("huggingface")),
                        modelscope_set=bool(t.get("modelscope")))


# ── HuggingFace search ────────────────────────────────────────────────────────

def _detect_quant(filename: str) -> str:
    """Detect quantization from GGUF filename."""
    fn = filename.upper()
    for q in ("Q2_K_S", "Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L", "Q3_K",
              "Q4_0", "Q4_1", "Q4_K_S", "Q4_K_M", "Q4_K_L", "Q4_K",
              "Q5_0", "Q5_1", "Q5_K_S", "Q5_K_M", "Q5_K",
              "Q6_K", "Q8_0", "F16", "BF16", "F32",
              "IQ1_S", "IQ2_S", "IQ2_M", "IQ3_S", "IQ3_M", "IQ4_XS", "IQ4_NL"):
        if q in fn:
            return q
    return "UNKNOWN"


def _parse_hf_file(f: dict) -> HFFile:
    fn = f.get("rfilename", "")
    size = f.get("size") or 0
    m = re.search(r'-(\d+)-of-(\d+)', fn)
    is_split = m is not None
    part_index = int(m.group(1)) if m else None
    total_parts = int(m.group(2)) if m else None
    # group key = base name without -XXXXX-of-YYYYY part
    split_group = re.sub(r'-\d+-of-\d+', '', fn).rstrip(".gguf").lower() if is_split else None
    return HFFile(filename=fn, size_bytes=size, size_human=_human_size(size),
                  quant=_detect_quant(fn), is_split=is_split,
                  split_group=split_group, part_index=part_index, total_parts=total_parts)


async def search_hf_models(
    q: str = Query(..., min_length=1),
    quant: str | None = Query(None, description="Filter: Q4_K_M, Q8_0, F16 …"),
    min_gb: float = Query(0, ge=0),
    max_gb: float = Query(100, le=1000),
    sort: str = Query("downloads", description="downloads | likes | lastModified"),
    limit: int = Query(20, ge=1, le=50),
) -> list[HFModelResult]:
    params = {
        "search": q,
        "filter": "gguf",
        "sort": sort,
        "direction": "-1",
        "limit": min(limit * 2, 100),  # over-fetch to allow client-side filtering
        "full": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_hf_headers()) as client:
            r = await client.get(f"{HF_API}/models", params=params)
            r.raise_for_status()
            raw = r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="HuggingFace token required for gated models")
        raise HTTPException(status_code=502, detail=f"HuggingFace API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HuggingFace API unavailable: {e}")

    results: list[HFModelResult] = []
    for m in raw:
        repo_id = m.get("id", "")
        author = repo_id.split("/")[0] if "/" in repo_id else ""
        model_name = repo_id.split("/")[1] if "/" in repo_id else repo_id
        tags = m.get("tags", [])
        gated = bool(m.get("gated"))

        # Parse files from siblings (requires blobs=true for sizes, but full=true gives some info)
        siblings = m.get("siblings", [])
        files = [_parse_hf_file(f) for f in siblings if f.get("rfilename", "").endswith(".gguf")]

        # Filter by quant
        if quant:
            qt = quant.upper().replace("-", "_")
            files = [f for f in files if qt in f.quant.upper()]

        # Filter by size
        files = [f for f in files
                 if not f.is_split  # skip split files by default
                 and (min_gb * 1e9 <= f.size_bytes <= max_gb * 1e9 or f.size_bytes == 0)]

        # Sort files by size
        files.sort(key=lambda f: f.size_bytes)

        results.append(HFModelResult(
            repo_id=repo_id, author=author, model_name=model_name,
            downloads=m.get("downloads", 0) or 0,
            likes=m.get("likes", 0) or 0,
            tags=tags, gated=gated, files=files,
        ))

    # Sort results & trim
    results = results[:limit]
    return results


async def get_hf_model_files(
    repo_id: str,
    quant: str | None = Query(None),
    max_gb: float = Query(100),
    include_split: bool = Query(True),
) -> list[HFFile]:
    """Fetch GGUF file list for a specific HuggingFace repo with sizes.

    Returns single-file GGUFs first, then split-file groups (only part 1 shown as representative).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_hf_headers()) as client:
            r = await client.get(f"{HF_API}/models/{repo_id}", params={"blobs": "true"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HuggingFace API error: {e}")

    siblings = data.get("siblings", [])
    all_files = [_parse_hf_file(f) for f in siblings if f.get("rfilename", "").endswith(".gguf")]

    if quant:
        qt = quant.upper().replace("-", "_")
        all_files = [f for f in all_files if qt in f.quant.upper()]

    # Single files — straightforward
    single_files = [f for f in all_files if not f.is_split]
    single_files = [f for f in single_files if f.size_bytes <= max_gb * 1e9 or f.size_bytes == 0]
    single_files.sort(key=lambda f: f.size_bytes)

    if not include_split:
        return single_files

    # For split groups: show only part 1 as representative; compute total size
    groups: dict[str, list[HFFile]] = {}
    for f in all_files:
        if f.is_split and f.split_group:
            groups.setdefault(f.split_group, []).append(f)

    split_representatives: list[HFFile] = []
    for group_files in groups.values():
        group_files.sort(key=lambda f: f.part_index or 0)
        total_size = sum(f.size_bytes for f in group_files)
        if total_size > max_gb * 1e9 and total_size > 0:
            continue
        rep = group_files[0]
        # Annotate representative with total size
        split_representatives.append(HFFile(
            filename=rep.filename,
            size_bytes=total_size,
            size_human=f"{_human_size(total_size)} ({len(group_files)} частей)",
            quant=rep.quant,
            is_split=True,
            split_group=rep.split_group,
            part_index=1,
            total_parts=len(group_files),
        ))

    split_representatives.sort(key=lambda f: f.size_bytes)
    return single_files + split_representatives


# ── ModelScope search ─────────────────────────────────────────────────────────

async def search_ms_models(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
) -> list[MSModelResult]:
    """Search ModelScope for GGUF models (requires ModelScope token)."""
    token = _load_tokens().get("modelscope", "")
    if not token:
        raise HTTPException(
            status_code=401,
            detail="ModelScope token required. Add it in the Tokens tab.",
        )
    headers = {"User-Agent": "llama-manager/1.0", "Authorization": f"token {token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            r = await client.post(
                f"{MS_API}/models",
                json={"Name": q, "PageSize": limit, "PageNumber": 1},
            )
            if r.status_code in (401, 403):
                raise HTTPException(status_code=401, detail="Invalid ModelScope token.")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ModelScope API error: {e}")

    models_raw = (
        (data.get("Data") or {}).get("Models")
        or data.get("data")
        or []
    )
    results: list[MSModelResult] = []
    for m in models_raw:
        repo_id = m.get("Path") or m.get("name") or ""
        results.append(MSModelResult(
            repo_id=repo_id,
            name=m.get("Name") or m.get("name") or repo_id,
            downloads=m.get("Downloads") or m.get("downloads") or 0,
            stars=m.get("Stars") or m.get("stars") or 0,
            tags=m.get("Tags") or m.get("tags") or [],
            files=[],
        ))
    return results


async def get_ms_model_files(repo_id: str) -> list[HFFile]:
    """List GGUF files for a ModelScope repo."""
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_ms_headers()) as client:
            r = await client.get(f"{MS_API}/models/{repo_id}/repo/files")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ModelScope API error: {e}")

    files_raw = (data.get("Data") or {}).get("Files") or data.get("data") or []
    files = []
    for f in files_raw:
        fn = f.get("Name") or f.get("name") or ""
        if not fn.endswith(".gguf"):
            continue
        size = f.get("Size") or f.get("size") or 0
        files.append(HFFile(filename=fn, size_bytes=size, size_human=_human_size(size),
                            quant=_detect_quant(fn), is_split=False))
    files.sort(key=lambda f: f.size_bytes)
    return files


# ── Local model management ────────────────────────────────────────────────────

async def list_gguf_models() -> list[GgufModel]:
    active_path = _load_config().get("model", "")
    result: list[GgufModel] = []
    if _MODELS_DIR.exists():
        for p in sorted(_MODELS_DIR.rglob("*.gguf")):
            size = p.stat().st_size
            is_mmproj = p.name.startswith("mmproj")
            result.append(GgufModel(name=p.name, path=str(p), size_bytes=size,
                                    size_human=_human_size(size), active=(str(p) == active_path),
                                    is_mmproj=is_mmproj))
    return result


async def delete_gguf_model(filename: str) -> dict:
    if not re.match(r'^[\w\-\. ]+\.gguf$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = _MODELS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Model not found")
    path.unlink()
    return {"deleted": filename}


async def activate_model(body: dict) -> ActivateModelResponse:
    """Activate a GGUF model: save config, write .active_model, restart llama-server.

    The llama-server entrypoint reads /models/.active_model on startup, so a
    container restart is sufficient to switch models without recreating it.
    Backend communicates with Docker Engine via Unix socket.
    """
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    if not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"Model file not found: {path}")

    server_path = _backend_to_server_path(path)

    # 1. Persist to agent config (Redis)
    cfg = _load_config()
    cfg["model"] = path
    _save_config(cfg)

    # 2. Write .active_model so llama-server reads it on next start
    try:
        _ACTIVE_MODEL_FILE.write_text(server_path, encoding="utf-8")
        logger.info("active_model_written", server_path=server_path)
    except Exception as exc:
        logger.warning("active_model_file_write_failed", error=str(exc))

    # 3. Find the llama-server container via Docker socket
    if not Path(_DOCKER_SOCK).exists():
        return ActivateModelResponse(
            status="no_docker",
            model=path,
            server_path=server_path,
            message=(
                f"Модель сохранена ({Path(path).name}). "
                "Docker socket недоступен — перезапустите llama-server вручную: "
                "docker compose restart llama-server"
            ),
        )

    service_name = os.environ.get("LLAMACPP_SERVICE_NAME", "llama-server")
    container_id = await _docker_find_container(service_name)
    if not container_id:
        return ActivateModelResponse(
            status="no_docker",
            model=path,
            server_path=server_path,
            message=(
                f"Модель сохранена ({Path(path).name}). "
                f"Контейнер '{service_name}' не найден — перезапустите вручную."
            ),
        )

    # 4. Restart the container (graceful stop → start)
    try:
        logger.info("llamacpp_restart_triggered", container=container_id[:12], model=Path(path).name)
        await _docker_restart_container(container_id)
    except Exception as exc:
        logger.error("llamacpp_restart_failed", error=str(exc))
        return ActivateModelResponse(
            status="error",
            model=path,
            server_path=server_path,
            message=f"Модель сохранена, но перезапуск не удался: {exc}",
        )

    # 5. Wait until healthy (model loading can take 30-90 s for large GGUFs)
    base_url = _get_llamacpp_base_url()
    healthy = await _wait_llama_healthy(base_url, timeout=120)
    logger.info("llamacpp_after_activate", healthy=healthy, model=Path(path).name)

    if healthy:
        return ActivateModelResponse(
            status="ok",
            model=path,
            server_path=server_path,
            message=f"Модель активирована: {Path(path).name}",
            server_running=True,
        )
    return ActivateModelResponse(
        status="restarting",
        model=path,
        server_path=server_path,
        message="Сервер перезапускается. Загрузка большой модели может занять больше 2 мин — проверьте /status.",
        server_running=False,
    )


async def restart_llamacpp_server() -> ActivateModelResponse:
    """Restart llama-server without changing the model (e.g. after config update)."""
    cfg = _load_config()
    path = cfg.get("model", "")
    server_path = _backend_to_server_path(path) if path else ""

    if not Path(_DOCKER_SOCK).exists():
        raise HTTPException(status_code=503, detail="Docker socket недоступен.")

    service_name = os.environ.get("LLAMACPP_SERVICE_NAME", "llama-server")
    container_id = await _docker_find_container(service_name)
    if not container_id:
        raise HTTPException(status_code=404, detail=f"Контейнер '{service_name}' не найден.")

    await _docker_restart_container(container_id)
    base_url = _get_llamacpp_base_url()
    healthy = await _wait_llama_healthy(base_url, timeout=120)

    return ActivateModelResponse(
        status="ok" if healthy else "restarting",
        model=path,
        server_path=server_path,
        message="Сервер перезапущен успешно." if healthy else "Сервер перезапускается…",
        server_running=healthy,
    )


# ── Download ──────────────────────────────────────────────────────────────────

async def list_downloads() -> list[DownloadStatus]:
    result = []
    for dl_id, d in _downloads.items():
        total = d.get("total_bytes", 0)
        progress = d.get("progress_bytes", 0)
        result.append(DownloadStatus(
            download_id=dl_id,
            repo_id=d.get("repo_id", ""),
            filename=d.get("filename", ""),
            status=d.get("status", "unknown"),
            progress_bytes=progress,
            total_bytes=total,
            progress_pct=round(progress / total * 100, 1) if total > 0 else 0.0,
            error=d.get("error"),
        ))
    return result


async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks) -> dict:
    dl_id = f"{req.repo_id}/{req.filename}".replace("/", "__")

    if dl_id in _downloads and _downloads[dl_id].get("status") == "downloading":
        return {"message": "Already downloading", "download_id": dl_id}

    # Resolve download URL
    if req.url:
        url = req.url
    elif req.source == "huggingface":
        url = f"https://huggingface.co/{req.repo_id}/resolve/main/{req.filename}"
        token = _load_tokens().get("huggingface", "")
        if token:
            url += f"?token={token}"
    elif req.source == "modelscope":
        url = f"https://modelscope.cn/api/v1/models/{req.repo_id}/repo?FilePath={req.filename}"
    else:
        raise HTTPException(status_code=400, detail="Unknown source")

    dest_name = req.filename
    _downloads[dl_id] = {"status": "pending", "progress_bytes": 0, "total_bytes": 0,
                         "error": None, "repo_id": req.repo_id, "filename": req.filename}
    background_tasks.add_task(_download_model, dl_id, url, dest_name, req.source)
    return {"message": "Download started", "download_id": dl_id, "dest": dest_name}


async def cancel_download(dl_id: str) -> dict:
    if dl_id in _downloads:
        _downloads[dl_id]["status"] = "cancelled"
    return {"cancelled": dl_id}


async def get_download_status(dl_id: str) -> DownloadStatus:
    d = _downloads.get(dl_id)
    if not d:
        raise HTTPException(status_code=404, detail="Download not found")
    total = d.get("total_bytes", 0)
    progress = d.get("progress_bytes", 0)
    return DownloadStatus(download_id=dl_id, repo_id=d.get("repo_id", ""),
                          filename=d.get("filename", ""), status=d.get("status", "unknown"),
                          progress_bytes=progress, total_bytes=total,
                          progress_pct=round(progress / total * 100, 1) if total > 0 else 0.0,
                          error=d.get("error"))


async def stream_download(dl_id: str):
    async def _gen() -> AsyncIterator[str]:
        while True:
            d = _downloads.get(dl_id)
            if not d:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break
            total = d.get("total_bytes", 0)
            progress = d.get("progress_bytes", 0)
            payload = {"status": d.get("status"), "progress_bytes": progress, "total_bytes": total,
                       "progress_pct": round(progress / total * 100, 1) if total > 0 else 0.0,
                       "error": d.get("error")}
            yield f"data: {json.dumps(payload)}\n\n"
            if d.get("status") in ("done", "error", "cancelled"):
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(_gen(), media_type="text/event-stream")


async def _list_repo_gguf_files(repo_id: str, source: str) -> list[str]:
    """List the .gguf filenames in a HuggingFace / ModelScope repo (best-effort)."""
    try:
        if source == "huggingface":
            async with httpx.AsyncClient(timeout=20, headers=_hf_headers()) as client:
                r = await client.get(f"https://huggingface.co/api/models/{repo_id}")
                r.raise_for_status()
                return [s.get("rfilename", "") for s in r.json().get("siblings", [])]
        async with httpx.AsyncClient(timeout=20, headers=_ms_headers()) as client:
            r = await client.get(f"{MS_API}/models/{repo_id}/repo/files")
            r.raise_for_status()
            files = (r.json().get("Data") or {}).get("Files") or []
            return [f.get("Path") or f.get("Name") or "" for f in files]
    except Exception as exc:  # noqa: BLE001 — companion lookup is best-effort
        logger.warning("repo_file_list_failed", repo=repo_id, error=str(exc)[:120])
        return []


async def _maybe_download_mmproj(repo_id: str | None, source: str, model_filename: str) -> None:
    """Auto-fetch a vision model's mmproj projector alongside its main GGUF.

    llama-server needs the mmproj-*.gguf companion to enable vision; its
    entrypoint already auto-detects one lying next to the model, but the file
    has to be present. When the same repo ships an mmproj, download it too so a
    vision model works end-to-end from a single click. No-op for text models
    (repo has no mmproj) and for the mmproj download itself."""
    if not repo_id or "mmproj" in model_filename.lower():
        return
    files = await _list_repo_gguf_files(repo_id, source)
    mmprojs = [f for f in files if "mmproj" in f.lower() and f.lower().endswith(".gguf")]
    if not mmprojs:
        return  # not a vision model / no projector shipped
    # Prefer a higher-precision projector (f16) — projectors are small, quality
    # matters more than size here.
    mmprojs.sort(key=lambda f: (0 if "f16" in f.lower() else 1, f))
    mmproj = mmprojs[0]
    dest = _MODELS_DIR / Path(mmproj).name
    if dest.exists():
        return
    if source == "huggingface":
        url = f"https://huggingface.co/{repo_id}/resolve/main/{mmproj}"
    else:
        url = f"https://modelscope.cn/api/v1/models/{repo_id}/repo?FilePath={mmproj}"
    dl_id = f"{repo_id}/{mmproj}".replace("/", "__")
    logger.info("llamacpp_mmproj_autodownload", repo=repo_id, mmproj=mmproj)
    await _download_model(dl_id, url, Path(mmproj).name, source)  # repo_id=None → no recursion


async def _download_model(
    dl_id: str, url: str, dest_name: str, source: str = "huggingface",
    repo_id: str | None = None,
) -> None:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _MODELS_DIR / dest_name
    tmp = _MODELS_DIR / f"{dest_name}.tmp"
    _downloads[dl_id] = {**_downloads.get(dl_id, {}), "status": "downloading",
                         "progress_bytes": 0, "total_bytes": 0, "error": None}
    logger.info("llamacpp_download_start", dl_id=dl_id, url=url[:80])

    headers: dict = {}
    if source == "huggingface":
        token = _load_tokens().get("huggingface", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif source == "modelscope":
        token = _load_tokens().get("modelscope", "")
        if token:
            headers["Authorization"] = f"token {token}"
    headers["User-Agent"] = "llama-manager/1.0"

    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code == 401:
                    raise RuntimeError("Authentication required — set your API token in Настройки токенов")
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} from server")
                total = int(resp.headers.get("content-length", 0))
                _downloads[dl_id]["total_bytes"] = total
                downloaded = 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                        if _downloads.get(dl_id, {}).get("status") == "cancelled":
                            tmp.unlink(missing_ok=True)
                            return
                        f.write(chunk)
                        downloaded += len(chunk)
                        _downloads[dl_id]["progress_bytes"] = downloaded
        shutil.move(str(tmp), str(dest))
        _downloads[dl_id]["status"] = "done"
        logger.info("llamacpp_download_done", dl_id=dl_id, path=str(dest))
        # Vision models need their mmproj projector next to the model — fetch it
        # automatically so a single download yields a working vision model.
        try:
            await _maybe_download_mmproj(repo_id, source, dest_name)
        except Exception as exc:  # noqa: BLE001 — never fail the main download
            logger.warning("mmproj_autodownload_failed", dl_id=dl_id, error=str(exc)[:120])
    except Exception as e:
        tmp.unlink(missing_ok=True)
        _downloads[dl_id]["status"] = "error"
        _downloads[dl_id]["error"] = str(e)
        logger.error("llamacpp_download_failed", dl_id=dl_id, error=str(e))


# ── Diagnostics ───────────────────────────────────────────────────────────────

async def get_llamacpp_slots() -> dict:
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/slots")
            r.raise_for_status()
            return {"slots": r.json()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"llama-server unavailable: {e}")


async def get_llamacpp_metrics():
    base = _get_llamacpp_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/metrics")
            return PlainTextResponse(r.text, status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


async def test_generation() -> dict:
    base = _get_llamacpp_base_url()
    payload = {"messages": [{"role": "user", "content": "Reply with exactly: OK"}],
               "max_tokens": 16, "temperature": 0}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{base}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            return {"ok": True, "response": data["choices"][0]["message"]["content"], "model": data.get("model")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
