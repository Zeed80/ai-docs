"""AI settings API — model management and config."""

import json
from pathlib import Path

import httpx
import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent_config import (
    BuiltinAgentConfig,
    BuiltinAgentConfigUpdate,
    get_builtin_agent_config,
    reset_builtin_agent_config,
    update_builtin_agent_config,
)
from app.ai.gateway_config import gateway_config
from app.ai.model_registry import ModelRegistry
from app.ai.schemas import AITask, Modality
from app.config import settings
from app.db.models import MemoryEmbeddingRecord
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()

_CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / "ai_config.json"
_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

_REDIS_KEY = "ai_config"

_DEFAULT_CONFIG = {
    "model_agent": "qwen3.5:9b",
    "model_ocr": settings.ollama_model_ocr,
    "model_reasoning": settings.ollama_model_reasoning,
    "model_vlm": settings.ollama_model_vlm,
    "embedding_model": "qwen3_embedding_8b_ollama",
    "reranker_model": "local_reranker_ollama",
    "verify_model_1": settings.ollama_model_ocr,
    "turboquant_enabled": False,
    "turboquant_kv_cache_dtype": "turboquant_k8v4",
    "turboquant_max_model_len": 131072,
}


def _redis_get_config() -> dict | None:
    """Read config from Redis (shared between backend and workers)."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True)
        raw = r.get(_REDIS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def _redis_set_config(cfg: dict) -> None:
    """Write config to Redis so all workers pick it up immediately."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.redis_url, decode_responses=True)
        r.set(_REDIS_KEY, json.dumps(cfg, ensure_ascii=False))
    except Exception as e:
        logger.warning("ai_config_redis_write_failed", error=str(e))


def get_ai_config() -> dict:
    # 1. Redis — shared across all containers
    redis_cfg = _redis_get_config()
    if redis_cfg:
        cfg = {**_DEFAULT_CONFIG, **redis_cfg}
        cfg.pop("verify_model_2", None)
        return cfg
    # 2. Local file fallback (backend container only)
    if _CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(_CONFIG_FILE.read_text())
            # Migrate to Redis so workers can read it
            _redis_set_config(file_cfg)
            cfg = {**_DEFAULT_CONFIG, **file_cfg}
            cfg.pop("verify_model_2", None)
            return cfg
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def save_ai_config(cfg: dict) -> None:
    # File-first: ensures durable backup exists before workers read from Redis.
    # If Redis write fails, the file serves as ground truth on next load.
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    try:
        _redis_set_config(cfg)
    except Exception:
        pass  # File is the authoritative backup; Redis is best-effort for workers


def _sync_ai_model_agent(model_name: str | None) -> None:
    """Mirror built-in agent model into ai_config for backward compatibility."""
    if not model_name:
        return
    cfg = get_ai_config()
    cfg["model_agent"] = str(model_name)
    cfg.pop("verify_model_2", None)
    save_ai_config(cfg)


# ── Models list ───────────────────────────────────────────────────────────────

@router.get("/models")
async def list_models() -> dict:
    ollama_url = settings.ollama_url
    error_detail: str | None = None
    models: list[dict] = []
    gpu_info: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = sorted(
                [
                    {
                        "name": m["name"],
                        "size": m.get("size", 0),
                        "modified_at": m.get("modified_at", ""),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "family": m.get("details", {}).get("family", ""),
                    }
                    for m in data.get("models", [])
                ],
                key=lambda x: x["name"],
            )
            # Check GPU visibility via Ollama's /api/ps endpoint (shows running models + GPU)
            try:
                ps_resp = await client.get(f"{ollama_url}/api/ps")
                if ps_resp.status_code == 200:
                    ps_data = ps_resp.json()
                    gpu_info = {
                        "running_models": [
                            {"name": m.get("name"), "size_vram": m.get("size_vram", 0)}
                            for m in ps_data.get("models", [])
                        ],
                        "has_gpu": any(
                            m.get("size_vram", 0) > 0
                            for m in ps_data.get("models", [])
                        ),
                    }
            except Exception:
                gpu_info = None
    except Exception as e:
        error_detail = str(e)
        logger.warning("ollama_unavailable", url=ollama_url, error=error_detail)

    return {
        "models": models,
        "ollama_url": ollama_url,
        "ollama_available": error_detail is None,
        "ollama_error": error_detail,
        "gpu_info": gpu_info,
    }


# ── Pull model (streaming progress) ──────────────────────────────────────────

class PullRequest(BaseModel):
    name: str


@router.post("/models/pull")
async def pull_model(payload: PullRequest):
    """Pull a model from Ollama registry. Streams NDJSON progress."""
    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                async with client.stream(
                    "POST",
                    f"{settings.ollama_url}/api/pull",
                    json={"name": payload.name, "stream": True},
                ) as resp:
                    if resp.status_code >= 400:
                        err = await resp.aread()
                        yield json.dumps({"status": "error", "error": err.decode()}) + "\n"
                        return
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n"
        except Exception as e:
            yield json.dumps({"status": "error", "error": str(e)}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ── Delete model ──────────────────────────────────────────────────────────────

@router.delete("/models/{model_name:path}")
async def delete_model(model_name: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE",
                f"{settings.ollama_url}/api/delete",
                json={"name": model_name},
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Model not found")
            resp.raise_for_status()
            return {"deleted": model_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Config CRUD ───────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config() -> dict:
    return get_ai_config()


@router.get("/config/status")
async def get_config_status() -> dict:
    cfg = get_ai_config()
    warnings: list[str] = []
    installed_models: set[str] = set()
    installed_model_aliases: set[str] = set()
    ollama_available = False

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            installed_models = {item.get("name", "") for item in resp.json().get("models", [])}
            installed_model_aliases = {
                model.removesuffix(":latest")
                for model in installed_models
                if model
            } | installed_models
            ollama_available = True
    except Exception as exc:
        warnings.append(f"Ollama unavailable: {exc}")

    for key in ("model_agent", "model_ocr", "model_reasoning", "model_vlm"):
        model_name = cfg.get(key)
        if model_name and installed_models and model_name not in installed_model_aliases:
            warnings.append(f"{key} points to a missing Ollama model: {model_name}")

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    registry_warnings: list[str] = []
    for key in ("embedding_model", "reranker_model"):
        model_key = cfg.get(key)
        if not model_key:
            continue
        try:
            model = registry.get_model(model_key)
        except KeyError:
            registry_warnings.append(f"{key} is not in model registry: {model_key}")
            continue
        if (
            model.provider == "ollama"
            and installed_models
            and model.provider_model not in installed_model_aliases
        ):
            registry_warnings.append(
                f"{key} provider model is not installed in Ollama: {model.provider_model}"
            )

    warnings.extend(registry_warnings)
    return {
        "ok": not warnings,
        "ollama_available": ollama_available,
        "installed_models": sorted(installed_models),
        "config": cfg,
        "warnings": warnings,
    }


class ConfigUpdate(BaseModel):
    model_agent: str | None = None
    model_ocr: str | None = None
    model_reasoning: str | None = None
    model_vlm: str | None = None
    embedding_model: str | None = None
    reranker_model: str | None = None
    verify_model_1: str | None = None
    turboquant_enabled: bool | None = None
    turboquant_kv_cache_dtype: str | None = None
    turboquant_max_model_len: int | None = None


@router.patch("/config")
async def update_config(
    payload: ConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    cfg = get_ai_config()
    previous_embedding_model = cfg.get("embedding_model")
    update = payload.model_dump(include=payload.model_fields_set)
    cfg.update(update)
    cfg.pop("verify_model_2", None)
    save_ai_config(cfg)
    if "model_agent" in update and update["model_agent"]:
        update_builtin_agent_config(
            BuiltinAgentConfigUpdate(model=str(update["model_agent"]))
        )
    if (
        payload.embedding_model
        and previous_embedding_model
        and payload.embedding_model != previous_embedding_model
    ):
        await db.execute(
            sql_update(MemoryEmbeddingRecord)
            .where(MemoryEmbeddingRecord.status.in_(["queued", "indexed"]))
            .values(status="stale")
        )
        await db.commit()
    logger.info("ai_config_updated", **update)
    return cfg


# ── Built-in agent config ────────────────────────────────────────────────────

@router.get("/agent-config", response_model=BuiltinAgentConfig)
async def get_agent_config() -> BuiltinAgentConfig:
    return get_builtin_agent_config()


@router.patch("/agent-config", response_model=BuiltinAgentConfig)
async def patch_agent_config(
    payload: BuiltinAgentConfigUpdate,
) -> BuiltinAgentConfig:
    config = update_builtin_agent_config(payload)
    if payload.model is not None:
        _sync_ai_model_agent(payload.model)
    logger.info("builtin_agent_config_updated", **payload.model_dump(exclude_unset=True))
    return config


def _pick_first_installed(
    installed: set[str],
    preferred: list[str],
    fallback: str | None = None,
) -> str | None:
    aliases = installed | {name.removesuffix(":latest") for name in installed}
    for model in preferred:
        if model in aliases:
            return model
    return fallback


async def _installed_ollama_model_names() -> set[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            return {
                str(item.get("name") or "").strip()
                for item in resp.json().get("models", [])
                if str(item.get("name") or "").strip()
            }
    except Exception:
        return set()


@router.post("/agent-config/presets/stable-local", response_model=BuiltinAgentConfig)
async def apply_stable_local_agent_preset() -> BuiltinAgentConfig:
    """Apply a 3-model local preset: fast orchestrator, main workers, large builder."""
    current = get_builtin_agent_config()
    installed = await _installed_ollama_model_names()
    orchestrator_model = _pick_first_installed(
        installed,
        ["gemma4:e2b", "nemotron-3-nano:latest", "gemma4:e4b", "qwen3.5:9b"],
        current.orchestrator_model or current.fast_model or current.model,
    )
    main_model = _pick_first_installed(
        installed,
        ["qwen3.5:9b", "ministral-3:8b", "gemma4:e4b", "gemma4:e2b"],
        current.worker_model or current.model,
    )
    large_model = _pick_first_installed(
        installed,
        [
            "qwen3.6:35b",
            "fredrezones55/Qwen3.6-35B-A3B-APEX:Compact",
            "gemma4:31b",
            "granite4.1:30b",
        ],
        current.builder_model or current.model,
    )
    config = update_builtin_agent_config(
        BuiltinAgentConfigUpdate(
            provider="ollama",
            model=main_model,
            orchestrator_provider="ollama",
            orchestrator_model=orchestrator_model,
            worker_provider="ollama",
            worker_model=main_model,
            auditor_provider="ollama",
            auditor_model=main_model,
            builder_provider="ollama",
            builder_model=large_model,
            fast_provider="ollama",
            fast_model=orchestrator_model,
            compression_model=main_model,
            fallback_providers=[],
            disable_thinking=True,
            orchestrator_disable_thinking=True,
            worker_disable_thinking=True,
            auditor_disable_thinking=True,
            builder_disable_thinking=False,
            fast_disable_thinking=True,
            department_enabled=True,
            audit_enabled=True,
            context_compression_enabled=True,
        )
    )
    _sync_ai_model_agent(config.model)
    logger.info(
        "builtin_agent_stable_local_preset_applied",
        orchestrator_model=orchestrator_model,
        main_model=main_model,
        large_model=large_model,
    )
    return config


@router.post("/agent-config/reset", response_model=BuiltinAgentConfig)
async def reset_agent_config() -> BuiltinAgentConfig:
    config = reset_builtin_agent_config()
    _sync_ai_model_agent(config.model)
    logger.info("builtin_agent_config_reset")
    return config


@router.get("/agent-skills")
async def list_agent_skills() -> dict:
    config = get_builtin_agent_config()
    approval_gates = set(config.approval_gates)

    # Capabilities mode: return capabilities, not raw registry endpoints
    if gateway_config.skills_mode == "capabilities":
        cap_path = gateway_config.capabilities_path
        if not cap_path.exists():
            return {"skills": [], "mode": "capabilities"}
        data = yaml.safe_load(cap_path.read_text(encoding="utf-8")) or {}
        skills = []
        for cap in data.get("capabilities") or []:
            name = cap.get("name")
            if not name:
                continue
            gate_actions = cap.get("gate_actions") or []
            skills.append({
                "name": name,
                "description": (cap.get("description") or "").strip()[:200],
                "method": cap.get("method", "POST"),
                "path": cap.get("path", f"/api/agent/cap/{name}"),
                "enabled": True,
                "approval_required": bool(gate_actions),
                "gate_actions": gate_actions,
            })
        return {"skills": skills, "mode": "capabilities"}

    # Legacy registry mode: deduplicate by name
    registry_path = gateway_config.registry_path
    if not registry_path.exists():
        return {"skills": [], "mode": "registry"}
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    exposed = set(config.exposed_skills)
    seen: set[str] = set()
    skills = []
    for skill in data.get("skills") or data.get("tools") or []:
        name = skill.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        skills.append({
            "name": name,
            "description": skill.get("description", ""),
            "method": skill.get("method", ""),
            "path": skill.get("path", ""),
            "enabled": name in exposed,
            "approval_required": name in approval_gates,
        })
    return {"skills": skills, "mode": "registry"}


@router.get("/embedding-profile")
async def get_embedding_profile() -> dict:
    from app.ai.embeddings import get_active_embedding_profile

    return get_active_embedding_profile().__dict__


@router.get("/models/capabilities")
async def list_model_capabilities() -> dict:
    """List registry models with embedding/reranker capabilities."""
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    return {
        "models": [
            model.model_dump(mode="json")
            for model in registry.models.values()
        ],
        "routes": {
            task.value: route.model_dump(mode="json")
            for task, route in registry.routes.items()
            if task in {AITask.EMBEDDING, AITask.RERANKING}
        },
    }


@router.get("/models/discover")
async def discover_local_model_capabilities() -> dict:
    """Discover local Ollama models and infer initial embedding/reranker metadata."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Ollama unavailable: {e}")

    discovered = []
    for item in data.get("models", []):
        name = item.get("name", "")
        details = item.get("details", {})
        family = details.get("family") or ""
        modalities = ["text"]
        if any(token in name.lower() for token in ("embed", "bge", "e5", "nomic")):
            modalities = [Modality.EMBEDDING.value]
        discovered.append(
            {
                "name": name,
                "provider": "ollama",
                "provider_model": name,
                "modalities": modalities,
                "model_family": family,
                "parameter_size": details.get("parameter_size", ""),
                "capability_source": "discovered",
                "embedding_dimension": _known_embedding_dimension(name),
                "distance_metric": "cosine",
                "normalize_embeddings": True,
                "supports_batching": Modality.EMBEDDING.value in modalities,
            }
        )
    return {"models": discovered}


def _known_embedding_dimension(model_name: str) -> int | None:
    name = model_name.lower()
    if "nomic-embed-text" in name:
        return 768
    if "multilingual-e5-large" in name:
        return 1024
    if "bge-m3" in name:
        return 1024
    return None
