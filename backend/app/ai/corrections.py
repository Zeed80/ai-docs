"""Phase 3 — learning from user corrections.

When the user corrects the agent («нет, группируй по дате», «среднюю цену, а не
сумму»), we parse the correction with the SAME deterministic NL→ops machinery
used for table edits and remember the resulting PatchOps. On a later request the
learned ops are replayed (0 LLM) so the same mistake is not repeated.

Keying is **vector-similarity** over the request that produced the wrong result
(Qdrant ``correction_triggers``), so a correction generalises to *similar*
phrasings — not just an identical string. A Redis exact-hash entry is kept as a
cheap shortcut for the identical-request case and an offline fallback. All
storage/retrieval is best-effort: any failure degrades to "no learning".
"""

from __future__ import annotations

import json

import structlog

from app.ai.orchestrator_memory import _hash_intent
from app.domain.table_spec import (
    SOURCES,
    ColumnSpec,
    PatchOp,
    TableSpec,
    parse_patch_command,
    reconcile_ops,
)

logger = structlog.get_logger()

_KEY_PREFIX = "agent:correction:"
_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days — learned preferences are durable
_VECTOR_SCOPE = "correction_triggers"
# Replaying a WRONG correction is costly, so require high request similarity.
# Measured on qwen3-embedding: same-intent paraphrases score ~0.82–0.91, an
# unrelated request ~0.66 — 0.80 sits in that gap (and corrections are also
# source-scoped, adding safety).
CORRECTION_REPLAY_SCORE = 0.80

_CORRECTION_MARKERS = (
    "не так", "не то", "не это", "неверно", "неправильно", "не правильно",
    "я просил", "я же просил", "просил же", "надо было", "должно быть",
    "нет, ", "нет надо", "нет, надо", "не надо", "а не ", "вместо",
    "поправь", "исправь", "ошиб", "имел в виду", "имелось в виду", "не верно",
)


def _redis():
    try:
        from app.utils.redis_client import get_sync_redis
        return get_sync_redis()
    except Exception:
        return None


def is_correction(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(t) and any(m in t for m in _CORRECTION_MARKERS)


def _min_spec(source: str) -> TableSpec | None:
    src = SOURCES.get(source)
    if src is None:
        return None
    cols = [ColumnSpec(field=f) for f in (src.default_columns or list(src.fields)[:4])]
    return TableSpec(source=source, columns=cols)


def correction_to_ops(source: str, correction_text: str) -> list[PatchOp]:
    """Parse a correction into deterministic PatchOps (grouping/sort/agg/filter/
    column edits). Empty list when nothing recognisable."""
    spec = _min_spec(source)
    if spec is None:
        return []
    ops: list[PatchOp] = []
    try:
        ops.extend(reconcile_ops(spec, correction_text)[0])
    except Exception:
        pass
    try:
        parsed = parse_patch_command(correction_text, spec)
        if parsed:
            ops.extend(parsed.ops)
    except Exception:
        pass
    seen: set[tuple] = set()
    unique: list[PatchOp] = []
    for op in ops:
        key = (op.op, op.field, op.agg)
        if key not in seen:
            seen.add(key)
            unique.append(op)
    return unique


def _ops_to_json(ops: list[PatchOp]) -> str:
    return json.dumps([o.model_dump(mode="json", exclude_none=True) for o in ops],
                      ensure_ascii=False)


def _ops_from_json(raw: str) -> list[PatchOp]:
    return [PatchOp.model_validate(d) for d in json.loads(raw)]


def _collection_name() -> str:
    from app.ai.embeddings import embedding_collection_name, get_active_embedding_profile
    p = get_active_embedding_profile()
    return embedding_collection_name(scope=_VECTOR_SCOPE, model_key=p.model_key,
                                     dimension=p.dimension, distance_metric=p.distance_metric)


async def _vector_upsert(point_id: str, source: str, ops_json: str, text: str) -> None:
    """Index the request that was corrected, keyed for similarity replay."""
    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import ensure_collection, upsert_memory_embedding

    p = get_active_embedding_profile()
    vector = await embed_text(text, task_type="passage")
    collection = _collection_name()
    ensure_collection(collection, vector_size=p.dimension, distance_metric=p.distance_metric)
    upsert_memory_embedding(
        point_id=point_id, vector=vector, collection_name=collection,
        payload={"content_type": "correction_trigger", "source": source,
                 "ops": ops_json, "text": text[:500]},
    )


async def _vector_search(request: str, source: str) -> list[PatchOp]:
    """Best correction learned for a request SIMILAR to this one, same source."""
    from app.ai.embeddings import embed_text
    from app.vector.qdrant_store import search_similar

    vector = await embed_text(request, task_type="query")
    hits = search_similar(vector, limit=5, collection_name=_collection_name(),
                          score_threshold=CORRECTION_REPLAY_SCORE)
    for hit in hits:
        payload = hit.get("payload") or {}
        if payload.get("source") == source and payload.get("ops"):
            return _ops_from_json(payload["ops"])
    return []


async def record_correction(prev_request: str, source: str, correction_text: str) -> list[PatchOp]:
    """Learn from a correction: remember the ops it implies against the request
    that produced the wrong result. Returns the parsed ops (possibly empty)."""
    ops = correction_to_ops(source, correction_text)
    if not ops:
        return []
    ops_json = _ops_to_json(ops)
    intent_hash = _hash_intent(prev_request, source)
    # Redis exact shortcut (cheap, offline-safe).
    r = _redis()
    if r is not None:
        try:
            r.setex(_KEY_PREFIX + intent_hash, _TTL_SECONDS, ops_json)
        except Exception as exc:
            logger.warning("correction_redis_write_failed", error=str(exc))
    # Vector trigger for similarity generalisation (best-effort).
    try:
        await _vector_upsert(f"correction:{intent_hash}", source, ops_json, prev_request)
    except Exception as exc:
        logger.warning("correction_vector_write_failed", error=str(exc))
    logger.info("correction_learned", source=source, ops=[o.op for o in ops],
                prev=prev_request[:80])
    return ops


async def learned_ops_for(request: str, source: str) -> list[PatchOp]:
    """Replay PatchOps learned from a past correction of an identical (Redis) or
    similar (vector) request, same source."""
    # 1) Exact match — cheap, no embedding.
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_KEY_PREFIX + _hash_intent(request, source))
            if raw:
                return _ops_from_json(raw)
        except Exception:
            pass
    # 2) Vector similarity — generalises to similar phrasings.
    try:
        return await _vector_search(request, source)
    except Exception:
        return []
