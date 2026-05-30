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
    return DocumentGroup(
        vision_model=_primary_for(AITask.INVOICE_OCR),
        text_model=_primary_for(AITask.ENGINEERING_REASONING),
        embedding_model=_primary_for(AITask.EMBEDDING),
        rerank_model=_primary_for(AITask.RERANKING),
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

def _set_primary(task: AITask, model_key: str) -> None:
    """Make ``model_key`` the primary for ``task``, preserving the fallback tail.

    Reuses :func:`task_routing.save_task_routing`, so confidentiality and catalog
    validation are enforced and the lifecycle cache is invalidated.
    """
    current = get_routing_for(task)
    tail = [m for m in current.models if m != model_key]
    # Confidential tasks reject cloud models in the chain; the YAML defaults may
    # still list cloud fallbacks, so drop non-local keys from the preserved tail.
    if task in CONFIDENTIAL_TASKS:
        tail = [m for m in tail if _is_local_key(m)]
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

    Several consumers (ollama_client reasoning, embeddings, memory rerank) still
    read model selection from ``ai_config`` rather than ``task_routing``. Mirror
    the simplified group there so changes made in the UI take effect for them.
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
    if changed:
        save_ai_config(cfg)


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
