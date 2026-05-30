"""Unified local models API — manages Ollama, llama.cpp and vLLM from one place.

Endpoints:
  GET  /api/local-models/status                    — all providers + GPU
  GET  /api/local-models/gpu-budget                — VRAM allocations
  POST /api/local-models/gpu-budget                — set VRAM soft limits
  GET  /api/local-models/search                    — HF/ModelScope unified search
  GET  /api/local-models/{provider}/models         — local models for provider
  POST /api/local-models/{provider}/activate       — activate model (VRAM check)
  POST /api/local-models/{provider}/download       — start download
  GET  /api/local-models/{provider}/download/{id}/stream — SSE progress
  GET  /api/local-models/parameter-profiles        — inference parameter profiles
  POST /api/local-models/parameter-profiles/{name} — save custom profile
  DELETE /api/local-models/parameter-profiles/{name} — delete custom profile
  GET  /api/local-models/task-profiles             — task → profile mapping
  POST /api/local-models/task-profiles             — set task profile override
  GET  /api/local-models/provider-defaults         — hardware defaults per provider
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.ai import gpu_manager
from app.ai.parameter_profiles import (
    PROVIDER_HARDWARE_DEFAULTS,
    TASK_DEFAULT_PROFILE,
    delete_custom_profile,
    get_all_profiles,
    get_inference_params,
    get_task_profile_overrides,
    save_custom_profile,
    set_task_profile_override,
)
from app.ai.schemas import AITask

router = APIRouter()
logger = structlog.get_logger()

ProviderName = Literal["ollama", "llamacpp", "vllm"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VRAMLimitsUpdate(BaseModel):
    ollama: float | None = None
    llamacpp: float | None = None
    vllm: float | None = None


class ActivateRequest(BaseModel):
    model_path: str
    vram_gb_estimate: float | None = None    # for pre-check; 0 = skip check


class DownloadRequest(BaseModel):
    repo_id: str
    filename: str
    source: Literal["huggingface", "modelscope"] = "huggingface"
    url: str | None = None


class ProfileSaveRequest(BaseModel):
    params: dict[str, Any]


class TaskProfileOverrideRequest(BaseModel):
    task: str
    profile: str


class RoutingUpdate(BaseModel):
    models: list[str]
    profile: str = "balanced"
    local_only: bool = True
    allow_cloud: bool = False


# ---------------------------------------------------------------------------
# Status & GPU
# ---------------------------------------------------------------------------

@router.get("/status")
async def get_all_providers_status() -> dict:
    """Return status of all local providers + real GPU stats."""

    import httpx

    from app.ai.providers.vllm_manager import get_vllm_status
    from app.config import settings

    # Ollama status
    ollama_status = {"running": False, "url": str(settings.ollama_url), "models": []}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{settings.ollama_url.rstrip('/')}/api/ps")
            if r.status_code == 200:
                data = r.json()
                ollama_status["running"] = True
                ollama_status["models"] = [m.get("name") for m in data.get("models", [])]
                ollama_status["model_count"] = len(ollama_status["models"])
    except Exception as exc:
        ollama_status["error"] = str(exc)

    # llama.cpp status
    from app.ai.providers.llamacpp_manager import get_llamacpp_status
    try:
        lc_status = await get_llamacpp_status()
        llamacpp_status = lc_status.model_dump()
    except Exception as exc:
        llamacpp_status = {"running": False, "error": str(exc)}

    # vLLM status
    vllm_status = await get_vllm_status()

    # GPU stats
    gpu = await gpu_manager.get_gpu_stats()
    allocations = await gpu_manager.get_allocations()

    return {
        "providers": {
            "ollama": ollama_status,
            "llamacpp": llamacpp_status,
            "vllm": vllm_status,
        },
        "gpu": gpu.__dict__ if gpu else None,
        "vram_allocations": {
            k: {
                "vram_used_gb": v.vram_used_gb,
                "vram_limit_gb": v.vram_limit_gb,
                "models": [m.__dict__ for m in v.models],
                "running": v.running,
            }
            for k, v in allocations.items()
        },
        "total_vram_gb": gpu_manager.TOTAL_VRAM_GB,
    }


@router.get("/gpu-budget")
async def get_gpu_budget() -> dict:
    """Return VRAM usage and soft limits per provider."""
    gpu = await gpu_manager.get_gpu_stats()
    allocations = await gpu_manager.get_allocations()
    total = gpu.total_gb if gpu else gpu_manager.TOTAL_VRAM_GB
    used = sum(a.vram_used_gb for a in allocations.values())
    return {
        "total_gb": total,
        "used_gb": round(used, 2),
        "free_gb": round(total - used, 2),
        "providers": {
            k: {
                "used_gb": v.vram_used_gb,
                "limit_gb": v.vram_limit_gb,
                "running": v.running,
            }
            for k, v in allocations.items()
        },
        "gpu": gpu.__dict__ if gpu else None,
    }


@router.post("/gpu-budget")
async def set_gpu_budget(limits: VRAMLimitsUpdate) -> dict:
    """Set soft VRAM limits per provider (0 = no limit)."""
    current = gpu_manager._load_vram_limits()
    if limits.ollama is not None:
        current["ollama"] = limits.ollama
    if limits.llamacpp is not None:
        current["llamacpp"] = limits.llamacpp
    if limits.vllm is not None:
        current["vllm"] = limits.vllm
    gpu_manager.save_vram_limits(current)
    return {"ok": True, "limits": current}


# ---------------------------------------------------------------------------
# Unified search
# ---------------------------------------------------------------------------

@router.get("/search")
async def search_models(
    q: str = Query(..., description="Model name or keyword"),
    source: Literal["huggingface", "modelscope"] = Query("huggingface"),
    provider: ProviderName | None = Query(None, description="Filter by target provider"),
    format: str | None = Query(None, description="gguf | safetensors | awq | gptq"),
    limit: int = Query(10, ge=1, le=30),
) -> dict:
    """Unified search across HuggingFace and ModelScope.

    - provider=llamacpp → GGUF format, llamacpp_api search
    - provider=vllm → Safetensors/AWQ/GPTQ, vllm_manager search
    - provider=ollama or None → falls through to HF general search
    """
    from app.ai.providers.vllm_manager import (
        search_hf_models as vllm_hf_search,
    )
    from app.ai.providers.vllm_manager import (
        search_ms_models as vllm_ms_search,
    )
    from app.ai.providers.llamacpp_manager import (
        _hf_headers as lc_hf_headers,
    )
    from app.ai.providers.llamacpp_manager import (
        _ms_headers as lc_ms_headers,
    )

    # vLLM-native search (safetensors/AWQ)
    if provider == "vllm":
        if source == "huggingface":
            results = await vllm_hf_search(q, limit=limit)
        else:
            results = await vllm_ms_search(q, limit=limit)
        return {"results": results, "source": source, "provider": "vllm"}

    # llama.cpp / Ollama → GGUF search via existing llamacpp search logic
    import httpx
    if source == "huggingface":
        hf_filter = "gguf" if provider in ("llamacpp", "ollama") else "safetensors"
        actual_filter = format or hf_filter
        try:
            params = {
                "search": q,
                "limit": limit,
                "filter": actual_filter,
                "sort": "downloads",
                "direction": -1,
            }
            async with httpx.AsyncClient(timeout=15.0, headers=lc_hf_headers()) as client:
                r = await client.get("https://huggingface.co/api/models", params=params)
                r.raise_for_status()
                raw = r.json()
            results = [
                {
                    "repo_id": m.get("id", ""),
                    "author": m.get("author", ""),
                    "model_name": m.get("id", "").split("/")[-1],
                    "downloads": m.get("downloads", 0),
                    "likes": m.get("likes", 0),
                    "tags": m.get("tags", []),
                    "gated": m.get("gated", False),
                    "source": "huggingface",
                }
                for m in raw
            ]
        except Exception as exc:
            logger.warning("unified_search_hf_failed", error=str(exc))
            results = []
    else:
        # ModelScope
        try:
            params = {"Name": q, "PageSize": limit, "SortBy": "Downloads"}
            async with httpx.AsyncClient(timeout=15.0, headers=lc_ms_headers()) as client:
                r = await client.get("https://modelscope.cn/api/v1/models", params=params)
                r.raise_for_status()
                raw = r.json()
            results = [
                {
                    "repo_id": m.get("Path", ""),
                    "name": m.get("Name", ""),
                    "downloads": m.get("Downloads", 0),
                    "stars": m.get("Stars", 0),
                    "tags": m.get("Tags", []),
                    "source": "modelscope",
                }
                for m in raw.get("Data", {}).get("Models", [])
            ]
        except Exception as exc:
            logger.warning("unified_search_ms_failed", error=str(exc))
            results = []

    return {"results": results, "source": source, "provider": provider}


# ---------------------------------------------------------------------------
# Provider-specific: list local models
# ---------------------------------------------------------------------------

@router.get("/{provider}/models")
async def list_provider_models(provider: ProviderName) -> dict:
    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import list_gguf_models
        models = await list_gguf_models()
        return {"provider": "llamacpp", "models": [m.model_dump() for m in models]}

    if provider == "vllm":
        from app.ai.providers.vllm_manager import list_local_models
        return {"provider": "vllm", "models": list_local_models()}

    if provider == "ollama":
        import httpx

        from app.config import settings
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
                r.raise_for_status()
                data = r.json()
            return {
                "provider": "ollama",
                "models": [
                    {
                        "name": m.get("name"),
                        "size_bytes": m.get("size", 0),
                        "size_human": f"{m.get('size', 0) / 1024**3:.1f} GB",
                        "modified_at": m.get("modified_at"),
                    }
                    for m in data.get("models", [])
                ],
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach Ollama: {exc}")

    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Provider-specific: activate model (with VRAM check)
# ---------------------------------------------------------------------------

@router.post("/ollama/unload-all")
async def unload_all_ollama() -> dict:
    """Unload ALL Ollama models from VRAM immediately (keep_alive=0 for each)."""
    unloaded = await gpu_manager.unload_all_ollama_models()
    return {"ok": True, "unloaded": unloaded, "count": len(unloaded)}


@router.post("/ollama/unload/{model_name:path}")
async def unload_ollama_model(model_name: str) -> dict:
    """Unload a specific Ollama model from VRAM."""
    ok = await gpu_manager.unload_ollama_model(model_name)
    return {"ok": ok, "model": model_name}


class OllamaPullRequest(BaseModel):
    name: str


@router.post("/ollama/pull")
async def pull_ollama_model(body: OllamaPullRequest) -> StreamingResponse:
    """Pull a model from the Ollama registry. Streams NDJSON progress."""
    import httpx

    from app.config import settings

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream(
                    "POST",
                    f"{settings.ollama_url.rstrip('/')}/api/pull",
                    json={"name": body.name, "stream": True},
                ) as resp:
                    if resp.status_code >= 400:
                        err = await resp.aread()
                        yield json.dumps({"status": "error", "error": err.decode()}) + "\n"
                        return
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n"
        except Exception as exc:
            yield json.dumps({"status": "error", "error": str(exc)}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.delete("/ollama/models/{model_name:path}")
async def delete_ollama_model(model_name: str) -> dict:
    """Delete a model from local Ollama storage."""
    import httpx

    from app.config import settings

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE",
                f"{settings.ollama_url.rstrip('/')}/api/delete",
                json={"name": model_name},
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Model not found")
            resp.raise_for_status()
            return {"deleted": model_name}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/{provider}/activate")
async def activate_provider_model(provider: ProviderName, body: ActivateRequest) -> dict:
    # VRAM pre-check with optional auto-free of Ollama models
    if body.vram_gb_estimate and body.vram_gb_estimate > 0:
        can_load, reason = await gpu_manager.ensure_vram_for(
            provider, body.vram_gb_estimate, auto_free=True
        )
        if not can_load:
            raise HTTPException(status_code=409, detail=reason)

    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import activate_model as lc_activate
        result = await lc_activate({"path": body.model_path})
        return result.model_dump() if hasattr(result, "model_dump") else result

    if provider == "vllm":
        from app.ai.providers.vllm_manager import activate_model as vllm_activate
        result = await vllm_activate(body.model_path)
        return result

    if provider == "ollama":
        # Ollama pulls and loads via CLI — expose run command info
        return {
            "status": "info",
            "message": f"Use Ollama pull/run to load model: ollama pull {body.model_path}",
            "model": body.model_path,
        }

    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Provider-specific: download
# ---------------------------------------------------------------------------

@router.post("/{provider}/download")
async def start_model_download(provider: ProviderName, body: DownloadRequest) -> dict:
    import asyncio

    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import (
            _download_model as lc_download_model,
        )
        from app.ai.providers.llamacpp_manager import (
            _downloads as lc_downloads,
        )
        dl_id = f"{body.repo_id}/{body.filename}".replace("/", "__")
        if dl_id in lc_downloads and lc_downloads[dl_id].get("status") == "downloading":
            return {"download_id": dl_id, "message": "Already downloading"}
        if body.url:
            url = body.url
        elif body.source == "huggingface":
            url = f"https://huggingface.co/{body.repo_id}/resolve/main/{body.filename}"
        else:
            url = f"https://modelscope.cn/api/v1/models/{body.repo_id}/repo?FilePath={body.filename}"
        lc_downloads[dl_id] = {
            "status": "pending", "progress_bytes": 0, "total_bytes": 0,
            "error": None, "repo_id": body.repo_id, "filename": body.filename,
        }
        asyncio.create_task(lc_download_model(dl_id, url, body.filename, body.source))
        return {"download_id": dl_id, "message": "Download started"}

    if provider == "vllm":
        from app.ai.providers.vllm_manager import start_download as vllm_download
        did = await vllm_download(body.repo_id, body.filename, body.source, body.url)
        return {"download_id": did}

    raise HTTPException(status_code=400, detail=f"Download not supported for provider: {provider}")


@router.get("/{provider}/download/{download_id}/stream")
async def stream_download_progress(provider: ProviderName, download_id: str) -> StreamingResponse:
    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import _downloads as lc_downloads

        async def gen():
            import asyncio as _asyncio
            while True:
                info = lc_downloads.get(download_id)
                if info is None:
                    yield f"data: {json.dumps({'error': 'not_found'})}\n\n"
                    return
                yield f"data: {json.dumps(info)}\n\n"
                if info.get("status") in ("completed", "error"):
                    return
                await _asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    if provider == "vllm":
        from app.ai.providers.vllm_manager import stream_download_progress as vllm_prog

        return StreamingResponse(vllm_prog(download_id), media_type="text/event-stream")

    raise HTTPException(status_code=400, detail=f"Download stream not supported for: {provider}")


# ---------------------------------------------------------------------------
# Parameter profiles
# ---------------------------------------------------------------------------

@router.get("/parameter-profiles")
async def list_parameter_profiles() -> dict:
    profiles = get_all_profiles()
    builtin_names = {"anti_hallucination", "structured_reasoning", "balanced", "creative"}
    return {
        "profiles": [
            {
                "name": name,
                "builtin": name in builtin_names,
                **params,
            }
            for name, params in profiles.items()
        ]
    }


@router.post("/parameter-profiles/{name}")
async def save_profile(name: str, body: ProfileSaveRequest) -> dict:
    try:
        save_custom_profile(name, body.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name}


@router.delete("/parameter-profiles/{name}")
async def remove_profile(name: str) -> dict:
    try:
        delete_custom_profile(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Task → profile mapping
# ---------------------------------------------------------------------------

@router.get("/task-profiles")
async def get_task_profiles() -> dict:
    overrides = get_task_profile_overrides()
    task_map = []
    for task in AITask:
        profile = overrides.get(task.value) or TASK_DEFAULT_PROFILE.get(task, "balanced")
        params = get_inference_params(task)
        task_map.append({
            "task": task.value,
            "profile": profile,
            "overridden": task.value in overrides,
            "params": params,
        })
    return {"tasks": task_map}


@router.post("/task-profiles")
async def update_task_profile(body: TaskProfileOverrideRequest) -> dict:
    try:
        task = AITask(body.task)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown task: {body.task}")
    all_profiles = get_all_profiles()
    if body.profile not in all_profiles:
        raise HTTPException(status_code=400, detail=f"Unknown profile: {body.profile}")
    set_task_profile_override(task, body.profile)
    return {"ok": True, "task": body.task, "profile": body.profile}


# ---------------------------------------------------------------------------
# Task routing — single source of truth (model + fallback + profile + policy)
# ---------------------------------------------------------------------------

# Human-readable RU labels for the Маршрутизация UI.
TASK_LABELS: dict[str, str] = {
    "invoice_ocr": "OCR счётов",
    "structured_extraction": "Извлечение данных",
    "drawing_analysis": "Анализ чертежей",
    "drawing_analysis_vlm": "VLM чертежи",
    "engineering_reasoning": "Инж. рассуждения",
    "email_drafting": "Генерация писем",
    "embedding": "Эмбеддинги",
    "reranking": "Реранкинг",
    "classification": "Классификация",
    "long_context_summarization": "Суммаризация",
    "tool_calling": "Tool calling",
    "speech": "Речь",
    "orchestrator_planning": "Планирование агента",
    "code_generation": "Генерация кода",
}


def _catalog_entries() -> list[dict]:
    """Catalog of all known models for routing dropdowns (incl. runtime overlay)."""
    from app.ai.model_registry import ModelRegistry

    reg = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    out = []
    for key, cap in reg.models.items():
        out.append({
            "key": key,
            "provider": cap.provider.value,
            "provider_model": cap.provider_model,
            "modalities": sorted(m.value for m in cap.modalities),
            "local_only": cap.local_only,
            "vram_gb_estimate": cap.vram_gb_estimate,
            "status": cap.status.value,
        })
    return sorted(out, key=lambda e: (e["provider"], e["key"]))


@router.get("/routing")
async def get_routing() -> dict:
    """All tasks with their effective routing + catalog for the Маршрутизация UI."""
    from app.ai.model_registry import ModelRegistry
    from app.ai.task_routing import CONFIDENTIAL_TASKS, get_task_routing

    reg = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    routing = get_task_routing()
    tasks = []
    for task, r in routing.items():
        route = reg.routes.get(task)
        tasks.append({
            "task": task.value,
            "label": TASK_LABELS.get(task.value, task.value),
            "models": r.models,
            "profile": r.profile,
            "local_only": r.local_only,
            "allow_cloud": r.allow_cloud,
            "confidential_locked": task in CONFIDENTIAL_TASKS,
            "required_modalities": (
                sorted(m.value for m in route.required_modalities) if route else []
            ),
            "params": get_inference_params(task),
        })
    return {"tasks": tasks, "catalog": _catalog_entries()}


@router.put("/routing/{task}")
async def update_routing(task: str, body: RoutingUpdate) -> dict:
    from app.ai.task_routing import TaskRouting, save_task_routing

    try:
        t = AITask(task)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown task: {task}")
    try:
        saved = save_task_routing(t, TaskRouting(task=task, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return saved.model_dump()


@router.post("/routing/{task}/reset")
async def reset_routing(task: str) -> dict:
    from app.ai.task_routing import reset_task_routing

    try:
        t = AITask(task)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown task: {task}")
    return reset_task_routing(t).model_dump()


# ---------------------------------------------------------------------------
# Simplified assignment — two groups (Документы / Агент)
# ---------------------------------------------------------------------------

class DocumentGroupUpdate(BaseModel):
    vision_model: str | None = None
    text_model: str | None = None
    embedding_model: str | None = None
    rerank_model: str | None = None


class AgentGroupUpdate(BaseModel):
    agent_model: str | None = None
    agent_provider: str | None = None
    large_model: str | None = None
    large_provider: str | None = None


@router.get("/assignment")
async def get_assignment() -> dict:
    """Both assignment groups + catalog for the simplified Назначение UI."""
    from app.ai.assignment_groups import get_groups

    return {**get_groups(), "catalog": _catalog_entries()}


@router.put("/assignment/documents")
async def set_document_assignment(body: DocumentGroupUpdate) -> dict:
    from app.ai.assignment_groups import DocumentGroup, set_document_group

    try:
        result = set_document_group(DocumentGroup(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result.model_dump()


@router.put("/assignment/agent")
async def set_agent_assignment(body: AgentGroupUpdate) -> dict:
    from app.ai.assignment_groups import AgentGroup, set_agent_group

    result = set_agent_group(AgentGroup(**body.model_dump()))
    return result.model_dump()


# ---------------------------------------------------------------------------
# Per-provider server config / tokens / lifecycle / repo files
# ---------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    config: dict[str, Any]


class TokensUpdate(BaseModel):
    huggingface: str | None = None
    modelscope: str | None = None


# Docker compose service name per provider (override via env).
_SERVICE_NAMES = {
    "llamacpp": os.environ.get("LLAMACPP_SERVICE_NAME", "llama-server"),
    "vllm": os.environ.get("VLLM_SERVICE_NAME", "vllm-server"),
    "ollama": os.environ.get("OLLAMA_SERVICE_NAME", "ollama"),
}


@router.get("/{provider}/config")
async def get_provider_config(provider: ProviderName) -> dict:
    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import _load_config
        return {"provider": "llamacpp", "config": _load_config()}
    if provider == "vllm":
        from app.ai.providers.vllm_manager import load_vllm_config
        return {"provider": "vllm", "config": load_vllm_config()}
    # Ollama has no editable server config — expose hardware defaults read-only.
    return {
        "provider": "ollama",
        "config": PROVIDER_HARDWARE_DEFAULTS.get("ollama", {}),
        "readonly": True,
    }


@router.patch("/{provider}/config")
async def patch_provider_config(provider: ProviderName, body: ConfigUpdate) -> dict:
    if provider == "llamacpp":
        from app.ai.providers.llamacpp_manager import _load_config, _save_config
        cfg = {**_load_config(), **body.config}
        _save_config(cfg)
        return {"provider": "llamacpp", "config": cfg}
    if provider == "vllm":
        from app.ai.providers.vllm_manager import load_vllm_config, save_vllm_config
        cfg = {**load_vllm_config(), **body.config}
        save_vllm_config(cfg)
        return {"provider": "vllm", "config": cfg}
    raise HTTPException(status_code=400, detail=f"Config not editable for provider: {provider}")


@router.get("/tokens")
async def get_tokens() -> dict:
    """HF/ModelScope tokens are shared across providers (used for gated downloads)."""
    from app.ai.providers.llamacpp_manager import _load_tokens
    tokens = _load_tokens()
    return {
        "huggingface": bool(tokens.get("huggingface")),
        "modelscope": bool(tokens.get("modelscope")),
    }


@router.patch("/tokens")
async def patch_tokens(body: TokensUpdate) -> dict:
    from app.ai.providers.llamacpp_manager import _load_tokens, _save_tokens
    tokens = _load_tokens()
    if body.huggingface is not None:
        tokens["huggingface"] = body.huggingface
    if body.modelscope is not None:
        tokens["modelscope"] = body.modelscope
    _save_tokens(tokens)
    return {
        "huggingface": bool(tokens.get("huggingface")),
        "modelscope": bool(tokens.get("modelscope")),
    }


@router.delete("/tokens/{token_provider}")
async def delete_token(token_provider: Literal["huggingface", "modelscope"]) -> dict:
    from app.ai.providers.llamacpp_manager import _load_tokens, _save_tokens
    tokens = _load_tokens()
    tokens.pop(token_provider, None)
    _save_tokens(tokens)
    return {
        "huggingface": bool(tokens.get("huggingface")),
        "modelscope": bool(tokens.get("modelscope")),
    }


@router.post("/{provider}/server/{action}")
async def control_provider_server(
    provider: ProviderName,
    action: Literal["start", "stop", "restart"],
) -> dict:
    """Start / stop / restart a local model server container via the Docker socket."""
    import httpx

    service = _SERVICE_NAMES.get(provider)
    if not service:
        raise HTTPException(status_code=400, detail=f"No server to control for: {provider}")

    filters = json.dumps({"label": [f"com.docker.compose.service={service}"]})
    try:
        async with httpx.AsyncClient(
            transport=httpx.HTTPTransport(uds="/var/run/docker.sock"), base_url="http://docker"
        ) as client:
            r = await client.get("/containers/json", params={"filters": filters, "all": "true"})
            r.raise_for_status()
            containers = r.json()
            if not containers:
                raise HTTPException(
                    status_code=404,
                    detail=f"Container for service '{service}' not found. Manage it manually.",
                )
            cid = containers[0]["Id"]
            resp = await client.post(f"/containers/{cid}/{action}", params={"t": "5"})
            if resp.status_code not in (204, 304):
                raise HTTPException(status_code=502, detail=f"Docker {action} failed: {resp.text}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Docker socket unavailable: {exc}")
    # Reset the idle timer so a just-started server isn't swept immediately.
    if action in ("start", "restart"):
        from app.ai.server_lifecycle import mark_used
        mark_used(provider)
    return {"ok": True, "provider": provider, "service": service, "action": action}


# ---------------------------------------------------------------------------
# Hardware presets
# ---------------------------------------------------------------------------

@router.get("/presets")
async def list_hardware_presets() -> dict:
    from app.ai.presets import list_presets
    return {"presets": list_presets()}


@router.post("/presets/{name}/apply")
async def apply_hardware_preset(name: str) -> dict:
    from app.ai.presets import apply_preset
    try:
        return apply_preset(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# Usage telemetry
# ---------------------------------------------------------------------------

@router.get("/telemetry/summary")
async def telemetry_summary() -> dict:
    from app.ai import telemetry
    return telemetry.get_summary()


@router.post("/telemetry/reset")
async def telemetry_reset() -> dict:
    from app.ai import telemetry
    telemetry.reset()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Benchmark — measure real latency of a model on a task (forward-looking)
# ---------------------------------------------------------------------------

class BenchmarkRequest(BaseModel):
    model_key: str
    task: str = "classification"
    prompt: str = "Привет! Ответь одним словом: работает?"


@router.post("/benchmark")
async def benchmark_model(body: BenchmarkRequest) -> dict:
    """Run one timed generation against a catalog model. Returns latency + tokens."""
    import time

    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, ChatMessage

    try:
        task = AITask(body.task)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown task: {body.task}")

    started = time.perf_counter()
    try:
        resp = await ai_router.run(
            AIRequest(
                task=task,
                messages=[ChatMessage(role="user", content=body.prompt)],
                preferred_model=body.model_key,
                confidential=True,
                allow_cloud=False,
            )
        )
    except Exception as exc:
        return {"ok": False, "model_key": body.model_key, "error": str(exc)}

    return {
        "ok": True,
        "model_key": body.model_key,
        "model": resp.model,
        "provider": resp.provider.value,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
        "text_preview": (resp.text or "")[:200],
    }


@router.get("/{provider}/model/{repo_id:path}/files")
async def get_model_files(
    provider: ProviderName,
    repo_id: str,
    source: Literal["huggingface", "modelscope"] = Query("huggingface"),
    quant: str | None = Query(None),
    max_gb: float | None = Query(None),
) -> dict:
    """List downloadable files (GGUF quants / safetensors) of a repo for a provider."""
    if provider == "vllm":
        from app.ai.providers.vllm_manager import list_hf_files
        files = await list_hf_files(repo_id)
        return {"provider": "vllm", "repo_id": repo_id, "files": files}

    # llamacpp / ollama → GGUF files via existing llamacpp helpers
    from app.ai.providers.llamacpp_manager import get_hf_model_files, get_ms_model_files
    if source == "huggingface":
        files = await get_hf_model_files(
            repo_id, quant=quant, max_gb=max_gb if max_gb is not None else 100
        )
    else:
        files = await get_ms_model_files(repo_id)
    return {
        "provider": provider,
        "repo_id": repo_id,
        "source": source,
        "files": [f.model_dump() if hasattr(f, "model_dump") else f for f in files],
    }


# ---------------------------------------------------------------------------
# Provider hardware defaults
# ---------------------------------------------------------------------------

@router.get("/provider-defaults")
async def get_provider_defaults() -> dict:
    return {"defaults": PROVIDER_HARDWARE_DEFAULTS, "total_vram_gb": gpu_manager.TOTAL_VRAM_GB}
