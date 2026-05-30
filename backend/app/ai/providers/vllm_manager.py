"""vLLM model manager — download, activate, and manage vLLM models.

Mirrors the functionality of llamacpp_api.py but for vLLM:
- Models are stored in Safetensors/AWQ/GPTQ format (not GGUF)
- Model activation restarts the vLLM container with a new --model path
- Search is unified with HuggingFace and ModelScope
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger()

_VLLM_MODELS_DIR = Path(os.environ.get("VLLM_MODELS_DIR", "/vllm-models"))
# Shared read path for GGUF models downloaded by llama.cpp (vLLM ≥0.6 can load GGUF natively).
_LLAMACPP_MODELS_DIR = Path(os.environ.get("LLAMACPP_MODELS_DIR", "/llamacpp-models"))
_DOCKER_SOCK = "/var/run/docker.sock"
_REDIS_KEY_CONFIG = "vllm_config"
_REDIS_KEY_TOKENS = "vllm_tokens"   # shares token storage with llamacpp

# Active downloads keyed by download_id
_downloads: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

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
    except Exception as exc:
        logger.warning("vllm_manager_redis_write_failed", key=key, error=str(exc))


def load_vllm_config() -> dict:
    defaults = {
        "url": os.environ.get("VLLM_URL", "http://vllm-server:8000"),
        "model": os.environ.get("VLLM_MODEL", ""),
        "gpu_memory_utilization": float(os.environ.get("VLLM_GPU_MEMORY_UTILIZATION", "0.85")),
        "max_model_len": int(os.environ.get("VLLM_MAX_MODEL_LEN", "16384")),
        "dtype": os.environ.get("VLLM_DTYPE", "bfloat16"),
        "quantization": os.environ.get("VLLM_QUANTIZATION", ""),
        "tensor_parallel_size": 1,
    }
    stored = _redis_get(_REDIS_KEY_CONFIG) or {}
    return {**defaults, **stored}


def save_vllm_config(cfg: dict) -> None:
    _redis_set(_REDIS_KEY_CONFIG, cfg)


def load_tokens() -> dict:
    return _redis_get("llamacpp_tokens") or {}   # shared token store


# ---------------------------------------------------------------------------
# Model file helpers
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _detect_format(filename: str) -> str:
    fn = filename.lower()
    if "awq" in fn:
        return "awq"
    if "gptq" in fn:
        return "gptq"
    if "gguf" in fn:
        return "gguf"
    if fn.endswith(".safetensors"):
        return "safetensors"
    return "unknown"


def list_local_models() -> list[dict]:
    """List models available for vLLM: native vLLM models + shared GGUF from llama.cpp."""
    _VLLM_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_vllm_config()
    active = cfg.get("model", "")
    models = []

    # Native vLLM models (Safetensors / AWQ / GPTQ directories)
    for entry in sorted(_VLLM_MODELS_DIR.iterdir()):
        if entry.is_dir():
            total = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            formats: set[str] = set()
            for f in entry.rglob("*.safetensors"):
                formats.add(_detect_format(f.name))
            fmt = next(iter(formats), "safetensors")
            models.append({
                "name": entry.name,
                "path": str(entry),
                "size_bytes": total,
                "size_human": _human_size(total),
                "format": fmt,
                "source": "vllm",
                "active": str(entry) == active or entry.name in active,
            })

    # Shared GGUF models from llama.cpp volume (vLLM ≥0.6 GGUF support)
    if _LLAMACPP_MODELS_DIR.exists():
        for f in sorted(_LLAMACPP_MODELS_DIR.rglob("*.gguf")):
            if f.name.startswith("mmproj"):
                continue  # skip vision projector files
            size = f.stat().st_size
            models.append({
                "name": f.name,
                "path": str(f),
                "size_bytes": size,
                "size_human": _human_size(size),
                "format": "gguf",
                "source": "llamacpp-shared",
                "active": str(f) == active or f.name in active,
                "note": "Shared from llama.cpp — requires vLLM ≥0.6 and --quantization gguf",
            })

    return models


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def get_vllm_status() -> dict:
    cfg = load_vllm_config()
    base = cfg["url"].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{base}/health")
            r.raise_for_status()
        # Fetch loaded models
        async with httpx.AsyncClient(timeout=4.0) as client:
            rm = await client.get(f"{base}/v1/models")
            models_data = rm.json().get("data", []) if rm.status_code == 200 else []
        model_names = [m.get("id", "") for m in models_data]
        return {
            "running": True,
            "url": base,
            "models": model_names,
            "model_loaded": model_names[0] if model_names else None,
            "gpu_memory_utilization": cfg.get("gpu_memory_utilization"),
            "max_model_len": cfg.get("max_model_len"),
            "dtype": cfg.get("dtype"),
        }
    except Exception as exc:
        return {
            "running": False,
            "url": base,
            "models": [],
            "model_loaded": None,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Docker container restart
# ---------------------------------------------------------------------------

async def _docker_find_container(service_name: str) -> str | None:
    if not Path(_DOCKER_SOCK).exists():
        return None
    try:
        transport = httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK)
        filters = json.dumps({"label": [f"com.docker.compose.service={service_name}"]})
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=5.0
        ) as client:
            r = await client.get("/containers/json", params={"filters": filters})
            if r.status_code == 200:
                containers = r.json()
                if containers:
                    return containers[0]["Id"]
    except Exception as exc:
        logger.warning("vllm_docker_find_failed", service=service_name, error=str(exc))
    return None


async def _docker_restart_container(container_id: str) -> None:
    transport = httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", timeout=30.0
    ) as client:
        r = await client.post(f"/containers/{container_id}/restart", params={"t": "5"})
        r.raise_for_status()


async def _wait_vllm_healthy(url: str, timeout: int = 180) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{url}/health")
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(3.0)
    return False


async def activate_model(model_path: str) -> dict:
    """Set a new model path and restart the vLLM container."""
    cfg = load_vllm_config()
    cfg["model"] = model_path
    save_vllm_config(cfg)

    service_name = os.environ.get("VLLM_SERVICE_NAME", "vllm-server")
    container_id = await _docker_find_container(service_name)
    if not container_id:
        return {
            "status": "no_docker",
            "model": model_path,
            "message": "Docker socket not available or service not found. Restart vLLM manually.",
        }

    await _docker_restart_container(container_id)
    base = cfg["url"].rstrip("/")
    healthy = await _wait_vllm_healthy(base)
    return {
        "status": "ok" if healthy else "timeout",
        "model": model_path,
        "message": "vLLM restarted and healthy." if healthy else "vLLM restart timed out — check logs.",
    }


# ---------------------------------------------------------------------------
# HuggingFace / ModelScope search for Safetensors/AWQ/GPTQ models
# ---------------------------------------------------------------------------

HF_API = "https://huggingface.co/api"
MS_API = "https://modelscope.cn/api/v1"

_VLLM_EXTENSIONS = {".safetensors", ".bin"}
_VLLM_QUANT_KEYWORDS = {"awq", "gptq", "bnb", "fp8", "int8", "int4"}


def _hf_headers() -> dict:
    token = load_tokens().get("huggingface", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _ms_headers() -> dict:
    token = load_tokens().get("modelscope", "")
    h = {"User-Agent": "vllm-manager/1.0"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _classify_vllm_file(filename: str) -> str:
    fn = filename.lower()
    if "awq" in fn:
        return "AWQ"
    if "gptq" in fn:
        return "GPTQ"
    if "gguf" in fn:
        return "GGUF"
    if fn.endswith(".safetensors"):
        return "safetensors"
    return "other"


async def search_hf_models(query: str, limit: int = 10) -> list[dict]:
    """Search HuggingFace for vLLM-compatible models (safetensors, AWQ, GPTQ)."""
    try:
        params = {
            "search": query,
            "limit": limit,
            "filter": "safetensors",
            "sort": "downloads",
            "direction": -1,
        }
        async with httpx.AsyncClient(timeout=15.0, headers=_hf_headers()) as client:
            r = await client.get(f"{HF_API}/models", params=params)
            r.raise_for_status()
            raw = r.json()
    except Exception as exc:
        logger.warning("vllm_hf_search_failed", error=str(exc))
        return []

    results = []
    for m in raw:
        results.append({
            "repo_id": m.get("id", ""),
            "author": m.get("author", ""),
            "model_name": m.get("id", "").split("/")[-1],
            "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "tags": m.get("tags", []),
            "gated": m.get("gated", False),
            "library": m.get("library_name", ""),
            "source": "huggingface",
        })
    return results


async def list_hf_files(repo_id: str) -> list[dict]:
    """List model files in an HF repo, focusing on vLLM-compatible formats."""
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_hf_headers()) as client:
            r = await client.get(f"{HF_API}/models/{repo_id}")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("vllm_hf_list_files_failed", repo_id=repo_id, error=str(exc))
        return []

    files = []
    for sf in data.get("siblings", []):
        fn = sf.get("rfilename", "")
        ext = Path(fn).suffix.lower()
        if ext not in _VLLM_EXTENSIONS and "gguf" not in fn.lower():
            continue
        fmt = _classify_vllm_file(fn)
        size = sf.get("size", 0) or 0
        files.append({
            "filename": fn,
            "size_bytes": size,
            "size_human": _human_size(size),
            "format": fmt,
            "download_url": f"https://huggingface.co/{repo_id}/resolve/main/{fn}",
        })
    return files


async def search_ms_models(query: str, limit: int = 10) -> list[dict]:
    """Search ModelScope for vLLM-compatible models."""
    try:
        params = {"Name": query, "PageSize": limit, "SortBy": "Downloads"}
        async with httpx.AsyncClient(timeout=15.0, headers=_ms_headers()) as client:
            r = await client.get(f"{MS_API}/models", params=params)
            r.raise_for_status()
            raw = r.json()
    except Exception as exc:
        logger.warning("vllm_ms_search_failed", error=str(exc))
        return []

    results = []
    for m in raw.get("Data", {}).get("Models", []):
        results.append({
            "repo_id": m.get("Path", ""),
            "name": m.get("Name", ""),
            "downloads": m.get("Downloads", 0),
            "stars": m.get("Stars", 0),
            "tags": m.get("Tags", []),
            "source": "modelscope",
        })
    return results


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


async def _stream_download(
    url: str,
    dest: Path,
    headers: dict,
    download_id: str,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _downloads[download_id].update({"status": "downloading", "error": None})
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                _downloads[download_id]["total_bytes"] = total
                received = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        received += len(chunk)
                        _downloads[download_id]["progress_bytes"] = received
                        if total:
                            _downloads[download_id]["progress_pct"] = round(received / total * 100, 1)
        _downloads[download_id]["status"] = "completed"
    except Exception as exc:
        _downloads[download_id].update({"status": "error", "error": str(exc)})
        raise


async def start_download(
    repo_id: str,
    filename: str,
    source: str = "huggingface",
    url: str | None = None,
) -> str:
    """Start a background download. Returns download_id."""
    did = _download_id()
    _downloads[did] = {
        "download_id": did,
        "repo_id": repo_id,
        "filename": filename,
        "status": "pending",
        "progress_bytes": 0,
        "total_bytes": 0,
        "progress_pct": 0.0,
        "error": None,
    }

    # Determine local path — for whole-repo downloads, use repo name as dir
    model_dir = _VLLM_MODELS_DIR / re.sub(r"[^\w\-.]", "_", repo_id.split("/")[-1])
    dest = model_dir / filename

    # Resolve download URL
    if not url:
        if source == "huggingface":
            url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
            headers = _hf_headers()
        else:
            url = f"https://modelscope.cn/models/{repo_id}/resolve/main/{filename}"
            headers = _ms_headers()
    else:
        headers = {}

    asyncio.create_task(_stream_download(url, dest, headers, did))
    return did


def get_download_status(download_id: str) -> dict | None:
    return _downloads.get(download_id)


async def stream_download_progress(download_id: str):
    """AsyncGenerator of SSE lines for download progress."""
    while True:
        info = _downloads.get(download_id)
        if info is None:
            yield f"data: {json.dumps({'error': 'not_found'})}\n\n"
            return
        yield f"data: {json.dumps(info)}\n\n"
        if info.get("status") in ("completed", "error"):
            return
        await asyncio.sleep(0.5)
