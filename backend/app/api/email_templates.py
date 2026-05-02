"""Email templates API — CRUD + create from existing message + render."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailMessage, EmailTemplateCategory, EmailTemplateDB
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class EmailTemplateCreate(BaseModel):
    name: str
    slug: str | None = None
    category: EmailTemplateCategory = EmailTemplateCategory.custom
    language: str = "ru"
    subject: str
    body_html: str
    body_text: str | None = None
    variables: list[str] = []


class EmailTemplateUpdate(BaseModel):
    name: str | None = None
    category: EmailTemplateCategory | None = None
    language: str | None = None
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    variables: list[str] | None = None


class EmailTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    category: EmailTemplateCategory
    language: str
    subject: str
    body_html: str
    body_text: str | None
    variables: list[str] | None
    is_builtin: bool
    source_email_id: uuid.UUID | None
    use_count: int
    last_used_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EmailTemplateFromMessageRequest(BaseModel):
    email_id: uuid.UUID
    name: str
    category: EmailTemplateCategory = EmailTemplateCategory.custom
    extract_variables: bool = True


class EmailTemplateRenderRequest(BaseModel):
    variables: dict[str, str] = {}


class EmailTemplateRenderResponse(BaseModel):
    subject: str
    body_html: str
    body_text: str | None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-zа-яё0-9\s_-]", "", slug)
    slug = re.sub(r"\s+", "_", slug)
    slug = re.sub(r"[а-яё]", "", slug)  # strip cyrillic chars
    slug = slug[:80] or "template"
    return slug


def _detect_variables(text: str) -> list[str]:
    """Find {variable} placeholders in text."""
    return sorted(set(re.findall(r"\{(\w+)\}", text)))


async def _ensure_unique_slug(db: AsyncSession, base_slug: str, exclude_id: uuid.UUID | None = None) -> str:
    slug = base_slug
    suffix = 0
    while True:
        q = select(EmailTemplateDB).where(EmailTemplateDB.slug == slug)
        if exclude_id:
            q = q.where(EmailTemplateDB.id != exclude_id)
        existing = await db.execute(q)
        if not existing.scalar_one_or_none():
            return slug
        suffix += 1
        slug = f"{base_slug}_{suffix}"


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.get("/", response_model=list[EmailTemplateOut])
async def list_templates(
    category: EmailTemplateCategory | None = None,
    is_builtin: bool | None = None,
    language: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[EmailTemplateOut]:
    """Skill: email.templates.list — List email templates with optional filters."""
    q = select(EmailTemplateDB).order_by(EmailTemplateDB.is_builtin.desc(), EmailTemplateDB.name)
    if category is not None:
        q = q.where(EmailTemplateDB.category == category)
    if is_builtin is not None:
        q = q.where(EmailTemplateDB.is_builtin == is_builtin)
    if language:
        q = q.where(EmailTemplateDB.language == language)
    result = await db.execute(q)
    return [EmailTemplateOut.model_validate(t) for t in result.scalars().all()]


@router.post("/", response_model=EmailTemplateOut, status_code=201)
async def create_template(
    payload: EmailTemplateCreate,
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    """Skill: email.templates.create — Create a new email template."""
    base_slug = _slugify(payload.slug or payload.name)
    slug = await _ensure_unique_slug(db, base_slug)
    variables = payload.variables or _detect_variables(payload.body_html + " " + (payload.body_text or ""))

    tpl = EmailTemplateDB(
        name=payload.name,
        slug=slug,
        category=payload.category,
        language=payload.language,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        variables=variables,
        is_builtin=False,
        created_by="user",
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return EmailTemplateOut.model_validate(tpl)


@router.get("/{template_id}", response_model=EmailTemplateOut)
async def get_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    """Skill: email.templates.get — Get a template by ID."""
    tpl = await db.get(EmailTemplateDB, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return EmailTemplateOut.model_validate(tpl)


@router.patch("/{template_id}", response_model=EmailTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    payload: EmailTemplateUpdate,
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    """Skill: email.templates.update — Update a custom template."""
    tpl = await db.get(EmailTemplateDB, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.is_builtin:
        raise HTTPException(status_code=403, detail="Built-in templates cannot be modified")

    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(tpl, key, value)

    # Re-detect variables if body changed but variables not provided
    if (payload.body_html or payload.body_text) and payload.variables is None:
        tpl.variables = _detect_variables(
            (tpl.body_html or "") + " " + (tpl.body_text or "")
        )

    await db.commit()
    await db.refresh(tpl)
    return EmailTemplateOut.model_validate(tpl)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Skill: email.templates.delete — Delete a custom template."""
    tpl = await db.get(EmailTemplateDB, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.is_builtin:
        raise HTTPException(status_code=403, detail="Built-in templates cannot be deleted")
    await db.delete(tpl)
    await db.commit()


@router.post("/from-message", response_model=EmailTemplateOut, status_code=201)
async def create_template_from_message(
    payload: EmailTemplateFromMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateOut:
    """Skill: email.templates.from_message — Create a template from an existing email.

    Optionally uses pattern detection to find {variable} placeholders.
    """
    msg = await db.get(EmailMessage, payload.email_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Email message not found")

    body_html = msg.body_html or (f"<p>{msg.body_text}</p>" if msg.body_text else "")
    body_text = msg.body_text or ""
    subject = msg.subject or ""

    variables: list[str] = []
    if payload.extract_variables:
        # Try AI extraction first, fall back to simple pattern detection
        try:
            variables = await _ai_extract_variables(body_text or body_html)
        except Exception:
            variables = _detect_variables(body_html + " " + body_text + " " + subject)

    base_slug = _slugify(payload.name)
    slug = await _ensure_unique_slug(db, base_slug)

    tpl = EmailTemplateDB(
        name=payload.name,
        slug=slug,
        category=payload.category,
        language="ru",
        subject=subject,
        body_html=body_html,
        body_text=body_text or None,
        variables=variables or None,
        is_builtin=False,
        source_email_id=msg.id,
        created_by="user",
    )
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    logger.info("email_template_created_from_message", slug=slug, email_id=str(payload.email_id))
    return EmailTemplateOut.model_validate(tpl)


@router.post("/{template_id}/render", response_model=EmailTemplateRenderResponse)
async def render_template(
    template_id: uuid.UUID,
    payload: EmailTemplateRenderRequest,
    db: AsyncSession = Depends(get_db),
) -> EmailTemplateRenderResponse:
    """Skill: email.templates.render — Render a template with variable substitution."""
    tpl = await db.get(EmailTemplateDB, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Increment usage counter
    tpl.use_count = (tpl.use_count or 0) + 1
    tpl.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    def _substitute(text: str | None) -> str | None:
        if not text:
            return text
        result = text
        for key, value in payload.variables.items():
            result = result.replace(f"{{{key}}}", value)
        return result

    return EmailTemplateRenderResponse(
        subject=_substitute(tpl.subject) or "",
        body_html=_substitute(tpl.body_html) or "",
        body_text=_substitute(tpl.body_text),
    )


# ── AI variable extraction ─────────────────────────────────────────────────────

async def _ai_extract_variables(text: str) -> list[str]:
    """Ask AI to identify parameterisable fields in an email body."""
    try:
        from app.ai.router import ai_router

        prompt = (
            "Найди в тексте письма поля, которые можно параметризировать "
            "(например: номер счёта, дата, сумма, наименование компании, ФИО и т.п.). "
            "Верни JSON-массив строк snake_case (только массив, без пояснений). "
            f"Текст:\n\n{text[:3000]}"
        )
        result = await ai_router.complete(prompt)
        import json
        parsed = json.loads(result)
        if isinstance(parsed, list):
            return [str(v) for v in parsed if isinstance(v, str)]
    except Exception:
        pass
    return []
