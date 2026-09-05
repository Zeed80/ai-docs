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

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Literal

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
from app.ai.secret_box import decrypt, encrypt, key_state, mask
from app.ai.thinking_params import effective_thinking_levels
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
    # "unset" | "set" | "corrupt". Испорченный ключ (например после смены
    # app_secret_key) раньше был неотличим от отсутствующего: api_key_set
    # приходил False, и человек вводил ключ заново с тем же результатом.
    api_key_state: str
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
        api_key_state=key_state(inst.api_key_encrypted),
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
    old_name = inst.name
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
    if inst.name != old_name:
        _repin_models_after_rename(old_name, inst.name)
    await _sync_cache(db)
    return _to_out(inst)


def _repin_models_after_rename(old_name: str, new_name: str) -> None:
    """Перенести пины моделей на переименованный узел.

    Пин хранится строкой и матчится по имени ИЛИ id, но записывается всегда
    имя. После переименования узла ни одно из сравнений не срабатывало, и
    select_instance молча уходил на первый попавшийся узел — модель, прибитая
    к конкретной машине, начинала считаться на другой, без единого сообщения.
    """
    try:
        from app.ai.model_registry import _load_preferred_instances, set_preferred_instance

        moved = [k for k, v in _load_preferred_instances().items() if v == old_name]
        for model_key in moved:
            set_preferred_instance(model_key, new_name)
        if moved:
            logger.info(
                "model_pins_followed_node_rename",
                old_name=old_name, new_name=new_name, models=moved,
            )
    except Exception as exc:  # noqa: BLE001 — переименование уже сохранено
        logger.warning("model_pin_repin_failed", error=str(exc))


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

    from app.ai.provider_catalog_probes import capability_from_listing

    registry = _registry()
    added: list[str] = []
    for item in data:
        provider_model = item.get("id")
        if not provider_model:
            continue
        key = f"{kind.value}_{provider_model}".replace("/", "_").replace(":", "_").replace(".", "_")
        # Раньше здесь каждой модели вслепую проставлялись
        # supports_tool_calling=True и {TEXT, TOOL_CALLING} — независимо от
        # провайдера и от того, что он на самом деле сообщил. Теперь метаданные
        # берутся из ответа, а где их нет — возможности честно помечаются
        # неподтверждёнными.
        cap = capability_from_listing(key, kind, item)
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
    thinking_levels: list[str] = []
    thinking_level_default: str | None = None
    preferred_instance: str | None
    quality_score: float
    speed_score: float
    vram_gb_estimate: float | None
    # available / missing / unknown — см. provider_registry.Availability.
    # Каталог помечал «production» модели, которых давно нет ни на одном узле
    # (gemma4 после перехода на qwen3.8), и они спокойно назначались из GUI.
    availability: str = "unknown"
    # Всё нижеследующее лежит в ModelCapability с самого начала и просто не
    # доходило до интерфейса: при выборе модели не было видно ни размера
    # контекста, ни умеет ли она вызывать инструменты, ни сколько стоит.
    max_context_tokens: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    notes: str | None = None


@router.get("/models", response_model=list[CatalogModelOut], dependencies=_admin)
async def list_models(
    include_disabled: bool = True,
    check_availability: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[CatalogModelOut]:
    """Catalog models with status + thinking flags. The UI filters by ``status``
    to declutter (production by default, ``include all`` reveals candidates).

    ``availability`` says whether the model is actually served by any enabled
    node right now — статус в каталоге этого не знает и знать не может.
    """
    registry = _registry()
    avail: dict[str, str] = {}
    if check_availability:
        from app.ai.provider_registry import catalog_availability

        try:
            avail = {
                k: v.value
                for k, v in (
                    await asyncio.to_thread(catalog_availability, registry.models)
                ).items()
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_availability_failed", error=str(exc))
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
                thinking_levels=effective_thinking_levels(
                    cap.thinking_supported, cap.provider.value, cap.thinking_levels
                ),
                thinking_level_default=cap.thinking_level_default,
                preferred_instance=_pin_display_name(cap.preferred_instance),
                quality_score=cap.quality_score,
                speed_score=cap.speed_score,
                vram_gb_estimate=cap.vram_gb_estimate,
                availability=avail.get(key, "unknown"),
                max_context_tokens=cap.max_context_tokens,
                supports_tool_calling=cap.supports_tool_calling,
                supports_structured_output=cap.supports_structured_output,
                cost_per_1k_input=cap.cost_per_1k_input,
                cost_per_1k_output=cap.cost_per_1k_output,
                notes=cap.notes,
            )
        )
    return out


class RoutingChainEntry(BaseModel):
    key: str
    provider_model: str | None = None
    provider: str | None = None
    availability: str  # available | missing | unknown | not_in_catalog
    is_primary: bool = False


class RoutingChainOut(BaseModel):
    task: str
    models: list[RoutingChainEntry]
    dead: int = 0


@router.get("/routing-health", response_model=list[RoutingChainOut], dependencies=_admin)
async def routing_health() -> list[RoutingChainOut]:
    """Полная цепочка моделей каждой задачи, а не только её голова.

    Экран моделей показывает по задаче одну — назначенную — модель, поэтому
    остальная часть цепочки невидима, и мусор в ней может лежать годами. Так и
    вышло: после перехода gemma4 → qwen3.8 менялась голова, а хвост сохранялся
    целиком, и ссылки на несуществующие модели остались у большинства задач.
    """
    from app.ai.provider_registry import Availability, model_availability
    from app.ai.task_routing import get_task_routing

    registry = _registry()

    def _entry(key: str, primary: bool) -> RoutingChainEntry:
        cap = registry.models.get(key)
        if cap is None:
            return RoutingChainEntry(
                key=key, availability="not_in_catalog", is_primary=primary
            )
        try:
            state = model_availability(cap.provider, cap.provider_model).value
        except Exception:  # noqa: BLE001
            state = Availability.UNKNOWN.value
        return RoutingChainEntry(
            key=key, provider_model=cap.provider_model,
            provider=cap.provider.value, availability=state, is_primary=primary,
        )

    def _build() -> list[RoutingChainOut]:
        out: list[RoutingChainOut] = []
        for task, cfg in get_task_routing().items():
            entries = [
                _entry(k, i == 0) for i, k in enumerate(cfg.models or [])
            ]
            out.append(RoutingChainOut(
                task=str(task).split(".")[-1].lower(),
                models=entries,
                dead=sum(
                    1 for e in entries
                    if e.availability in ("missing", "not_in_catalog")
                ),
            ))
        return sorted(out, key=lambda r: r.task)

    return await asyncio.to_thread(_build)


class RoutingPruneOut(BaseModel):
    pruned: dict[str, list[str]] = {}
    total: int = 0
    skipped_head: dict[str, str] = {}
    # Задача, чью цепочку не удалось сохранить (например в YAML-дефолте у
    # конфиденциальной задачи прописана облачная модель). Это не наша поломка
    # и молча её глотать нельзя — цепочка так и останется с мусором.
    failed: dict[str, str] = {}


@router.post("/routing-health/prune", response_model=RoutingPruneOut, dependencies=_admin)
async def prune_routing(db: AsyncSession = Depends(get_db)) -> RoutingPruneOut:
    """Убрать из цепочек ссылки на модели, которых заведомо нет.

    Голову цепочки не трогаем никогда, даже мёртвую: это осознанное назначение
    человека, и молча его снять — то же самое, что назначить другую модель за
    него. Такую задачу возвращаем в ``skipped_head``, чтобы её было видно.
    """
    from app.ai.assignment_groups import prune_dead_keys
    from app.ai.task_routing import get_task_routing, save_task_routing

    def _run() -> RoutingPruneOut:
        result = RoutingPruneOut()
        for task, cfg in get_task_routing().items():
            models = list(cfg.models or [])
            if not models:
                continue
            head, tail = models[0], models[1:]
            kept, dropped = prune_dead_keys(tail)
            head_alive, head_dead = prune_dead_keys([head])
            name = str(task).split(".")[-1].lower()
            if head_dead:
                result.skipped_head[name] = head
            if not dropped:
                continue
            try:
                save_task_routing(
                    task, cfg.model_copy(update={"models": [head, *kept]})
                )
            except Exception as exc:  # noqa: BLE001
                # Одна невалидная цепочка не должна отменять чистку остальных.
                result.failed[name] = str(exc)
                continue
            result.pruned[name] = dropped
            result.total += len(dropped)
        return result

    out = await asyncio.to_thread(_run)

    # Redis — только кэш: назначения durable лежат в Postgres и при старте
    # оттуда же восстанавливаются поверх Redis. Без записи в Postgres чистка
    # жила бы до первого рестарта и молча откатывалась — ровно тот случай,
    # ради которого durable-хранилище и заводили.
    if out.pruned:
        from app.ai import model_runtime_store
        from app.ai.task_routing import get_routing_for

        for name in out.pruned:
            try:
                task = AITask(name)
            except ValueError:
                continue
            await model_runtime_store.persist_task_routing(
                db, task=task.value,
                routing=get_routing_for(task).model_dump(mode="json"),
            )
        await db.commit()
        await model_runtime_store.hydrate_runtime_cache(db)

    logger.info("task_routing_pruned", total=out.total, tasks=list(out.pruned))
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
    thinking_levels: list[str] = []
    loaded: bool              # actually present on a node right now
    node: str | None          # which node hosts it (local multi-node)
    # Node this model is pinned to, if any (None = the router picks). Lets the
    # UI offer "run this model on the GPU node / on the CPU node".
    preferred_instance: str | None = None
    vram_gb_estimate: float | None
    # Те же факты, что и в CatalogModelOut: вкладка «Назначение» читает
    # /live-models, а не /models, и потому не видела ни размера контекста, ни
    # умеет ли модель вызывать инструменты, ни цены облачной.
    max_context_tokens: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    notes: str | None = None


def _pin_display_name(pin: str | None) -> str | None:
    """Показать пин узла человеку.

    Хранится id: имя — редактируемое поле, и пин по нему рвался при
    переименовании узла. В интерфейсе нужно имя — UUID ничего не говорит о
    том, на какой машине считается модель. Пины, записанные до перехода на id,
    хранят имя; такое значение возвращаем как есть.
    """
    if not pin:
        return None
    try:
        from app.ai.provider_registry import _redis_get_instances

        for row in _redis_get_instances():
            if str(row.get("id")) == pin:
                return str(row.get("name") or pin)
    except Exception as exc:  # noqa: BLE001 — кэш узлов недоступен
        logger.debug("pin_display_lookup_failed", pin=pin, error=str(exc))
    return pin


def _capability_facts(cap) -> dict:
    """Поля модели, одинаковые для всех трёх мест сборки LiveModelOut."""
    return {
        "max_context_tokens": cap.max_context_tokens,
        "supports_tool_calling": cap.supports_tool_calling,
        "supports_structured_output": cap.supports_structured_output,
        "cost_per_1k_input": cap.cost_per_1k_input,
        "cost_per_1k_output": cap.cost_per_1k_output,
        "notes": cap.notes,
    }


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


def _infer_thinking_levels(name: str, provider_kind: str) -> list[str]:
    """Best-effort guess at which reasoning-effort levels a discovered model
    accepts — conservative on purpose. Unlike ``_infer_thinking`` (a plain
    on/off guess later corrected by Ollama's real ``/api/show`` capabilities
    when available), no provider API reports qualitative-level support
    anywhere — Ollama's ``capabilities`` list only ever contains the flat
    string ``"thinking"``, never a level. So this stays a name-hint guess
    forever, and every family except the one documented as 3-level (gpt-oss)
    defaults to ``[]`` (on/off only) until a human verifies it and curates
    ``thinking_levels`` directly in model_registry.yaml — the same
    "Verified ... against ..." convention already used for
    ``thinking_supported``.
    """
    n = name.lower()
    from app.ai.thinking_params import REASONING_EFFORT_PROVIDERS

    if "gpt-oss" in n and (
        provider_kind in REASONING_EFFORT_PROVIDERS or provider_kind in ("ollama", "ollama_cloud")
    ):
        return ["low", "medium", "high"]
    return []


def _synth_key(provider: str, provider_model: str) -> str:
    raw = f"{provider}_{provider_model}"
    return "".join(c if c.isalnum() else "_" for c in raw).strip("_")


async def _ollama_show_capabilities(base_url: str, provider_model: str) -> set[str] | None:
    """Ollama's own ``/api/show`` reports a ``capabilities`` list (e.g.
    ``["completion","vision","tools","thinking"]``) straight from the GGUF
    metadata — ground truth, unlike guessing from the model tag. Name-based
    hints (``_VISION_HINTS``/``_THINK_HINTS`` below) are a fallback for
    non-Ollama providers and for Ollama versions predating this field; a tag
    that doesn't match any hint (a new model family, e.g. "qwen3.8") must not
    silently lose a capability the runtime actually has. Returns ``None`` (not
    an empty set) on any failure so callers know to fall back, rather than
    treating "couldn't ask" as "has nothing".
    """
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(f"{base}/api/show", json={"model": provider_model})
            resp.raise_for_status()
            caps = resp.json().get("capabilities")
    except Exception:
        return None
    if not isinstance(caps, list):
        return None
    return {str(c) for c in caps}


# Fixed prompt/seed/temperature for the level-support probe below — every
# call must be bit-for-bit reproducible so a difference between levels is
# only ever attributable to the level itself, never sampling noise.
_LEVEL_PROBE_PROMPT = "Explain briefly why the sky is blue."
_LEVEL_PROBE_OPTIONS = {"temperature": 0, "seed": 42, "num_predict": 200}


async def _ollama_probe_thinking_levels(base_url: str, provider_model: str) -> bool | None:
    """Live deterministic differential probe: does this model's ``think``
    level string cause a REAL behavioural difference, or does Ollama just
    accept-and-ignore it?

    Ollama never rejects an unrecognised ``think`` level (lenient parsing),
    so "the request didn't error" is not evidence of real support — this was
    the original (wrong) assumption behind treating Ollama levels as
    unverifiable. Confirmed empirically 2026-08-17: with DEFAULT (non-zero)
    sampling temperature, ``think=low/medium/high`` on qwen3.8:27b produced
    410/430/216 chars of reasoning with no consistent trend across repeats —
    pure sampling noise, indistinguishable from no effect. Pinning
    ``temperature=0`` + a fixed ``seed`` removes that noise entirely:
    repeated calls at the SAME level then return byte-identical output, so
    ANY difference BETWEEN two levels at temp=0 is a real, reproducible
    signal. Re-verified with exactly this method on two different prompts —
    qwen3.8:27b: 404 vs 208 chars (low/high), and separately 152 vs 307
    chars — each pair individually reproduced identically twice. This *is*
    a reliable, universal support test; it costs two short real generations
    (including a possible cold model load — a large model can take well
    over a minute the first time), so callers must run it once per model
    and cache the result (see the ``existing is None or stale`` gate at the
    call site), not on every poll.

    Returns ``None`` (not ``False``) when the probe itself failed to
    complete (timeout/network/HTTP error) — a transient infra hiccup must
    never get cached as a permanent "unsupported" verdict; the caller is
    expected to leave the model unprobed and retry on a later poll.
    """
    base = base_url.rstrip("/")

    async def _call(level: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    f"{base}/api/chat",
                    json={
                        "model": provider_model,
                        "messages": [{"role": "user", "content": _LEVEL_PROBE_PROMPT}],
                        "think": level,
                        "stream": False,
                        "options": _LEVEL_PROBE_OPTIONS,
                    },
                )
                resp.raise_for_status()
                return (resp.json().get("message") or {}).get("thinking") or ""
        except Exception:
            return None

    low = await _call("low")
    if low is None:
        return None  # infra failure — retry later, don't cache a verdict
    high = await _call("high")
    if high is None:
        return None
    if not low and not high:
        return False  # thinking didn't engage at all — nothing to differentiate
    return low != high


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
    # Live-probed thinking-level determinations to persist once after the
    # scan, via the thinking-override path (the only one that can attach a
    # level result to a model_registry.yaml-defined entry — see
    # _ollama_probe_thinking_levels and the unified post-`if hit:` check
    # below, which covers BOTH curated and newly-discovered models).
    level_overrides_to_persist: list[dict] = []

    # 1) Live local nodes.
    local_kinds = [ProviderKind.OLLAMA, ProviderKind.VLLM, ProviderKind.LLAMACPP,
                   ProviderKind.OPENAI_COMPATIBLE]
    for kind in local_kinds:
        for inst in provider_registry.list_instances(kind):
            loaded = await _node_loaded_models(inst)
            for pm, vram in loaded:
                bare = pm.split(":")[0]
                hit = by_pm.get((kind.value, pm)) or by_pm.get((kind.value, bare))
                existing = hit[1] if hit else registry.models.get(_synth_key(kind.value, pm))
                # Ollama reports real capabilities (GGUF metadata) — ground truth
                # over the name-hint guess in `_infer_modalities`/`_infer_thinking`.
                # A tag that predates every entry in _VISION_HINTS/_THINK_HINTS (a
                # new model family, e.g. "qwen3.8") would otherwise silently lose
                # "vision"/"thinking" and never be picked by _first_vision_model()
                # for any vision task, no matter how it's assigned in Settings. A
                # manually-curated/verified entry is never touched here — only a
                # capability_source=="discovered" entry (itself just a guess, or
                # not yet registered at all) gets corrected/created.
                if kind == ProviderKind.OLLAMA and (
                    existing is None or existing.capability_source == "discovered"
                ):
                    mods = _infer_modalities(pm)
                    thinking = _infer_thinking(pm)
                    real_caps = await _ollama_show_capabilities(inst.base_url, pm)
                    if real_caps is not None:
                        mods = {"text"}
                        if "tools" in real_caps:
                            mods.add("tool_calling")
                        if "vision" in real_caps:
                            mods.add("vision")
                        if "embedding" in real_caps:
                            mods = {"embedding"}
                        thinking = "thinking" in real_caps
                    stale = existing is not None and (
                        existing.modalities != {Modality(m) for m in mods}
                        or existing.thinking_supported != thinking
                    )
                    if existing is None or stale:
                        key = hit[0] if hit else _synth_key(kind.value, pm)
                        # Level support: a manually curated non-empty
                        # thinking_levels always survives a discovery pass.
                        # A never-curated entry tries the (conservative,
                        # zero-cost) name-hint guess (gpt-oss) here; the live
                        # differential probe — real per-model evidence,
                        # verified 2026-08-17 to actually detect it — runs
                        # once uniformly for ANY thinking-capable Ollama
                        # model (curated or discovered) in the unified
                        # `if hit:` check below, not duplicated here.
                        levels = existing.thinking_levels if existing else []
                        levels_probed = bool(existing and existing.thinking_levels_probed)
                        if not levels and not levels_probed:
                            levels = _infer_thinking_levels(pm, kind.value)
                            levels_probed = bool(levels)
                        cap = ModelCapability(
                            name=key, provider=kind, provider_model=pm,
                            status=existing.status if existing else ModelStatus.CANDIDATE,
                            modalities={Modality(m) for m in mods},
                            supports_tool_calling="tool_calling" in mods,
                            supports_structured_output=True, local_only=True,
                            thinking_supported=thinking, capability_source="discovered",
                            thinking_levels=levels,
                            thinking_levels_probed=levels_probed,
                            vram_gb_estimate=vram,
                            # A correction to modalities/thinking_supported must not
                            # reset a UI-set toggle/pin that lives on this same
                            # capability row — `_load_thinking_overrides()` reapplies
                            # its own source of truth on every registry load anyway,
                            # but the persisted overlay row should stay consistent
                            # with it rather than silently reverting in between.
                            thinking_enabled=existing.thinking_enabled if existing else False,
                            thinking_level_default=existing.thinking_level_default if existing else None,
                            preferred_instance=existing.preferred_instance if existing else None,
                        )
                        registry.add_model(key, cap, persist=True)
                        by_pm[(kind.value, pm)] = (key, cap)
                        discovered_to_persist.append({
                            "model_key": key,
                            "provider": kind.value,
                            "provider_model": pm,
                            "capability": cap.model_dump(mode="json", exclude={"name"}),
                            "source": "local_live_discovery",
                            "verification_status": "discovered",
                        })
                        hit = (key, cap)
                if hit:
                    key, cap = hit
                    seen_keys.add(key)
                    if (
                        kind == ProviderKind.OLLAMA
                        and cap.thinking_supported
                        and not cap.thinking_levels_probed
                    ):
                        # Unified probe point: runs for ANY thinking-capable
                        # Ollama model that hasn't been determined yet —
                        # curated (model_registry.yaml) or discovered alike.
                        # Writes through the thinking-override path (not the
                        # catalog overlay above), which is the only one that
                        # can attach a result to a YAML-defined entry (the
                        # catalog overlay uses setdefault, so YAML always
                        # wins there and a plain overlay write would be
                        # silently ignored for an already-YAML-defined key).
                        probe_result = await _ollama_probe_thinking_levels(
                            inst.base_url, cap.provider_model
                        )
                        if probe_result is not None:
                            levels = ["low", "medium", "high"] if probe_result else []
                            from app.ai.model_registry import set_thinking_override

                            set_thinking_override(key, levels=levels)
                            level_overrides_to_persist.append(
                                {"model_key": key, "thinking_levels": levels}
                            )
                            cap = cap.model_copy(
                                update={"thinking_levels": levels, "thinking_levels_probed": True}
                            )
                            registry.models[key] = cap
                            by_pm[(kind.value, pm)] = (key, cap)
                        # else: infra hiccup — leave unprobed, retry next poll.
                    out[key] = LiveModelOut(
                        key=key, provider=kind.value, provider_model=cap.provider_model,
                        status=cap.status.value, modalities=sorted(m.value for m in cap.modalities),
                        local_only=cap.local_only, thinking_supported=cap.thinking_supported,
                        thinking_enabled=cap.thinking_enabled,
                        thinking_levels=effective_thinking_levels(
                            cap.thinking_supported, kind.value, cap.thinking_levels
                        ),
                        loaded=True, node=inst.name,
                        preferred_instance=_pin_display_name(cap.preferred_instance),
                        vram_gb_estimate=cap.vram_gb_estimate or vram,
                        **_capability_facts(cap),
                    )
                else:
                    # Discovered model on a non-Ollama provider — register into
                    # the catalog overlay using the name-heuristic guess only
                    # (no real-capability endpoint to ask, unlike Ollama above).
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
                            thinking_levels=_infer_thinking_levels(pm, kind.value),
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
                        thinking_enabled=th.thinking_enabled,
                        thinking_levels=effective_thinking_levels(
                            th.thinking_supported, kind.value, th.thinking_levels
                        ),
                        loaded=True, node=inst.name,
                        preferred_instance=_pin_display_name(th.preferred_instance),
                        vram_gb_estimate=vram,
                        **_capability_facts(th),
                    )

    # 2) Non-loaded catalog models that are still selectable:
    #    • cloud models (local_only False) — usable once an API key is set; and
    #    • enabled NON-ollama local single-server providers (vLLM / llama.cpp),
    #      whose profile-gated server may simply be stopped right now — surfacing
    #      them makes the provider assignable, and the assignment autostart then
    #      brings the server up (the concrete downloaded model per such server is
    #      chosen in the Library tab, since each serves one model at a time).
    #    A non-loaded OLLAMA model, by contrast, just isn't pulled → keep hidden.
    for key, cap in registry.models.items():
        if key in seen_keys or cap.status == ModelStatus.DISABLED:
            continue
        if cap.local_only and cap.provider == ProviderKind.OLLAMA:
            continue
        out[key] = LiveModelOut(
            key=key, provider=cap.provider.value, provider_model=cap.provider_model,
            status=cap.status.value, modalities=sorted(m.value for m in cap.modalities),
            local_only=cap.local_only, thinking_supported=cap.thinking_supported,
            thinking_enabled=cap.thinking_enabled,
            thinking_levels=effective_thinking_levels(
                cap.thinking_supported, cap.provider.value, cap.thinking_levels
            ),
            loaded=False, node=None,
            preferred_instance=_pin_display_name(cap.preferred_instance),
            vram_gb_estimate=cap.vram_gb_estimate,
            **_capability_facts(cap),
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

    # Persist live-probed thinking-level determinations once (covers curated
    # model_registry.yaml entries too — see the unified probe point above).
    # Best-effort, same as the catalog persist above.
    if level_overrides_to_persist:
        try:
            for entry in level_overrides_to_persist:
                await model_runtime_store.persist_model_override(db, **entry)
            await db.commit()
            await model_runtime_store.hydrate_runtime_cache(db)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.warning("live_models_level_probe_persist_failed", error=str(exc))

    return list(out.values())


class ThinkingUpdate(BaseModel):
    enabled: bool
    level: str | None = None  # reasoning-effort level; only valid for the model's thinking_levels


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
    cap = registry.models[model_key]
    allowed_levels = effective_thinking_levels(
        cap.thinking_supported, cap.provider.value, cap.thinking_levels
    )
    if payload.level is not None and payload.level not in allowed_levels:
        raise HTTPException(
            400,
            f"Model '{model_key}' does not support thinking level '{payload.level}' "
            f"(supported: {allowed_levels or 'none — on/off only'})",
        )
    set_thinking_override(model_key, payload.enabled, level=payload.level)
    await model_runtime_store.persist_model_override(
        db,
        model_key=model_key,
        thinking_enabled=payload.enabled,
        thinking_level=payload.level,
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
    """Pin a model to a specific provider node (multi-machine routing).

    Пин сохраняется как id узла, а не как имя. Имя — редактируемое поле:
    храня его, пин рвался при переименовании узла, и select_instance молча
    уходил на первый попавшийся узел — модель, прибитая к конкретной машине,
    начинала считаться на другой. На вход принимаем и то, и другое: интерфейс
    и старые записи оперируют именами.
    """
    from app.ai.model_registry import set_preferred_instance

    registry = _registry()
    if model_key not in registry.models:
        raise HTTPException(404, f"Unknown model: {model_key}")

    pin = await _instance_id_for_pin(db, payload.instance_name)
    set_preferred_instance(model_key, pin)
    await model_runtime_store.persist_model_override(
        db,
        model_key=model_key,
        preferred_instance=pin or "",
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "model": model_key, "preferred_instance": pin}


async def _instance_id_for_pin(db: AsyncSession, value: str | None) -> str | None:
    """Привести пин к id узла: на вход может прийти имя, id или пустая строка."""
    if not value:
        return None
    rows = (await db.execute(select(ProviderInstance))).scalars().all()
    for inst in rows:
        if str(inst.id) == value:
            return str(inst.id)
    for inst in rows:
        if inst.name == value:
            return str(inst.id)
    # Узла с таким именем нет — сохранять нечего: пин, который ни на что не
    # указывает, тише всего ломает маршрутизацию.
    raise HTTPException(404, f"Unknown provider node: {value}")


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
    local_only: bool               # EFFECTIVE: cloud forbidden for this slot right now
    cloud_optionable: bool = False  # a confidential slot that CAN be opened to cloud
    cloud_allowed: bool = False     # admin opted this slot into cloud models
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
    # Поля за одним переключателем разошлись — переключатель показывает
    # состояние первого, и это надо назвать вслух.
    thinking_mixed: bool = False
    thinking_warning: str | None = None
    # Reasoning-effort level for the selected model, if it declares any.
    thinking_levels: list[str] = []          # levels the SELECTED model supports (empty = none)
    thinking_level_override: str | None = None  # this slot's explicit level override
    thinking_level_effective: str | None = None  # resolved level actually in effect


# local_only=True → слот видит содержимое документов и потому локален по
# умолчанию. Это не запрет: облако включается отдельным защищённым действием
# (PATCH /slots/{slot}/allow-cloud), после чего _slot_effective_local_only
# перестаёт держать слот локальным.
_SLOTS = [
    ("ocr_fast", "Документы", "Быстрая (OCR/VLM)",
     "OCR счётов, классификация и первичный VLM-анализ", True),
    ("structured_extraction", "Документы", "Извлечение полей",
     "Структурированное извлечение и текстовая проверка документов", True),
    ("ocr_large", "Документы", "Крупная (сложные случаи)",
     "Повторное извлечение при низкой уверенности/ошибках", True),
    # Агентские слоты помечены local-only не потому, что облако для них
    # запрещено — CLAUDE.md прямо разрешает cloud-модели для planner/auditor и
    # генерации писем, — а потому, что через них проходит содержимое писем,
    # счетов и чертежей. Флаг делает их cloud-opt-in: облако остаётся
    # доступным, но включается осознанно (PATCH /slots/{slot}/allow-cloud с
    # подтверждением), а не побочным эффектом выбора модели из списка.
    ("agent_orchestrator", "Агент", "Оркестратор",
     "Планирование, вызов инструментов, диалог", True),
    ("agent_fast", "Агент", "Быстрая (роутер/простые ходы)",
     "Классификация хода и быстрые ответы — лёгкая модель, structured output", True),
    ("agent_email", "Агент", "Письма",
     "Генерация деловых писем и черновиков", True),
    ("agent_large", "Агент", "Большая (скиллы/скрипты/ТП)",
     "Генерация кода, навыков, техпроцессов", True),
    ("embedding", "Поиск", "Векторизация (embedding)",
     "Семантический поиск по документам", True),
    ("rerank", "Поиск", "Реранкинг",
     "Переранжирование результатов поиска", True),
    ("cad_spec_read", "Оцифровка", "Чтение чертежа (VLM)",
     "Метод «по описанию»: модель читает исходное изображение в структурный спек", True),
    # The text layer is its own slot because it is its own job: transcribing
    # what is WRITTEN on the sheet, where a small document specialist beats the
    # large general reader (fit recall 0.800 -> 1.000). It used to be a
    # hardcoded model name, so nothing else could be tried against it.
    ("cad_text_ocr", "Оцифровка", "Текстовый слой чертежа (OCR)",
     "Транскрипция надписей и размеров с листа. Специализированная документная "
     "модель читает точнее общего VLM; геометрию не определяет", True),
    # NB: the experimental whole-sheet graph pipeline (layout / fragment /
    # evidence-verify / legacy read) is intentionally NOT surfaced as user
    # slots — it runs opt-in on its model_registry.yaml fallback defaults.
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
    "cad_text_ocr": "vision",
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
    "cad_spec_read": ["cad_spec_read"],
    "cad_text_ocr": ["cad_text_ocr"],
    "cad_spec_draft": ["cad_spec_draft"],
}
_SLOT_THINKING_AGENT_FIELDS: dict[str, list[str]] = {
    "agent_orchestrator": ["orchestrator_disable_thinking", "worker_disable_thinking"],
    "agent_fast": ["fast_disable_thinking"],
    "agent_large": ["builder_disable_thinking"],
}
# Same slot→field(s) shape as above, for the reasoning-effort level tri-state.
_SLOT_THINKING_LEVEL_AGENT_FIELDS: dict[str, list[str]] = {
    "agent_orchestrator": ["orchestrator_thinking_level", "worker_thinking_level"],
    "agent_fast": ["fast_thinking_level"],
    "agent_large": ["builder_thinking_level"],
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
    if slot == "cad_text_ocr":
        return get_routing_for(AITask.CAD_TEXT_OCR).primary
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


class SlotDraft(BaseModel):
    """Черновик одного слота: модель и всё, что вместе с ней применяется.

    Раньше черновик вмещал только имя модели, поэтому рассуждение, узел и
    разрешение облака приходилось применять немедленно, отдельными запросами.
    В одной карточке получалось два разных поведения: модель ждала кнопки
    «Применить», а соседний переключатель срабатывал сразу — и понять, что
    именно требует подтверждения, было нельзя.

    Порядок тоже важен: `allow_cloud` расширяет множество допустимых моделей,
    значит должен учитываться ДО валидации, а не после неё. А рассуждение
    зависит от выбранной модели — включить его и следом сменить модель на
    не-думающую означало молча потерять настройку.
    """

    model: str | None = None
    thinking: bool | None = None
    thinking_level: Literal["low", "medium", "high"] | None = None
    allow_cloud: bool | None = None
    preferred_instance: str | None = None


def _as_slot_draft(value: SlotDraft | str | None) -> SlotDraft:
    """Строка означает «только модель» — так выглядели все прежние вызовы."""
    if isinstance(value, SlotDraft):
        return value
    return SlotDraft(model=value)


class AssignmentDraftIn(BaseModel):
    # str | None принимается ради совместимости: так черновик выглядел раньше,
    # и на этой форме держатся существующие тесты и внешние вызовы.
    slots: dict[str, SlotDraft | str | None]
    confirm_warnings: bool = False

    @property
    def model_slots(self) -> dict[str, str | None]:
        """Только модели — форма, которую ждёт валидация и применение."""
        return {k: _as_slot_draft(v).model for k, v in self.slots.items()}

    @property
    def drafts(self) -> dict[str, SlotDraft]:
        return {k: _as_slot_draft(v) for k, v in self.slots.items()}


def _cloud_overrides(payload: "AssignmentDraftIn") -> dict[str, bool]:
    """Слоты, которым черновик открывает или закрывает облако."""
    return {
        slot: draft.allow_cloud
        for slot, draft in payload.drafts.items()
        if draft.allow_cloud is not None
    }


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


# ── Per-slot cloud opt-in ────────────────────────────────────────────────────
# Confidential slots (documents / digitize / search) default to local-only so
# invoice and drawing content never leaves the machine. An admin may explicitly
# opt a single slot into cloud models; the effective local_only below then flips
# to False, which both unlocks cloud in the picker AND lets the AI router allow
# cloud for that task (routing.local_only follows the assigned model).
_SLOT_CLOUD_KEY = "providers:slot_allow_cloud"


def _cloud_allowed_slots() -> set[str]:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().smembers(_SLOT_CLOUD_KEY)
        return {m.decode() if isinstance(m, bytes) else str(m) for m in (raw or set())}
    except Exception:  # noqa: BLE001 — absence of Redis just means "no opt-in"
        return set()


def _set_slot_cloud_allowed(slot: str, allowed: bool) -> None:
    from app.utils.redis_client import get_sync_redis

    client = get_sync_redis()
    if allowed:
        client.sadd(_SLOT_CLOUD_KEY, slot)
    else:
        client.srem(_SLOT_CLOUD_KEY, slot)


def _slot_base_local_only(slot: str) -> bool:
    meta = _slot_meta(slot)
    return bool(meta[4]) if meta else False


def _slot_effective_local_only(slot: str, cloud_slots: set[str] | None = None) -> bool:
    """A slot is local-only unless it is a cloud-opt-in slot the admin enabled."""
    if not _slot_base_local_only(slot):
        return False
    if cloud_slots is None:
        cloud_slots = _cloud_allowed_slots()
    return slot not in cloud_slots


def _slot_supports_thinking(slot: str) -> bool:
    return slot in _SLOT_THINKING_TASKS or slot in _SLOT_THINKING_AGENT_FIELDS


def _slot_thinking_values(slot: str) -> list[bool | None]:
    """Состояние рассуждения по КАЖДОМУ полю, которое стоит за слотом.

    Слот `agent_orchestrator` пишет сразу в две роли (orchestrator и worker), а
    читалось только первое поле. Если значения разошлись — например, роль
    поменяли из другого места, — интерфейс показывал одно и умалчивал про
    второе, и человек видел не то состояние, которое реально применяется.
    """
    if not _slot_supports_thinking(slot):
        return []
    if slot in _SLOT_THINKING_AGENT_FIELDS:
        from app.ai.agent_config import get_builtin_agent_config

        cfg = get_builtin_agent_config()
        out: list[bool | None] = []
        for field in _SLOT_THINKING_AGENT_FIELDS[slot]:
            disable = getattr(cfg, field, None)
            out.append(None if disable is None else (not disable))
        return out
    if slot in _SLOT_THINKING_TASKS:
        from app.ai.schemas import AITask
        from app.ai.task_routing import get_routing_for

        out = []
        for task_value in _SLOT_THINKING_TASKS[slot]:
            try:
                out.append(get_routing_for(AITask(task_value)).thinking)
            except (ValueError, KeyError):
                out.append(None)
        return out
    return []


def _slot_thinking_mixed(slot: str) -> bool:
    """Разошлись ли поля, стоящие за одним переключателем."""
    values = _slot_thinking_values(slot)
    return len(set(values)) > 1 if values else False


def _slot_thinking_override(slot: str) -> bool | None:
    """Current per-assignment reasoning override. None = model default."""
    if not _slot_supports_thinking(slot):
        return None
    if slot in _SLOT_THINKING_AGENT_FIELDS:
        values = _slot_thinking_values(slot)
        return values[0] if values else None
    if slot in _SLOT_THINKING_TASKS:
        from app.ai.schemas import AITask
        from app.ai.task_routing import get_routing_for
        try:
            return get_routing_for(AITask(_SLOT_THINKING_TASKS[slot][0])).thinking
        except (ValueError, KeyError):
            return None
    return None


def _slot_thinking_level_override(slot: str) -> str | None:
    """Current per-assignment reasoning-effort level override, or None."""
    if not _slot_supports_thinking(slot):
        return None
    if slot in _SLOT_THINKING_LEVEL_AGENT_FIELDS:
        from app.ai.agent_config import get_builtin_agent_config
        cfg = get_builtin_agent_config()
        field = _SLOT_THINKING_LEVEL_AGENT_FIELDS[slot][0]
        return getattr(cfg, field, None)
    if slot in _SLOT_THINKING_TASKS:
        from app.ai.schemas import AITask
        from app.ai.task_routing import get_routing_for
        try:
            return get_routing_for(AITask(_SLOT_THINKING_TASKS[slot][0])).thinking_level
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

    # Reasoning-effort level — only meaningful when the selected model
    # declares thinking_levels and the slot ends up with thinking ON.
    model_levels = (
        effective_thinking_levels(cap.thinking_supported, cap.provider.value, cap.thinking_levels)
        if cap
        else []
    )
    level_override = _slot_thinking_level_override(slot) if model_levels else None
    level_effective = None
    if effective and model_levels:
        level_effective = level_override
        if level_effective is None:
            level_effective = cap.thinking_level_default or "medium"
        if level_effective not in model_levels:
            level_effective = model_levels[0]

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
        # За одним переключателем может стоять несколько полей (слот
        # «Оркестратор» пишет и в orchestrator, и в worker). Если они
        # разошлись, честнее сказать об этом, чем показать значение первого.
        "thinking_mixed": _slot_thinking_mixed(slot),
        "thinking_warning": warning,
        "thinking_levels": model_levels,
        "thinking_level_override": level_override,
        "thinking_level_effective": level_effective,
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
    cloud_slots: set[str] | None = None,
) -> SlotOut:
    """SlotOut with single-source required_modality + effective reasoning state.

    ``local_only`` here is the slot's BASE policy; the response reports the
    EFFECTIVE policy (base minus any admin cloud opt-in) so the picker filters
    correctly and can show the opt-in toggle."""
    applied = _slot_current_model(slot, registry) if current_model is ... else current_model
    thinking = _slot_thinking_state(slot, registry, model)
    cloud_allowed = bool(local_only) and slot in (
        cloud_slots if cloud_slots is not None else _cloud_allowed_slots()
    )
    effective_local_only = bool(local_only) and not cloud_allowed
    return SlotOut(
        slot=slot, group=group, label=label, hint=hint,
        model=model,
        current_model=applied,
        local_only=effective_local_only,
        cloud_optionable=bool(local_only),
        cloud_allowed=cloud_allowed,
        required_modality=_SLOT_MODALITY.get(slot),
        **thinking,
    )


def _all_slots_out(model_of, registry) -> list[SlotOut]:
    """Build every SlotOut; `model_of(slot)` returns the assigned model key."""
    cloud_slots = _cloud_allowed_slots()
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
            cloud_slots=cloud_slots,
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
    if slot == "cad_text_ocr":
        return [AITask.CAD_TEXT_OCR.value]
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
    cloud_overrides: dict[str, bool] | None = None,
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
        # Проверка шла по БАЗОВОМУ local_only, то есть игнорировала выданное
        # разрешение на облако: применение (_apply_slot_assignment) его
        # учитывает, а валидация — нет, и она оказывалась строже. Слот, где
        # облако разрешено осознанно, всё равно отвергался.
        cloud_ok = (cloud_overrides or {}).get(slot)
        slot_local_only = (
            not cloud_ok if cloud_ok is not None else _slot_effective_local_only(slot)
        )
        if slot_local_only and not cap.local_only:
            errors.append(AssignmentIssue(
                slot=slot, model=model_key, code="cloud_for_confidential",
                message=(
                    "Слот работает с содержимым документов — облачную модель "
                    "нужно разрешить для него отдельно"
                ),
                severity="error",
            ))
        required = _SLOT_MODALITY.get(slot)
        if required and required not in {m.value for m in cap.modalities}:
            # «Не умеет» и «мы не проверяли» — разные вещи для того, кто
            # выбирает модель. Второе чинится пробным запросом, первое нет.
            if getattr(cap, "capabilities_unknown", False):
                warnings.append(AssignmentIssue(
                    slot=slot, model=model_key, code="capabilities_unknown",
                    message=(
                        "Провайдер не сообщает возможности этой модели — "
                        "проверьте пробным запросом"
                    ),
                ))
            else:
                warnings.append(AssignmentIssue(
                    slot=slot, model=model_key, code="modality_mismatch",
                    message=f"Модель не заявляет capability '{required}'",
                ))
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
    if _slot_effective_local_only(slot) and not cap.local_only:
        raise HTTPException(
            400,
            "Этот слот сейчас только для локальных моделей (конфиденциально). "
            "Разрешите облако для этого слота, если это осознанное решение.",
        )

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
        elif slot == "cad_text_ocr":
            _assign_task(AITask.CAD_TEXT_OCR, key)
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


# Strong refs to fire-and-forget autostart tasks so they aren't GC'd mid-flight.
_AUTOSTART_TASKS: set = set()


async def _autostart_assigned_provider(model_key: str, registry) -> None:
    """Bring a lazy single-model local server up for a newly assigned model.

    vLLM and llama.cpp each serve ONE model from a profile-gated container that
    is stopped between activations; assigning such a model as a provider should
    make it usable without a separate manual "load model" step. Best-effort:
    Ollama is always-on and cloud models are ignored (no-op), and any failure
    only logs — the assignment itself has already been persisted.
    """
    from app.ai.schemas import ProviderKind

    cap = registry.models.get(model_key)
    if cap is None:
        return
    try:
        # VRAM headroom first (auto-frees resident Ollama models). A generic
        # vLLM/llama.cpp entry has vram_gb_estimate=0, but starting the server
        # still claims most of the GPU (gpu-memory-utilization × total), so use a
        # per-kind floor — otherwise the pre-check is a no-op and the server
        # crashes on startup with "Free memory < desired GPU memory utilization"
        # while a big Ollama model is still resident (observed live 2026-07-23).
        _VRAM_FLOOR = {ProviderKind.VLLM: 18.0, ProviderKind.LLAMACPP: 8.0}
        vram = float(getattr(cap, "vram_gb_estimate", 0) or 0.0)
        vram = max(vram, _VRAM_FLOOR.get(cap.provider, 0.0))
        if vram > 0:
            try:
                from app.ai import gpu_manager

                await gpu_manager.ensure_vram_for(
                    cap.provider.value, vram, auto_free=True
                )
            except Exception as exc:  # noqa: BLE001 — pre-check is advisory
                logger.warning("assignment_autostart_vram_failed", error=str(exc)[:160])

        if cap.provider == ProviderKind.VLLM:
            from app.ai.providers import vllm_manager

            pm = str(cap.provider_model or "")
            # vLLM serves one model under a fixed served-name; a generic entry
            # ("local"/"") just needs the server up, while a concrete HF-repo
            # entry additionally sets that model before (re)starting.
            if pm and pm != "local":
                res = await vllm_manager.ensure_model_active(pm)
            else:
                res = await vllm_manager.ensure_server_running()
            logger.info(
                "assignment_autostart", provider="vllm",
                model=pm, status=res.get("status"),
            )
        elif cap.provider == ProviderKind.LLAMACPP:
            from app.ai.providers import llamacpp_manager

            pm = str(cap.provider_model or "")
            if pm.endswith(".gguf"):
                # A concrete GGUF was assigned → load exactly that file + start.
                res = await llamacpp_manager.activate_model({"path": pm})
                status = getattr(res, "status", None)
            else:
                # Generic llama.cpp entry → just ensure the server is up with its
                # currently-active GGUF (chosen via the model-management UI).
                res = await llamacpp_manager.ensure_server_running()
                status = res.get("status") if isinstance(res, dict) else None
            logger.info("assignment_autostart", provider="llamacpp", status=status)

        # Give the freshly (re)started server a full idle window so the on-demand
        # idle reaper (server_lifecycle.stop_idle_servers) doesn't stop it before
        # a request ever arrives — a start triggered by assignment records no use.
        try:
            from app.ai import server_lifecycle

            server_lifecycle.mark_used(cap.provider.value)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — never fail an assignment on autostart
        logger.warning("assignment_autostart_failed", model=model_key, error=str(exc)[:200])


def _schedule_provider_autostart(model_keys, registry) -> None:
    """Fire-and-forget autostart for assigned models. Bringing an ML server up
    can take minutes, so it must never block the assignment response. At most one
    model per lazy kind (vLLM/llama.cpp) is started — a single-model server can
    only serve one, so the last assigned model of each kind wins without racing
    container restarts."""
    import asyncio

    from app.ai.schemas import ProviderKind

    per_kind: dict = {}
    for model_key in model_keys:
        cap = registry.models.get(model_key) if model_key else None
        if cap is not None and cap.provider in (ProviderKind.VLLM, ProviderKind.LLAMACPP):
            per_kind[cap.provider] = model_key
    for model_key in per_kind.values():
        task = asyncio.create_task(_autostart_assigned_provider(model_key, registry))
        _AUTOSTART_TASKS.add(task)
        task.add_done_callback(_AUTOSTART_TASKS.discard)


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
    # allow_cloud из черновика расширяет множество допустимых моделей, поэтому
    # учитывается ДО проверки — иначе выбор облачной модели вместе с галочкой
    # «разрешить облако» отвергался бы как ошибка.
    diff, warnings, errors = await _validate_assignment_draft(
        registry, payload.model_slots, cloud_overrides=_cloud_overrides(payload)
    )
    return AssignmentDraftOut(
        # model_slots, а не slots: в черновике теперь лежит объект, и его
        # нельзя подставить туда, где ждут ключ модели.
        slots=_all_slots_out(
            lambda s: payload.model_slots.get(s, _slot_current_model(s, registry)),
            registry,
        ),
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
    diff, warnings, errors = await _validate_assignment_draft(
        registry, payload.model_slots, loaded, cloud_overrides=_cloud_overrides(payload)
    )
    if errors:
        raise HTTPException(400, {"errors": [e.model_dump() for e in errors]})
    if warnings and not payload.confirm_warnings:
        raise HTTPException(409, {"warnings": [w.model_dump() for w in warnings]})

    before = _assignment_snapshot(registry)
    # Разрешение облака применяется ПЕРВЫМ: оно определяет, законно ли само
    # назначение. Остальное — после того, как модель встала на место, потому
    # что рассуждение и узел зависят от выбранной модели.
    for slot, d in payload.drafts.items():
        if d.allow_cloud is not None and _slot_base_local_only(slot):
            _set_slot_cloud_allowed(slot, d.allow_cloud)

    await _apply_draft_atomic(db, diff, before, registry)  # rolls back Redis on error

    for slot, d in payload.drafts.items():
        if d.thinking is not None or d.thinking_level is not None:
            try:
                _apply_slot_thinking(slot, d.thinking, d.thinking_level)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("slot_thinking_apply_failed", slot=slot, error=str(exc))
        if d.preferred_instance is not None:
            model_key = d.model or _slot_current_model(slot, registry)
            if model_key:
                pin = await _instance_id_for_pin(db, d.preferred_instance)
                from app.ai.model_registry import set_preferred_instance

                set_preferred_instance(model_key, pin)
                await model_runtime_store.persist_model_override(
                    db, model_key=model_key, preferred_instance=pin or ""
                )
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
    _schedule_provider_autostart([d.new_model for d in diff], after_registry)
    return AssignmentDraftOut(
        slots=_all_slots_out(lambda s: _slot_current_model(s, after_registry), after_registry),
        diff=diff,
        warnings=warnings,
        ok_to_apply=True,
        revision_id=str(revision.id),
    )


class ModelCandidateReason(BaseModel):
    code: str
    message: str
    # Что нажать, чтобы это починить: подключить облако, включить узел,
    # скачать модель. `none` — чинить нечем, это свойство самой модели.
    fix_action: Literal[
        "open_cloud_provider", "enable_node", "pull_model", "verify_model", "none"
    ] = "none"
    fix_target: str | None = None


class ModelCandidateOut(BaseModel):
    key: str
    provider: str
    provider_model: str
    node: str | None = None
    availability: str = "unknown"
    modalities: list[str] = []
    max_context_tokens: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    vram_gb_estimate: float | None = None
    thinking_supported: bool = False
    thinking_levels: list[str] = []
    local_only: bool = True
    capabilities_unknown: bool = False
    notes: str | None = None
    # ok — можно назначать; needs_action — чинится действием человека;
    # unsuitable — не подходит слоту, но выбор возможен с предупреждением;
    # forbidden — выбор запрещён политикой.
    eligibility: Literal["ok", "needs_action", "unsuitable", "forbidden"] = "ok"
    reasons: list[ModelCandidateReason] = []


def _model_eligibility(
    slot: str,
    cap: ModelCapability,
    *,
    is_loaded: bool,
    slot_local_only: bool,
) -> tuple[str, list[ModelCandidateReason]]:
    """Пригодность модели для слота — по тем же правилам, что и валидация.

    Правила жили только в _validate_assignment_draft, поэтому интерфейс держал
    их вторую копию на TypeScript. Копии уже расходились: список локальных
    провайдеров и набор умеющих выключать рассуждение отличались от серверных.
    Здесь один источник для обоих.
    """
    reasons: list[ModelCandidateReason] = []
    verdict = "ok"

    if slot_local_only and not cap.local_only:
        reasons.append(ModelCandidateReason(
            code="cloud_for_confidential",
            message="Слот работает с содержимым документов — выберите облако осознанно",
            fix_action="open_cloud_provider",
            fix_target=cap.provider.value,
        ))
        verdict = "forbidden"

    if cap.provider in _LOCAL_KINDS and not is_loaded:
        reasons.append(ModelCandidateReason(
            code="not_loaded",
            message="Модели нет ни на одном включённом узле",
            fix_action="pull_model",
            fix_target=cap.provider_model,
        ))
        if verdict == "ok":
            verdict = "needs_action"

    required = _SLOT_MODALITY.get(slot)
    if required and required not in {m.value for m in cap.modalities}:
        if getattr(cap, "capabilities_unknown", False):
            reasons.append(ModelCandidateReason(
                code="capabilities_unknown",
                message="Провайдер не сообщает возможности модели — проверьте пробным запросом",
                fix_action="verify_model",
                fix_target=cap.name,
            ))
            if verdict == "ok":
                verdict = "needs_action"
        else:
            reasons.append(ModelCandidateReason(
                code="modality_mismatch",
                message=f"Модель не заявляет «{required}»",
            ))
            if verdict == "ok":
                verdict = "unsuitable"

    return verdict, reasons


@router.get(
    "/slots/{slot}/candidates",
    response_model=list[ModelCandidateOut],
    dependencies=_admin,
)
async def slot_candidates(slot: str) -> list[ModelCandidateOut]:
    """Модели для слота с готовым вердиктом пригодности.

    Интерфейс раньше решал это сам и потому повторял серверные правила на
    TypeScript. Здесь тот же расчёт, что и в валидации черновика, вместе с
    подсказкой, каким действием чинится каждая помеха.
    """
    if _slot_meta(slot) is None:
        raise HTTPException(404, f"Unknown slot: {slot}")

    registry = _registry()
    loaded = await _loaded_index()
    slot_local_only = _slot_effective_local_only(slot)

    try:
        from app.ai.provider_registry import catalog_availability

        avail = {
            k: v.value
            for k, v in (
                await asyncio.to_thread(catalog_availability, registry.models)
            ).items()
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot_candidates_availability_failed", error=str(exc))
        avail = {}

    out: list[ModelCandidateOut] = []
    for key, cap in registry.models.items():
        if cap.status == ModelStatus.DISABLED:
            continue
        node = _loaded_node_for(cap, loaded)
        verdict, reasons = _model_eligibility(
            slot, cap, is_loaded=node is not None, slot_local_only=slot_local_only
        )
        out.append(ModelCandidateOut(
            key=key,
            provider=cap.provider.value,
            provider_model=cap.provider_model,
            node=node,
            availability=avail.get(key, "unknown"),
            modalities=sorted(m.value for m in cap.modalities),
            thinking_supported=cap.thinking_supported,
            thinking_levels=effective_thinking_levels(
                cap.thinking_supported, cap.provider.value, cap.thinking_levels
            ),
            local_only=cap.local_only,
            capabilities_unknown=getattr(cap, "capabilities_unknown", False),
            vram_gb_estimate=cap.vram_gb_estimate,
            eligibility=verdict,
            reasons=reasons,
            **_capability_facts(cap),
        ))
    return out


class SlotHealthOut(BaseModel):
    slot: str
    label: str
    model: str | None
    provider: str | None
    availability: str
    node: str | None
    calls: int = 0
    errors: int = 0
    error_rate: float = 0.0
    avg_latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    # None означает «цена неизвестна», а не «бесплатно»: у большинства
    # моделей cost_per_1k_* в каталоге не заполнена.
    cost_usd: float | None = None
    priced: bool = False


@router.get("/slots/health", response_model=list[SlotHealthOut], dependencies=_admin)
async def slots_health() -> list[SlotHealthOut]:
    """Здоровье каждого слота одним запросом.

    Три источника уже существовали, но лежали на разных вкладках: доступность
    модели — в каталоге, вызовы и задержка — в телеметрии, мёртвые звенья
    цепочки — в отдельной панели. Человек, назначивший модель, не мог узнать,
    работает ли она, не уходя с экрана.

    Задержка честно называется средней: телеметрия хранит сумму и счётчик, а
    не гистограмму, поэтому медианы здесь взяться неоткуда.
    """
    from app.ai import telemetry
    from app.ai.provider_registry import catalog_availability

    registry = _registry()
    try:
        avail = {
            k: v.value
            for k, v in (
                await asyncio.to_thread(catalog_availability, registry.models)
            ).items()
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot_health_availability_failed", error=str(exc))
        avail = {}

    # Телеметрия хранит строку на КАЖДУЮ пару (задача, модель): у одной задачи
    # их столько, сколько моделей на ней перебывало. Схлопывать по задаче
    # нельзя — иначе слот показывает статистику давно снятой модели: на стенде
    # так и вышло, embedding отдавал 100% ошибок от local_embedding_ollama,
    # которой в каталоге уже нет, вместо 0.7% у назначенной сейчас.
    by_task_model: dict[tuple[str, str], dict] = {}
    try:
        for row in (telemetry.get_summary().get("by_model") or []):
            by_task_model[(str(row.get("task")), str(row.get("model")))] = row
    except Exception as exc:  # noqa: BLE001 — телеметрия не критична
        logger.warning("slot_health_telemetry_failed", error=str(exc))

    out: list[SlotHealthOut] = []
    for slot, _group, label, _hint, _local in _SLOTS:
        model_key = _slot_current_model(slot, registry)
        cap = registry.models.get(model_key) if model_key else None

        calls = errors = tokens_in = tokens_out = latency_sum = 0
        for task in _slot_affected(slot):
            row = by_task_model.get((task, model_key or ""))
            if not row:
                continue
            c = int(row.get("calls") or 0)
            calls += c
            errors += int(row.get("errors") or 0)
            tokens_in += int(row.get("tokens_in") or 0)
            tokens_out += int(row.get("tokens_out") or 0)
            latency_sum += int(row.get("avg_latency_ms") or 0) * c

        priced = bool(cap and (cap.cost_per_1k_input or cap.cost_per_1k_output))
        cost = None
        if priced and cap:
            cost = (
                tokens_in / 1000 * (cap.cost_per_1k_input or 0)
                + tokens_out / 1000 * (cap.cost_per_1k_output or 0)
            )

        out.append(SlotHealthOut(
            slot=slot,
            label=label,
            model=model_key,
            provider=cap.provider.value if cap else None,
            availability=avail.get(model_key or "", "unknown"),
            node=_pin_display_name(cap.preferred_instance) if cap else None,
            calls=calls,
            errors=errors,
            error_rate=(errors / calls) if calls else 0.0,
            avg_latency_ms=round(latency_sum / calls) if calls else 0,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 4) if cost is not None else None,
            priced=priced,
        ))
    return out


class AssignmentRevisionOut(BaseModel):
    id: str
    created_at: datetime
    created_by: str
    summary: list[dict]
    warnings_count: int


@router.get(
    "/assignments/revisions",
    response_model=list[AssignmentRevisionOut],
    dependencies=_admin,
)
async def list_assignment_revisions(
    limit: int = 20, db: AsyncSession = Depends(get_db)
) -> list[AssignmentRevisionOut]:
    """История назначений.

    Ревизии писались с самого начала — с автором, снимками до и после, diff и
    предупреждениями, — но прочитать их было нечем: существовал только откат
    по id. Поэтому «Откатить» работал лишь для последнего изменения текущей
    вкладки и исчезал при перезагрузке страницы, а узнать, кто и когда сменил
    модель, было нельзя вовсе.
    """
    from app.db.models import ModelAssignmentRevision

    rows = (
        await db.execute(
            select(ModelAssignmentRevision)
            .order_by(ModelAssignmentRevision.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).scalars().all()

    return [
        AssignmentRevisionOut(
            id=str(r.id),
            created_at=r.created_at,
            created_by=r.created_by,
            summary=[
                {
                    "slot": d.get("slot"),
                    "old_model": d.get("old_model"),
                    "new_model": d.get("new_model"),
                }
                for d in (r.diff or [])
            ],
            warnings_count=len(r.warnings or []),
        )
        for r in rows
    ]


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
    _schedule_provider_autostart([d.new_model for d in diff], after_registry)
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
    _schedule_provider_autostart([payload.model], registry)
    key = payload.model
    return {"ok": True, "slot": slot, "model": key}


class SlotCloudWrite(BaseModel):
    allowed: bool


@router.patch("/slots/{slot}/allow-cloud", dependencies=_admin)
async def set_slot_allow_cloud(slot: str, payload: SlotCloudWrite) -> dict:
    """Opt a confidential slot into (or out of) cloud models — protected setting.

    Only meaningful for a base-local-only slot: enabling it lets the picker offer
    cloud models for this slot and lets the AI router send this task's content to
    a cloud provider once a cloud model is assigned. Non-confidential slots always
    allow cloud, so this is a no-op there.
    """
    meta = _slot_meta(slot)
    if meta is None:
        raise HTTPException(404, f"Unknown slot: {slot}")
    if not _slot_base_local_only(slot):
        return {"ok": True, "slot": slot, "cloud_allowed": True, "note": "slot already allows cloud"}
    _set_slot_cloud_allowed(slot, payload.allowed)
    return {"ok": True, "slot": slot, "cloud_allowed": payload.allowed}


class SlotThinkingWrite(BaseModel):
    enabled: bool | None  # None → model default; True/False → force on/off for this slot
    level: str | None = None  # reasoning-effort level; only valid for the selected model's thinking_levels


class SlotSmokeIn(BaseModel):
    model: str | None = None
    thinking: bool | None = None
    thinking_level: str | None = None
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


def _apply_slot_thinking(slot: str, enabled: bool | None, level: str | None = None) -> None:
    """Set per-assignment reasoning (+ optional level) for a slot
    (task_routing + agent_config). The same model can thus reason in one
    slot and not in another. Idempotent.
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
            routing = get_routing_for(task).model_copy(
                update={"thinking": enabled, "thinking_level": level}
            )
            save_task_routing(task, routing)
    # Agent-config slots: write the tri-state *_disable_thinking field(s).
    if slot in _SLOT_THINKING_AGENT_FIELDS:
        from app.ai.agent_config import BuiltinAgentConfigUpdate, update_builtin_agent_config
        disable = None if enabled is None else (not enabled)
        patch = {field: disable for field in _SLOT_THINKING_AGENT_FIELDS[slot]}
        if slot in _SLOT_THINKING_LEVEL_AGENT_FIELDS:
            patch.update({field: level for field in _SLOT_THINKING_LEVEL_AGENT_FIELDS[slot]})
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
    _apply_slot_thinking(slot, payload.enabled, payload.level)
    await _persist_slot_durable(db, slot)
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return {"ok": True, "slot": slot, "thinking_enabled": payload.enabled, "thinking_level": payload.level}


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
    thinking_level_requested = (
        payload.thinking_level
        if payload.thinking_level is not None
        else thinking_state["thinking_level_effective"]
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
        thinking_level=thinking_level_requested,
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
