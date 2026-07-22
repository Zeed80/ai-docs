"""Provider instances API — manage AI provider nodes and cloud API keys.

One UI surface for both local nodes (multiple Ollama/vLLM/llama.cpp endpoints on
different machines) and cloud providers (Anthropic/OpenRouter/…). API keys are
encrypted at rest (see app.ai.secret_box) and never returned in clear — only a
mask and an ``api_key_set`` flag.

Endpoints (prefix /api/providers):
  GET    /                       — list instances grouped by kind + known kinds
  POST   /                       — add an instance (node)
  PUT    /{instance_id}          — update base_url / name / enabled / api_key
  DELETE /{instance_id}          — remove an instance
  POST   /{instance_id}/test     — health/connection check
  POST   /{instance_id}/refresh-models — pull available models (cloud) / sync (local)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import provider_registry
from app.ai import model_runtime_store
from app.ai.model_registry import ModelRegistry
from app.ai.schemas import AITask, ModelCapability, ModelStatus, ProviderKind
from app.ai.secret_box import decrypt, encrypt, mask
from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole
from app.db.models import ProviderInstance
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()

_LOCAL_KINDS = {
    ProviderKind.OLLAMA,
    ProviderKind.VLLM,
    ProviderKind.LLAMACPP,
    ProviderKind.OPENAI_COMPATIBLE,
    ProviderKind.LMSTUDIO,
    ProviderKind.COMFYUI,
}

_admin = [Depends(require_role(UserRole.admin))]


# ── Schemas ─────────────────────────────────────────────────────────────────


class ProviderInstanceOut(BaseModel):
    id: str
    kind: str
    name: str
    base_url: str | None       # effective URL (stored override or default)
    default_base_url: str       # the kind's default from the registry
    enabled: bool
    is_local: bool
    api_key_set: bool
    api_key_mask: str
    extra: dict                 # {headers: {...}, body: {...}} — provider-specific params
    last_check_at: datetime | None
    last_check_ok: bool | None
    last_error: str | None


class KnownKind(BaseModel):
    kind: str
    is_local: bool
    default_base_url: str
    requires_api_key: bool


class ProvidersListOut(BaseModel):
    instances: list[ProviderInstanceOut]
    known_kinds: list[KnownKind]


class ProviderInstanceCreate(BaseModel):
    kind: str
    name: str
    base_url: str | None = None
    enabled: bool = True
    is_local: bool | None = None
    api_key: str | None = None


class ProviderInstanceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    enabled: bool | None = None
    api_key: str | None = None  # "" clears the key; None leaves it unchanged
    extra: dict | None = None   # {headers: {...}, body: {...}}; replaces if provided


# ── Helpers ─────────────────────────────────────────────────────────────────


def _registry() -> ModelRegistry:
    return ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")


def _default_base_url(kind: str) -> str:
    """Effective default endpoint for a kind (registry YAML + env overrides)."""
    try:
        return provider_registry._default_instance(ProviderKind(kind)).base_url  # noqa: SLF001
    except Exception:
        return ""


def _to_out(inst: ProviderInstance) -> ProviderInstanceOut:
    key = decrypt(inst.api_key_encrypted)
    default_url = _default_base_url(inst.kind)
    return ProviderInstanceOut(
        id=str(inst.id),
        kind=inst.kind,
        name=inst.name,
        base_url=inst.base_url or default_url,
        default_base_url=default_url,
        enabled=inst.enabled,
        is_local=inst.is_local,
        extra=inst.extra or {},
        api_key_set=bool(key),
        api_key_mask=mask(key),
        last_check_at=inst.last_check_at,
        last_check_ok=inst.last_check_ok,
        last_error=inst.last_error,
    )


async def _sync_cache(db: AsyncSession) -> None:
    await provider_registry.refresh_cache_from_db(db)


# ── List + known kinds ──────────────────────────────────────────────────────


@router.get("", response_model=ProvidersListOut, dependencies=_admin)
@router.get("/", response_model=ProvidersListOut, dependencies=_admin)
async def list_providers(db: AsyncSession = Depends(get_db)) -> ProvidersListOut:
    result = await db.execute(select(ProviderInstance).order_by(ProviderInstance.kind, ProviderInstance.name))
    instances = [_to_out(i) for i in result.scalars().all()]

    registry = _registry()
    known: list[KnownKind] = []
    for kind, cfg in registry.providers.items():
        known.append(
            KnownKind(
                kind=kind.value,
                is_local=cfg.is_local,
                default_base_url=str(cfg.base_url),
                requires_api_key=bool(cfg.api_key_env) or not cfg.is_local,
            )
        )
    return ProvidersListOut(instances=instances, known_kinds=known)


# ── Create ──────────────────────────────────────────────────────────────────


@router.post("", response_model=ProviderInstanceOut, dependencies=_admin)
@router.post("/", response_model=ProviderInstanceOut, dependencies=_admin)
async def create_provider(
    payload: ProviderInstanceCreate, db: AsyncSession = Depends(get_db)
) -> ProviderInstanceOut:
    try:
        kind = ProviderKind(payload.kind)
    except ValueError:
        raise HTTPException(400, f"Unknown provider kind: {payload.kind}")

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    dup = await db.scalar(select(ProviderInstance).where(ProviderInstance.name == name))
    if dup:
        raise HTTPException(409, f"Provider node '{name}' already exists")

    is_local = payload.is_local if payload.is_local is not None else (kind in _LOCAL_KINDS)
    inst = ProviderInstance(
        kind=kind.value,
        name=name,
        base_url=(payload.base_url or "").strip() or None,
        enabled=payload.enabled,
        is_local=is_local,
        api_key_encrypted=encrypt(payload.api_key) if payload.api_key else None,
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)
    await _sync_cache(db)
    logger.info("provider_instance_created", kind=kind.value, name=name)
    return _to_out(inst)


# ── Update ──────────────────────────────────────────────────────────────────


async def _get_or_404(db: AsyncSession, instance_id: str) -> ProviderInstance:
    inst = await db.get(ProviderInstance, instance_id)
    if not inst:
        raise HTTPException(404, "Provider instance not found")
    return inst


@router.put("/{instance_id}", response_model=ProviderInstanceOut, dependencies=_admin)
async def update_provider(
    instance_id: str, payload: ProviderInstanceUpdate, db: AsyncSession = Depends(get_db)
) -> ProviderInstanceOut:
    inst = await _get_or_404(db, instance_id)
    if payload.name is not None:
        inst.name = payload.name.strip() or inst.name
    if payload.base_url is not None:
        new_url = payload.base_url.strip()
        # Storing the default URL → keep it as inherited (None) so future default
        # changes propagate and the row stays clean.
        inst.base_url = None if (not new_url or new_url == _default_base_url(inst.kind)) else new_url
    if payload.enabled is not None:
        inst.enabled = payload.enabled
    if payload.api_key is not None:
        # "" clears the stored key; non-empty replaces it.
        inst.api_key_encrypted = encrypt(payload.api_key) if payload.api_key else None
    if payload.extra is not None:
        inst.extra = payload.extra or None
    await db.commit()
    await db.refresh(inst)
    await _sync_cache(db)
    return _to_out(inst)


# ── Delete ──────────────────────────────────────────────────────────────────


@router.delete("/{instance_id}", dependencies=_admin)
async def delete_provider(instance_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    inst = await _get_or_404(db, instance_id)
    await db.delete(inst)
    await db.commit()
    await _sync_cache(db)
    return {"ok": True}


# ── Test connection ─────────────────────────────────────────────────────────


@router.post("/{instance_id}/test", dependencies=_admin)
async def test_provider(instance_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    inst = await _get_or_404(db, instance_id)
    kind = ProviderKind(inst.kind)
    resolved = provider_registry.select_instance(kind)
    # Force the URL/key of THIS row (select_instance picks first enabled node).
    base = (inst.base_url or resolved.base_url or "").rstrip("/")
    api_key = decrypt(inst.api_key_encrypted) or resolved.api_key

    ok = False
    error: str | None = None
    model_count = 0
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if kind == ProviderKind.OLLAMA:
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                model_count = len(resp.json().get("models", []))
            elif kind == ProviderKind.COMFYUI:
                # ComfyUI exposes no /models; /system_stats confirms it's alive.
                resp = await client.get(f"{base}/system_stats")
                resp.raise_for_status()
            else:
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                url = base if base.endswith("/v1") else f"{base}/v1"
                resp = await client.get(f"{url}/models", headers=headers)
                resp.raise_for_status()
                model_count = len(resp.json().get("data", []))
        ok = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    inst.last_check_at = datetime.now(timezone.utc)
    inst.last_check_ok = ok
    inst.last_error = error
    await db.commit()
    await _sync_cache(db)
    return {"ok": ok, "error": error, "model_count": model_count}


# ── Refresh models (cloud auto-fetch / local sync) ──────────────────────────


@router.post("/{instance_id}/refresh-models", dependencies=_admin)
async def refresh_models(instance_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    inst = await _get_or_404(db, instance_id)
    kind = ProviderKind(inst.kind)
    if kind in _LOCAL_KINDS:
        # Local availability is discovered live by the router; nothing to persist.
        return {"ok": True, "added": [], "note": "local models are discovered live"}

    resolved = provider_registry.select_instance(kind)
    base = (inst.base_url or resolved.base_url or "").rstrip("/")
    api_key = decrypt(inst.api_key_encrypted) or resolved.api_key
    if not api_key:
        raise HTTPException(400, "API key is not set for this provider")

    url = base if base.endswith("/v1") else f"{base}/v1"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{url}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Failed to fetch models: {exc}")

    from app.ai.schemas import ModelCapability, Modality, ModelStatus

    registry = _registry()
    added: list[str] = []
    for item in data:
        provider_model = item.get("id")
        if not provider_model:
            continue
        key = f"{kind.value}_{provider_model}".replace("/", "_").replace(":", "_").replace(".", "_")
        cap = ModelCapability(
            name=key,
            provider=kind,
            provider_model=provider_model,
            status=ModelStatus.CANDIDATE,
            modalities={Modality.TEXT, Modality.TOOL_CALLING},
            supports_tool_calling=True,
            supports_structured_output=True,
            local_only=False,
            capability_source="discovered",
            notes=f"Auto-fetched from {kind.value} on {time.strftime('%Y-%m-%d')}.",
        )
        registry.add_model(key, cap, persist=True)
        await model_runtime_store.persist_catalog_entry(
            db,
            model_key=key,
            provider=kind.value,
            provider_model=provider_model,
            capability=cap.model_dump(mode="json", exclude={"name"}),
            source="cloud_refresh",
            verification_status="discovered",
        )
        added.append(key)

    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "added": added, "count": len(added)}


# ── Model catalog (for assignment UI + thinking toggle) ─────────────────────


class CatalogModelOut(BaseModel):
    key: str
    provider: str
    provider_model: str
    status: str
    modalities: list[str]
    local_only: bool
    thinking_supported: bool
    thinking_enabled: bool
    preferred_instance: str | None
    quality_score: float
    speed_score: float
    vram_gb_estimate: float | None


@router.get("/models", response_model=list[CatalogModelOut], dependencies=_admin)
async def list_models(
    include_disabled: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[CatalogModelOut]:
    """Catalog models with status + thinking flags. The UI filters by ``status``
    to declutter (production by default, ``include all`` reveals candidates)."""
    registry = _registry()
    out: list[CatalogModelOut] = []
    for key, cap in registry.models.items():
        if not include_disabled and cap.status.value == "disabled":
            continue
        out.append(
            CatalogModelOut(
                key=key,
                provider=cap.provider.value,
                provider_model=cap.provider_model,
                status=cap.status.value,
                modalities=sorted(m.value for m in cap.modalities),
                local_only=cap.local_only,
                thinking_supported=cap.thinking_supported,
                thinking_enabled=cap.thinking_enabled,
                preferred_instance=cap.preferred_instance,
                quality_score=cap.quality_score,
                speed_score=cap.speed_score,
                vram_gb_estimate=cap.vram_gb_estimate,
            )
        )
    return out


class LiveModelOut(BaseModel):
    key: str
    provider: str
    provider_model: str
    status: str               # production/candidate/loaded/…
    modalities: list[str]
    local_only: bool
    thinking_supported: bool
    thinking_enabled: bool
    loaded: bool              # actually present on a node right now
    node: str | None          # which node hosts it (local multi-node)
    vram_gb_estimate: float | None


_VISION_HINTS = (
    "vl", "vision", "llava", "gemma3", "gemma4", "minicpm-v", "moondream",
    "internvl", "glm-4v", "glm4v", "pixtral", "llama3.2-vision", "qwen2.5vl",
    "qwen3-vl", "qwen3.5", "qwen3.6",
)
_THINK_HINTS = (
    "qwen3", "deepseek-r1", "deepseek_r1", "qwq", "reasoner", "thinking",
    "gpt-oss", "magistral", "r1", "marco-o1", "skywork-o1",
)


def _infer_modalities(name: str) -> set[str]:
    n = name.lower()
    if "embed" in n:
        return {"embedding"}
    if "rerank" in n:
        return {"rerank"}
    mods = {"text", "tool_calling"}
    if any(h in n for h in _VISION_HINTS):
        mods.add("vision")
    return mods


def _infer_thinking(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _THINK_HINTS)


def _synth_key(provider: str, provider_model: str) -> str:
    raw = f"{provider}_{provider_model}"
    return "".join(c if c.isalnum() else "_" for c in raw).strip("_")


async def _node_loaded_models(resolved) -> list[tuple[str, float | None]]:
    """Return (provider_model, vram_gb|None) for models loaded on a node."""
    base = resolved.base_url.rstrip("/")
    out: list[tuple[str, float | None]] = []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            if resolved.kind == ProviderKind.OLLAMA:
                r = await client.get(f"{base}/api/tags")
                r.raise_for_status()
                for m in r.json().get("models", []):
                    size = m.get("size")
                    out.append((m.get("name", ""), round(size / 1e9, 1) if size else None))
            else:
                url = base if base.endswith("/v1") else f"{base}/v1"
                r = await client.get(f"{url}/models")
                r.raise_for_status()
                for m in r.json().get("data", []):
                    out.append((m.get("id", ""), None))
    except Exception:
        return []
    return [(n, v) for n, v in out if n]


@router.get("/live-models", response_model=list[LiveModelOut], dependencies=_admin)
async def live_models(db: AsyncSession = Depends(get_db)) -> list[LiveModelOut]:
    """All selectable models: every model actually loaded on every configured
    provider node, merged with catalog metadata. Discovered models are registered
    into the catalog overlay so they get a stable key (assignable + thinking)."""
    from app.ai.schemas import ModelCapability, Modality, ModelStatus

    registry = _registry()
    # catalog: provider_model (per provider) → (key, cap)
    by_pm: dict[tuple[str, str], tuple[str, object]] = {}
    for key, cap in registry.models.items():
        pm_key = (cap.provider.value, cap.provider_model)
        existing = by_pm.get(pm_key)
        if existing is None:
            by_pm[pm_key] = (key, cap)
            continue
        _existing_key, existing_cap = existing
        existing_is_weaker = (
            existing_cap.capability_source == "discovered" and cap.capability_source != "discovered"
        ) or (
            existing_cap.status != ModelStatus.PRODUCTION and cap.status == ModelStatus.PRODUCTION
        )
        if existing_is_weaker:
            by_pm[pm_key] = (key, cap)

    out: dict[str, LiveModelOut] = {}
    seen_keys: set[str] = set()
    # Discovered models to persist once after the scan (race-safe upsert, single
    # commit) — never write/commit per-iteration inside this GET.
    discovered_to_persist: list[dict] = []

    # 1) Live local nodes.
    local_kinds = [ProviderKind.OLLAMA, ProviderKind.VLLM, ProviderKind.LLAMACPP,
                   ProviderKind.OPENAI_COMPATIBLE]
    for kind in local_kinds:
        for inst in provider_registry.list_instances(kind):
            loaded = await _node_loaded_models(inst)
            for pm, vram in loaded:
                bare = pm.split(":")[0]
                hit = by_pm.get((kind.value, pm)) or by_pm.get((kind.value, bare))
                if hit:
                    key, cap = hit
                    seen_keys.add(key)
                    out[key] = LiveModelOut(
                        key=key, provider=kind.value, provider_model=cap.provider_model,
                        status=cap.status.value, modalities=sorted(m.value for m in cap.modalities),
                        local_only=cap.local_only, thinking_supported=cap.thinking_supported,
                        thinking_enabled=cap.thinking_enabled, loaded=True, node=inst.name,
                        vram_gb_estimate=cap.vram_gb_estimate or vram,
                    )
                else:
                    # Discovered model — register into the catalog overlay.
                    key = _synth_key(kind.value, pm)
                    mods = _infer_modalities(pm)
                    thinking = _infer_thinking(pm)
                    if key not in registry.models:
                        cap = ModelCapability(
                            name=key, provider=kind, provider_model=pm,
                            status=ModelStatus.CANDIDATE,
                            modalities={Modality(m) for m in mods},
                            supports_tool_calling="tool_calling" in mods,
                            supports_structured_output=True, local_only=True,
                            thinking_supported=thinking, capability_source="discovered",
                            vram_gb_estimate=vram,
                        )
                        registry.add_model(key, cap, persist=True)
                        discovered_to_persist.append({
                            "model_key": key,
                            "provider": kind.value,
                            "provider_model": pm,
                            "capability": cap.model_dump(mode="json", exclude={"name"}),
                            "source": "local_live_discovery",
                            "verification_status": "discovered",
                        })
                    seen_keys.add(key)
                    th = registry.models[key]
                    out[key] = LiveModelOut(
                        key=key, provider=kind.value, provider_model=pm,
                        status="loaded", modalities=sorted(mods), local_only=True,
                        thinking_supported=th.thinking_supported,
                        thinking_enabled=th.thinking_enabled, loaded=True, node=inst.name,
                        vram_gb_estimate=vram,
                    )

    # 2) Catalog cloud models (selectable once a key is set; not "loaded").
    for key, cap in registry.models.items():
        if key in seen_keys or cap.status == ModelStatus.DISABLED:
            continue
        if cap.local_only:
            continue  # local models that aren't loaded are hidden
        out[key] = LiveModelOut(
            key=key, provider=cap.provider.value, provider_model=cap.provider_model,
            status=cap.status.value, modalities=sorted(m.value for m in cap.modalities),
            local_only=cap.local_only, thinking_supported=cap.thinking_supported,
            thinking_enabled=cap.thinking_enabled, loaded=False, node=None,
            vram_gb_estimate=cap.vram_gb_estimate,
        )

    # Persist newly-discovered models once (race-safe upsert + single commit).
    # Best-effort: a GET must still return the list even if the write fails.
    if discovered_to_persist:
        try:
            for entry in discovered_to_persist:
                await model_runtime_store.persist_catalog_entry(db, **entry)
            await db.commit()
            await model_runtime_store.hydrate_runtime_cache(db)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.warning("live_models_discovery_persist_failed", error=str(exc))

    return list(out.values())


class ThinkingUpdate(BaseModel):
    enabled: bool


@router.patch("/models/{model_key}/thinking", dependencies=_admin)
async def set_model_thinking(
    model_key: str,
    payload: ThinkingUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Toggle the per-model reasoning (CoT) flag — the local-model checkbox."""
    from app.ai.model_registry import set_thinking_override

    registry = _registry()
    if model_key not in registry.models:
        raise HTTPException(404, f"Unknown model: {model_key}")
    set_thinking_override(model_key, payload.enabled)
    await model_runtime_store.persist_model_override(
        db,
        model_key=model_key,
        thinking_enabled=payload.enabled,
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "model": model_key, "thinking_enabled": payload.enabled}


class PreferredInstanceUpdate(BaseModel):
    instance_name: str | None = None  # None/"" clears the pin


@router.patch("/models/{model_key}/preferred-instance", dependencies=_admin)
async def set_model_preferred_instance(
    model_key: str,
    payload: PreferredInstanceUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pin a model to a specific provider node (multi-machine routing)."""
    from app.ai.model_registry import set_preferred_instance

    registry = _registry()
    if model_key not in registry.models:
        raise HTTPException(404, f"Unknown model: {model_key}")
    set_preferred_instance(model_key, payload.instance_name or None)
    await model_runtime_store.persist_model_override(
        db,
        model_key=model_key,
        preferred_instance=payload.instance_name or "",
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "model": model_key, "preferred_instance": payload.instance_name or None}


# ── Simplified assignment slots ─────────────────────────────────────────────
# Practical slots fan out to task_routing + agent_config + ai_config mirror.
# The UI uses assignment-draft endpoints; PUT /slots/{slot} remains for
# backward compatibility and tests.


class SlotOut(BaseModel):
    slot: str
    group: str
    label: str
    hint: str
    model: str | None              # catalog key currently shown by this response (current or draft)
    current_model: str | None = None  # catalog key actually applied right now
    local_only: bool               # cloud models forbidden for this slot
    required_modality: str | None = None  # capability the slot needs (UI ⚠ source)
    thinking_capable: bool = False        # compatibility alias: slot + selected model support reasoning
    thinking_enabled: bool | None = None  # compatibility alias: per-assignment override
    thinking_supported_by_slot: bool = False
    thinking_supported_by_model: bool = False
    thinking_model_default: bool | None = None
    thinking_override: bool | None = None
    thinking_effective: bool | None = None
    thinking_source: str = "unsupported"  # slot | model | unsupported
    thinking_disable_supported: bool = True
    thinking_warning: str | None = None


# local_only=True → конфиденциальные задачи (содержимое документов), облако
# запрещено. Агентские слоты допускают облако (выбор оператора).
_SLOTS = [
    ("ocr_fast", "Документы", "Быстрая (OCR/VLM)",
     "OCR счётов, классификация и первичный VLM-анализ", True),
    ("structured_extraction", "Документы", "Извлечение полей",
     "Структурированное извлечение и текстовая проверка документов", True),
    ("ocr_large", "Документы", "Крупная (сложные случаи)",
     "Повторное извлечение при низкой уверенности/ошибках", True),
    ("agent_orchestrator", "Агент", "Оркестратор",
     "Планирование, вызов инструментов, диалог", False),
    ("agent_fast", "Агент", "Быстрая (роутер/простые ходы)",
     "Классификация хода и быстрые ответы — лёгкая модель, structured output", False),
    ("agent_email", "Агент", "Письма",
     "Генерация деловых писем и черновиков", False),
    ("agent_large", "Агент", "Большая (скиллы/скрипты/ТП)",
     "Генерация кода, навыков, техпроцессов", False),
    ("embedding", "Поиск", "Векторизация (embedding)",
     "Семантический поиск по документам", True),
    ("rerank", "Поиск", "Реранкинг",
     "Переранжирование результатов поиска", True),
    ("cad_spec_read", "Оцифровка", "Чтение чертежа (VLM)",
     "Вспомогательное чтение параметрического ТЗ; не основной graph-reader", True),
    ("cad_drawing_graph_read", "Оцифровка", "Координатный reader листа",
     "Основной метод «по описанию»: все элементы, текст, размеры и связи с координатами", True),
    ("cad_drawing_graph_evidence_verify", "Оцифровка", "VLM-проверка надписей",
     "Независимое чтение source-resolution crops: текст, размеры, допуски и обозначения; без Tesseract", True),
    ("cad_spec_draft", "Оцифровка", "Чертёжник (по описанию)",
     "Генеративная модель строит геометрию из описания (можно LoRA). "
     "Не задано → детерминированный чертёжник тел вращения", True),
]

_SLOT_MODALITY = {
    "ocr_fast": "vision",
    "structured_extraction": "text",
    "ocr_large": "vision",
    "agent_orchestrator": "tool_calling",
    "agent_fast": "text",
    "agent_email": "text",
    "agent_large": "text",
    "embedding": "embedding",
    "rerank": "rerank",
    "cad_spec_read": "vision",
    "cad_drawing_graph_read": "vision",
    "cad_drawing_graph_evidence_verify": "vision",
    "cad_spec_draft": "text",
}

# Per-assignment thinking storage. Task slots store the override in
# task_routing.thinking of each listed AITask; agent slots store it in the
# agent_config tri-state *_disable_thinking field(s). Slots absent here don't
# support reasoning (embedding/rerank/ocr_large) → no toggle.
_SLOT_THINKING_TASKS: dict[str, list[str]] = {
    "ocr_fast": ["invoice_ocr", "classification", "drawing_analysis", "drawing_analysis_vlm"],
    "structured_extraction": ["structured_extraction", "long_context_summarization"],
    "agent_email": ["email_drafting"],
    "agent_large": ["code_generation"],
    "cad_drawing_graph_read": ["cad_drawing_graph_read"],
    "cad_drawing_graph_evidence_verify": ["cad_drawing_graph_evidence_verify"],
}
_SLOT_THINKING_AGENT_FIELDS: dict[str, list[str]] = {
    "agent_orchestrator": ["orchestrator_disable_thinking", "worker_disable_thinking"],
    "agent_fast": ["fast_disable_thinking"],
    "agent_large": ["builder_disable_thinking"],
}

_THINKING_DISABLE_SUPPORTED_PROVIDERS = {
    "ollama",
    "llamacpp",
    "vllm",
    "openrouter",
    "ollama_cloud",
    "openai",
    "groq",
    "xai",
    "dashscope",
    "qwen",
    "cerebras",
}


def _key_for_raw(registry, raw: str | None) -> str | None:
    """Map a raw provider_model name (or key) to its catalog key."""
    if not raw:
        return None
    for k, cap in registry.models.items():
        if k == raw or cap.provider_model == raw:
            return k
    return raw


def _slot_current_model(slot: str, registry) -> str | None:
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for

    if slot == "ocr_fast":
        return get_routing_for(AITask.INVOICE_OCR).primary
    if slot == "structured_extraction":
        return get_routing_for(AITask.STRUCTURED_EXTRACTION).primary
    if slot == "embedding":
        return get_routing_for(AITask.EMBEDDING).primary
    if slot == "rerank":
        return get_routing_for(AITask.RERANKING).primary
    if slot == "agent_email":
        return get_routing_for(AITask.EMAIL_DRAFTING).primary
    if slot == "cad_spec_read":
        return get_routing_for(AITask.CAD_SPEC_READ).primary
    if slot == "cad_drawing_graph_read":
        return get_routing_for(AITask.CAD_DRAWING_GRAPH_READ).primary
    if slot == "cad_drawing_graph_evidence_verify":
        return get_routing_for(AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY).primary
    if slot == "cad_spec_draft":
        return get_routing_for(AITask.CAD_SPEC_DRAFT).primary
    if slot == "ocr_large":
        try:
            from app.api.ai_settings import get_ai_config
            return get_ai_config().get("model_ocr_fallback")
        except Exception:
            return None
    cfg = get_builtin_agent_config()
    if slot == "agent_orchestrator":
        return _key_for_raw(registry, cfg.orchestrator_model or cfg.model)
    if slot == "agent_fast":
        return _key_for_raw(registry, cfg.fast_model)
    if slot == "agent_large":
        return _key_for_raw(registry, cfg.builder_model)
    return None


class SlotWrite(BaseModel):
    model: str  # catalog key


class AssignmentDraftIn(BaseModel):
    slots: dict[str, str | None]
    confirm_warnings: bool = False


class AssignmentIssue(BaseModel):
    slot: str
    model: str | None = None
    code: str
    message: str
    severity: str = "warning"


class AssignmentDiffItem(BaseModel):
    slot: str
    old_model: str | None
    new_model: str | None
    affected: list[str]


class AssignmentDraftOut(BaseModel):
    slots: list[SlotOut]
    diff: list[AssignmentDiffItem] = []
    warnings: list[AssignmentIssue] = []
    errors: list[AssignmentIssue] = []
    ok_to_apply: bool = True
    revision_id: str | None = None


def _slot_meta(slot: str):
    return next((s for s in _SLOTS if s[0] == slot), None)


def _slot_supports_thinking(slot: str) -> bool:
    return slot in _SLOT_THINKING_TASKS or slot in _SLOT_THINKING_AGENT_FIELDS


def _slot_thinking_override(slot: str) -> bool | None:
    """Current per-assignment reasoning override. None = model default."""
    if not _slot_supports_thinking(slot):
        return None
    if slot in _SLOT_THINKING_AGENT_FIELDS:
        from app.ai.agent_config import get_builtin_agent_config
        cfg = get_builtin_agent_config()
        field = _SLOT_THINKING_AGENT_FIELDS[slot][0]
        disable = getattr(cfg, field, None)
        return None if disable is None else (not disable)
    if slot in _SLOT_THINKING_TASKS:
        from app.ai.schemas import AITask
        from app.ai.task_routing import get_routing_for
        try:
            return get_routing_for(AITask(_SLOT_THINKING_TASKS[slot][0])).thinking
        except (ValueError, KeyError):
            return None
    return None


def _slot_thinking_state(slot: str, registry, model_key: str | None) -> dict[str, Any]:
    """Effective reasoning state for the selected model in a slot."""
    slot_supported = _slot_supports_thinking(slot)
    cap = registry.models.get(model_key) if model_key else None
    model_supported = bool(cap and cap.thinking_supported)
    model_default = cap.thinking_enabled if cap and cap.thinking_supported else None
    override = _slot_thinking_override(slot)
    if not slot_supported or not model_supported:
        effective = None
        source = "unsupported"
    elif override is not None:
        effective = override
        source = "slot"
    else:
        effective = bool(model_default)
        source = "model"
    provider = cap.provider.value if cap else None
    disable_supported = (
        provider in _THINKING_DISABLE_SUPPORTED_PROVIDERS
        if provider
        else True
    )
    warning = None
    if slot_supported and model_supported and effective is False and not disable_supported:
        warning = (
            "У этого провайдера нет известного API-параметра для выключения reasoning; "
            "сервер может проигнорировать override."
        )
    return {
        "thinking_capable": slot_supported and model_supported,
        "thinking_enabled": override,
        "thinking_supported_by_slot": slot_supported,
        "thinking_supported_by_model": model_supported,
        "thinking_model_default": model_default,
        "thinking_override": override,
        "thinking_effective": effective,
        "thinking_source": source,
        "thinking_disable_supported": disable_supported,
        "thinking_warning": warning,
    }


def _build_slot_out(
    slot: str,
    group: str,
    label: str,
    hint: str,
    local_only: bool,
    model: str | None,
    registry,
    *,
    current_model: str | None | object = ...,
) -> SlotOut:
    """SlotOut with single-source required_modality + effective reasoning state."""
    applied = _slot_current_model(slot, registry) if current_model is ... else current_model
    thinking = _slot_thinking_state(slot, registry, model)
    return SlotOut(
        slot=slot, group=group, label=label, hint=hint,
        model=model,
        current_model=applied,
        local_only=local_only,
        required_modality=_SLOT_MODALITY.get(slot),
        **thinking,
    )


def _all_slots_out(model_of, registry) -> list[SlotOut]:
    """Build every SlotOut; `model_of(slot)` returns the assigned model key."""
    return [
        _build_slot_out(
            slot,
            group,
            label,
            hint,
            local_only,
            model_of(slot),
            registry,
            current_model=_slot_current_model(slot, registry),
        )
        for slot, group, label, hint, local_only in _SLOTS
    ]


@router.get("/slots", response_model=list[SlotOut], dependencies=_admin)
async def get_slots() -> list[SlotOut]:
    registry = _registry()
    return _all_slots_out(lambda s: _slot_current_model(s, registry), registry)


def _slot_affected(slot: str) -> list[str]:
    if slot == "ocr_fast":
        return [
            AITask.INVOICE_OCR.value,
            AITask.CLASSIFICATION.value,
            AITask.DRAWING_ANALYSIS.value,
            AITask.DRAWING_ANALYSIS_VLM.value,
        ]
    if slot == "structured_extraction":
        return [AITask.STRUCTURED_EXTRACTION.value, AITask.LONG_CONTEXT_SUMMARIZATION.value]
    if slot == "ocr_large":
        return ["ai_config.model_ocr_fallback"]
    if slot == "embedding":
        return [AITask.EMBEDDING.value]
    if slot == "rerank":
        return [AITask.RERANKING.value]
    if slot == "agent_email":
        return [AITask.EMAIL_DRAFTING.value]
    if slot == "cad_spec_read":
        return [AITask.CAD_SPEC_READ.value]
    if slot == "cad_drawing_graph_read":
        return [AITask.CAD_DRAWING_GRAPH_READ.value]
    if slot == "cad_drawing_graph_evidence_verify":
        return [AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY.value]
    if slot == "cad_spec_draft":
        return [AITask.CAD_SPEC_DRAFT.value]
    if slot == "agent_orchestrator":
        return [
            "agent_config.orchestrator_model",
            "agent_config.worker_model",
            AITask.ORCHESTRATOR_PLANNING.value,
            AITask.TOOL_CALLING.value,
        ]
    if slot == "agent_fast":
        return ["agent_config.fast_model"]
    if slot == "agent_large":
        return ["agent_config.builder_model", AITask.CODE_GENERATION.value]
    return []


def _assignment_snapshot(registry) -> dict[str, Any]:
    return {"slots": {slot: _slot_current_model(slot, registry) for slot, *_ in _SLOTS}}


async def _loaded_index() -> dict[tuple[str, str], str]:
    """One pass over all local nodes → {(provider, model_or_bare): node}.

    Built once per request and reused, instead of per-slot HTTP fan-out to each
    node's /api/tags during draft validation.
    """
    index: dict[tuple[str, str], str] = {}
    for kind in _LOCAL_KINDS:
        for inst in provider_registry.list_instances(kind):
            for name, _vram in await _node_loaded_models(inst):
                index.setdefault((kind.value, name), inst.name)
                index.setdefault((kind.value, name.split(":")[0]), inst.name)
    return index


def _loaded_node_for(cap: ModelCapability, index: dict[tuple[str, str], str]) -> str | None:
    if cap.provider not in _LOCAL_KINDS:
        return None
    pv = cap.provider.value
    return (
        index.get((pv, cap.provider_model))
        or index.get((pv, cap.provider_model.split(":")[0]))
    )


def _verification_warning(
    slot: str, model_key: str, cap: ModelCapability, is_loaded: bool = False
) -> AssignmentIssue | None:
    """Return only actionable verification warnings.

    A production model with manually curated capabilities is not a failed eval.
    It is the normal state for the static YAML registry. Reserve failure wording
    for explicit failed verification records when such records are wired into
    the catalog.

    A model physically loaded on a node has proven it runs, so catalog-status
    caveats (disabled / not-production / auto-discovered profile) are suppressed
    — the operator already sees it working. Mirrors the frontend `selectable`
    rule where a loaded model is always selectable regardless of catalog status.
    """
    if is_loaded:
        return None
    if cap.status == ModelStatus.DISABLED:
        return AssignmentIssue(
            slot=slot,
            model=model_key,
            code="disabled_model",
            message="Модель отключена в каталоге; используйте только если она реально загружена и нужна как override",
        )
    if cap.status in {ModelStatus.CANDIDATE, ModelStatus.STAGING}:
        return AssignmentIssue(
            slot=slot,
            model=model_key,
            code="not_production",
            message="Модель ещё не переведена в production-профиль",
        )
    if cap.capability_source == "discovered":
        return AssignmentIssue(
            slot=slot,
            model=model_key,
            code="unverified_capability_profile",
            message="Модель обнаружена автоматически; capability-профиль ещё не подтверждён smoke/eval",
        )
    return None


async def _validate_assignment_draft(
    registry,
    draft: dict[str, str | None],
    loaded: dict[tuple[str, str], str] | None = None,
) -> tuple[list[AssignmentDiffItem], list[AssignmentIssue], list[AssignmentIssue]]:
    warnings: list[AssignmentIssue] = []
    errors: list[AssignmentIssue] = []
    diff: list[AssignmentDiffItem] = []
    current = _assignment_snapshot(registry)["slots"]
    if loaded is None:
        loaded = await _loaded_index()

    for slot, model_key in draft.items():
        meta = _slot_meta(slot)
        if meta is None:
            errors.append(AssignmentIssue(slot=slot, model=model_key, code="unknown_slot", message="Неизвестный слот", severity="error"))
            continue
        if not model_key:
            # Explicit unset (e.g. rollback to an empty old_model) — emit a diff
            # so it is actually applied; no model = no capability checks.
            old = current.get(slot)
            if old is not None:
                diff.append(AssignmentDiffItem(slot=slot, old_model=old, new_model=None, affected=_slot_affected(slot)))
            continue
        cap = registry.models.get(model_key)
        if cap is None:
            errors.append(AssignmentIssue(slot=slot, model=model_key, code="unknown_model", message="Модель не найдена в каталоге", severity="error"))
            continue
        if bool(meta[4]) and not cap.local_only:
            errors.append(AssignmentIssue(slot=slot, model=model_key, code="cloud_for_confidential", message="Конфиденциальный слот допускает только локальные модели", severity="error"))
        required = _SLOT_MODALITY.get(slot)
        if required and required not in {m.value for m in cap.modalities}:
            warnings.append(AssignmentIssue(slot=slot, model=model_key, code="modality_mismatch", message=f"Модель не заявляет capability '{required}'"))
        # A loaded local model has proven it runs → suppress catalog-status and
        # not-loaded caveats; only real constraints (modality/confidential) stand.
        is_loaded = cap.provider in _LOCAL_KINDS and _loaded_node_for(cap, loaded) is not None
        verification_warning = _verification_warning(slot, model_key, cap, is_loaded=is_loaded)
        if verification_warning is not None:
            warnings.append(verification_warning)
        if cap.provider in _LOCAL_KINDS and not is_loaded:
            warnings.append(AssignmentIssue(slot=slot, model=model_key, code="not_loaded", message="Модель не найдена ни на одном локальном узле сейчас"))
        old = current.get(slot)
        if old != model_key:
            diff.append(AssignmentDiffItem(slot=slot, old_model=old, new_model=model_key, affected=_slot_affected(slot)))
    return diff, warnings, errors


def _apply_slot_assignment(slot: str, model_key: str, registry) -> None:
    cap = registry.models.get(model_key)
    if cap is None:
        raise HTTPException(404, f"Unknown model: {model_key}")
    meta = _slot_meta(slot)
    if meta is None:
        raise HTTPException(400, f"Unknown slot: {slot}")
    if meta[4] and not cap.local_only:
        raise HTTPException(400, "Этот слот допускает только локальные модели (конфиденциально)")

    from app.ai.agent_config import BuiltinAgentConfigUpdate, update_builtin_agent_config
    from app.ai.assignment_groups import DocumentGroup, _mirror_ai_config, _set_primary
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for, save_task_routing

    def _assign_task(
        task: AITask,
        model_key: str,
        *,
        fallback_keys: list[str] | None = None,
    ) -> None:
        """Set primary + local/cloud policy from the model (non-confidential tasks).

        Cloud model → local_only=False, allow_cloud=True so the AI router won't
        block it at dispatch; local model → local-only.
        """
        current = get_routing_for(task)
        valid_keys = set(registry.models)
        stale_tail = [m for m in current.models if m != model_key and m not in valid_keys]
        if stale_tail:
            logger.warning(
                "task_routing_stale_fallbacks_dropped",
                task=task.value,
                models=stale_tail,
            )
        source_tail = current.models if fallback_keys is None else fallback_keys
        tail = [m for m in source_tail if m != model_key and m in valid_keys]
        routing = current.model_copy(update={
            "models": [model_key, *tail],
            "local_only": cap.local_only,
            "allow_cloud": not cap.local_only,
        })
        save_task_routing(task, routing)

    key = model_key
    try:
        if slot == "ocr_fast":
            for t in (
                AITask.INVOICE_OCR, AITask.CLASSIFICATION,
                AITask.DRAWING_ANALYSIS, AITask.DRAWING_ANALYSIS_VLM,
            ):
                _set_primary(t, key)
            _mirror_ai_config(DocumentGroup(vision_model=key))
        elif slot == "structured_extraction":
            for t in (AITask.STRUCTURED_EXTRACTION, AITask.LONG_CONTEXT_SUMMARIZATION):
                _set_primary(t, key)
            _mirror_ai_config(DocumentGroup(text_model=key))
        elif slot == "ocr_large":
            _mirror_ai_config(DocumentGroup(ocr_fallback_model=key))
        elif slot == "embedding":
            _set_primary(AITask.EMBEDDING, key)
            _mirror_ai_config(DocumentGroup(embedding_model=key))
        elif slot == "rerank":
            _set_primary(AITask.RERANKING, key)
            _mirror_ai_config(DocumentGroup(rerank_model=key))
        elif slot == "agent_email":
            _assign_task(AITask.EMAIL_DRAFTING, key)
        elif slot == "cad_spec_read":
            # This slot has a safety-reviewed dedicated route. Do not retain an
            # old generic-VLM tail (notably qwen3-vl) after the operator changes
            # its primary model through the UI.
            cad_route = registry.routes.get(AITask.CAD_SPEC_READ)
            _assign_task(
                AITask.CAD_SPEC_READ,
                key,
                fallback_keys=list(cad_route.fallback_chain) if cad_route else [],
            )
        elif slot == "cad_drawing_graph_read":
            graph_route = registry.routes.get(AITask.CAD_DRAWING_GRAPH_READ)
            _assign_task(
                AITask.CAD_DRAWING_GRAPH_READ,
                key,
                fallback_keys=(
                    list(graph_route.fallback_chain) if graph_route else []
                ),
            )
        elif slot == "cad_drawing_graph_evidence_verify":
            evidence_route = registry.routes.get(
                AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY
            )
            _assign_task(
                AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY,
                key,
                fallback_keys=(
                    list(evidence_route.fallback_chain) if evidence_route else []
                ),
            )
        elif slot == "cad_spec_draft":
            _set_primary(AITask.CAD_SPEC_DRAFT, key)
        elif slot == "agent_orchestrator":
            # Orchestrator + worker + base model. fast_model is a SEPARATE slot
            # (agent_fast) so a heavy orchestrator no longer forces a heavy router.
            update_builtin_agent_config(BuiltinAgentConfigUpdate(
                provider=cap.provider.value, model=cap.provider_model,
                orchestrator_provider=cap.provider.value, orchestrator_model=cap.provider_model,
                worker_provider=cap.provider.value, worker_model=cap.provider_model,
            ))
            for t in (AITask.ORCHESTRATOR_PLANNING, AITask.TOOL_CALLING):
                _assign_task(t, key)
        elif slot == "agent_fast":
            update_builtin_agent_config(BuiltinAgentConfigUpdate(
                fast_provider=cap.provider.value, fast_model=cap.provider_model,
            ))
        elif slot == "agent_large":
            update_builtin_agent_config(BuiltinAgentConfigUpdate(
                builder_provider=cap.provider.value, builder_model=cap.provider_model,
            ))
            _assign_task(AITask.CODE_GENERATION, key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _unset_slot(slot: str, registry) -> None:
    """Revert a slot to its YAML/default (used by rollback to an empty old_model)."""
    from app.ai.agent_config import (
        BuiltinAgentConfigUpdate,
        _default_config,
        update_builtin_agent_config,
    )
    from app.ai.schemas import AITask
    from app.ai.task_routing import reset_task_routing

    defaults = _default_config()
    if slot == "agent_orchestrator":
        update_builtin_agent_config(BuiltinAgentConfigUpdate(
            model=defaults.model, orchestrator_model=defaults.orchestrator_model,
            worker_model=defaults.worker_model,
        ))
        for t in (AITask.ORCHESTRATOR_PLANNING, AITask.TOOL_CALLING):
            reset_task_routing(t)
    elif slot == "agent_fast":
        update_builtin_agent_config(BuiltinAgentConfigUpdate(fast_model=defaults.fast_model))
    elif slot == "agent_large":
        update_builtin_agent_config(BuiltinAgentConfigUpdate(builder_model=defaults.builder_model))
        reset_task_routing(AITask.CODE_GENERATION)
    else:
        for item in _slot_affected(slot):
            if "." not in item:
                try:
                    reset_task_routing(AITask(item))
                except ValueError:
                    pass


async def _persist_slot_durable(db: AsyncSession, slot: str) -> None:
    """Mirror a just-applied slot's effective state into Postgres (durable).

    task_routing and agent_config are otherwise Redis-only and reset on a flush.
    (ocr_large maps only to ai_config — a separate store, out of scope here.)
    """
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for

    agent_done = False
    for item in _slot_affected(slot):
        if item.startswith("agent_config."):
            if not agent_done:
                await model_runtime_store.persist_agent_config(
                    db, config=get_builtin_agent_config().model_dump(mode="json"))
                agent_done = True
        elif "." not in item:  # an AITask value
            try:
                task = AITask(item)
            except ValueError:
                continue
            await model_runtime_store.persist_task_routing(
                db, task=task.value, routing=get_routing_for(task).model_dump(mode="json"))


async def _apply_draft_atomic(
    db: AsyncSession, diff: list[AssignmentDiffItem], before: dict, registry,
) -> None:
    """Apply all slots; on any failure restore already-applied slots to `before`.

    Redis writes are outside the DB transaction, so we compensate manually to
    avoid a half-applied assignment with no revision.
    """
    applied: list[str] = []
    try:
        for item in diff:
            if item.new_model:
                _apply_slot_assignment(item.slot, item.new_model, registry)
            else:
                _unset_slot(item.slot, registry)
            applied.append(item.slot)
            await _persist_slot_durable(db, item.slot)
    except Exception:
        for slot in applied:
            old = before["slots"].get(slot)
            try:
                if old:
                    _apply_slot_assignment(slot, old, registry)
                else:
                    _unset_slot(slot, registry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("assignment_rollback_failed", slot=slot, error=str(exc))
        raise


@router.get("/assignment-draft", response_model=AssignmentDraftOut, dependencies=_admin)
async def get_assignment_draft(db: AsyncSession = Depends(get_db)) -> AssignmentDraftOut:
    registry = _registry()
    return AssignmentDraftOut(
        slots=_all_slots_out(lambda s: _slot_current_model(s, registry), registry)
    )


@router.post("/assignment-draft/validate", response_model=AssignmentDraftOut, dependencies=_admin)
async def validate_assignment_draft(
    payload: AssignmentDraftIn,
    db: AsyncSession = Depends(get_db),
) -> AssignmentDraftOut:
    registry = _registry()
    diff, warnings, errors = await _validate_assignment_draft(registry, payload.slots)
    return AssignmentDraftOut(
        slots=_all_slots_out(lambda s: payload.slots.get(s, _slot_current_model(s, registry)), registry),
        diff=diff,
        warnings=warnings,
        errors=errors,
        ok_to_apply=not errors,
    )


@router.post("/assignment-draft/apply", response_model=AssignmentDraftOut, dependencies=_admin)
async def apply_assignment_draft(
    payload: AssignmentDraftIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> AssignmentDraftOut:
    registry = _registry()
    loaded = await _loaded_index()
    diff, warnings, errors = await _validate_assignment_draft(registry, payload.slots, loaded)
    if errors:
        raise HTTPException(400, {"errors": [e.model_dump() for e in errors]})
    if warnings and not payload.confirm_warnings:
        raise HTTPException(409, {"warnings": [w.model_dump() for w in warnings]})

    before = _assignment_snapshot(registry)
    await _apply_draft_atomic(db, diff, before, registry)  # rolls back Redis on error
    after_registry = _registry()
    after = _assignment_snapshot(after_registry)
    revision = await model_runtime_store.create_assignment_revision(
        db,
        created_by=user.sub,
        before_snapshot=before,
        after_snapshot=after,
        diff=[d.model_dump() for d in diff],
        warnings=[w.model_dump() for w in warnings],
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return AssignmentDraftOut(
        slots=_all_slots_out(lambda s: _slot_current_model(s, after_registry), after_registry),
        diff=diff,
        warnings=warnings,
        ok_to_apply=True,
        revision_id=str(revision.id),
    )


@router.post("/assignments/{revision_id}/rollback", response_model=AssignmentDraftOut, dependencies=_admin)
async def rollback_assignment_revision(
    revision_id: str,
    confirm_warnings: bool = False,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> AssignmentDraftOut:
    revision = await model_runtime_store.get_assignment_revision(db, revision_id)
    if revision is None:
        raise HTTPException(404, "Assignment revision not found")
    registry = _registry()
    loaded = await _loaded_index()
    before = _assignment_snapshot(registry)
    # Restore every changed slot to its old value, INCLUDING slots that were
    # previously unset (old_model is None) → explicit unset on rollback.
    target = {
        item.get("slot"): item.get("old_model")
        for item in (revision.diff or [])
        if item.get("slot")
    }
    diff, warnings, errors = await _validate_assignment_draft(registry, target, loaded)
    if errors:
        raise HTTPException(400, {"errors": [e.model_dump() for e in errors]})
    if warnings and not confirm_warnings:
        raise HTTPException(409, {"warnings": [w.model_dump() for w in warnings]})
    await _apply_draft_atomic(db, diff, before, registry)
    after_registry = _registry()
    after = _assignment_snapshot(after_registry)
    rollback_revision = await model_runtime_store.create_assignment_revision(
        db,
        created_by=user.sub,
        before_snapshot=before,
        after_snapshot=after,
        diff=[d.model_dump() for d in diff],
        warnings=[w.model_dump() for w in warnings],
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return AssignmentDraftOut(
        slots=_all_slots_out(lambda s: _slot_current_model(s, after_registry), after_registry),
        diff=diff,
        warnings=warnings,
        ok_to_apply=True,
        revision_id=str(rollback_revision.id),
    )


@router.put("/slots/{slot}", dependencies=_admin)
async def set_slot(
    slot: str,
    payload: SlotWrite,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Assign a model to a slot immediately. Kept for compatibility."""
    registry = _registry()
    _apply_slot_assignment(slot, payload.model, registry)
    await _persist_slot_durable(db, slot)
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    key = payload.model
    return {"ok": True, "slot": slot, "model": key}


class SlotThinkingWrite(BaseModel):
    enabled: bool | None  # None → model default; True/False → force on/off for this slot


class SlotSmokeIn(BaseModel):
    model: str | None = None
    thinking: bool | None = None
    dry_run: bool = True


class SlotSmokeOut(BaseModel):
    ok: bool
    slot: str
    model: str | None
    provider: str | None = None
    provider_model: str | None = None
    dry_run: bool = True
    thinking_requested: bool | None = None
    thinking_payload_supported: bool = True
    latency_ms: int | None = None
    warnings: list[AssignmentIssue] = []
    error: str | None = None


def _apply_slot_thinking(slot: str, enabled: bool | None) -> None:
    """Set per-assignment reasoning for a slot (task_routing + agent_config).

    The same model can thus reason in one slot and not in another. Idempotent.
    """
    # Task-routing slots: write thinking into each underlying task.
    if slot in _SLOT_THINKING_TASKS:
        from app.ai.schemas import AITask
        from app.ai.task_routing import get_routing_for, save_task_routing
        for tval in _SLOT_THINKING_TASKS[slot]:
            try:
                task = AITask(tval)
            except ValueError:
                continue
            routing = get_routing_for(task).model_copy(update={"thinking": enabled})
            save_task_routing(task, routing)
    # Agent-config slots: write the tri-state *_disable_thinking field(s).
    if slot in _SLOT_THINKING_AGENT_FIELDS:
        from app.ai.agent_config import BuiltinAgentConfigUpdate, update_builtin_agent_config
        disable = None if enabled is None else (not enabled)
        patch = {field: disable for field in _SLOT_THINKING_AGENT_FIELDS[slot]}
        update_builtin_agent_config(BuiltinAgentConfigUpdate(**patch))


@router.patch("/slots/{slot}/thinking", dependencies=_admin)
async def set_slot_thinking(
    slot: str,
    payload: SlotThinkingWrite,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-assignment reasoning toggle (None=model default, True/False=force)."""
    if not _slot_supports_thinking(slot):
        raise HTTPException(400, f"Слот '{slot}' не поддерживает переключение рассуждения")
    _apply_slot_thinking(slot, payload.enabled)
    await _persist_slot_durable(db, slot)
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "slot": slot, "thinking_enabled": payload.enabled}


def _slot_smoke_task(slot: str) -> AITask:
    if slot == "agent_fast":
        return AITask.ORCHESTRATOR_PLANNING
    affected = [item for item in _slot_affected(slot) if "." not in item]
    for item in affected:
        try:
            return AITask(item)
        except ValueError:
            continue
    return AITask.CLASSIFICATION


@router.post("/slots/{slot}/smoke", response_model=SlotSmokeOut, dependencies=_admin)
async def smoke_slot_assignment(
    slot: str,
    payload: SlotSmokeIn,
    db: AsyncSession = Depends(get_db),
) -> SlotSmokeOut:
    """Validate a slot/model pair and optionally run one tiny provider call.

    ``dry_run`` is intentionally true by default: the endpoint resolves catalog,
    policy, loaded-node and reasoning-payload state without spending tokens or
    changing any assignment. Passing ``dry_run=false`` performs a short live call.
    """
    import time as _time

    registry = _registry()
    meta = _slot_meta(slot)
    if meta is None:
        raise HTTPException(404, f"Unknown slot: {slot}")
    model_key = payload.model or _slot_current_model(slot, registry)
    if not model_key:
        return SlotSmokeOut(ok=False, slot=slot, model=None, error="No model selected")
    cap = registry.models.get(model_key)
    if cap is None:
        return SlotSmokeOut(ok=False, slot=slot, model=model_key, error="Unknown model")

    loaded = await _loaded_index()
    warnings: list[AssignmentIssue] = []
    if bool(meta[4]) and not cap.local_only:
        return SlotSmokeOut(
            ok=False,
            slot=slot,
            model=model_key,
            provider=cap.provider.value,
            provider_model=cap.provider_model,
            error="Confidential slot allows only local models",
        )
    required = _SLOT_MODALITY.get(slot)
    if required and required not in {m.value for m in cap.modalities}:
        warnings.append(
            AssignmentIssue(
                slot=slot,
                model=model_key,
                code="modality_mismatch",
                message=f"Модель не заявляет capability '{required}'",
            )
        )
    is_loaded = cap.provider in _LOCAL_KINDS and _loaded_node_for(cap, loaded) is not None
    if cap.provider in _LOCAL_KINDS and not is_loaded:
        warnings.append(
            AssignmentIssue(
                slot=slot,
                model=model_key,
                code="not_loaded",
                message="Модель не найдена ни на одном локальном узле сейчас",
            )
        )
    thinking_state = _slot_thinking_state(slot, registry, model_key)
    thinking_requested = (
        payload.thinking
        if payload.thinking is not None
        else thinking_state["thinking_effective"]
    )
    thinking_payload_supported = bool(thinking_state["thinking_disable_supported"])
    if thinking_state["thinking_warning"]:
        warnings.append(
            AssignmentIssue(
                slot=slot,
                model=model_key,
                code="thinking_disable_not_guaranteed",
                message=thinking_state["thinking_warning"],
            )
        )

    if payload.dry_run:
        return SlotSmokeOut(
            ok=True,
            slot=slot,
            model=model_key,
            provider=cap.provider.value,
            provider_model=cap.provider_model,
            dry_run=True,
            thinking_requested=thinking_requested,
            thinking_payload_supported=thinking_payload_supported,
            warnings=warnings,
        )

    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, ChatMessage

    task = _slot_smoke_task(slot)
    request = AIRequest(
        task=task,
        messages=[ChatMessage(role="user", content="Ответь коротко: ok")],
        input_text="ok",
        confidential=bool(meta[4]),
        allow_cloud=not cap.local_only,
        preferred_model=model_key,
        thinking=thinking_requested,
        metadata={"documents": ["ok", "other"]} if task == AITask.RERANKING else {},
    )
    started = _time.perf_counter()
    try:
        response = await ai_router.run(request)
    except Exception as exc:  # noqa: BLE001
        return SlotSmokeOut(
            ok=False,
            slot=slot,
            model=model_key,
            provider=cap.provider.value,
            provider_model=cap.provider_model,
            dry_run=False,
            thinking_requested=thinking_requested,
            thinking_payload_supported=thinking_payload_supported,
            latency_ms=int((_time.perf_counter() - started) * 1000),
            warnings=warnings,
            error=str(exc),
        )
    if response.model != cap.provider_model:
        warnings.append(
            AssignmentIssue(
                slot=slot,
                model=model_key,
                code="smoke_used_fallback",
                message=f"Smoke ушёл на fallback model '{response.model}'",
            )
        )
    return SlotSmokeOut(
        ok=bool(response.text or response.data or response.embedding or response.scores),
        slot=slot,
        model=model_key,
        provider=response.provider.value,
        provider_model=response.model,
        dry_run=False,
        thinking_requested=thinking_requested,
        thinking_payload_supported=thinking_payload_supported,
        latency_ms=int((_time.perf_counter() - started) * 1000),
        warnings=warnings,
    )
