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


def format_flow_summary_human(snapshot: dict) -> str:
    """User-facing prioritised summary of the document flow (markdown).

    Deterministic — the secretary front-agent answers flow-status questions
    from real numbers without an LLM call. Ordered by urgency: what blocks or
    burns first, then the rest.
    """
    overdue = int(snapshot.get("overdue_payments") or 0)
    pending = int(snapshot.get("pending_approvals") or 0)
    anomalies = int(snapshot.get("open_anomalies") or 0)
    needs_review = int(snapshot.get("documents_needs_review") or 0)
    quarantine = int(snapshot.get("quarantine_count") or 0)
    unread = int(snapshot.get("unread_emails") or 0)

    urgent: list[str] = []
    if overdue:
        urgent.append(f"- **Просроченные платежи: {overdue}** — требуют немедленного решения")
    if pending:
        urgent.append(f"- **Ожидают согласования: {pending}** — блокируют дальнейшую обработку")
    if anomalies:
        urgent.append(f"- **Открытые аномалии: {anomalies}** — нужна проверка руководителем")

    regular: list[str] = []
    if needs_review:
        regular.append(f"- Документы на проверке: {needs_review}")
    if quarantine:
        regular.append(f"- В карантине: {quarantine}")
    if unread:
        regular.append(f"- Непрочитанные письма: {unread}")

    if not urgent and not regular:
        return (
            "Всё спокойно: нет ожидающих согласований, просрочек, аномалий "
            "и документов на проверке."
        )

    parts: list[str] = ["**Состояние документооборота:**"]
    if urgent:
        parts.append("\n🔴 Требует внимания в первую очередь:\n" + "\n".join(urgent))
    if regular:
        parts.append("\n🟡 В работе:\n" + "\n".join(regular))
    return "\n".join(parts)


async def get_flow_snapshot(config, *, use_cache: bool = True) -> dict | None:
    """Return raw flow numbers as a dict, or None if unavailable.

    Keys: pending_approvals, documents_needs_review, open_anomalies,
    quarantine_count, unread_emails, overdue_payments (may be absent).
    Cached in Redis for ``_CACHE_TTL`` seconds; failures are non-fatal.
    """
    r = _redis() if use_cache else None
    if r is not None:
        try:
            cached = r.get(_CACHE_KEY)
            if cached:
                raw = cached if isinstance(cached, str) else cached.decode("utf-8")
                return json.loads(raw)
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
        return None

    if not today:
        return None

    snapshot = {
        "pending_approvals": today.get("pending_approvals", 0),
        "documents_needs_review": today.get("documents_needs_review", 0),
        "open_anomalies": today.get("open_anomalies", 0),
        "quarantine_count": today.get("quarantine_count", 0),
        "unread_emails": today.get("unread_emails", 0),
    }
    if overdue is not None:
        snapshot["overdue_payments"] = overdue
    if r is not None:
        try:
            r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(snapshot, ensure_ascii=False))
        except Exception:
            pass
    return snapshot


async def get_flow_context(config, *, use_cache: bool = True) -> str:
    """Return a compact ``<flow-context>`` block, or "" if unavailable."""
    snapshot = await get_flow_snapshot(config, use_cache=use_cache)
    if not snapshot:
        return ""
    overdue = snapshot.get("overdue_payments")
    return _format_snapshot(snapshot, int(overdue) if overdue is not None else None)
