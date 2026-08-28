"""Mailbox configuration API — CRUD + connection test."""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.acting import get_effective_user
from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.db.models import AuditLog, EmailMessage, MailboxConfig, OAuthAppConfig
from app.db.session import get_db
from app.domain.admin import UserMailboxOut, UserMailboxSweepUpdate
from app.domain.mailbox_presets import PRESETS
from app.utils.crypto import decrypt_password, encrypt_password

router = APIRouter()
logger = structlog.get_logger()

# Shared mailboxes (procurement/accounting/general) are org infrastructure: their
# credentials decide which address the company sends from and whose inbox the
# agent reads. The router is mounted with authentication only, so without this
# every logged-in user — viewer included — could repoint the procurement mailbox
# at their own account. Personal mailboxes stay self-service through /me below.
_admin_dep = Depends(require_role(UserRole.admin))


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class MailboxConfigCreate(BaseModel):
    name: str
    display_name: str | None = None
    imap_host: str
    imap_port: int = 993
    imap_user: str
    imap_password: str = ""  # unused (but still required column) when auth_method="oauth2"
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
    # OAuth2: the one-time session id from GET /api/oauth/pending/{session}
    # (issued after a successful consent popup, see app/api/oauth.py) — its
    # tokens get attached to this mailbox instead of imap_password/smtp_password.
    oauth_session: str | None = None


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
    auth_method: str | None = None  # "password" | "oauth2" — switching to "password" clears oauth_* fields
    oauth_session: str | None = None


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
    smtp_user: str | None
    smtp_use_tls: bool
    smtp_from_address: str | None
    smtp_from_name: str | None
    default_doc_type: str | None
    assigned_role: str | None
    is_active: bool
    last_sync_at: datetime | None
    sync_error: str | None
    created_at: datetime
    updated_at: datetime
    auth_method: str
    oauth_provider: str | None
    oauth_email: str | None
    oauth_connected: bool
    message_count: int = 0
    last_message_at: datetime | None = None

    model_config = {"from_attributes": True}


class MailboxTestResult(BaseModel):
    imap_ok: bool
    smtp_ok: bool | None
    imap_error: str | None = None
    smtp_error: str | None = None
    message_count: int | None = None


class MailboxSyncResult(BaseModel):
    task_id: str
    mailbox: str


async def _mailbox_stats(db: AsyncSession, names: list[str]) -> dict[str, tuple[int, datetime | None]]:
    """COUNT(*) and MAX(received_at) of EmailMessage per mailbox name."""
    if not names:
        return {}
    rows = (
        await db.execute(
            select(
                EmailMessage.mailbox,
                func.count(EmailMessage.id),
                func.max(EmailMessage.received_at),
            )
            .where(EmailMessage.mailbox.in_(names))
            .group_by(EmailMessage.mailbox)
        )
    ).all()
    return {r[0]: (int(r[1] or 0), r[2]) for r in rows}


class MailboxPresetOut(BaseModel):
    id: str
    label: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool
    auth_methods: list[str]
    oauth_provider: str | None
    oauth_configured: bool  # whether an admin has set up this provider's Client ID/Secret
    hint: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_out(cfg: MailboxConfig, stats: tuple[int, datetime | None] | None = None) -> MailboxConfigOut:
    return MailboxConfigOut(
        message_count=(stats or (0, None))[0],
        last_message_at=(stats or (0, None))[1],
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
        smtp_user=cfg.smtp_user,
        smtp_use_tls=cfg.smtp_use_tls,
        smtp_from_address=cfg.smtp_from_address,
        smtp_from_name=cfg.smtp_from_name,
        default_doc_type=cfg.default_doc_type,
        assigned_role=cfg.assigned_role,
        is_active=cfg.is_active,
        last_sync_at=cfg.last_sync_at,
        sync_error=cfg.sync_error,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
        auth_method=cfg.auth_method,
        oauth_provider=cfg.oauth_provider,
        oauth_email=cfg.oauth_email,
        oauth_connected=bool(cfg.oauth_refresh_token_encrypted),
    )


async def _attach_oauth_session(db: AsyncSession, cfg: MailboxConfig, session: str) -> None:
    """Copy a just-finished consent result (see app/api/oauth.py) onto `cfg`."""
    from app.api.oauth import consume_pending_result

    data = await consume_pending_result(db, session)
    if not data:
        raise HTTPException(status_code=409, detail="Сессия подключения почты истекла, подключите заново")
    cfg.auth_method = "oauth2"
    cfg.oauth_provider = data["provider"]
    cfg.oauth_refresh_token_encrypted = data["refresh_token_encrypted"]
    cfg.oauth_access_token_encrypted = data["access_token_encrypted"]
    cfg.oauth_token_expires_at = datetime.fromisoformat(data["expires_at"])
    cfg.oauth_scope = data.get("scope")
    cfg.oauth_email = data.get("email")
    if data.get("email"):
        if not cfg.imap_user:
            cfg.imap_user = data["email"]
        if not cfg.smtp_user:
            cfg.smtp_user = data["email"]
            cfg.smtp_from_address = cfg.smtp_from_address or data["email"]


# ── Provider presets ──────────────────────────────────────────────────────────


@router.get("/presets", response_model=list[MailboxPresetOut])
async def list_presets(db: AsyncSession = Depends(get_db)) -> list[MailboxPresetOut]:
    """Skill: mailbox.presets — Known providers with autofill + auth hints."""
    configured_providers = {
        row.provider
        for row in (await db.execute(select(OAuthAppConfig))).scalars().all()
        if row.client_id and row.client_secret_encrypted and row.redirect_uri
    }
    return [
        MailboxPresetOut(
            id=p.id,
            label=p.label,
            imap_host=p.imap_host,
            imap_port=p.imap_port,
            imap_ssl=p.imap_ssl,
            smtp_host=p.smtp_host,
            smtp_port=p.smtp_port,
            smtp_use_tls=p.smtp_use_tls,
            auth_methods=list(p.auth_methods),
            oauth_provider=p.oauth_provider,
            oauth_configured=p.oauth_provider in configured_providers if p.oauth_provider else False,
            hint=p.hint,
        )
        for p in PRESETS
    ]


# ── Self-service: "my" personal mailbox ─────────────────────────────────────

@router.get("/me", response_model=UserMailboxOut)
async def get_my_mailbox(
    current_user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> UserMailboxOut:
    """The caller's own @<domain> mailbox, if an admin has provisioned one.

    Personal mailboxes are provisioned by an admin (POST
    /api/admin/users/{sub}/mailbox) — this endpoint is read-only self-service
    for /settings to show the address, not a way to create one.
    """
    cfg = await _my_personal_mailbox(db, current_user.sub)
    if cfg is None:
        return UserMailboxOut()
    return await _mailbox_out(cfg)


@router.patch("/me/sweep", response_model=UserMailboxOut)
async def set_my_mailbox_sweep(
    payload: UserMailboxSweepUpdate,
    current_user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> UserMailboxOut:
    """Owner's consent switch: let the AI read this mailbox, or stop it.

    Deliberately self-service and revocable at any moment — the person whose
    private correspondence it is decides, not only the admin who issued the
    mailbox. Off by default (see the provisioning endpoint).
    """
    cfg = await _my_personal_mailbox(db, current_user.sub)
    if cfg is None:
        raise HTTPException(status_code=404, detail="У вас нет личного почтового ящика")

    cfg.sweep_enabled = payload.sweep_enabled
    db.add(AuditLog(
        user_id=current_user.sub,
        action="mailbox.sweep_consent",
        entity_type="mailbox",
        details={"address": cfg.name, "sweep_enabled": payload.sweep_enabled},
    ))
    await db.commit()
    logger.info(
        "mailbox_sweep_consent", user=current_user.sub, enabled=payload.sweep_enabled
    )
    return await _mailbox_out(cfg)


@router.post("/me/sync", response_model=MailboxSyncResult)
async def sync_my_mailbox(
    current_user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> MailboxSyncResult:
    """Owner-triggered immediate poll of their own personal mailbox."""
    cfg = await _my_personal_mailbox(db, current_user.sub)
    if cfg is None:
        raise HTTPException(status_code=404, detail="У вас нет личного почтового ящика")
    from app.tasks.ingest import poll_imap_mailbox

    task = poll_imap_mailbox.delay(cfg.name)
    logger.info("mailbox_sync_triggered_self", name=cfg.name, user=current_user.sub, task_id=task.id)
    return MailboxSyncResult(task_id=task.id, mailbox=cfg.name)


async def _my_personal_mailbox(db: AsyncSession, sub: str) -> MailboxConfig | None:
    return (
        await db.execute(
            select(MailboxConfig).where(
                MailboxConfig.owner_sub == sub,
                MailboxConfig.mailbox_type == "personal",
                MailboxConfig.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()


async def _mailbox_out(cfg: MailboxConfig) -> UserMailboxOut:
    from app.services.integration_config import get_mail_server_config

    mail_cfg = await get_mail_server_config()
    return UserMailboxOut(
        address=cfg.name,
        is_active=cfg.is_active,
        webmail_url=mail_cfg.webmail_url,
        last_sync_at=cfg.last_sync_at,
        sync_error=cfg.sync_error,
        sweep_enabled=cfg.sweep_enabled,
        quota_mb=cfg.quota_mb,
    )


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/configs", response_model=MailboxConfigOut, status_code=201)
async def create_mailbox(
    payload: MailboxConfigCreate,
    _admin: UserInfo = _admin_dep,
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
    if payload.oauth_session:
        await _attach_oauth_session(db, cfg, payload.oauth_session)
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    logger.info("mailbox_created", name=cfg.name, auth_method=cfg.auth_method)
    return _to_out(cfg)


@router.get("/configs", response_model=list[MailboxConfigOut])
async def list_mailboxes(
    active_only: bool = False,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> list[MailboxConfigOut]:
    """Skill: mailbox.list — List all configured mailboxes."""
    q = select(MailboxConfig).order_by(MailboxConfig.name)
    if active_only:
        q = q.where(MailboxConfig.is_active == True)  # noqa: E712
    result = await db.execute(q)
    cfgs = list(result.scalars().all())
    stats = await _mailbox_stats(db, [c.name for c in cfgs])
    return [_to_out(cfg, stats.get(cfg.name)) for cfg in cfgs]


@router.get("/configs/{mailbox_id}", response_model=MailboxConfigOut)
async def get_mailbox(
    mailbox_id: uuid.UUID,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> MailboxConfigOut:
    """Skill: mailbox.get — Get a mailbox configuration by ID."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    stats = await _mailbox_stats(db, [cfg.name])
    return _to_out(cfg, stats.get(cfg.name))


@router.post("/configs/{mailbox_id}/sync", response_model=MailboxSyncResult)
async def sync_mailbox(
    mailbox_id: uuid.UUID,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> MailboxSyncResult:
    """Skill: mailbox.sync — Trigger an immediate IMAP poll for this mailbox."""
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    from app.tasks.ingest import poll_imap_mailbox

    task = poll_imap_mailbox.delay(cfg.name)
    logger.info("mailbox_sync_triggered", name=cfg.name, task_id=task.id)
    return MailboxSyncResult(task_id=task.id, mailbox=cfg.name)


@router.patch("/configs/{mailbox_id}", response_model=MailboxConfigOut)
async def update_mailbox(
    mailbox_id: uuid.UUID,
    payload: MailboxConfigUpdate,
    _admin: UserInfo = _admin_dep,
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
    oauth_session = update_data.pop("oauth_session", None)
    if oauth_session:
        await _attach_oauth_session(db, cfg, oauth_session)
    if update_data.get("auth_method") == "password":
        # Reverting to password auth — the old refresh token is now stale,
        # don't leave it lying around encrypted in the row.
        cfg.oauth_provider = None
        cfg.oauth_refresh_token_encrypted = None
        cfg.oauth_access_token_encrypted = None
        cfg.oauth_token_expires_at = None
        cfg.oauth_scope = None
        cfg.oauth_email = None
    for key, value in update_data.items():
        setattr(cfg, key, value)

    await db.commit()
    await db.refresh(cfg)
    return _to_out(cfg)


@router.delete("/configs/{mailbox_id}", status_code=204)
async def delete_mailbox(
    mailbox_id: uuid.UUID,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Skill: mailbox.delete — Remove a mailbox configuration."""
    from app.domain.oauth_mail import revoke_tokens

    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    # Ask the provider to invalidate the grant before we forget it: dropping
    # the row alone leaves a working refresh token on Google's side that
    # nobody can revoke any more, because we just deleted the only record of
    # it. Failure is logged, never fatal — the local delete must still happen.
    reason = await revoke_tokens(db, cfg)
    if reason:
        logger.warning(
            "mailbox_oauth_revoke_failed",
            name=cfg.name, provider=cfg.oauth_provider, reason=reason,
        )

    await db.delete(cfg)
    await db.commit()


@router.post("/configs/{mailbox_id}/test", response_model=MailboxTestResult)
async def test_mailbox(
    mailbox_id: uuid.UUID,
    _admin: UserInfo = _admin_dep,
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

    access_token: str | None = None
    if cfg.auth_method == "oauth2":
        from app.domain import oauth_mail
        try:
            access_token = await oauth_mail.get_valid_access_token(db, cfg)
        except (oauth_mail.MailboxNotOAuthConnected, oauth_mail.OAuthAppNotConfigured) as e:
            return MailboxTestResult(imap_ok=False, smtp_ok=None, imap_error=str(e))
        except Exception as e:
            return MailboxTestResult(imap_ok=False, smtp_ok=None, imap_error=f"Не удалось обновить OAuth-токен: {e}")

    # Test IMAP
    try:
        import imaplib
        if cfg.imap_ssl:
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        if access_token:
            from app.domain.oauth_mail import imap_xoauth2_authobject
            conn.authenticate("XOAUTH2", imap_xoauth2_authobject(cfg.imap_user, access_token))
        else:
            password = decrypt_password(cfg.imap_password_encrypted)
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
            if cfg.smtp_use_tls:
                srv = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port or 587, timeout=10)
                srv.starttls()
            else:
                srv = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port or 465, timeout=10)
            if access_token:
                from app.domain.oauth_mail import xoauth2_base64
                code, resp = srv.docmd("AUTH", "XOAUTH2 " + xoauth2_base64(cfg.smtp_user, access_token))
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, resp)
            else:
                smtp_password = decrypt_password(cfg.smtp_password_encrypted or "")
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
