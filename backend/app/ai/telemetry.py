"""Lightweight AI usage telemetry (Redis-backed, migration-free).

Records one entry per :meth:`AIRouter.run` attempt — calls, errors, latency and
token counts aggregated per ``(task, model)`` plus a capped ring buffer of
recent calls. Powers the usage widget in Settings → Модели → Обзор and helps
justify routing/benchmark decisions.

Storage:
  - ``ai_telemetry:agg``     Redis hash, atomic ``HINCRBY`` counters per metric.
  - ``ai_telemetry:recent``  Redis list (LPUSH + LTRIM), last 200 calls.

Aggregates are approximate-but-cheap; for full historical analysis a dedicated
table would be added later. Failures here never affect the AI call itself.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

logger = structlog.get_logger()

_AGG_KEY = "ai_telemetry:agg"
_RECENT_KEY = "ai_telemetry:recent"
_RECENT_MAX = 200


def _redis():
    from app.utils.redis_client import get_sync_redis

    return get_sync_redis()


def record_call(
    *,
    task: str,
    model: str,
    provider: str,
    latency_ms: int,
    ok: bool,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: str | None = None,
) -> None:
    """Record one AI call attempt. Never raises."""
    field = f"{task}|{model}"
    try:
        r = _redis()
        pipe = r.pipeline()
        pipe.hincrby(_AGG_KEY, f"{field}:calls", 1)
        pipe.hincrby(_AGG_KEY, f"{field}:lat_ms", max(0, int(latency_ms)))
        if not ok:
            pipe.hincrby(_AGG_KEY, f"{field}:errors", 1)
        if input_tokens:
            pipe.hincrby(_AGG_KEY, f"{field}:tin", int(input_tokens))
        if output_tokens:
            pipe.hincrby(_AGG_KEY, f"{field}:tout", int(output_tokens))
        entry = {
            "ts": int(time.time()),
            "task": task,
            "model": model,
            "provider": provider,
            "latency_ms": int(latency_ms),
            "ok": ok,
            "error": (error or "")[:200] if error else None,
        }
        pipe.lpush(_RECENT_KEY, json.dumps(entry, ensure_ascii=False))
        pipe.ltrim(_RECENT_KEY, 0, _RECENT_MAX - 1)
        pipe.execute()
    except Exception as exc:
        logger.debug("ai_telemetry_record_failed", error=str(exc))


def get_summary() -> dict[str, Any]:
    """Return per-(task, model) aggregates + recent calls for the UI."""
    try:
        r = _redis()
        agg = r.hgetall(_AGG_KEY) or {}
        raw_recent = r.lrange(_RECENT_KEY, 0, _RECENT_MAX - 1) or []
    except Exception:
        return {"by_model": [], "recent": [], "totals": {"calls": 0, "errors": 0}}

    def _dec(v: Any) -> str:
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

    grouped: dict[str, dict[str, int]] = {}
    for k, v in agg.items():
        key = _dec(k)
        field, _, metric = key.rpartition(":")
        grouped.setdefault(field, {})[metric] = int(_dec(v))

    by_model = []
    total_calls = total_errors = 0
    for field, m in sorted(grouped.items(), key=lambda kv: -kv[1].get("calls", 0)):
        task, _, model = field.partition("|")
        calls = m.get("calls", 0)
        errors = m.get("errors", 0)
        total_calls += calls
        total_errors += errors
        by_model.append({
            "task": task,
            "model": model,
            "calls": calls,
            "errors": errors,
            "avg_latency_ms": round(m.get("lat_ms", 0) / calls) if calls else 0,
            "tokens_in": m.get("tin", 0),
            "tokens_out": m.get("tout", 0),
        })

    recent = []
    for item in raw_recent:
        try:
            recent.append(json.loads(_dec(item)))
        except Exception:
            continue

    return {
        "by_model": by_model,
        "recent": recent,
        "totals": {"calls": total_calls, "errors": total_errors},
    }


def reset() -> None:
    try:
        r = _redis()
        r.delete(_AGG_KEY, _RECENT_KEY)
    except Exception as exc:
        logger.warning("ai_telemetry_reset_failed", error=str(exc))
