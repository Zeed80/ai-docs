"""Mailbox configuration API — CRUD + connection test."""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MailboxConfig
from app.db.session import get_db
from app.utils.crypto import decrypt_password, encrypt_password

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class MailboxConfigCreate(BaseModel):
    name: str
    display_name: str | None = None
    imap_host: str
    imap_port: int = 993
    imap_user: str
    imap_password: str
    imap_ssl: bool = True
    imap_folder: str = "INBOX"
    smtp_host: str | None = None
    smtp_port: int | None = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str | None = None
    smtp_from_name: str | None = None
    default_doc_type: str | None = None
    assigned_role: str | None = None
    is_active: bool = True


class MailboxConfigUpdate(BaseModel):
    display_name: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_user: str | None = None
    imap_password: str | None = None
    imap_ssl: bool | None = None
    imap_folder: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool | None = None
    smtp_from_address: str | None = None
    smtp_from_name: str | None = None
    default_doc_type: str | None = None
    assigned_role: str | None = None
    is_active: bool | None = None


class MailboxConfigOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str | None
    imap_host: str
    imap_port: int
    imap_user: str
    imap_ssl: bool
    imap_folder: str
    smtp_host: str | None
    smtp_port: int | None
    smtp_from_address: str | None
    smtp_from_name: str | None
    default_doc_type: str | None
    assigned_role: str | None
    is_active: bool
    last_sync_at: datetime | None
    sync_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MailboxTestResult(BaseModel):
    imap_ok: bool
    smtp_ok: bool | None
    imap_error: str | None = None
    smtp_error: str | None = None
    message_count: int | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_out(cfg: MailboxConfig) -> MailboxConfigOut:
    return MailboxConfigOut(
        id=cfg.id,
        name=cfg.name,
        display_name=cfg.display_name,
        imap_host=cfg.imap_host,
        imap_port=cfg.imap_port,
        imap_user=cfg.imap_user,
        imap_ssl=cfg.imap_ssl,
        imap_folder=cfg.imap_folder,
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_from_address=cfg.smtp_from_address,
        smtp_from_name=cfg.smtp_from_name,
        default_doc_type=cfg.default_doc_type,
        assigned_role=cfg.assigned_role,
        is_active=cfg.is_active,
        last_sync_at=cfg.last_sync_at,
        sync_error=cfg.sync_error,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/configs", response_model=MailboxConfigOut, status_code=201)
async def create_mailbox(
    payload: MailboxConfigCreate,
    db: AsyncSession = Depends(get_db),
) -> MailboxConfigOut:
    """Skill: mailbox.create — Add a new IMAP/SMTP mailbox configuration."""
    existing = await db.execute(
        select(MailboxConfig).where(MailboxConfig.name == payload.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Mailbox '{payload.name}' already exists")

    cfg = MailboxConfig(
        name=payload.name,
        display_name=payload.display_name,
        imap_host=payload.imap_host,
        imap_port=payload.imap_port,
        imap_user=payload.imap_user,
        imap_password_encrypted=encrypt_password(payload.imap_password),
        imap_ssl=payload.imap_ssl,
        imap_folder=payload.imap_folder,
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        smtp_user=payload.smtp_user,
        smtp_password_encrypted=encrypt_password(payload.smtp_password) if payload.smtp_password else None,
        smtp_use_tls=payload.smtp_use_tls,
        smtp_from_address=payload.smtp_from_address,
        smtp_from_name=payload.smtp_from_name,
        default_doc_type=payload.default_doc_type,
        assigned_role=payload.assigned_role,
        is_active=payload.is_active,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    logger.info("mailbox_created", name=cfg.name)
    return _to_out(cfg)


@router.get("/configs", response_model=list[MailboxConfigOut])
async def list_mailboxes(
    active_only: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[MailboxConfigOut]:
    """Skill: mailbox.list — List all configured mailboxes."""
    q = select(MailboxConfig).order_by(MailboxConfig.name)
    if active_only:
        q = q.where(MailboxConfig.is_active == True)  # noqa: E712
    result = await db.execute(q)
    return [_to_out(cfg) for cfg in result.scalars().all()]


@router.get("/configs/{mailbox_id}", response_model=MailboxConfigOut)
async def get_mailbox(
    mailbox_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> MailboxConfigOut:
    """Skill: mailbox.get — Get a mailbox configuration by ID."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    return _to_out(cfg)


@router.patch("/configs/{mailbox_id}", response_model=MailboxConfigOut)
async def update_mailbox(
    mailbox_id: uuid.UUID,
    payload: MailboxConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> MailboxConfigOut:
    """Skill: mailbox.update — Update mailbox settings."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    update_data = payload.model_dump(exclude_none=True)
    if "imap_password" in update_data:
        cfg.imap_password_encrypted = encrypt_password(update_data.pop("imap_password"))
    if "smtp_password" in update_data:
        cfg.smtp_password_encrypted = encrypt_password(update_data.pop("smtp_password"))
    for key, value in update_data.items():
        setattr(cfg, key, value)

    await db.commit()
    await db.refresh(cfg)
    return _to_out(cfg)


@router.delete("/configs/{mailbox_id}", status_code=204)
async def delete_mailbox(
    mailbox_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Skill: mailbox.delete — Remove a mailbox configuration."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    await db.delete(cfg)
    await db.commit()


@router.post("/configs/{mailbox_id}/test", response_model=MailboxTestResult)
async def test_mailbox(
    mailbox_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> MailboxTestResult:
    """Skill: mailbox.test — Test IMAP and SMTP connectivity."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    imap_ok = False
    imap_error: str | None = None
    smtp_ok: bool | None = None
    smtp_error: str | None = None
    message_count: int | None = None

    # Test IMAP
    try:
        import imaplib
        password = decrypt_password(cfg.imap_password_encrypted)
        if cfg.imap_ssl:
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        conn.login(cfg.imap_user, password)
        status, data = conn.select(cfg.imap_folder, readonly=True)
        if status == "OK" and data:
            message_count = int(data[0]) if data[0] else 0
        conn.logout()
        imap_ok = True
    except Exception as e:
        imap_error = str(e)
        logger.warning("mailbox_imap_test_failed", name=cfg.name, error=str(e))

    # Test SMTP (only if configured)
    if cfg.smtp_host and cfg.smtp_user:
        try:
            import smtplib
            smtp_password = decrypt_password(cfg.smtp_password_encrypted or "")
            if cfg.smtp_use_tls:
                srv = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port or 587, timeout=10)
                srv.starttls()
            else:
                srv = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port or 465, timeout=10)
            srv.login(cfg.smtp_user, smtp_password)
            srv.quit()
            smtp_ok = True
        except Exception as e:
            smtp_ok = False
            smtp_error = str(e)
            logger.warning("mailbox_smtp_test_failed", name=cfg.name, error=str(e))

    return MailboxTestResult(
        imap_ok=imap_ok,
        smtp_ok=smtp_ok,
        imap_error=imap_error,
        smtp_error=smtp_error,
        message_count=message_count,
    )
