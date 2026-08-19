"""Б16: email as a real agent-instruction channel for WorkOrder, distinct
from email_triage.py's "Scenario 1 degraded mode" invoice pipeline — that
pipeline classifies/OCRs/extracts attachments, it has nothing to do with
turning an email into a poручение.

Reply-to-source is NOT a bespoke SMTP send — it's a 3-step WorkPlan
(answer -> draft_reply -> send_reply) built entirely from existing
mechanisms: the "capability" step kind, dataflow templating
(${steps.<key>.output.<path>}, work_planning.py), and the email capability's
existing draft/send split (capabilities.yml: gate_actions: [send, ...]) —
the reply goes through the same approval gate email.send already enforces
for every other agent-composed email, not a shortcut around it.

Not wired to a live Celery beat poll in this pass: the obvious source,
imap_client.get_mailbox_configs(), is the same mailbox list email_triage.py
polls for invoices. fetch_unseen_from_mailbox marks fetched messages seen
(shared mailboxes) or advances a UID watermark (personal ones) — sharing
that source between two independent consumers means whichever task's beat
tick fires first "steals" every unseen message, not just the ones it
understands, silently breaking the other pipeline. A real second channel
needs its own dedicated mailbox or IMAP folder (MailboxConfig.folder
already supports a subfolder), which doesn't exist in this deployment yet
— wiring polling against the shared source would be actively harmful, not
merely undertested, so the ingress logic below is implemented and tested
standalone, ready to be called once a dedicated source exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkOrder
from app.domain.work_orders import create_work_order, create_work_plan

# Case-insensitive subject prefix. Configurable-later, not settings-backed
# yet — see the module docstring on why this isn't wired to a live poll.
INSTRUCTION_MARKER = "поручение:"


class _ParsedEmailLike:
    """Structural type for imap_client.ParsedEmail — avoids importing
    imap_client (imaplib-dependent) into domain code for a type hint."""

    message_id: str | None
    from_address: str
    subject: str
    body_text: str


def is_agent_instruction_email(parsed: Any) -> bool:
    """True when the subject starts with INSTRUCTION_MARKER (case-insensitive,
    surrounding whitespace ignored)."""
    return str(parsed.subject or "").strip().casefold().startswith(INSTRUCTION_MARKER)


def _objective_from_email(parsed: Any) -> str:
    subject = str(parsed.subject or "").strip()
    if subject.casefold().startswith(INSTRUCTION_MARKER):
        subject = subject[len(INSTRUCTION_MARKER):].strip()
    if subject:
        return subject
    body = str(parsed.body_text or "").strip()
    return body[:200] if body else "Поручение из письма"


async def create_work_order_from_email(db: AsyncSession, parsed: Any) -> WorkOrder:
    """Create the WorkOrder + 3-step plan (answer -> draft_reply -> send_reply)
    for one agent-instruction email. Caller is responsible for having already
    confirmed is_agent_instruction_email(parsed) and for the commit."""
    prompt = str(parsed.body_text or parsed.subject or "").strip()
    order = await create_work_order(
        db,
        owner_key=f"email:{parsed.from_address}",
        objective=_objective_from_email(parsed),
        description=prompt[:2000] or None,
        source="email",
        metadata={
            "email_message_id": parsed.message_id,
            "email_from": parsed.from_address,
            "email_subject": parsed.subject,
        },
    )
    reply_subject = str(parsed.subject or "").strip()
    if not reply_subject.casefold().startswith("re:"):
        reply_subject = f"Re: {reply_subject}" if reply_subject else "Re: поручение"
    await create_work_plan(
        db,
        order,
        steps=[
            {
                "step_key": "answer",
                "title": "Выполнить поручение из письма",
                "kind": "agent_turn",
                "input": {"prompt": prompt},
                "depends_on": [],
            },
            {
                "step_key": "draft_reply",
                "title": "Подготовить черновик ответа",
                "kind": "capability",
                "capability": "email",
                "action": "draft",
                "input": {
                    "to_addresses": [parsed.from_address],
                    "subject": reply_subject,
                    "body_html": "${steps.answer.output.text}",
                    "body_text": "${steps.answer.output.text}",
                },
                "depends_on": ["answer"],
            },
            {
                # gate_actions: [send, ...] in capabilities.yml — this step
                # always goes through the standard approval flow, exactly
                # like a human- or chat-composed reply. No bypass. risk_level
                # is set explicitly here because create_work_plan takes raw
                # step dicts directly (bypassing the LLM-planner path's
                # validate_capability_plan, which is what normally elevates
                # a gated action's risk_level to "high") — a hand-built plan
                # must carry the same signal itself, not rely on a check
                # that this code path never runs.
                "step_key": "send_reply",
                "title": "Отправить ответ (approval-gated)",
                "kind": "capability",
                "capability": "email",
                "action": "send",
                "input": {"draft_id": "${steps.draft_reply.output.id}"},
                "depends_on": ["draft_reply"],
                "risk_level": "high",
            },
        ],
    )
    return order
