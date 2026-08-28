"""Reusable "help me write this email" service.

Powers:
  * the composer's "AI help" button (api/email.py::compose_assist) — rewrites the
    human's draft per a free-text instruction, returns a diff for preview;
  * the agent's email.compose capability (api/email.py::agent_generate_draft) —
    generates a draft from an intent + context, then goes through the normal
    risk_check → send [GATE] path.

LLM runs through app.ai.ollama_client.reasoning_generate with confidential=True
(email bodies may quote internal data), so it stays on a local model unless the
operator has explicitly allowed a cloud reasoning model.
"""

from __future__ import annotations

import difflib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailMessage, EmailThread, Invoice, Party

logger = structlog.get_logger()

_PROMPT_PATH = Path(__file__).resolve().parents[2].parent / "aiagent" / "prompts" / "email-drafting.md"
_FALLBACK_SYSTEM = (
    "Ты — Света, ассистент отдела снабжения. Пишешь деловые письма на русском языке. "
    "Тон деловой, структура: приветствие, суть в первом абзаце, конкретные ссылки "
    "(номера счетов, даты, суммы), профессиональное завершение. Никогда не отправляй "
    "письмо сам — всегда нужно подтверждение человека."
)


def _system_prompt() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return _FALLBACK_SYSTEM


@dataclass
class ComposeContext:
    thread_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    mailbox: str | None = None


@dataclass
class ComposeResult:
    subject: str
    body_html: str
    body_text: str
    diff: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tone: str = "formal"


def _plain(html_or_text: str) -> str:
    import re

    if "<" in html_or_text and ">" in html_or_text:
        return re.sub(r"<[^>]+>", "", html_or_text).strip()
    return html_or_text.strip()


def _clean_notes(notes) -> list[str]:
    """Drop notes that leak the JSON/prompt scaffolding to the user."""
    out = []
    for n in notes or []:
        low = str(n).lower()
        if "json" in low or "поля subject" in low or "структур" in low and "письм" not in low:
            continue
        out.append(str(n))
    return out


def _diff(before: str, after: str) -> list[dict]:
    ops: list[dict] = []
    sm = difflib.SequenceMatcher(a=before.split(), b=after.split())
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ops.append({"op": "equal", "text": " ".join(before.split()[i1:i2])})
        elif tag == "delete":
            ops.append({"op": "delete", "text": " ".join(before.split()[i1:i2])})
        elif tag == "insert":
            ops.append({"op": "insert", "text": " ".join(after.split()[j1:j2])})
        elif tag == "replace":
            ops.append({"op": "delete", "text": " ".join(before.split()[i1:i2])})
            ops.append({"op": "insert", "text": " ".join(after.split()[j1:j2])})
    return ops


async def _gather_context(db: AsyncSession, ctx: ComposeContext) -> tuple[str, str, list[EmailMessage]]:
    """Return (context_text, tone, prior_messages_with_counterparty)."""
    lines: list[str] = []
    prior: list[EmailMessage] = []
    counterparty_email: str | None = None

    if ctx.supplier_id:
        party = (await db.execute(select(Party).where(Party.id == ctx.supplier_id))).scalar_one_or_none()
        if party:
            lines.append(f"Контрагент: {party.name}")
            if party.contact_email:
                counterparty_email = party.contact_email
                lines.append(f"Email контрагента: {party.contact_email}")

    if ctx.invoice_id:
        inv = (await db.execute(select(Invoice).where(Invoice.id == ctx.invoice_id))).scalar_one_or_none()
        if inv:
            lines.append(
                f"Счёт №{inv.invoice_number or '—'} от "
                f"{inv.invoice_date.date() if inv.invoice_date else '—'}, "
                f"сумма {inv.total_amount or '—'} {inv.currency}, "
                f"срок оплаты {inv.due_date.date() if inv.due_date else '—'}"
            )

    if ctx.thread_id:
        th = (
            await db.execute(select(EmailThread).where(EmailThread.id == ctx.thread_id))
        ).scalar_one_or_none()
        if th:
            msgs = (
                await db.execute(
                    select(EmailMessage)
                    .where(EmailMessage.thread_id == th.id)
                    .order_by(EmailMessage.received_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            prior = list(reversed(msgs))
            if prior:
                lines.append("Последние сообщения в переписке:")
                for m in prior:
                    lines.append(f"  [{m.from_address}] {(m.body_text or '')[:400]}")

    tone = "formal"
    if not prior and counterparty_email:
        try:
            from app.ai.router import ai_router

            hist = (
                await db.execute(
                    select(EmailMessage)
                    .where(EmailMessage.from_address.ilike(f"%{counterparty_email}%"))
                    .order_by(EmailMessage.received_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            if hist:
                text = "\n---\n".join(f"{m.from_address}: {(m.body_text or '')[:400]}" for m in hist)
                style = await ai_router.analyze_email_style(text, len(hist))
                tone = style.get("tone", "formal")
                if style.get("greeting_style"):
                    lines.append(f"Обычное приветствие контрагента: {style['greeting_style']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("compose_style_analyze_failed", error=str(exc))

    return "\n".join(lines), tone, prior


async def _run_llm(system: str, prompt: str) -> dict:
    from app.ai.ollama_client import reasoning_generate

    raw = await reasoning_generate(
        prompt, system=system, temperature=0.6, format_json=True, confidential=True
    )
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return {"subject": "", "body_text": raw if isinstance(raw, str) else "", "body_html": ""}


async def assist_compose(
    db: AsyncSession,
    *,
    draft_subject: str,
    draft_body: str,
    instruction: str,
    context: ComposeContext,
) -> ComposeResult:
    """Rewrite an existing draft per ``instruction`` (composer 'AI help')."""
    ctx_text, tone, _ = await _gather_context(db, context)
    before = _plain(draft_body)
    ctx_block = f"Контекст:\n{ctx_text}\n\n" if ctx_text else ""
    prompt = (
        f"Тон переписки: {tone}.\n{ctx_block}"
        f"Текущий черновик письма:\nТема: {draft_subject}\n{before}\n\n"
        f"Инструкция по доработке: {instruction}\n\n"
        'Верни JSON: {"subject": "...", "body_text": "...", '
        '"body_html": "<p>...</p>", "notes": ["что изменено"]}'
    )
    out = await _run_llm(_system_prompt(), prompt)
    body_text = (out.get("body_text") or before).strip()
    body_html = out.get("body_html") or "".join(f"<p>{p}</p>" for p in body_text.split("\n\n") if p)
    return ComposeResult(
        subject=out.get("subject") or draft_subject,
        body_html=body_html,
        body_text=body_text,
        diff=_diff(before, body_text),
        notes=_clean_notes(out.get("notes")),
        tone=tone,
    )


async def generate_draft_body(
    db: AsyncSession,
    *,
    intent: str,
    context: ComposeContext,
    tone_override: str | None = None,
) -> ComposeResult:
    """Generate a fresh draft from an intent (agent email.compose)."""
    ctx_text, tone, _ = await _gather_context(db, context)
    tone = tone_override or tone
    ctx_block = f"Контекст:\n{ctx_text}\n\n" if ctx_text else ""
    prompt = (
        f"Тон письма: {tone}.\n{ctx_block}"
        f"Задача: {intent}\n\n"
        'Верни JSON: {"subject": "...", "body_text": "...", '
        '"body_html": "<p>...</p>", "notes": []}'
    )
    out = await _run_llm(_system_prompt(), prompt)
    body_text = (out.get("body_text") or "").strip()
    body_html = out.get("body_html") or "".join(f"<p>{p}</p>" for p in body_text.split("\n\n") if p)
    return ComposeResult(
        subject=out.get("subject") or intent[:120],
        body_html=body_html,
        body_text=body_text,
        notes=out.get("notes") or [],
        tone=tone,
    )
