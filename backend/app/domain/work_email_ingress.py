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

Live since ``MailboxConfig.assigned_role == "agent_ingress"`` (a dedicated
mailbox or IMAP subfolder, so it never races the invoice poller for the same
unseen messages): app.tasks.ingest.poll_imap_mailbox ingests such a mailbox
like any other and additionally calls this module for messages that pass two
checks — the subject marker (``is_agent_instruction_email``) and the sender
allowlist (``MailboxConfig.ingress_allowed_senders``). Both are the caller's
responsibility and both are enforced there; until Ф0.6 neither was, so every
message that landed in the ingress mailbox — spam included, from any sender —
became a task executed with the agent's own permissions.
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


async def create_work_order_from_email(
    db: AsyncSession,
    parsed: Any,
    *,
    mailbox: str | None = None,
    email_message_pk: Any = None,
) -> WorkOrder:
    """Create the WorkOrder + 3-step plan (answer -> draft_reply -> send_reply)
    for one agent-instruction email. Caller is responsible for having already
    confirmed is_agent_instruction_email(parsed), for checking the sender, and
    for the commit.

    ``mailbox`` is the ingress mailbox the instruction arrived in — the reply
    must go out from that same address, not from whatever single account the
    .env fallback names. ``email_message_pk`` is our stored EmailMessage id, so
    the order can be traced back to a letter a human can actually open.
    """
    from app.ai.input_sanitizer import UNTRUSTED_NOTE, wrap_untrusted

    # Текст поручения написан человеком снаружи — размечаем его как данные.
    # Отправитель проверен (DKIM/DMARC + allowlist, app.tasks.ingest), но
    # «письмо от коллеги» не делает его содержимое системной инструкцией: сюда
    # попадает и пересланная переписка, и цитаты третьих лиц.
    letter = str(parsed.body_text or parsed.subject or "").strip()
    prompt = (
        f"{UNTRUSTED_NOTE}\n\nПоручение из письма:\n"
        + wrap_untrusted(letter, "email-instruction")
    ) if letter else ""
    order = await create_work_order(
        db,
        owner_key=f"email:{parsed.from_address}",
        objective=_objective_from_email(parsed),
        description=letter[:2000] or None,
        source="email",
        metadata={
            "email_message_id": parsed.message_id,
            "email_message_pk": str(email_message_pk) if email_message_pk else None,
            "email_from": parsed.from_address,
            "email_subject": parsed.subject,
            "mailbox": mailbox,
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
                    # Reply from the ingress mailbox itself; without this the
                    # send falls back to the global .env account.
                    "mailbox": mailbox,
                    "in_reply_to_message_id": (
                        str(email_message_pk) if email_message_pk else None
                    ),
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
                # expected_digest binds the approval to this exact letter
                # (Ф0.2): it flows into work_planning.tool_call_digest, so a
                # decision cannot be spent on content rewritten afterwards.
                "input": {
                    "draft_id": "${steps.draft_reply.output.id}",
                    "expected_digest": "${steps.draft_reply.output.content_digest}",
                },
                "depends_on": ["draft_reply"],
                "risk_level": "high",
            },
        ],
    )
    return order
