"""Agentic "help me write this email" service.

Powers:
  * the composer's "AI help" button (api/email.py::compose_assist) — improves the
    human's draft per a free-text instruction, returns a diff for preview;
  * the agent's email.compose capability (api/email.py::agent_generate_draft) —
    drafts an email from an intent + context, then goes through the normal
    risk_check → send [GATE] path.

Both run a REAL headless agent turn (app.ai.agent_loop.AgentSession) scoped to a
worker role, so the model can look things up with tools — e.g. "выясни, какие
позиции были в счёте УТ-2562, и напиши про их наличие" makes the agent fetch the
invoice, its line items and stock before writing. The model is the one
configured for AITask.EMAIL_DRAFTING (Settings → Модели → Маршрутизация).

A fast single-shot LLM call is the fallback when the agent turn errors, times
out or returns nothing usable.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
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
# The worker role whose capability allowlist the email turn runs under — needs
# invoices / suppliers / procurement / documents / warehouse read access so the
# agent can gather the facts a business email references.
_EMAIL_TURN_ROLE = "procurement_specialist"
_TURN_TIMEOUT_S = 150.0


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
    if "<" in html_or_text and ">" in html_or_text:
        return re.sub(r"<[^>]+>", "", html_or_text).strip()
    return html_or_text.strip()


def _clean_notes(notes) -> list[str]:
    out = []
    for n in notes or []:
        low = str(n).lower()
        if "json" in low or "поля subject" in low or ("структур" in low and "письм" not in low):
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


def _paragraphs_to_html(text: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br/>')}</p>" for p in parts)


def _strip_preamble(text: str) -> str:
    """Remove a leading 'Вот доработанное письмо:' style line."""
    t = text.strip()
    first, _, rest = t.partition("\n")
    low = first.lower()
    if len(first) < 120 and any(
        k in low for k in ("письмо", "вариант", "черновик", "готово", "текст письма", "результат")
    ) and first.rstrip().endswith((":", "—", "-")):
        return rest.strip() or t
    return t


def _extract_result(agent_text: str, *, fallback_subject: str) -> dict:
    """Pull {subject, body_text, body_html, notes} out of a free-form agent reply.

    Preferred: a trailing ```json ...``` fence. Fallback: treat the whole reply
    (minus any preamble / trailing json fence) as the letter body.
    """
    notes: list[str] = []
    subject = fallback_subject
    body_text = agent_text

    m = re.search(r"```json\s*(\{.*?\})\s*```", agent_text, re.DOTALL) or re.search(
        r"(\{[^{}]*\"body_text\"[^{}]*\})", agent_text, re.DOTALL
    )
    if m:
        try:
            data = json.loads(m.group(1))
            subject = (data.get("subject") or fallback_subject).strip()
            body_text = (data.get("body_text") or "").strip() or agent_text
            notes = data.get("notes") or []
            body_html = (data.get("body_html") or "").strip()
            if body_html:
                return {"subject": subject, "body_text": body_text,
                        "body_html": body_html, "notes": notes}
        except Exception:  # noqa: BLE001
            pass
        # strip the fence from the prose body
        body_text = agent_text[: m.start()].strip() or body_text

    body_text = _cut_to_letter(_strip_preamble(body_text))
    return {
        "subject": subject,
        "body_text": body_text,
        "body_html": _paragraphs_to_html(body_text),
        "notes": notes,
    }


async def _gather_context(db: AsyncSession, ctx: ComposeContext) -> tuple[str, str]:
    """Return (context_text, tone) — light hints; the agent looks up the rest."""
    lines: list[str] = []
    counterparty_email: str | None = None
    prior_seen = False

    if ctx.supplier_id:
        party = (await db.execute(select(Party).where(Party.id == ctx.supplier_id))).scalar_one_or_none()
        if party:
            lines.append(f"Контрагент: {party.name} (id {party.id})")
            if party.contact_email:
                counterparty_email = party.contact_email
                lines.append(f"Email контрагента: {party.contact_email}")

    if ctx.invoice_id:
        inv = (await db.execute(select(Invoice).where(Invoice.id == ctx.invoice_id))).scalar_one_or_none()
        if inv:
            lines.append(
                f"Счёт №{inv.invoice_number or '—'} (id {inv.id}), сумма "
                f"{inv.total_amount or '—'} {inv.currency}, "
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
            if msgs:
                prior_seen = True
                lines.append("Последние сообщения в переписке:")
                for m in reversed(msgs):
                    lines.append(f"  [{m.from_address}] {(m.body_text or '')[:400]}")

    tone = "formal"
    if not prior_seen and counterparty_email:
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

    return "\n".join(lines), tone


def _email_model_override() -> str | None:
    try:
        from app.ai.schemas import AITask
        from app.ai.task_routing import resolve_model

        model, provider = resolve_model(AITask.EMAIL_DRAFTING)
        if model and provider in ("ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"):
            return model
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_model_resolve_failed", error=str(exc))
    return None


async def _run_agent_email_turn(prompt: str, *, acting_user_sub: str | None) -> str:
    """One headless agent turn, scoped to the email/procurement role, on the
    configured email model. Returns the agent's final text ('' on failure)."""
    from app.ai.actor_context import get_acting_user, set_acting_user
    from app.ai.agent_loop import AgentSession

    chunks: list[str] = []
    errors: list[str] = []

    async def collect(event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype == "text":
            chunks.append(str(event.get("content") or ""))
        elif etype == "error":
            errors.append(str(event.get("content") or ""))

    token = None
    prev = get_acting_user()
    try:
        set_acting_user(acting_user_sub)
        session = AgentSession(collect)
        session._active_role = _EMAIL_TURN_ROLE
        session._response_budget = 3000
        session._turn_model_override = _email_model_override()
        await asyncio.wait_for(session.on_user_message(prompt), timeout=_TURN_TIMEOUT_S)
    except TimeoutError:
        logger.warning("email_agent_turn_timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_agent_turn_failed", error=str(exc))
    finally:
        set_acting_user(prev)
        if token is not None:  # pragma: no cover
            pass

    text = "".join(chunks).strip()
    if errors and not text:
        logger.warning("email_agent_turn_errors", errors=errors[:3])
    return text


async def _single_shot(system: str, prompt: str) -> dict:
    from app.ai.ollama_client import reasoning_generate

    raw = await reasoning_generate(
        prompt, system=system, temperature=0.6, format_json=True, confidential=True
    )
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return {"subject": "", "body_text": raw if isinstance(raw, str) else "", "body_html": ""}


_JSON_TAIL = (
    'ФОРМАТ ОТВЕТА (строго):\n'
    '1) Основная часть ответа — это ТОЛЬКО готовый текст письма, начиная с '
    'приветствия («Добрый день!» / «Уважаемые коллеги!»). Без строк «Нашёл счёт…», '
    '«Готовый текст письма:», «Тема:», без разделителей «---» перед текстом.\n'
    '2) В самом конце — ОДИН блок:\n'
    '```json\n{"subject": "<тема письма>", "notes": ["<что ты выяснил и учёл>"]}\n```'
)


_LETTER_MARKERS = re.compile(
    r"(?:готовый\s+текст\s+письма|текст\s+письма|^\s*письмо)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _cut_to_letter(text: str) -> str:
    """Drop any 'Нашёл счёт… Готовый текст письма: --- Тема: …' scaffolding the
    model sometimes puts before the actual letter."""
    t = text.strip()
    m = None
    for m in _LETTER_MARKERS.finditer(t):
        pass
    if m:
        t = t[m.end():].strip()
    # leading separator line
    t = re.sub(r"^\s*-{3,}\s*\n", "", t).strip()
    # leading "Тема: ..." line
    t = re.sub(r"^\s*тема\s*:.*\n+", "", t, flags=re.IGNORECASE).strip()
    # trailing separator + anything after it
    t = re.split(r"\n\s*-{3,}\s*(?:\n|$)", t)[0].strip()
    return t


async def assist_compose(
    db: AsyncSession,
    *,
    draft_subject: str,
    draft_body: str,
    instruction: str,
    context: ComposeContext,
    acting_user_sub: str | None = None,
) -> ComposeResult:
    """Improve an existing draft per ``instruction`` (composer 'AI help')."""
    ctx_text, tone = await _gather_context(db, context)
    before = _plain(draft_body)
    ctx_block = f"Известный контекст:\n{ctx_text}\n\n" if ctx_text else ""

    prompt = (
        f"{_system_prompt()}\n\n"
        f"Тебе нужно доработать черновик делового письма и вернуть готовый текст.\n\n"
        f"ТЕКУЩИЙ ЧЕРНОВИК:\nТема: {draft_subject}\n{before or '(пусто)'}\n\n"
        f"ЗАДАЧА ОТ ЧЕЛОВЕКА: {instruction}\n\n"
        f"{ctx_block}"
        f"Если для выполнения задачи нужны факты (позиции и суммы по счёту, "
        f"наличие на складе, данные поставщика, история переписки) — СНАЧАЛА собери "
        f"их своими инструментами, потом пиши. Тон: {tone}. Пиши на русском.\n\n"
        f"{_JSON_TAIL}"
    )

    agent_text = await _run_agent_email_turn(prompt, acting_user_sub=acting_user_sub)
    if agent_text:
        parsed = _extract_result(agent_text, fallback_subject=draft_subject or "")
    else:
        out = await _single_shot(
            _system_prompt(),
            f"Тон: {tone}.\n{ctx_block}Черновик:\nТема: {draft_subject}\n{before}\n\n"
            f"Инструкция: {instruction}\n\n"
            'Верни JSON: {"subject":"...","body_text":"...","body_html":"<p>...</p>","notes":[]}',
        )
        bt = (out.get("body_text") or before).strip()
        parsed = {
            "subject": out.get("subject") or draft_subject,
            "body_text": bt,
            "body_html": out.get("body_html") or _paragraphs_to_html(bt),
            "notes": out.get("notes") or [],
        }

    return ComposeResult(
        subject=parsed["subject"] or draft_subject,
        body_html=parsed["body_html"],
        body_text=parsed["body_text"],
        diff=_diff(before, parsed["body_text"]),
        notes=_clean_notes(parsed["notes"]),
        tone=tone,
    )


async def generate_draft_body(
    db: AsyncSession,
    *,
    intent: str,
    context: ComposeContext,
    tone_override: str | None = None,
    acting_user_sub: str | None = None,
) -> ComposeResult:
    """Draft a fresh email from an intent.

    Single-shot: called from the agent's own turn (email.compose / email.reply),
    which already has the tools + context — a nested agent turn here would be
    recursion. The caller is responsible for gathering facts before invoking it.
    """
    ctx_text, tone = await _gather_context(db, context)
    tone = tone_override or tone
    ctx_block = f"Известный контекст:\n{ctx_text}\n\n" if ctx_text else ""

    out = await _single_shot(
        _system_prompt(),
        f"Тон: {tone}.\n{ctx_block}Задача: {intent}\n\n"
        'Верни JSON: {"subject":"...","body_text":"...","body_html":"<p>...</p>","notes":[]}',
    )
    bt = (out.get("body_text") or "").strip()
    parsed = {
        "subject": out.get("subject") or intent[:120],
        "body_text": bt,
        "body_html": out.get("body_html") or _paragraphs_to_html(bt),
        "notes": out.get("notes") or [],
    }

    return ComposeResult(
        subject=parsed["subject"] or intent[:120],
        body_html=parsed["body_html"],
        body_text=parsed["body_text"],
        notes=_clean_notes(parsed["notes"]),
        tone=tone,
    )
