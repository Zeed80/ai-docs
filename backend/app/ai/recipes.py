"""Recipe skills — self-learning through declarative macros, not code generation.

A recipe is a recorded sequence of capability calls that solved a user task:
``[{capability, action, args_template}, ...]`` plus parameter slots extracted
from the user text (``{{user.supplier_name}}``). Recipes can only compose
existing capabilities, so a learned skill can never exceed what the capability
registry (and its approval gates) already allows.

Lifecycle:
- **draft** — recorded automatically from a successful turn (mechanical audit
  passed, no explicit semantic failure, ≥2 tool calls, no approval-gated
  actions). Drafts are used as planner hints only.
- **active** — after ``_ACTIVATE_AFTER`` successful replays or manual approval
  via the control-plane API. Active recipes with a high-similarity trigger
  match are replayed deterministically (0 planner LLM calls).
- **retired** — demoted on fail-rate, capability schema drift (hash of
  capabilities.yml changed in an incompatible way), or manually.

Retrieval is vector similarity over trigger examples (Qdrant collection scoped
``recipe_triggers``), not the old exact intent-hash that almost never matched.
"""

from __future__ import annotations

import hashlib
import re
import uuid as uuid_module
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.ai.degradation import log_degraded

logger = structlog.get_logger()

# Similarity thresholds (cosine score from Qdrant).
REPLAY_SCORE = 0.86   # active recipe + resolvable slots → deterministic replay
HINT_SCORE = 0.70     # any recipe → planner hint with recommended steps
DEDUPE_SCORE = 0.93   # candidate is "the same task" → add trigger example

_ACTIVATE_AFTER = 2       # successful replays before draft → active
_RETIRE_FAIL_RATE = 0.5   # retired when fail rate exceeds this (≥4 uses)
_RETIRE_MIN_USES = 4
_MAX_TRIGGER_EXAMPLES = 5
_MIN_STEPS = 2
_MAX_STEPS = 6

_VECTOR_SCOPE = "recipe_triggers"


# ── Capability schema hash ─────────────────────────────────────────────────────


def capabilities_schema_hash() -> str:
    """Hash of capabilities.yml — recipes recorded against another schema retire."""
    try:
        from app.ai.gateway_config import gateway_config
        return hashlib.md5(gateway_config.capabilities_path.read_bytes()).hexdigest()
    except Exception as exc:
        log_degraded("recipes.schema_hash", exc)
        return ""


def _gate_actions_map() -> dict[str, set[str]]:
    """capability name → set of approval-gated actions."""
    try:
        from app.ai.agent_loop import _load_capabilities
        _, skill_map = _load_capabilities()
        return {
            name: set(entry.get("gate_actions") or [])
            for name, entry in skill_map.items()
            if isinstance(entry, dict)
        }
    except Exception as exc:
        log_degraded("recipes.gate_map", exc)
        return {}


# ── Parameterization ───────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\b")
_QUOTED_RE = re.compile(r"[«\"']([^«»\"']{3,60})[»\"']")
_SLOT_RE = re.compile(r"\{\{user\.([a-z_0-9]+)\}\}")


def extract_entities(text: str) -> dict[str, str]:
    """Entities from user text that may become recipe parameter slots."""
    entities: dict[str, str] = {}
    try:
        from app.ai import route_table
        supplier = route_table.extract_supplier_name(text)
        if supplier:
            entities["supplier_name"] = supplier
    except Exception:
        pass
    dates = _DATE_RE.findall(text or "")
    for idx, date in enumerate(dates[:2]):
        entities[f"date_{idx + 1}"] = date
    quoted = _QUOTED_RE.findall(text or "")
    for idx, value in enumerate(quoted[:2]):
        slot = f"quoted_{idx + 1}"
        if value not in entities.values():
            entities[slot] = value.strip()
    return entities


def _slotify_value(value: Any, entities: dict[str, str]) -> Any:
    """Replace a literal arg value with a slot placeholder when it matches an entity."""
    if isinstance(value, str):
        for slot, entity in entities.items():
            if entity and entity.strip().lower() == value.strip().lower():
                return f"{{{{user.{slot}}}}}"
        return value
    if isinstance(value, dict):
        return {k: _slotify_value(v, entities) for k, v in value.items()}
    if isinstance(value, list):
        return [_slotify_value(v, entities) for v in value]
    return value


def parameterize_steps(
    steps: list[dict], user_text: str
) -> tuple[list[dict], dict[str, dict]]:
    """Replace literal args matching user-text entities with ``{{user.*}}`` slots."""
    entities = extract_entities(user_text)
    if not entities:
        return steps, {}
    templated = [
        {**step, "args_template": _slotify_value(step.get("args_template") or {}, entities)}
        for step in steps
    ]
    used_slots = {
        slot
        for step in templated
        for slot in _SLOT_RE.findall(str(step.get("args_template")))
    }
    param_slots = {
        slot: {"source": slot, "example": entities[slot]}
        for slot in used_slots
        if slot in entities
    }
    return templated, param_slots


def resolve_slots(recipe_slots: dict | None, text: str) -> dict[str, str] | None:
    """Slot values for this text, or None when any declared slot is unresolvable."""
    if not recipe_slots:
        return {}
    entities = extract_entities(text)
    resolved: dict[str, str] = {}
    for slot in recipe_slots:
        value = entities.get(slot)
        if not value:
            return None
        resolved[slot] = value
    return resolved


def render_args(args_template: Any, slots: dict[str, str]) -> Any:
    if isinstance(args_template, str):
        def _sub(match: re.Match) -> str:
            return slots.get(match.group(1), match.group(0))
        return _SLOT_RE.sub(_sub, args_template)
    if isinstance(args_template, dict):
        return {k: render_args(v, slots) for k, v in args_template.items()}
    if isinstance(args_template, list):
        return [render_args(v, slots) for v in args_template]
    return args_template


# ── Vector index over trigger examples ─────────────────────────────────────────


def _collection_name() -> str:
    from app.ai.embeddings import embedding_collection_name, get_active_embedding_profile
    profile = get_active_embedding_profile()
    return embedding_collection_name(
        scope=_VECTOR_SCOPE,
        model_key=profile.model_key,
        dimension=profile.dimension,
        distance_metric=profile.distance_metric,
    )


async def _index_trigger(recipe_id: str, example_idx: int, text: str) -> None:
    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import ensure_collection, upsert_memory_embedding

    profile = get_active_embedding_profile()
    vector = await embed_text(text, task_type="passage")
    collection = _collection_name()
    ensure_collection(collection, vector_size=profile.dimension,
                      distance_metric=profile.distance_metric)
    upsert_memory_embedding(
        point_id=f"recipe:{recipe_id}:{example_idx}",
        vector=vector,
        collection_name=collection,
        payload={"recipe_id": recipe_id, "content_type": "recipe_trigger", "text": text[:500]},
    )


async def _search_triggers(text: str, limit: int = 3) -> list[dict]:
    """[{recipe_id, score}] best matches, deduped by recipe (best score wins)."""
    from app.ai.embeddings import embed_text
    from app.vector.qdrant_store import search_similar

    vector = await embed_text(text, task_type="query")
    hits = search_similar(
        vector, limit=limit * 3, collection_name=_collection_name(), score_threshold=HINT_SCORE
    )
    best: dict[str, float] = {}
    for hit in hits:
        recipe_id = str((hit.get("payload") or {}).get("recipe_id") or "")
        if recipe_id:
            best[recipe_id] = max(best.get(recipe_id, 0.0), float(hit["score"]))
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"recipe_id": rid, "score": score} for rid, score in ranked]


# ── Recording ──────────────────────────────────────────────────────────────────


def _derive_name(intent: str, steps: list[dict]) -> str:
    caps = []
    for step in steps:
        cap = str(step.get("capability") or "")
        if cap and cap not in caps:
            caps.append(cap)
    base = intent if intent not in ("", "general") else "task"
    return f"{base}__{'_'.join(caps[:3]) or 'steps'}"[:200]


async def record_candidate(
    *,
    user_text: str,
    role: str,
    intent: str,
    steps: list[dict],
    session_id: str | None = None,
) -> bool:
    """Record a successful tool sequence as a draft recipe (or enrich a duplicate).

    ``steps``: [{"capability": str, "action": str, "args_template": dict}] in
    execution order. Returns True when something was recorded.
    """
    if not steps or len(steps) < _MIN_STEPS or len(steps) > _MAX_STEPS:
        return False
    gates = _gate_actions_map()
    for step in steps:
        cap = str(step.get("capability") or "")
        action = str(step.get("action") or "")
        if not cap:
            return False
        if action and action in gates.get(cap, set()):
            return False  # approval-gated actions never enter recipes

    templated_steps, param_slots = parameterize_steps(steps, user_text)

    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    factory = _get_session_factory()

    # Dedupe: a near-identical task → add the text as a new trigger example.
    try:
        matches = await _search_triggers(user_text, limit=1)
    except Exception as exc:
        log_degraded("recipes.search_on_record", exc)
        matches = []

    async with factory() as db:
        if matches and matches[0]["score"] >= DEDUPE_SCORE:
            existing = await db.get(RecipeSkill, uuid_module.UUID(matches[0]["recipe_id"]))
            if existing is not None:
                examples = list(existing.trigger_examples or [])
                if user_text not in examples and len(examples) < _MAX_TRIGGER_EXAMPLES:
                    examples.append(user_text)
                    existing.trigger_examples = examples
                    await db.commit()
                    try:
                        await _index_trigger(str(existing.id), len(examples) - 1, user_text)
                    except Exception as exc:
                        log_degraded("recipes.index_trigger", exc)
                    logger.info("recipe_trigger_added", recipe=str(existing.id))
                return True

        recipe = RecipeSkill(
            name=_derive_name(intent, templated_steps),
            description=f"Выучено из задачи: {user_text[:300]}",
            role=role,
            trigger_examples=[user_text],
            steps=templated_steps,
            param_slots=param_slots or None,
            source_session_id=(session_id or "")[:64] or None,
            capability_schema_hash=capabilities_schema_hash(),
            status="draft",
        )
        db.add(recipe)
        await db.commit()
        await db.refresh(recipe)
        recipe_id = str(recipe.id)

    try:
        await _index_trigger(recipe_id, 0, user_text)
    except Exception as exc:
        log_degraded("recipes.index_trigger", exc)
    logger.info("recipe_recorded", recipe=recipe_id, steps=len(templated_steps), role=role)
    return True


# ── Retrieval ──────────────────────────────────────────────────────────────────


async def find_recipe(text: str) -> tuple[Any, float] | None:
    """Best (RecipeSkill, score) for the text, or None. Retired recipes excluded."""
    try:
        matches = await _search_triggers(text, limit=3)
    except Exception as exc:
        log_degraded("recipes.search", exc)
        return None
    if not matches:
        return None

    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        for match in matches:
            try:
                recipe = await db.get(RecipeSkill, uuid_module.UUID(match["recipe_id"]))
            except Exception:
                continue
            if recipe is not None and recipe.status != "retired":
                return recipe, float(match["score"])
    return None


# ── Replay ─────────────────────────────────────────────────────────────────────


async def replay(
    recipe: Any,
    slots: dict[str, str],
    config: Any,
    *,
    on_event=None,
) -> bool:
    """Execute recipe steps through the capability dispatcher.

    Deterministic — each step is a POST to /api/agent/cap/{capability} with the
    rendered args. Stops on the first error. ``on_event`` (async, optional)
    receives tool_call/tool_result events for the turn trace.
    """
    schema_hash = capabilities_schema_hash()
    if recipe.capability_schema_hash and recipe.capability_schema_hash != schema_hash:
        await record_outcome(recipe.id, success=False, retire=True)
        logger.warning("recipe_schema_drift_retired", recipe=str(recipe.id))
        return False

    from app.ai.orchestrator import _agent_headers  # X-API-Key for internal calls

    base = config.backend_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            for step in recipe.steps or []:
                capability = str(step.get("capability") or "")
                args = render_args(step.get("args_template") or {}, slots)
                if not isinstance(args, dict):
                    args = {}
                action = str(step.get("action") or "")
                if action and "action" not in args:
                    args["action"] = action
                if on_event:
                    await on_event({"type": "tool_call", "tool": capability, "args": args})
                resp = await client.post(
                    f"{base}/api/agent/cap/{capability}", json=args, headers=_agent_headers()
                )
                if resp.status_code >= 400:
                    if on_event:
                        await on_event({
                            "type": "tool_result",
                            "tool": capability,
                            "result": {
                                "error": f"HTTP {resp.status_code}",
                                "detail": resp.text[:300],
                            },
                        })
                    await record_outcome(recipe.id, success=False)
                    return False
                result = resp.json() if resp.content else {}
                if isinstance(result, dict) and result.get("error"):
                    if on_event:
                        await on_event(
                            {"type": "tool_result", "tool": capability, "result": result}
                        )
                    await record_outcome(recipe.id, success=False)
                    return False
                if on_event:
                    await on_event({"type": "tool_result", "tool": capability, "result": result})
    except Exception as exc:
        log_degraded("recipes.replay", exc, recipe=str(recipe.id))
        await record_outcome(recipe.id, success=False)
        return False

    await record_outcome(recipe.id, success=True)
    return True


async def record_outcome(recipe_id, *, success: bool, retire: bool = False) -> None:
    """Update replay stats; handles draft→active promotion and fail-rate demotion."""
    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    try:
        factory = _get_session_factory()
        async with factory() as db:
            recipe = await db.get(RecipeSkill, recipe_id)
            if recipe is None:
                return
            if success:
                recipe.success_count += 1
            else:
                recipe.fail_count += 1
            recipe.last_used_at = datetime.now(UTC)
            if retire:
                recipe.status = "retired"
            else:
                total = recipe.success_count + recipe.fail_count
                if (
                    recipe.status == "draft"
                    and recipe.success_count >= _ACTIVATE_AFTER
                    and recipe.fail_count == 0
                ):
                    recipe.status = "active"
                    logger.info("recipe_activated", recipe=str(recipe.id))
                elif (
                    total >= _RETIRE_MIN_USES
                    and recipe.fail_count / total > _RETIRE_FAIL_RATE
                ):
                    recipe.status = "retired"
                    logger.info("recipe_retired_failrate", recipe=str(recipe.id))
            await db.commit()
    except Exception as exc:
        log_degraded("recipes.record_outcome", exc)
