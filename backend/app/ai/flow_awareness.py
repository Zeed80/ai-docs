"""Document-flow situational awareness for the secretary role.

Builds a compact ``<flow-context>`` snapshot of the document workflow (pending
approvals, documents needing review, open anomalies, quarantine, unread mail,
overdue payments) so the agent can answer "what needs my attention?" without
guessing. Reuses the existing ``/api/dashboard/today`` aggregate (one cheap
query) plus the overdue-payments count, and caches the result in Redis with a
short TTL so it does not hit the DB on every turn.

This is an orchestrator-side awareness layer, not a separate agent.
"""

from __future__ import annotations

import json

import httpx
import structlog

logger = structlog.get_logger()

_CACHE_KEY = "agent:flow_context"
_CACHE_TTL = 20  # seconds — snapshot freshness vs. DB load


def _redis():
    try:
        from app.utils.redis_client import get_sync_redis
        return get_sync_redis()
    except Exception:
        return None


def _format_snapshot(today: dict, overdue: int | None) -> str:
    pending = today.get("pending_approvals", 0)
    needs_review = today.get("documents_needs_review", 0)
    anomalies = today.get("open_anomalies", 0)
    quarantine = today.get("quarantine_count", 0)
    unread = today.get("unread_emails", 0)

    lines = [
        "<flow-context>",
        "Текущее состояние документооборота (для ситуационной осведомлённости):",
        f"- Ожидают согласования (approval): {pending}",
        f"- Документы на проверке (needs_review): {needs_review}",
        f"- Открытые аномалии: {anomalies}",
        f"- В карантине (нерешённые): {quarantine}",
        f"- Непрочитанные входящие письма: {unread}",
    ]
    if overdue is not None:
        lines.append(f"- Просроченные платежи: {overdue}")
    lines.append("</flow-context>")
    return "\n".join(lines)


async def get_flow_context(config, *, use_cache: bool = True) -> str:
    """Return a compact ``<flow-context>`` block, or "" if unavailable.

    Cached in Redis for ``_CACHE_TTL`` seconds. Any failure is non-fatal and
    yields an empty string (the agent then runs without the snapshot).
    """
    r = _redis() if use_cache else None
    if r is not None:
        try:
            cached = r.get(_CACHE_KEY)
            if cached:
                return cached if isinstance(cached, str) else cached.decode("utf-8")
        except Exception:
            pass

    base = config.backend_url.rstrip("/")
    try:
        from app.ai.agent_loop import _internal_headers
        headers = _internal_headers()
    except Exception:
        headers = {}

    today: dict = {}
    overdue: int | None = None
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.get(f"{base}/api/dashboard/today", headers=headers)
            if resp.status_code == 200:
                today = resp.json() or {}
            try:
                ov = await client.get(f"{base}/api/payment-schedules/overdue", headers=headers)
                if ov.status_code == 200:
                    data = ov.json()
                    if isinstance(data, dict):
                        overdue = int(data.get("total") or len(data.get("items") or []))
                    elif isinstance(data, list):
                        overdue = len(data)
            except Exception:
                overdue = None
    except Exception as exc:
        logger.debug("flow_context_fetch_failed", error=str(exc))
        return ""

    if not today:
        return ""

    snapshot = _format_snapshot(today, overdue)
    if r is not None:
        try:
            r.setex(_CACHE_KEY, _CACHE_TTL, snapshot)
        except Exception:
            pass
    return snapshot
