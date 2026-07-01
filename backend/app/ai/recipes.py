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
# Длина сама по себе не вредит выигрышу от replay — вредит НЕвоспроизводимость
# (см. is_reproducible). Лимит держит цепочку обозримой, но поднят: «плоские»
# многошаговые ходы (все параметры из запроса) теперь тоже учатся.
_MAX_STEPS = 10

# Аргументы с этими ключами, заполненные литералом НЕ из запроса, — почти всегда
# runtime-значения из вывода предыдущего шага (id, полученный из list/search).
# Такой ход невоспроизводим: replay подставляет только слоты из текста запроса.
_RUNTIME_ID_KEYS = frozenset({
    "id", "invoice_id", "document_id", "supplier_id", "buyer_id", "item_id",
    "receipt_id", "node_id", "chunk_id", "message_id", "case_id", "payment_id",
    "anomaly_id", "draft_id", "thread_id", "order_id", "line_id", "contract_id",
    "request_id", "proposal_id", "recipe_id", "event_id", "rule_id",
})
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Component 1 — passive validation: worker independently reproduced the recipe's
# exact steps this many times → draft becomes active (no shadow double-run).
_CONFIRM_ACTIVATE_AFTER = 2
# Component 2 — replay gate: required score margin between the top recipe and the
# runner-up. Two near-equally-similar recipes → ambiguous → defer to the worker.
_REPLAY_MARGIN = 0.04
# Component 4 — explainable replay: below this many human-confirmed replays the
# agent ASKS before replaying; at/above it, replays silently.
_TRUST_AFTER_CONFIRMED = 2

# Intent-changing modifiers (component 2): if the current request contains one of
# these and the recipe's learned trigger did NOT, the task is likely different
# ("счета X" vs "счета КРОМЕ X") → do not replay, let the worker reason.
_INTENT_MODIFIERS = (
    "не ", "кроме", "без ", "только", "лишь", "исключени", "сравни", "против",
    " vs ", "динамик", "измени", "почему", "по сравнению",
)

_VECTOR_SCOPE = "recipe_triggers"


# ── Capability schema hash ─────────────────────────────────────────────────────


def capabilities_schema_hash() -> str:
    """Hash of capabilities.yml — recipes recorded against another schema retire."""
    try:
        from app.ai.capability_manifest import capability_schema_hash

        return capability_schema_hash()
    except Exception as exc:
        log_degraded("recipes.schema_hash", exc)
        return ""


def _gate_actions_map() -> dict[str, set[str]]:
    """capability name → set of approval-gated actions."""
    try:
        from app.ai.capability_manifest import load_capability_manifest

        return load_capability_manifest().gate_actions
    except Exception as exc:
        log_degraded("recipes.gate_map", exc)
        return {}


def _non_recipeable_actions_map() -> dict[str, set[str]]:
    """capability name → set of actions excluded from self-learning recipes.

    A distinct axis from gate_actions: not about needing human approval, but
    about reproducibility. Replaying a diffusion-generation step (fresh random
    seed each run) as if it deterministically reproduced the original result
    would be a silent correctness bug, not a safety one — this is why it's
    checked separately rather than folded into gate_actions.
    """
    try:
        from app.ai.capability_manifest import load_capability_manifest

        return load_capability_manifest().non_recipeable_actions
    except Exception as exc:
        log_degraded("recipes.non_recipeable_map", exc)
        return {}


# ── Parameterization ───────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\b")
_QUOTED_RE = re.compile(r"[«\"']([^«»\"']{3,60})[»\"']")
_SLOT_RE = re.compile(r"\{\{user\.([a-z_0-9]+)\}\}")
# Step reference slot: {{step.<index>.<dot.path>}} — at replay the value is read
# from the result of an earlier step (data-flow chains: id from list → details).
_STEP_REF_RE = re.compile(r"\{\{step\.(\d+)\.([a-zA-Z0-9_.]+)\}\}")
# Minimum length of a literal worth tracing back to a previous step's output —
# avoids matching trivial values ("1", "ok") that coincidentally appear.
_STEP_REF_MIN_LEN = 3


def _resolve_path(obj: Any, path: str) -> Any:
    """Read a value from a nested result by dot-path ('items.0.id')."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _find_value_path(obj: Any, target: str, _prefix: str = "") -> str | None:
    """Find the dot-path to a scalar equal to ``target`` inside a result object.

    Returns the shortest path found via depth-first scan, or None. Used at record
    time to express a step arg as a reference into an earlier step's output.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            sub = f"{_prefix}{key}"
            if isinstance(value, (str, int, float)) and str(value) == target:
                return sub
            found = _find_value_path(value, target, f"{sub}.")
            if found:
                return found
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            sub = f"{_prefix}{idx}"
            if isinstance(value, (str, int, float)) and str(value) == target:
                return sub
            found = _find_value_path(value, target, f"{sub}.")
            if found:
                return found
    return None


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


def _refify_value(value: Any, prior_results: list[Any]) -> Any:
    """Replace a literal that equals a value from an EARLIER step's result with a
    ``{{step.<idx>.<path>}}`` reference, enabling data-flow replay.

    Scanned earliest-first so a chain of N steps references the closest producer.
    Only non-trivial scalars are traced (``_STEP_REF_MIN_LEN``) to avoid matching
    coincidental small values.
    """
    if isinstance(value, dict):
        return {k: _refify_value(v, prior_results) for k, v in value.items()}
    if isinstance(value, list):
        return [_refify_value(v, prior_results) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if len(s) < _STEP_REF_MIN_LEN or "{{" in s:
            return value
        for idx, result in enumerate(prior_results):
            path = _find_value_path(result, s)
            if path:
                return f"{{{{step.{idx}.{path}}}}}"
    return value


def parameterize_steps(
    steps: list[dict],
    user_text: str,
    step_results: list[Any] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Parameterize a recorded chain for reproducible replay.

    1. Literals matching user-text entities → ``{{user.*}}`` slots.
    2. Literals equal to an EARLIER step's output → ``{{step.N.path}}`` refs
       (data-flow), when ``step_results`` (the per-step results, same order as
       ``steps``) is provided.
    """
    entities = extract_entities(user_text)
    results = step_results or []
    templated: list[dict] = []
    for i, step in enumerate(steps):
        args = step.get("args_template") or {}
        if entities:
            args = _slotify_value(args, entities)
        # Reference only outputs of steps BEFORE this one.
        if i > 0 and results:
            args = _refify_value(args, results[:i])
        templated.append({**step, "args_template": args})

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


def _arg_has_orphan_runtime_value(value: Any, text_low: str, key: str | None = None) -> bool:
    """True when an arg looks like a runtime value (output of a previous step)
    rather than something derivable from the user request.

    Such values make a recipe non-reproducible: replay can only substitute slots
    extracted from the request text, so a literal id captured from an earlier
    step would be replayed stale. Slot placeholders ({{user.*}}) and values that
    appear in the request are fine.
    """
    if isinstance(value, dict):
        return any(_arg_has_orphan_runtime_value(v, text_low, k) for k, v in value.items())
    if isinstance(value, list):
        return any(_arg_has_orphan_runtime_value(v, text_low) for v in value)
    if isinstance(value, str):
        s = value.strip()
        if not s or "{{user." in s or "{{step." in s:
            return False  # request slot or step-reference → reproducible
        if _UUID_RE.search(s):
            return True  # a UUID is never something the user typed
        if key and key.lower() in _RUNTIME_ID_KEYS and s.lower() not in text_low:
            return True  # an *_id literal not present in the request → runtime
    return False


def is_reproducible(templated_steps: list[dict], user_text: str) -> bool:
    """A recipe is reproducible only if every step's args are derivable from the
    request (slots / request-literals), with no orphan runtime values.

    This replaces a hard step-count cap as the real safety criterion: a long but
    "flat" chain (all params from the request) is fine; a short chain that feeds
    one step's output into the next is not.
    """
    text_low = (user_text or "").lower()
    for step in templated_steps:
        if _arg_has_orphan_runtime_value(step.get("args_template") or {}, text_low):
            return False
    return True


def resolve_slots(
    recipe_slots: dict | None,
    text: str,
    extra_entities: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Slot values for this text, or None when any declared slot is unresolvable.

    ``extra_entities`` (typed entities from the TurnDecision) take precedence over
    the regex extractor, so slot resolution follows the same understanding the
    router used to classify the turn.
    """
    if not recipe_slots:
        return {}
    entities = extract_entities(text)
    if extra_entities:
        entities = {**entities, **{k: v for k, v in extra_entities.items() if v}}
    resolved: dict[str, str] = {}
    for slot in recipe_slots:
        value = entities.get(slot)
        if not value:
            return None
        resolved[slot] = value
    return resolved


def _step_signature(steps: list[dict] | None) -> list[tuple[str, str]]:
    """Ordered (capability, action) pairs — identity of a step sequence."""
    out: list[tuple[str, str]] = []
    for step in steps or []:
        out.append((str(step.get("capability") or ""), str(step.get("action") or "")))
    return out


def steps_match(recipe_steps: list[dict] | None, worker_steps: list[dict] | None) -> bool:
    """True when the worker reproduced the recipe's exact (capability, action) order."""
    a = _step_signature(recipe_steps)
    b = _step_signature(worker_steps)
    return bool(a) and a == b


def _has_new_intent_modifier(content: str, trigger_examples: list[str] | None) -> bool:
    """True when the request carries an intent-changing modifier the recipe's
    learned triggers lacked — a strong signal the task differs ("кроме", "не",
    "сравни"…). Component 2 precision guard."""
    text = (content or "").lower()
    triggers = " ".join(trigger_examples or []).lower()
    for mod in _INTENT_MODIFIERS:
        if mod in text and mod not in triggers:
            return True
    return False


def replay_gate_ok(
    recipe: Any,
    score: float,
    margin: float,
    content: str,
) -> tuple[bool, str]:
    """Component 2 — decide whether an active recipe is safe to replay.

    Returns (ok, reason). Blocks on: ambiguity (small score margin to the
    runner-up) and intent drift (a modifier like "кроме"/"сравни" absent from
    the learned trigger). resolve_slots already guards entity resolvability.
    """
    if margin < _REPLAY_MARGIN:
        return False, f"ambiguous_match(margin={margin:.3f}<{_REPLAY_MARGIN})"
    if _has_new_intent_modifier(content, getattr(recipe, "trigger_examples", None)):
        return False, "intent_modifier_drift"
    return True, "ok"


async def confirm_draft_from_worker(user_text: str, worker_steps: list[dict]) -> None:
    """Component 1 — passive activation.

    After a worker turn, if a DRAFT recipe matches this trigger AND the worker
    reproduced its exact step sequence, count one confirmation. Enough
    independent confirmations promote the draft to active — without ever
    shadow-running it. Cheap: reuses work the worker already did.
    """
    try:
        matches = await _search_triggers(user_text, limit=1)
    except Exception as exc:
        log_degraded("recipes.confirm_search", exc)
        return
    if not matches or matches[0]["score"] < REPLAY_SCORE:
        return

    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        try:
            recipe = await db.get(RecipeSkill, uuid_module.UUID(matches[0]["recipe_id"]))
        except Exception:
            return
        if recipe is None or recipe.status != "draft":
            return
        if not steps_match(recipe.steps, worker_steps):
            return
        recipe.worker_confirmations = (recipe.worker_confirmations or 0) + 1
        if recipe.worker_confirmations >= _CONFIRM_ACTIVATE_AFTER:
            recipe.status = "active"
            logger.info(
                "recipe_activated_passive",
                recipe=str(recipe.id),
                confirmations=recipe.worker_confirmations,
            )
        else:
            logger.info(
                "recipe_confirmation_added",
                recipe=str(recipe.id),
                confirmations=recipe.worker_confirmations,
            )
        await db.commit()


async def record_confirmed_replay(recipe_id) -> None:
    """Component 4 — a human approved an explainable replay; build trust."""
    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    try:
        factory = _get_session_factory()
        async with factory() as db:
            recipe = await db.get(RecipeSkill, recipe_id)
            if recipe is None:
                return
            recipe.confirmed_replays = (recipe.confirmed_replays or 0) + 1
            await db.commit()
    except Exception as exc:
        log_degraded("recipes.record_confirmed_replay", exc)


def render_args(
    args_template: Any,
    slots: dict[str, str],
    step_results: list[Any] | None = None,
) -> Any:
    """Render a step's args: substitute {{user.*}} slots and, when step_results
    is given, {{step.N.path}} references from earlier steps' outputs.

    A step reference can resolve to a non-string (id as int, nested object): if
    the placeholder is the WHOLE value it's replaced in-place preserving type;
    inside a larger string it's stringified.
    """
    results = step_results or []

    if isinstance(args_template, str):
        # Whole-value step reference → preserve the resolved type.
        whole = _STEP_REF_RE.fullmatch(args_template.strip())
        if whole:
            idx, path = int(whole.group(1)), whole.group(2)
            if idx < len(results):
                resolved = _resolve_path(results[idx], path)
                if resolved is not None:
                    return resolved
            return args_template

        def _sub_user(match: re.Match) -> str:
            return slots.get(match.group(1), match.group(0))

        def _sub_step(match: re.Match) -> str:
            idx, path = int(match.group(1)), match.group(2)
            if idx < len(results):
                resolved = _resolve_path(results[idx], path)
                if resolved is not None:
                    return str(resolved)
            return match.group(0)

        rendered = _SLOT_RE.sub(_sub_user, args_template)
        rendered = _STEP_REF_RE.sub(_sub_step, rendered)
        return rendered
    if isinstance(args_template, dict):
        return {k: render_args(v, slots, results) for k, v in args_template.items()}
    if isinstance(args_template, list):
        return [render_args(v, slots, results) for v in args_template]
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
    step_results: list[Any] | None = None,
    session_id: str | None = None,
    output_channel: str | None = None,
) -> bool:
    """Record a successful tool sequence as a draft recipe (or enrich a duplicate).

    ``steps``: [{"capability": str, "action": str, "args_template": dict}] in
    execution order. ``step_results``: each step's result (same order) — lets
    data-flow args become {{step.N.path}} references. Returns True when recorded.
    """
    if not steps or len(steps) < _MIN_STEPS or len(steps) > _MAX_STEPS:
        return False
    gates = _gate_actions_map()
    non_recipeable = _non_recipeable_actions_map()
    for step in steps:
        cap = str(step.get("capability") or "")
        action = str(step.get("action") or "")
        if not cap:
            return False
        if action and action in gates.get(cap, set()):
            return False  # approval-gated actions never enter recipes
        if action and action in non_recipeable.get(cap, set()):
            return False  # e.g. non-deterministic diffusion generation

    templated_steps, param_slots = parameterize_steps(steps, user_text, step_results)

    # Reproducibility gate: skip chains whose steps depend on runtime output that
    # could NOT be turned into a {{step.N.path}} reference (orphan ids). Data-flow
    # captured as step references passes; truly unresolvable runtime values don't.
    # Length is allowed up to _MAX_STEPS; this is the real safety criterion.
    if not is_reproducible(templated_steps, user_text):
        logger.info("recipe_skipped_nonreproducible", steps=len(templated_steps))
        return False

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
            intent=intent or None,
            output_channel=output_channel,
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


async def find_recipe(text: str) -> tuple[Any, float, float] | None:
    """Best (RecipeSkill, score, margin) for the text, or None.

    ``margin`` is the score gap to the next distinct recipe candidate (0.0 when
    there is no runner-up) — component 2 uses it to refuse ambiguous matches.
    Retired recipes are excluded.
    """
    try:
        matches = await _search_triggers(text, limit=5)
    except Exception as exc:
        log_degraded("recipes.search", exc)
        return None
    if not matches:
        return None

    from app.db.models import RecipeSkill
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        chosen: Any = None
        chosen_score = 0.0
        runner_up_score: float | None = None
        for match in matches:
            try:
                recipe = await db.get(RecipeSkill, uuid_module.UUID(match["recipe_id"]))
            except Exception:
                continue
            if recipe is None or recipe.status == "retired":
                continue
            if chosen is None:
                chosen, chosen_score = recipe, float(match["score"])
            elif recipe.id != chosen.id:
                runner_up_score = float(match["score"])
                break
        if chosen is None:
            return None
        margin = chosen_score - runner_up_score if runner_up_score is not None else chosen_score
        return chosen, chosen_score, margin


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
    # Accumulated per-step results so later steps can reference earlier output
    # via {{step.N.path}} (data-flow replay).
    step_results: list[Any] = []
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            for step in recipe.steps or []:
                capability = str(step.get("capability") or "")
                args = render_args(step.get("args_template") or {}, slots, step_results)
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
                step_results.append(result)
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
