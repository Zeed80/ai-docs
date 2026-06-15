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

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import provider_registry
from app.ai.model_registry import ModelRegistry
from app.ai.schemas import ProviderKind
from app.ai.secret_box import decrypt, encrypt, mask
from app.auth.jwt import require_role
from app.auth.models import UserRole
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
        added.append(key)

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
async def list_models(include_disabled: bool = True) -> list[CatalogModelOut]:
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
async def live_models() -> list[LiveModelOut]:
    """All selectable models: every model actually loaded on every configured
    provider node, merged with catalog metadata. Discovered models are registered
    into the catalog overlay so they get a stable key (assignable + thinking)."""
    from app.ai.schemas import ModelCapability, Modality, ModelStatus

    registry = _registry()
    # catalog: provider_model (per provider) → (key, cap)
    by_pm: dict[tuple[str, str], tuple[str, object]] = {}
    for key, cap in registry.models.items():
        by_pm[(cap.provider.value, cap.provider_model)] = (key, cap)

    out: dict[str, LiveModelOut] = {}
    seen_keys: set[str] = set()

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
    return list(out.values())


class ThinkingUpdate(BaseModel):
    enabled: bool


@router.patch("/models/{model_key}/thinking", dependencies=_admin)
async def set_model_thinking(model_key: str, payload: ThinkingUpdate) -> dict:
    """Toggle the per-model reasoning (CoT) flag — the local-model checkbox."""
    from app.ai.model_registry import set_thinking_override

    registry = _registry()
    if model_key not in registry.models:
        raise HTTPException(404, f"Unknown model: {model_key}")
    set_thinking_override(model_key, payload.enabled)
    return {"ok": True, "model": model_key, "thinking_enabled": payload.enabled}


class PreferredInstanceUpdate(BaseModel):
    instance_name: str | None = None  # None/"" clears the pin


@router.patch("/models/{model_key}/preferred-instance", dependencies=_admin)
async def set_model_preferred_instance(model_key: str, payload: PreferredInstanceUpdate) -> dict:
    """Pin a model to a specific provider node (multi-machine routing)."""
    from app.ai.model_registry import set_preferred_instance

    registry = _registry()
    if model_key not in registry.models:
        raise HTTPException(404, f"Unknown model: {model_key}")
    set_preferred_instance(model_key, payload.instance_name or None)
    return {"ok": True, "model": model_key, "preferred_instance": payload.instance_name or None}


# ── Simplified assignment slots ─────────────────────────────────────────────
# Seven practical slots fan out to the underlying stores (task_routing +
# agent_config + ai_config mirror). Changes take effect immediately — the AI
# router reads task_routing/agent_config from Redis on every call.


class SlotOut(BaseModel):
    slot: str
    group: str
    label: str
    hint: str
    model: str | None      # catalog key
    local_only: bool       # cloud models forbidden for this slot


# local_only=True → конфиденциальные задачи (содержимое документов), облако
# запрещено. Агентские слоты допускают облако (выбор оператора).
_SLOTS = [
    ("ocr_fast", "Документы", "Быстрая (основная)",
     "OCR счётов, классификация, извлечение полей, чертежи", True),
    ("ocr_large", "Документы", "Крупная (сложные случаи)",
     "Повторное извлечение при низкой уверенности/ошибках", True),
    ("agent_orchestrator", "Агент", "Оркестратор",
     "Планирование, вызов инструментов, диалог", False),
    ("agent_email", "Агент", "Письма",
     "Генерация деловых писем и черновиков", False),
    ("agent_large", "Агент", "Большая (скиллы/скрипты/ТП)",
     "Генерация кода, навыков, техпроцессов", False),
    ("embedding", "Поиск", "Векторизация (embedding)",
     "Семантический поиск по документам", True),
    ("rerank", "Поиск", "Реранкинг",
     "Переранжирование результатов поиска", True),
]


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
    if slot == "embedding":
        return get_routing_for(AITask.EMBEDDING).primary
    if slot == "rerank":
        return get_routing_for(AITask.RERANKING).primary
    if slot == "agent_email":
        return get_routing_for(AITask.EMAIL_DRAFTING).primary
    if slot == "ocr_large":
        try:
            from app.api.ai_settings import get_ai_config
            return get_ai_config().get("model_ocr_fallback")
        except Exception:
            return None
    cfg = get_builtin_agent_config()
    if slot == "agent_orchestrator":
        return _key_for_raw(registry, cfg.orchestrator_model or cfg.model)
    if slot == "agent_large":
        return _key_for_raw(registry, cfg.builder_model)
    return None


@router.get("/slots", response_model=list[SlotOut], dependencies=_admin)
async def get_slots() -> list[SlotOut]:
    registry = _registry()
    return [
        SlotOut(
            slot=slot, group=group, label=label, hint=hint,
            model=_slot_current_model(slot, registry), local_only=local_only,
        )
        for slot, group, label, hint, local_only in _SLOTS
    ]


class SlotWrite(BaseModel):
    model: str  # catalog key


@router.put("/slots/{slot}", dependencies=_admin)
async def set_slot(slot: str, payload: SlotWrite) -> dict:
    """Assign a model to a slot — fans out to all underlying tasks/roles."""
    registry = _registry()
    cap = registry.models.get(payload.model)
    if cap is None:
        raise HTTPException(404, f"Unknown model: {payload.model}")
    meta = next((s for s in _SLOTS if s[0] == slot), None)
    if meta is None:
        raise HTTPException(400, f"Unknown slot: {slot}")
    if meta[4] and not cap.local_only:
        raise HTTPException(400, "Этот слот допускает только локальные модели (конфиденциально)")

    from app.ai.agent_config import BuiltinAgentConfigUpdate, update_builtin_agent_config
    from app.ai.assignment_groups import DocumentGroup, _mirror_ai_config, _set_primary
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for, save_task_routing

    def _assign_task(task: AITask, model_key: str) -> None:
        """Set primary + local/cloud policy from the model (non-confidential tasks).

        Cloud model → local_only=False, allow_cloud=True so the AI router won't
        block it at dispatch; local model → local-only.
        """
        current = get_routing_for(task)
        tail = [m for m in current.models if m != model_key]
        routing = current.model_copy(update={
            "models": [model_key, *tail],
            "local_only": cap.local_only,
            "allow_cloud": not cap.local_only,
        })
        save_task_routing(task, routing)

    key = payload.model
    try:
        if slot == "ocr_fast":
            for t in (
                AITask.INVOICE_OCR, AITask.STRUCTURED_EXTRACTION, AITask.CLASSIFICATION,
                AITask.DRAWING_ANALYSIS, AITask.DRAWING_ANALYSIS_VLM,
                AITask.LONG_CONTEXT_SUMMARIZATION, AITask.ENGINEERING_REASONING,
            ):
                _set_primary(t, key)
            _mirror_ai_config(DocumentGroup(vision_model=key, text_model=key))
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
        elif slot == "agent_orchestrator":
            update_builtin_agent_config(BuiltinAgentConfigUpdate(
                provider=cap.provider.value, model=cap.provider_model,
                orchestrator_provider=cap.provider.value, orchestrator_model=cap.provider_model,
                worker_provider=cap.provider.value, worker_model=cap.provider_model,
                fast_provider=cap.provider.value, fast_model=cap.provider_model,
            ))
            for t in (AITask.ORCHESTRATOR_PLANNING, AITask.TOOL_CALLING):
                _assign_task(t, key)
        elif slot == "agent_large":
            update_builtin_agent_config(BuiltinAgentConfigUpdate(
                builder_provider=cap.provider.value, builder_model=cap.provider_model,
            ))
            _assign_task(AITask.CODE_GENERATION, key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "slot": slot, "model": key}
