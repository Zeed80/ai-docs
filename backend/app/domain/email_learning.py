"""Ф6.8 — learning from what people correct.

A correction is the only reliable evidence that the agent got something wrong:
nobody files a ticket saying "the classifier mislabelled a newsletter", they
just fix it and move on. Captured as a MemoryFact with provenance, it becomes
context for later turns; captured nowhere, the same mistake repeats forever.

Deliberately narrow: one fact per correction, owner-scoped, with the original
and corrected label side by side. No attempt to retrain anything here.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


async def record_triage_correction(db, triage_row, message, *, corrected_by: str) -> None:
    """Persist "this letter was X, not Y" as a memory fact."""
    from app.db.models import MemoryFact
    from app.domain.email_triage import label_for

    if triage_row.corrected_category == triage_row.category:
        return

    sender = (message.from_address or "").strip()
    content = (
        f"Письмо от {sender} с темой «{(message.subject or '')[:120]}» "
        f"агент отнёс к категории «{label_for(triage_row.category)}», "
        f"человек исправил на «{label_for(triage_row.corrected_category)}»."
    )
    db.add(MemoryFact(
        scope=f"mailbox:{message.mailbox}"[:80],
        kind="correction",
        title=f"Категория письма от {sender}"[:500],
        summary=content,
        source="email_triage_correction",
        confidence=1.0,
        provenance={
            "message_id": str(message.id),
            "model_name": triage_row.model_name,
            "corrected_by": corrected_by,
        },
        metadata_={
            "from": sender,
            "model_category": triage_row.category,
            "human_category": triage_row.corrected_category,
        },
    ))
    from app.core.metrics import email_triage_corrections_total

    email_triage_corrections_total.labels(
        was=triage_row.category, now=triage_row.corrected_category,
    ).inc()
    logger.info(
        "email_triage_correction_recorded",
        message_id=str(message.id),
        was=triage_row.category, now=triage_row.corrected_category,
    )
