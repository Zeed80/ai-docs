"""Email address book — EmailContact CRUD + the composer autocomplete.

Autocomplete (``GET /api/email/contacts``) merges three sources: saved
EmailContacts, Party.contact_email, and recent inbound senders. The address-book
UI uses the /book, POST, PATCH, DELETE routes.

Mounted BEFORE email.router (app/main.py) — email.router has a catch-all
GET /api/email/{email_id} that would otherwise shadow these.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.models import EmailContact, EmailMessage, Party
from app.db.session import get_db
from app.domain.email_access import mailbox_filter

router = APIRouter()
logger = structlog.get_logger()

_LOCAL = ("ollama", "llamacpp", "vllm")  # unused; kept import minimal


class ContactOut(BaseModel):
    email: str
    name: str | None = None
    organization: str | None = None
    id: uuid.UUID | None = None
    is_favorite: bool = False
    source: str = "history"


class ContactBookItem(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    organization: str | None
    phone: str | None
    notes: str | None
    tags: list = []
    is_favorite: bool
    trust_images: bool = False
    source: str
    use_count: int
    owner_sub: str | None

    model_config = {"from_attributes": True}

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_not_none(cls, v):
        return v or []


class ContactCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    name: str | None = None
    organization: str | None = None
    phone: str | None = None
    notes: str | None = None
    tags: list[str] = []
    is_favorite: bool = False
    trust_images: bool = False
    shared: bool = False  # admins can create org-wide contacts
    # «Добавить в контакты» из письма нажимают, не помня, есть ли уже такой
    # адрес. С upsert повторное нажатие возвращает существующую карточку
    # вместо 409 — ошибка там сообщала бы человеку о его же памяти.
    upsert: bool = False


class ContactUpdate(BaseModel):
    name: str | None = None
    organization: str | None = None
    phone: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    is_favorite: bool | None = None
    trust_images: bool | None = None


def _split(addr: str) -> tuple[str, str]:
    """('Иван Петров <ivan@x.ru>') -> ('Иван Петров', 'ivan@x.ru')."""
    from email.utils import parseaddr

    name, email = parseaddr(addr or "")
    return name.strip(), (email or addr or "").strip()


def _bare(addr: str) -> str:
    return _split(addr)[1]


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


@router.get("", response_model=list[ContactOut])
async def autocomplete(
    q: str = "",
    limit: int = 10,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.contacts — Address autocomplete (book + suppliers + history).

    Empty ``q`` returns favourites + most-used contacts so a freshly-focused
    field already shows suggestions (Gmail-style)."""
    q = (q or "").strip()
    like = f"%{q}%"
    seen: dict[str, ContactOut] = {}

    # 1. saved contacts (personal + shared)
    cq = select(EmailContact).where(
        or_(EmailContact.owner_sub.is_(None), EmailContact.owner_sub == user.sub)
    )
    if q:
        cq = cq.where(or_(EmailContact.email.ilike(like), EmailContact.name.ilike(like),
                          EmailContact.organization.ilike(like)))
    cq = cq.order_by(
        EmailContact.is_favorite.desc(),
        EmailContact.use_count.desc(),
        EmailContact.last_used_at.desc().nullslast(),
    ).limit(limit)
    for c in (await db.execute(cq)).scalars().all():
        seen[c.email.lower()] = ContactOut(
            email=c.email, name=c.name, organization=c.organization,
            id=c.id, is_favorite=c.is_favorite, source="book",
        )

    # 2. suppliers
    if len(seen) < limit and q:
        pq = select(Party).where(
            Party.contact_email.isnot(None),
            or_(Party.contact_email.ilike(like), Party.name.ilike(like)),
        ).limit(limit)
        for p in (await db.execute(pq)).scalars().all():
            key = (p.contact_email or "").lower()
            if key and key not in seen:
                seen[key] = ContactOut(email=p.contact_email, name=p.name,
                                       organization=p.name, source="party")

    # 3. history — everyone we have corresponded with, keeping the display name.
    # Filter in Python (ё/е-insensitive) over the recent window rather than in
    # SQL, so "петр" also matches "Пётр".
    if len(seen) < limit:
        scope = await mailbox_filter(db, user, mailbox_col=EmailMessage.mailbox)
        mq = select(EmailMessage.from_address, EmailMessage.to_addresses).where(
            EmailMessage.from_address.isnot(None)
        )
        if scope is not None:
            mq = mq.where(scope)
        rows = (await db.execute(mq.order_by(EmailMessage.received_at.desc()).limit(300))).all()
        nq = _norm(q)
        for from_addr, to_addrs in rows:
            for raw in [from_addr, *(to_addrs or [])]:
                name, email = _split(str(raw))
                key = email.lower()
                if not key or "@" not in key or key in seen:
                    continue
                if nq and nq not in _norm(key) and nq not in _norm(name):
                    continue
                seen[key] = ContactOut(email=email, name=name or None, source="history")
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break

    ordered = sorted(
        seen.values(),
        key=lambda c: (not c.is_favorite, {"book": 0, "party": 1, "history": 2}.get(c.source, 3)),
    )
    return ordered[:limit]


@router.get("/book", response_model=list[ContactBookItem])
async def list_book(
    q: str = "",
    favorites: bool = False,
    tag: str | None = None,
    limit: int = Query(200, le=1000),
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Full address book (saved contacts only)."""
    query = select(EmailContact).where(
        or_(EmailContact.owner_sub.is_(None), EmailContact.owner_sub == user.sub)
    )
    if q:
        like = f"%{q}%"
        query = query.where(or_(
            EmailContact.email.ilike(like), EmailContact.name.ilike(like),
            EmailContact.organization.ilike(like), EmailContact.phone.ilike(like),
        ))
    if favorites:
        query = query.where(EmailContact.is_favorite == True)  # noqa: E712
    if tag:
        query = query.where(cast(EmailContact.tags, String).ilike(f'%"{tag}"%'))
    query = query.order_by(EmailContact.is_favorite.desc(), EmailContact.name.nullslast(),
                           EmailContact.email).limit(limit)
    return list((await db.execute(query)).scalars().all())


@router.post("/book", response_model=ContactBookItem, status_code=201)
async def create_contact(
    payload: ContactCreate,
    response: Response,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    from app.auth.models import UserRole

    email = _bare(payload.email).lower()
    owner = None if (payload.shared and UserRole.admin in (user.roles or [])) else user.sub
    # Ищем и среди общих карточек: адрес, уже записанный автоматически как
    # общий контакт, иначе получал вторую, личную карточку — и в подсказках
    # один и тот же человек начинал двоиться.
    existing = (
        await db.execute(
            select(EmailContact).where(
                EmailContact.email == email,
                or_(EmailContact.owner_sub == owner, EmailContact.owner_sub.is_(None)),
            ).order_by(EmailContact.owner_sub.is_(None))
        )
    ).scalars().first()
    if existing and payload.upsert:
        # Дополняем пустые поля, ничего не затирая: карточка могла быть
        # заведена автоматически и с тех пор отредактирована человеком.
        existing.name = existing.name or payload.name
        existing.organization = existing.organization or payload.organization
        existing.phone = existing.phone or payload.phone
        existing.notes = existing.notes or payload.notes
        if payload.tags:
            existing.tags = sorted(set((existing.tags or []) + payload.tags))
        if existing.source == "auto":
            existing.source = "manual"
        await db.commit()
        await db.refresh(existing)
        # 200, а не 201: карточку не создали, а дополнили.
        response.status_code = 200
        return existing
    if existing:
        raise HTTPException(409, "Контакт с таким адресом уже есть")
    c = EmailContact(
        email=email, name=payload.name, organization=payload.organization,
        phone=payload.phone, notes=payload.notes, tags=payload.tags or [],
        is_favorite=payload.is_favorite, trust_images=payload.trust_images,
        owner_sub=owner, source="manual",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


class TrustImagesRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    name: str | None = None
    trust: bool = True


@router.post("/trust-images", response_model=ContactBookItem)
async def set_sender_image_trust(
    payload: TrustImagesRequest,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Ф1.4 — «показывать картинки этого отправителя всегда».

    Идемпотентно: у отправителя может ещё не быть карточки в книге, и человек,
    нажимающий «доверять», не заводит контакт — он снимает раздражитель. 409
    здесь был бы ответом на вопрос, которого он не задавал.
    """
    email = _bare(payload.email).lower()
    if not email:
        raise HTTPException(422, "Пустой адрес")
    c = (
        await db.execute(
            select(EmailContact).where(
                EmailContact.email == email, EmailContact.owner_sub == user.sub
            )
        )
    ).scalar_one_or_none()
    if c is None:
        c = EmailContact(
            email=email, name=payload.name, owner_sub=user.sub, source="auto",
            tags=[],
        )
        db.add(c)
    c.trust_images = payload.trust
    await db.commit()
    await db.refresh(c)
    return c


@router.patch("/book/{contact_id}", response_model=ContactBookItem)
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    from app.auth.models import UserRole

    c = await db.get(EmailContact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    if c.owner_sub not in (None, user.sub) or (c.owner_sub is None and UserRole.admin not in (user.roles or [])):
        raise HTTPException(403, "Нет прав на этот контакт")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.commit()
    await db.refresh(c)
    return c


@router.delete("/book/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    from app.auth.models import UserRole

    c = await db.get(EmailContact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    if c.owner_sub not in (None, user.sub) or (c.owner_sub is None and UserRole.admin not in (user.roles or [])):
        raise HTTPException(403, "Нет прав на этот контакт")
    await db.delete(c)
    await db.commit()


@router.get("/tags", response_model=list[str])
async def list_tags(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(EmailContact.tags).where(
                or_(EmailContact.owner_sub.is_(None), EmailContact.owner_sub == user.sub),
                EmailContact.tags.isnot(None),
            )
        )
    ).scalars().all()
    tags: set[str] = set()
    for t in rows:
        for x in (t or []):
            if x:
                tags.add(str(x))
    return sorted(tags)


@router.get("/export")
async def export_contacts(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of the caller's address book."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    rows = (
        await db.execute(
            select(EmailContact).where(
                or_(EmailContact.owner_sub.is_(None), EmailContact.owner_sub == user.sub)
            ).order_by(EmailContact.name.nullslast(), EmailContact.email)
        )
    ).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "organization", "phone", "tags", "notes", "favorite"])
    for c in rows:
        w.writerow([c.name or "", c.email, c.organization or "", c.phone or "",
                    ";".join(c.tags or []), (c.notes or "").replace("\n", " "),
                    "1" if c.is_favorite else ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


class ImportResult(BaseModel):
    added: int
    updated: int
    skipped: int
    # Что именно не прошло и почему: «пропущено 13» без строк и причин
    # невозможно ни исправить, ни проверить.
    skipped_rows: list[dict] = []


@router.post("/import", response_model=ImportResult)
async def import_contacts(
    payload: dict,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Import contacts from CSV text (payload {'csv': '...'}). Columns are
    matched by header name; 'email' is required. Upserts by email."""
    import csv
    import io

    text = str(payload.get("csv") or "")
    if not text.strip():
        raise HTTPException(422, "Пустой CSV")
    reader = csv.DictReader(io.StringIO(text))
    added = updated = skipped = 0
    skipped_rows: list[dict] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        _, email = _split(row.get("email") or row.get("e-mail") or row.get("адрес") or "")
        email = email.lower()
        if not email or "@" not in email:
            skipped += 1
            if len(skipped_rows) < 50:
                skipped_rows.append({
                    "line": line_no,
                    "value": (row.get("email") or row.get("e-mail") or row.get("адрес") or "")[:120],
                    "reason": "нет адреса" if not email else "адрес без @",
                })
            continue
        existing = (
            await db.execute(
                select(EmailContact).where(
                    EmailContact.email == email, EmailContact.owner_sub == user.sub
                )
            )
        ).scalar_one_or_none()
        name = row.get("name") or row.get("имя") or row.get("фио") or None
        org = row.get("organization") or row.get("company") or row.get("организация") or None
        phone = row.get("phone") or row.get("телефон") or None
        tags = [t.strip() for t in (row.get("tags") or "").replace(",", ";").split(";") if t.strip()]
        if existing:
            existing.name = existing.name or name
            existing.organization = existing.organization or org
            existing.phone = existing.phone or phone
            if tags:
                existing.tags = sorted(set((existing.tags or []) + tags))
            updated += 1
        else:
            db.add(EmailContact(
                email=email, name=name, organization=org, phone=phone,
                tags=tags or None, notes=row.get("notes") or row.get("заметки") or None,
                is_favorite=row.get("favorite") in ("1", "true", "yes", "да"),
                owner_sub=user.sub, source="manual",
            ))
            added += 1
    await db.commit()
    return ImportResult(
        added=added, updated=updated, skipped=skipped, skipped_rows=skipped_rows,
    )


async def remember_recipients(db: AsyncSession, *, owner_sub: str | None, addresses: list[str]) -> None:
    """Upsert 'auto' contacts for freshly-used recipient addresses. Best-effort."""
    try:
        for raw in addresses or []:
            name, email = _split(str(raw))
            email = email.lower()
            if not email or "@" not in email:
                continue
            existing = (
                await db.execute(
                    select(EmailContact).where(
                        EmailContact.email == email,
                        or_(EmailContact.owner_sub == owner_sub, EmailContact.owner_sub.is_(None)),
                    )
                )
            ).scalars().first()
            if existing:
                existing.use_count = (existing.use_count or 0) + 1
                existing.last_used_at = datetime.now(timezone.utc)
                if name and not existing.name:
                    existing.name = name
            else:
                db.add(EmailContact(
                    email=email, name=name or None, owner_sub=owner_sub, source="auto",
                    use_count=1, last_used_at=datetime.now(timezone.utc),
                ))
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("remember_recipients_failed", error=str(exc))
