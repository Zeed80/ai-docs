"""Б12: turn repeated WorkStepAttempt failures of the same shape into a draft
CapabilityProposal — evidence for a human (or the builder-model flow a human
approves) to decide whether a capability is genuinely missing or broken.

Never auto-promotes anything: a detected gap becomes a CapabilityProposal row
with status "draft", the exact same starting state as one a human or the chat
agent creates by hand (see agent_control_plane.py's _create_capability_proposal
— this module deliberately does NOT call that API-layer function, to keep the
domain layer independent of it; both paths converge on the same
CapabilityProposal table, reviewed the same way regardless of origin).

Runs as periodic housekeeping (a Celery beat task calls create_gap_proposals),
never synchronously on each failed attempt — a burst of the same failure
should produce one proposal, not one per attempt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CapabilityProposal, WorkStep, WorkStepAttempt
from app.domain.work_orders import utcnow

DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_THRESHOLD = 5


def _error_signature(error: dict[str, Any] | None) -> str:
    if not isinstance(error, dict):
        return "unknown"
    return str(error.get("code") or error.get("type") or "unknown")


async def detect_capability_gaps(
    db: AsyncSession,
    *,
    window: timedelta = DEFAULT_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """One aggregate group per (capability, action, error signature) that
    failed >= threshold times inside the window. Evidence only — pairs with
    create_gap_proposals, which turns qualifying groups into proposals."""
    since = utcnow() - window
    rows = list(
        (
            await db.execute(
                select(WorkStepAttempt, WorkStep)
                .join(WorkStep, WorkStep.id == WorkStepAttempt.step_id)
                .where(
                    WorkStepAttempt.status == "failed",
                    WorkStepAttempt.finished_at.isnot(None),
                    WorkStepAttempt.finished_at >= since,
                )
            )
        ).all()
    )
    groups: dict[tuple[str, str, str], list[tuple[WorkStepAttempt, WorkStep]]] = {}
    for attempt, step in rows:
        key = (step.capability or step.kind, step.action or "-", _error_signature(attempt.error))
        groups.setdefault(key, []).append((attempt, step))

    return [
        {
            "capability": capability,
            "action": action,
            "error_signature": error_signature,
            "count": len(items),
            "attempt_ids": [str(attempt.id) for attempt, _ in items],
            "sample_error": items[0][0].error,
        }
        for (capability, action, error_signature), items in groups.items()
        if len(items) >= threshold
    ]


def _gap_signature(gap: dict[str, Any]) -> str:
    return f"{gap['capability']}.{gap['action']}:{gap['error_signature']}"


async def create_gap_proposals(
    db: AsyncSession,
    *,
    window: timedelta = DEFAULT_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
) -> int:
    """Detect gaps and create one draft CapabilityProposal per new one.

    A gap already covered by an undecided proposal (status == "draft", same
    gap signature) is skipped — the periodic job runs repeatedly and must
    not spam a fresh proposal every tick for a failure pattern already
    awaiting a human decision.
    """
    gaps = await detect_capability_gaps(db, window=window, threshold=threshold)
    if not gaps:
        return 0

    # Filtered in Python, not a JSON-path SQL predicate on metadata — same
    # Postgres/SQLite portability reasoning as enforce_budgets (A4/Б15).
    open_metadata = list(
        (
            await db.execute(
                select(CapabilityProposal.metadata_).where(CapabilityProposal.status == "draft")
            )
        ).scalars()
    )
    open_signature_values = {m.get("gap_signature") for m in open_metadata if isinstance(m, dict)}

    created = 0
    for gap in gaps:
        signature = _gap_signature(gap)
        if signature in open_signature_values:
            continue
        db.add(
            CapabilityProposal(
                title=f"Повторяющийся сбой: {gap['capability']}.{gap['action']}",
                missing_capability=gap["capability"],
                reason=(
                    f"{gap['count']} провалов за последние {window.days} дн. "
                    f"с сигнатурой ошибки {gap['error_signature']!r}. "
                    "Требуется решение: капабилити отсутствует, сломано, или "
                    "это ожидаемые отказы (например, некорректный ввод пользователя)."
                ),
                suggested_artifact="tool",
                status="draft",
                risk_level="medium",
                draft={
                    "status": "not_generated",
                    "note": "Авто-обнаружено по повторяющимся провалам — код не сгенерирован, нужен builder-проход после решения человека.",
                },
                requested_by="gap-detector",
                metadata_={
                    "gap_signature": signature,
                    "auto_detected": True,
                    "attempt_ids": gap["attempt_ids"],
                    "sample_error": gap["sample_error"],
                },
            )
        )
        created += 1
    if created:
        await db.flush()
    return created


async def run_gap_detection(
    *,
    session_factory: Any | None = None,
    window: timedelta = DEFAULT_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
) -> int:
    """Session-owning entry point for the Celery beat task (work.detect_gaps)
    — opens its own transaction and commits, matching every other periodic
    housekeeping job in this codebase (expire_stale_work_memory, etc.)."""
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        created = await create_gap_proposals(db, window=window, threshold=threshold)
        await db.commit()
    return created
