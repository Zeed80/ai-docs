"""Source connectors — self-learning discovery strategies for web sources.

Ф5 (AGENT_AUTONOMY_ROADMAP.md): a direct structural sibling of
app.ai.recipes (RecipeSkill), scoped to "how to reach this domain/source
pattern" instead of "how to do this task". A connector records that a given
web_discover query/domain combination actually produced usable content —
learned from real exploratory WorkOrder cycles, not curated up front.

Lifecycle (simpler than recipes.py's four-component design — see the note
on confirm_draft_connector_from_worker below for why):
- **draft** — created automatically the first time a web_discover fetch for
  a domain actually succeeds (status 200, non-trivial content).
- **active** — after ``_CONNECTOR_ACTIVATE_AFTER`` successful uses with zero
  failures (own threshold, deliberately not reusing recipes.py's
  ``_ACTIVATE_AFTER`` — see AGENT_AUTONOMY_ROADMAP.md Ф5.B).
- **retired** — demoted on fail-rate, same shape as recipes.record_outcome.
  Never deleted — Ф5.C freshness fields (last_failure_at,
  consecutive_failures, revalidate_after) mark it for periodic
  re-attempt with a growing interval instead (actual periodic re-attempt
  execution is Ф6's idle-reflection job; this module only maintains the
  fields due_for_revalidation needs).

Deviation from the Ф5.B plan text worth recording (Правило 0): the plan
named a ``confirm_draft_connector_from_worker`` function mirroring recipes.
py's passive-confirmation component (a worker independently reproducing a
recipe's exact step sequence promotes it without a shadow run). Connectors
have no equivalent "ambiguous replay decision" to gate — a web_discover
fetch either produced usable content or it didn't, there is no separate
"the worker used it and it happened to match" question, so a second
promotion path adds a concept recipes.py needed for a different reason
without any counterpart problem here. record_connector_outcome (below)
covers connector promotion/demotion completely; no separate confirmation
function was added.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import structlog

from app.ai.degradation import log_degraded
from app.ai.recipes import capabilities_schema_hash

logger = structlog.get_logger()

_CONNECTOR_ACTIVATE_AFTER = 3       # successful uses before draft -> active
_CONNECTOR_RETIRE_FAIL_RATE = 0.5   # retired when fail rate exceeds this (>= min uses)
_CONNECTOR_RETIRE_MIN_USES = 4
_MAX_TRIGGER_EXAMPLES = 5
# A fetch worth learning from: real content, not an empty/error page. Same
# order of magnitude as verify_nonempty_result's own "has_result" bar
# elsewhere in this codebase — a light floor, not a content-quality judgment.
_MIN_USEFUL_TEXT_LENGTH = 200

# Vector-similarity hint threshold for the planner (find_connector_hints) —
# deliberately its own constant, not recipes.HINT_SCORE: connector matching
# is domain/pattern-level (coarser) than recipe-trigger matching (near-exact
# task phrasing), so reusing the same score would either miss obviously
# relevant connectors or force retuning the recipe threshold to compensate.
_HINT_SCORE = 0.65

# Ф5.C: growing re-attempt interval by consecutive_failures (1st failure ->
# try again tomorrow, keep failing -> back off to weekly, then monthly) —
# capped at the last entry, not unbounded backoff.
_REVALIDATION_INTERVALS = (timedelta(days=1), timedelta(days=7), timedelta(days=30))

_VECTOR_SCOPE = "connector_triggers"


def _domain_from_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


# ── Vector index over trigger examples (mirrors recipes.py) ────────────────


def _collection_name() -> str:
    from app.ai.embeddings import embedding_collection_name, get_active_embedding_profile

    profile = get_active_embedding_profile()
    return embedding_collection_name(
        scope=_VECTOR_SCOPE,
        model_key=profile.model_key,
        dimension=profile.dimension,
        distance_metric=profile.distance_metric,
    )


async def _index_trigger(connector_id: str, example_idx: int, text: str) -> None:
    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import ensure_collection, upsert_memory_embedding

    profile = get_active_embedding_profile()
    vector = await embed_text(text, task_type="passage")
    collection = _collection_name()
    ensure_collection(collection, vector_size=profile.dimension,
                      distance_metric=profile.distance_metric)
    upsert_memory_embedding(
        point_id=f"connector:{connector_id}:{example_idx}",
        vector=vector,
        collection_name=collection,
        payload={"connector_id": connector_id, "content_type": "connector_trigger", "text": text[:500]},
    )


async def _search_triggers(text: str, limit: int = 3) -> list[dict]:
    """[{connector_id, score}] best matches, deduped by connector (best score wins)."""
    from app.ai.embeddings import embed_text
    from app.vector.qdrant_store import search_similar

    vector = await embed_text(text, task_type="query")
    hits = search_similar(
        vector, limit=limit * 3, collection_name=_collection_name(), score_threshold=_HINT_SCORE
    )
    best: dict[str, float] = {}
    for hit in hits:
        connector_id = str((hit.get("payload") or {}).get("connector_id") or "")
        if connector_id:
            best[connector_id] = max(best.get(connector_id, 0.0), float(hit["score"]))
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"connector_id": cid, "score": score} for cid, score in ranked]


# ── Recording ────────────────────────────────────────────────────────────


async def record_connector_success(db: Any, *, url: str, queries: list[str]) -> str | None:
    """A web_discover fetch actually produced usable content for ``url``.

    Takes the caller's own AsyncSession (web_discover already has one open
    for the ComputerUseAction audit trail) rather than opening a second,
    independent connection — avoids any same-request transaction-visibility
    surprise. Creates a new draft connector for the URL's domain the first
    time, or adds a trigger example / bumps the outcome counters on an
    existing one. Returns the connector id, or None if the URL has no
    parseable domain or on any infra failure (never raises — a learning
    side-effect must not fail the web_discover call it's attached to).
    """
    domain = _domain_from_url(url)
    if not domain:
        return None
    try:
        from app.db.models import SourceConnector
        from sqlalchemy import select

        existing = (
            await db.execute(
                select(SourceConnector).where(
                    SourceConnector.domain_pattern == domain,
                    SourceConnector.status != "retired",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            connector = SourceConnector(
                domain_pattern=domain,
                strategy={"queries": list(queries), "sample_url": url},
                trigger_examples=list(dict.fromkeys(queries))[:_MAX_TRIGGER_EXAMPLES],
                schema_hash=capabilities_schema_hash(),
                status="draft",
                # Explicit, not relied on as ORM column defaults (those only
                # apply at flush/INSERT time) — _apply_connector_outcome
                # below increments these on the same in-memory object before
                # any flush has happened.
                success_count=0,
                fail_count=0,
                consecutive_failures=0,
            )
            db.add(connector)
            _apply_connector_outcome(connector, success=True)  # same session — see docstring above
            await db.commit()
            await db.refresh(connector)
            connector_id = str(connector.id)
            for idx, q in enumerate(connector.trigger_examples):
                try:
                    await _index_trigger(connector_id, idx, q)
                except Exception as exc:
                    log_degraded("connectors.index_trigger", exc)
            logger.info("connector_drafted", connector=connector_id, domain=domain)
        else:
            connector_id = str(existing.id)
            examples = list(existing.trigger_examples or [])
            new_examples = [q for q in queries if q not in examples]
            for q in new_examples:
                if len(examples) < _MAX_TRIGGER_EXAMPLES:
                    examples.append(q)
            if new_examples:
                existing.trigger_examples = examples
            strategy = dict(existing.strategy or {})
            strategy["sample_url"] = url
            strategy["queries"] = list(dict.fromkeys([*(strategy.get("queries") or []), *queries]))
            existing.strategy = strategy
            _apply_connector_outcome(existing, success=True)
            await db.commit()
            for q in new_examples:
                if q in examples:
                    try:
                        await _index_trigger(connector_id, examples.index(q), q)
                    except Exception as exc:
                        log_degraded("connectors.index_trigger", exc)
    except Exception as exc:
        log_degraded("connectors.record_success", exc, domain=domain)
        return None

    return connector_id


async def record_connector_failure(db: Any, *, url: str) -> None:
    """A web_discover fetch for ``url`` failed — only updates an EXISTING
    connector for that domain (draft or active); never creates one, matching
    "draft is created automatically after the first SUCCESSFUL cycle" — a
    domain with no prior success has nothing to demote. Takes the caller's
    own session for the lookup, same rationale as record_connector_success."""
    domain = _domain_from_url(url)
    if not domain:
        return
    try:
        from app.db.models import SourceConnector
        from sqlalchemy import select

        existing = (
            await db.execute(
                select(SourceConnector).where(
                    SourceConnector.domain_pattern == domain,
                    SourceConnector.status != "retired",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return
        _apply_connector_outcome(existing, success=False)
        await db.commit()
    except Exception as exc:
        log_degraded("connectors.record_failure", exc, domain=domain)


def _apply_connector_outcome(connector, *, success: bool) -> None:
    """Pure stat-mutation on an already-loaded, session-attached connector —
    no session of its own, no commit. Shared by record_connector_outcome
    (opens its own session — the standalone entry point) and
    record_connector_success/record_connector_failure (reuse the caller's
    session directly, see their own docstrings for why that split matters:
    a second, independently-opened session cannot see a row this same
    request only just created inside an uncommitted-until-teardown test
    transaction, and in production it is simply a wasted extra round trip).

    Mirrors recipes.record_outcome exactly for the success/fail-rate part;
    adds last_failure_at/consecutive_failures/revalidate_after on top for
    connectors specifically (recipes have no revalidation concept — a bad
    recipe is just retired).

    The "retired -> draft" revival branch below is unreachable from
    record_connector_success's own flow (its dedupe lookup excludes
    ``status == "retired"`` on purpose, so a retired domain gets a fresh
    draft instead of silently reviving the old one via ordinary discovery
    traffic) — it exists for Ф6's idle-reflection job, which is expected to
    deliberately re-attempt a specific retired connector_id and call
    record_connector_outcome directly on success.
    """
    now = datetime.now(UTC)
    if success:
        connector.success_count += 1
        connector.last_validated_at = now
        connector.consecutive_failures = 0
        connector.last_failure_at = None
        connector.revalidate_after = None
        if (
            connector.status == "draft"
            and connector.success_count >= _CONNECTOR_ACTIVATE_AFTER
            and connector.fail_count == 0
        ):
            connector.status = "active"
            logger.info("connector_activated", connector=str(connector.id))
        elif connector.status == "retired":
            connector.status = "draft"
            logger.info("connector_revived", connector=str(connector.id))
    else:
        connector.fail_count += 1
        connector.last_failure_at = now
        connector.consecutive_failures += 1
        interval = _REVALIDATION_INTERVALS[
            min(connector.consecutive_failures - 1, len(_REVALIDATION_INTERVALS) - 1)
        ]
        connector.revalidate_after = now + interval
        total = connector.success_count + connector.fail_count
        if total >= _CONNECTOR_RETIRE_MIN_USES and (
            connector.fail_count / total > _CONNECTOR_RETIRE_FAIL_RATE
        ):
            connector.status = "retired"
            logger.info("connector_retired_failrate", connector=str(connector.id))


async def record_connector_outcome(connector_id, *, success: bool) -> None:
    """Standalone entry point — opens its own session, loads the connector by
    id, applies _apply_connector_outcome, commits. For a caller with no
    session of its own (Ф6's idle-reflection revalidation job; direct API/
    script use). record_connector_success/record_connector_failure do NOT
    call this — see _apply_connector_outcome's docstring for why."""
    from app.db.models import SourceConnector
    from app.db.session import _get_session_factory

    try:
        factory = _get_session_factory()
        async with factory() as db:
            connector = await db.get(SourceConnector, connector_id)
            if connector is None:
                return
            _apply_connector_outcome(connector, success=success)
            await db.commit()
    except Exception as exc:
        log_degraded("connectors.record_outcome", exc)


# ── Retrieval ────────────────────────────────────────────────────────────


async def find_active_connector(domain_pattern: str):
    """Exact-match lookup — a known-working connector for exactly this
    domain, or None. Used before web_discover as a fast path; a connector
    overdue for revalidation (Ф5.C) is still returned here (it is still the
    best known strategy) — actual re-attempt scheduling is Ф6's concern, not
    a reason to withhold it from use now."""
    from app.db.models import SourceConnector
    from app.db.session import _get_session_factory
    from sqlalchemy import select

    factory = _get_session_factory()
    async with factory() as db:
        return (
            await db.execute(
                select(SourceConnector)
                .where(SourceConnector.domain_pattern == domain_pattern, SourceConnector.status == "active")
                .order_by(SourceConnector.last_validated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def find_connector_hints(text: str, limit: int = 3) -> list[dict]:
    """Similarity-based hints for the planner (generate_capability_plan):
    non-retired connectors whose trigger examples resemble ``text`` (the
    WorkOrder objective), each as {domain_pattern, strategy, status, score}.
    Best-effort — returns [] on any search/infra failure, never raises."""
    try:
        matches = await _search_triggers(text, limit=limit)
    except Exception as exc:
        log_degraded("connectors.search", exc)
        return []
    if not matches:
        return []

    from app.db.models import SourceConnector
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    hints: list[dict] = []
    async with factory() as db:
        for match in matches:
            try:
                connector = await db.get(SourceConnector, uuid_module.UUID(match["connector_id"]))
            except Exception:
                continue
            if connector is None or connector.status == "retired":
                continue
            hints.append({
                "domain_pattern": connector.domain_pattern,
                "strategy": connector.strategy,
                "status": connector.status,
                "score": match["score"],
            })
    return hints


def due_for_revalidation(connector) -> bool:
    """Ф5.C: whether a connector's growing-interval re-attempt window has
    elapsed. Only meaningful for a connector that has failed at least once
    (revalidate_after is unset otherwise) — actual re-attempt execution is
    Ф6's idle-reflection job; this is pure data-model logic."""
    if connector.revalidate_after is None:
        return False
    return datetime.now(UTC) >= connector.revalidate_after


async def reindex_all_triggers(db) -> dict[str, int]:
    """Re-embed every connector trigger into the ACTIVE profile's collection.

    Same reason as recipes.reindex_all_triggers: a changed embedding model
    leaves the learned connector library unreachable by similarity even though
    the rows are still in Postgres.
    """
    from sqlalchemy import select

    from app.db.models import SourceConnector

    rows = (await db.execute(select(SourceConnector))).scalars().all()
    indexed = failed = 0
    for connector in rows:
        for idx, example in enumerate(connector.trigger_examples or []):
            text = example if isinstance(example, str) else str(example.get("text") or "")
            if not text.strip():
                continue
            try:
                await _index_trigger(str(connector.id), idx, text)
                indexed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("connector_trigger_reindex_failed", connector=str(connector.id), error=str(exc)[:200])
    logger.info("connector_triggers_reindexed", connectors=len(rows), indexed=indexed, failed=failed)
    return {"connectors": len(rows), "triggers_indexed": indexed, "triggers_failed": failed}
