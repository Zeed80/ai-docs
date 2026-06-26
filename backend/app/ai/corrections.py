"""Phase 3 — learning from user corrections.

When the user corrects the agent («нет, группируй по дате», «среднюю цену, а не
сумму»), we parse the correction with the SAME deterministic NL→ops machinery
used for table edits and remember the resulting PatchOps keyed by the signature
of the request that produced the wrong result. On a later, similar request the
learned ops are replayed (0 LLM) so the same mistake is not repeated.

Redis-backed, best-effort: any failure degrades to "no learning", never breaks a
turn. Keying reuses ``orchestrator_memory._hash_intent`` (normalized text+source).
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

# Markers that a message is correcting the previous result rather than asking
# something new. Matched on normalized (lowercased) text.
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
    if not t:
        return False
    return any(m in t for m in _CORRECTION_MARKERS)


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
    # Dedupe by (op, field, agg) preserving order.
    seen: set[tuple] = set()
    unique: list[PatchOp] = []
    for op in ops:
        key = (op.op, op.field, op.agg)
        if key not in seen:
            seen.add(key)
            unique.append(op)
    return unique


def record_correction(prev_request: str, source: str, correction_text: str) -> list[PatchOp]:
    """Learn from a correction: store the ops it implies, keyed by the previous
    request. Returns the parsed ops (possibly empty)."""
    ops = correction_to_ops(source, correction_text)
    if not ops:
        return []
    r = _redis()
    if r is None:
        return ops
    try:
        key = _KEY_PREFIX + _hash_intent(prev_request, source)
        payload = json.dumps([op.model_dump(mode="json", exclude_none=True) for op in ops],
                             ensure_ascii=False)
        r.setex(key, _TTL_SECONDS, payload)
        logger.info("correction_learned", source=source,
                    ops=[o.op for o in ops], prev=prev_request[:80])
    except Exception as exc:
        logger.warning("correction_record_failed", error=str(exc))
    return ops


def learned_ops_for(request: str, source: str) -> list[PatchOp]:
    """Replay: PatchOps learned from a past correction of a matching request."""
    r = _redis()
    if r is None:
        return []
    try:
        raw = r.get(_KEY_PREFIX + _hash_intent(request, source))
        if not raw:
            return []
        return [PatchOp.model_validate(d) for d in json.loads(raw)]
    except Exception:
        return []
