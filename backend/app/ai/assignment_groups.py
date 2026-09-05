"""Simplified model assignment in two user-facing groups.

The raw routing layer exposes 14 :class:`AITask` entries plus 5 agent roles.
For day-to-day use the Settings UI collapses these into two groups:

* **"Обработка документов"** — a few practical slots (vision / text / embedding /
  rerank) that fan out to the document-processing tasks in ``task_routing``.
* **"Агент"** — the assistant ("Света"): one main model (= orchestrator = worker
  = auditor = fast) and an optional large "builder" model, written to
  ``agent_config`` AND synced into ``task_routing`` for the orchestrator/tool
  tasks so the pinned-orchestrator warmup (``model_lifecycle``) stays correct.

This is a thin convenience layer on top of the existing stores — it does NOT
introduce a new source of truth. The advanced view keeps editing the raw 14
tasks / 5 roles directly.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.ai.agent_config import (
    BuiltinAgentConfigUpdate,
    get_builtin_agent_config,
    update_builtin_agent_config,
)
from app.ai.schemas import AITask
from app.ai.task_routing import (
    CONFIDENTIAL_TASKS,
    _catalog_key_for,
    _is_local_key,
    get_routing_for,
    save_task_routing,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Document group → AITask fan-out
# ---------------------------------------------------------------------------
# Each slot drives a set of tasks. Setting a slot replaces the *primary* model
# of every task in the slot while preserving the existing fallback tail.

VISION_DOC_TASKS: list[AITask] = [
    AITask.INVOICE_OCR,
    AITask.DRAWING_ANALYSIS,
    AITask.DRAWING_ANALYSIS_VLM,
]
TEXT_DOC_TASKS: list[AITask] = [
    AITask.STRUCTURED_EXTRACTION,
    AITask.CLASSIFICATION,
    AITask.ENGINEERING_REASONING,
    AITask.EMAIL_DRAFTING,
    AITask.LONG_CONTEXT_SUMMARIZATION,
    AITask.CODE_GENERATION,
]
EMBEDDING_DOC_TASKS: list[AITask] = [AITask.EMBEDDING]
RERANK_DOC_TASKS: list[AITask] = [AITask.RERANKING]

# Agent group → task_routing tasks kept in sync with agent_config roles.
AGENT_SYNC_TASKS: list[AITask] = [AITask.ORCHESTRATOR_PLANNING, AITask.TOOL_CALLING]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DocumentGroup(BaseModel):
    """Catalog keys for the document-processing slots."""

    vision_model: str | None = None
    text_model: str | None = None
    embedding_model: str | None = None
    rerank_model: str | None = None
    # Large model used for re-extraction when primary OCR produces low-confidence
    # or arithmetic errors (e.g. discount column confusion). Local only — confidential.
    ocr_fallback_model: str | None = None


class AgentGroup(BaseModel):
    """Raw model names (+ provider) for the agent."""

    agent_model: str | None = None
    agent_provider: str | None = None
    large_model: str | None = None
    large_provider: str | None = None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _primary_for(task: AITask) -> str | None:
    return get_routing_for(task).primary


def get_document_group() -> DocumentGroup:
    try:
        from app.api.ai_settings import get_ai_config
        _fallback_name = get_ai_config().get("model_ocr_fallback")
    except Exception:
        _fallback_name = None
    return DocumentGroup(
        vision_model=_primary_for(AITask.INVOICE_OCR),
        text_model=_primary_for(AITask.ENGINEERING_REASONING),
        embedding_model=_primary_for(AITask.EMBEDDING),
        rerank_model=_primary_for(AITask.RERANKING),
        ocr_fallback_model=_fallback_name,
    )


def get_agent_group() -> AgentGroup:
    cfg = get_builtin_agent_config()
    return AgentGroup(
        agent_model=cfg.worker_model or cfg.model,
        agent_provider=cfg.worker_provider or cfg.provider,
        large_model=cfg.builder_model,
        large_provider=cfg.builder_provider or cfg.provider,
    )


def get_groups() -> dict:
    """Both groups, for the simplified Settings view."""
    return {
        "documents": get_document_group().model_dump(),
        "agent": get_agent_group().model_dump(),
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def prune_dead_keys(keys: list[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped) — выбросить из цепочки то, чего заведомо нет.

    Хвост фолбэков накапливался вечно: `_set_primary` ставил новую модель в
    голову и сохранял прежний хвост целиком, а GUI показывает только голову.
    Поэтому после перехода gemma4 → qwen3.8 ссылки на gemma4 остались в
    цепочках у большинства задач — невидимо, пока голова жива, и с 404 на
    каждой попытке фолбэка, как только она недоступна.

    Выбрасываем только то, в чём уверены: ключа нет в каталоге, либо узлы
    провайдера ответили и модели у них нет. Молчащий узел значит «неизвестно»
    — по нему не чистим, иначе одна сетевая заминка сотрёт рабочую настройку.
    """
    from app.ai.provider_registry import Availability, model_availability

    try:
        from app.ai.model_registry import ModelRegistry

        catalog = ModelRegistry.from_yaml(
            "backend/app/ai/config/model_registry.yaml"
        ).models
    except Exception as exc:  # noqa: BLE001
        # Без каталога судить не о чем — ничего не трогаем.
        logger.warning("prune_catalog_unavailable", error=str(exc))
        return list(keys), []

    kept, dropped = [], []
    for key in keys:
        cap = catalog.get(key)
        if cap is None:
            dropped.append(key)
            continue
        try:
            state = model_availability(cap.provider, cap.provider_model)
        except Exception as exc:  # noqa: BLE001
            logger.debug("prune_probe_failed", key=key, error=str(exc))
            kept.append(key)
            continue
        (dropped if state is Availability.MISSING else kept).append(key)
    return kept, dropped


def _set_primary(task: AITask, model_key: str) -> None:
    """Make ``model_key`` the primary for ``task``, dropping dead fallbacks.

    Reuses :func:`task_routing.save_task_routing`, so confidentiality and catalog
    validation are enforced and the lifecycle cache is invalidated.
    """
    current = get_routing_for(task)
    tail = [m for m in current.models if m != model_key]
    # Confidential tasks reject cloud models in the chain; the YAML defaults may
    # still list cloud fallbacks, so drop non-local keys from the preserved tail.
    if task in CONFIDENTIAL_TASKS:
        tail = [m for m in tail if _is_local_key(m)]
    tail, dropped = prune_dead_keys(tail)
    if dropped:
        logger.info(
            "task_routing_dead_fallbacks_pruned",
            task=str(task), dropped=dropped,
        )
    routing = current.model_copy(update={"models": [model_key, *tail]})
    save_task_routing(task, routing)


def _key_to_name_provider(model_key: str) -> tuple[str | None, str | None]:
    """Resolve a catalog key to its raw ``(provider_model, provider)``."""
    try:
        from app.ai.model_registry import ModelRegistry

        reg = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
        cap = reg.models.get(model_key)
        if cap is not None:
            return cap.provider_model, cap.provider.value
    except Exception as exc:
        logger.debug("key_to_name_resolve_failed", key=model_key, error=str(exc))
    return None, None


def _mirror_ai_config(group: DocumentGroup) -> None:
    """Keep the legacy ``ai_config`` document fields in sync with the group.

    No code path reads model selection from ``ai_config`` any more: OCR,
    reasoning, embeddings, rerank, verification and the agent all resolve
    through task routing. The mirror stays because the store is still exposed
    over ``/api/ai/config`` for outside consumers, and a stale model name there
    is worse than none — but it is now write-only, and nothing behaves
    differently if it falls out of sync.

    ``model_ocr``/``model_vlm``/``model_reasoning`` store raw provider names;
    ``embedding_model``/``reranker_model`` store catalog keys.
    """
    try:
        from app.api.ai_settings import get_ai_config, save_ai_config
    except Exception:
        return
    cfg = get_ai_config()
    changed = False
    if group.vision_model:
        name, provider = _key_to_name_provider(group.vision_model)
        if name:
            cfg["model_ocr"], cfg["model_ocr_provider"] = name, provider or "ollama"
            cfg["model_vlm"], cfg["model_vlm_provider"] = name, provider or "ollama"
            changed = True
    if group.text_model:
        name, provider = _key_to_name_provider(group.text_model)
        if name:
            cfg["model_reasoning"], cfg["model_reasoning_provider"] = name, provider or "ollama"
            changed = True
    if group.embedding_model:
        cfg["embedding_model"] = group.embedding_model
        changed = True
    if group.rerank_model:
        cfg["reranker_model"] = group.rerank_model
        changed = True
    if group.ocr_fallback_model is not None:
        # Store the catalog key so the UI can round-trip it correctly.
        # Extraction resolves the key to raw name+provider via model_registry at use time.
        if group.ocr_fallback_model == "":
            cfg.pop("model_ocr_fallback", None)
        else:
            cfg["model_ocr_fallback"] = group.ocr_fallback_model  # catalog key
        changed = True
    if changed:
        save_ai_config(cfg)


def reconcile_ai_config() -> dict:
    """Пересчитать легаси-store ``ai_config`` из текущих назначений.

    ``_mirror_ai_config`` срабатывает только когда человек сохраняет группу в
    GUI. Поэтому значение, записанное туда однажды, живёт вечно: в
    ``model_reasoning`` месяцами лежала `gemma4:e4b`, хотя модель давно
    заменили, — а старые модули (OCR-извлечение, reasoning-провайдер, память)
    читают именно этот store, минуя каталог.

    Вызывается на старте: настройки моделей остаются единственным источником
    правды, а зеркало пересчитывается, а не запоминается.
    """
    before = {}
    try:
        from app.api.ai_settings import get_ai_config

        before = dict(get_ai_config())
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_config_reconcile_read_failed", error=str(exc))
        return {"changed": {}}

    _mirror_ai_config(get_document_group())

    # ``model_agent`` зеркалится отдельным обработчиком API и точно так же
    # застревало: в store лежал `qwen3.6:35b`, хотя агенту назначена другая
    # модель. Для agent_loop это лишь запасной путь, но запасной путь,
    # ведущий в несуществующую модель, хуже отсутствующего.
    try:
        from app.api.ai_settings import _sync_ai_model_agent

        agent_model = get_agent_group().agent_model
        if agent_model:
            _sync_ai_model_agent(agent_model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_config_reconcile_agent_failed", error=str(exc))

    try:
        from app.api.ai_settings import get_ai_config

        after = get_ai_config()
    except Exception:  # noqa: BLE001
        return {"changed": {}}

    changed = {
        k: [before.get(k), after.get(k)]
        for k in set(before) | set(after)
        if before.get(k) != after.get(k)
    }
    if changed:
        logger.info("ai_config_reconciled", changed=changed)
    return {"changed": changed}


def set_document_group(group: DocumentGroup) -> DocumentGroup:
    """Apply the document slots that were provided (None = leave unchanged)."""
    slot_tasks: list[tuple[str | None, list[AITask]]] = [
        (group.vision_model, VISION_DOC_TASKS),
        (group.text_model, TEXT_DOC_TASKS),
        (group.embedding_model, EMBEDDING_DOC_TASKS),
        (group.rerank_model, RERANK_DOC_TASKS),
    ]
    for model_key, tasks in slot_tasks:
        if not model_key:
            continue
        for task in tasks:
            _set_primary(task, model_key)
    _mirror_ai_config(group)
    logger.info(
        "document_group_set",
        vision=group.vision_model,
        text=group.text_model,
        embedding=group.embedding_model,
        rerank=group.rerank_model,
        ocr_fallback=group.ocr_fallback_model,
    )
    return get_document_group()


def _sync_agent_routing(model_name: str | None, provider: str | None) -> None:
    """Point the orchestrator/tool tasks at the agent's model.

    Required so ``model_lifecycle.pinned_ollama_models`` warms the model the
    agent actually uses. If the model is not in the catalog (custom name) we
    cannot resolve a key — leave routing untouched and warn.
    """
    if not model_name or not provider:
        return
    key = _catalog_key_for(model_name, provider)
    if not key:
        logger.warning(
            "agent_routing_sync_skipped_no_catalog_key",
            model=model_name,
            provider=provider,
        )
        return
    for task in AGENT_SYNC_TASKS:
        _set_primary(task, key)


def set_agent_group(group: AgentGroup) -> AgentGroup:
    """Apply the agent model to all roles + sync orchestrator/tool routing."""
    patch: dict = {}
    if group.agent_model:
        provider = group.agent_provider or "ollama"
        patch.update(
            provider=provider,
            model=group.agent_model,
            orchestrator_provider=provider,
            orchestrator_model=group.agent_model,
            worker_provider=provider,
            worker_model=group.agent_model,
            auditor_provider=provider,
            auditor_model=group.agent_model,
            fast_provider=provider,
            fast_model=group.agent_model,
        )
    if group.large_model:
        patch.update(
            builder_provider=group.large_provider or group.agent_provider or "ollama",
            builder_model=group.large_model,
        )
    if patch:
        update_builtin_agent_config(BuiltinAgentConfigUpdate(**patch))
    # Keep task_routing's orchestrator in sync so the pinned model is correct.
    if group.agent_model:
        _sync_agent_routing(group.agent_model, group.agent_provider or "ollama")
    logger.info(
        "agent_group_set",
        agent_model=group.agent_model,
        large_model=group.large_model,
    )
    return get_agent_group()
