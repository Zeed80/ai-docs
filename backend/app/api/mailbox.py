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

# ``assigned_role`` carries two different meanings in one column, and until Ф0.6
# it was an unvalidated free-text field:
#   * a UserRole — who gets notified about mail in this shared mailbox
#     (app.tasks.ingest._mailbox_recipients matches User.role against it);
#   * the sentinel "agent_ingress" — this mailbox is the agent's instruction
#     channel (app.tasks.ingest.poll_imap_mailbox).
# A typo used to silently disable notification routing (no user matches
# "accountnat", so it fell back to admins) or, worse, silently fail to turn a
# mailbox into an ingress one. Enumerated and validated here.
AGENT_INGRESS_ROLE = "agent_ingress"
ALLOWED_ASSIGNED_ROLES = {r.value for r in UserRole} | {AGENT_INGRESS_ROLE}


_TRIAGE_MODES = ("off", "classify", "full")


def _validate_triage_mode(value: str | None) -> str:
    if value in (None, ""):
        return "classify"
    if value not in _TRIAGE_MODES:
        raise HTTPException(
            422, f"agent_triage_mode должен быть одним из: {', '.join(_TRIAGE_MODES)}"
        )
    return value


def _validate_retention(value: int | None) -> int:
    """Days to keep message bodies. 0 = forever (the default).

    Capped at ten years, and negatives rejected outright: a negative window
    would put the cutoff in the future and erase the mailbox on the next run.
    """
    if value in (None, ""):
        return 0
    days = int(value)
    if days < 0 or days > 3650:
        raise HTTPException(422, "body_retention_days: 0 (хранить бессрочно) … 3650")
    return days


def _validate_assigned_role(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if value not in ALLOWED_ASSIGNED_ROLES:
        raise HTTPException(
            422,
            "assigned_role должен быть ролью пользователя "
            f"({', '.join(sorted(r.value for r in UserRole))}) или '{AGENT_INGRESS_ROLE}'",
        )
    return value


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
    # Only meaningful with assigned_role="agent_ingress" (see Ф0.6).
    ingress_allowed_senders: list[str] | None = None
    # Ф6.1 automation policy.
    auto_process_attachments: bool = True
    auto_approve_invoices: bool = False
    agent_triage_mode: str = "classify"
    body_retention_days: int = 0
    auto_send_enabled: bool | None = None
    auto_send_max_per_day: int | None = None
    max_attachment_mb: int | None = None
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
    ingress_allowed_senders: list[str] | None = None
    auto_process_attachments: bool | None = None
    auto_approve_invoices: bool | None = None
    agent_triage_mode: str | None = None
    body_retention_days: int | None = None
    auto_send_enabled: bool | None = None
    auto_send_max_per_day: int | None = None
    max_attachment_mb: int | None = None
    is_active: bool | None = None
    auth_method: str | None = (
        None  # "password" | "oauth2" — switching to "password" clears oauth_* fields
    )
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
    ingress_allowed_senders: list[str] | None = None
    auto_process_attachments: bool = True
    auto_approve_invoices: bool = False
    agent_triage_mode: str = "classify"
    body_retention_days: int = 0
    auto_send_enabled: bool | None = None
    auto_send_max_per_day: int | None = None
    max_attachment_mb: int | None = None
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
    # Ф9 — "логин прошёл" ≠ "письмо дойдёт": a mailbox can authenticate and
    # still have every outgoing message rejected by the relay.
    test_send_ok: bool | None = None
    test_send_error: str | None = None
    test_send_to: str | None = None


class MailboxSyncResult(BaseModel):
    task_id: str
    mailbox: str


async def _mailbox_stats(
    db: AsyncSession, names: list[str]
) -> dict[str, tuple[int, datetime | None]]:
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


def _to_out(
    cfg: MailboxConfig, stats: tuple[int, datetime | None] | None = None
) -> MailboxConfigOut:
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
        ingress_allowed_senders=cfg.ingress_allowed_senders,
        auto_process_attachments=cfg.auto_process_attachments,
        auto_approve_invoices=cfg.auto_approve_invoices,
        agent_triage_mode=cfg.agent_triage_mode,
        body_retention_days=cfg.body_retention_days,
        auto_send_enabled=cfg.auto_send_enabled,
        auto_send_max_per_day=cfg.auto_send_max_per_day,
        max_attachment_mb=cfg.max_attachment_mb,
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
        raise HTTPException(
            status_code=409, detail="Сессия подключения почты истекла, подключите заново"
        )
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


class FolderHealth(BaseModel):
    id: uuid.UUID
    remote_name: str
    local_folder: str | None
    sync_enabled: bool
    last_sync_at: datetime | None
    sync_error: str | None
    uid_validity: int | None


class MailboxHealth(BaseModel):
    name: str
    display_name: str | None
    is_active: bool
    last_sync_at: datetime | None
    sync_error: str | None
    messages: int
    unread: int
    attachments_without_bytes: int
    pending_sync_ops: int
    failed_sync_ops: int
    triage_mode: str
    # Ф6.8/Ф8 — доля разобранных агентом писем, которые человек потом исправил.
    # Это единственный честный ответ на «можно ли ему доверять этот ящик».
    triaged: int = 0
    triage_corrections: int = 0
    folders: list[FolderHealth] = []


@router.get("/health", response_model=list[MailboxHealth])
async def mailbox_health(
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> list[MailboxHealth]:
    """Ф8 — one screen that answers "работает ли почта".

    Every failure in this subsystem is quiet: a mailbox stops syncing, the
    write-back queue backs up, attachments point at bytes that were never
    written. None of it raises anywhere a person looks, so it needs somewhere
    to be looked at.
    """
    from sqlalchemy import func as _f

    from app.db.models import (
        EmailAttachment,
        EmailMessage,
        EmailSyncOp,
        EmailThread,
        EmailTriageResult,
        MailboxFolder,
    )
    from app.domain.imap_sync import decode_mailbox_name

    configs = (await db.execute(select(MailboxConfig).order_by(MailboxConfig.name))).scalars().all()
    if not configs:
        return []
    names = [c.name for c in configs]

    def _counts(rows):
        return {name: int(n) for name, n in rows}

    messages = _counts(
        (
            await db.execute(
                select(EmailMessage.mailbox, _f.count(EmailMessage.id))
                .where(EmailMessage.mailbox.in_(names))
                .group_by(EmailMessage.mailbox)
            )
        ).all()
    )
    unread = _counts(
        (
            await db.execute(
                select(EmailThread.mailbox, _f.count(EmailThread.id))
                .where(EmailThread.mailbox.in_(names), EmailThread.is_read == False)  # noqa: E712
                .group_by(EmailThread.mailbox)
            )
        ).all()
    )
    orphaned = _counts(
        (
            await db.execute(
                select(EmailMessage.mailbox, _f.count(EmailAttachment.id))
                .join(EmailMessage, EmailAttachment.message_id == EmailMessage.id)
                .where(EmailMessage.mailbox.in_(names), EmailAttachment.storage_path.is_(None))
                .group_by(EmailMessage.mailbox)
            )
        ).all()
    )
    pending = _counts(
        (
            await db.execute(
                select(EmailSyncOp.mailbox, _f.count(EmailSyncOp.id))
                .where(EmailSyncOp.mailbox.in_(names), EmailSyncOp.state == "pending")
                .group_by(EmailSyncOp.mailbox)
            )
        ).all()
    )
    failed = _counts(
        (
            await db.execute(
                select(EmailSyncOp.mailbox, _f.count(EmailSyncOp.id))
                .where(EmailSyncOp.mailbox.in_(names), EmailSyncOp.state == "failed")
                .group_by(EmailSyncOp.mailbox)
            )
        ).all()
    )

    triaged = _counts(
        (
            await db.execute(
                select(EmailTriageResult.mailbox, _f.count(EmailTriageResult.id))
                .where(EmailTriageResult.mailbox.in_(names))
                .group_by(EmailTriageResult.mailbox)
            )
        ).all()
    )
    corrections = _counts(
        (
            await db.execute(
                select(EmailTriageResult.mailbox, _f.count(EmailTriageResult.id))
                .where(
                    EmailTriageResult.mailbox.in_(names),
                    EmailTriageResult.corrected_category.isnot(None),
                )
                .group_by(EmailTriageResult.mailbox)
            )
        ).all()
    )

    folder_rows = (
        (
            await db.execute(
                select(MailboxFolder)
                .where(MailboxFolder.mailbox.in_(names))
                .order_by(MailboxFolder.local_folder.nullslast(), MailboxFolder.remote_name)
            )
        )
        .scalars()
        .all()
    )
    folders_by_mailbox: dict[str, list[FolderHealth]] = {}
    for row in folder_rows:
        folders_by_mailbox.setdefault(row.mailbox, []).append(
            FolderHealth(
                id=row.id,
                # Имена приходят в modified UTF-7: «Корзина» это
                # &BBoEPgRABDcEOAQ9BDA- — в списке папок это нечитаемо.
                remote_name=decode_mailbox_name(row.remote_name),
                local_folder=row.local_folder,
                sync_enabled=row.sync_enabled,
                last_sync_at=row.last_sync_at,
                sync_error=row.sync_error,
                uid_validity=row.uid_validity,
            )
        )

    return [
        MailboxHealth(
            name=c.name,
            display_name=c.display_name,
            is_active=c.is_active,
            last_sync_at=c.last_sync_at,
            sync_error=c.sync_error,
            messages=messages.get(c.name, 0),
            unread=unread.get(c.name, 0),
            attachments_without_bytes=orphaned.get(c.name, 0),
            pending_sync_ops=pending.get(c.name, 0),
            failed_sync_ops=failed.get(c.name, 0),
            triage_mode=c.agent_triage_mode,
            triaged=triaged.get(c.name, 0),
            triage_corrections=corrections.get(c.name, 0),
            folders=folders_by_mailbox.get(c.name, []),
        )
        for c in configs
    ]


class FolderMappingUpdate(BaseModel):
    """Ф2.1 — «маппинг серверная папка → наша настраивается в UI ящика».

    Автоопределение закрывает обычные случаи (SPECIAL-USE, привычные имена,
    подпапки INBOX), но провайдеры раскладывают почту как хотят, и папку,
    которую мы не узнали, должен уметь назначить человек.
    """

    local_folder: str | None = None
    sync_enabled: bool | None = None


_LOCAL_FOLDERS = ("inbox", "sent", "drafts", "trash", "spam", "archive")


@router.patch("/folders/{folder_id}", response_model=FolderHealth)
async def update_folder_mapping(
    folder_id: uuid.UUID,
    payload: FolderMappingUpdate,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> FolderHealth:
    """Skill: mailbox.map_folder — Map a server folder to one of ours / toggle sync."""
    from app.db.models import MailboxFolder

    row = await db.get(MailboxFolder, folder_id)
    if not row:
        raise HTTPException(404, "Папка не найдена")

    if "local_folder" in payload.model_fields_set:
        value = payload.local_folder or None
        if value is not None and value not in _LOCAL_FOLDERS:
            raise HTTPException(422, f"local_folder: {', '.join(_LOCAL_FOLDERS)} или пусто")
        row.local_folder = value
        if value is None:
            # Nowhere to put its messages — syncing it would be a no-op that
            # looks like it works.
            row.sync_enabled = False
    if payload.sync_enabled is not None:
        if payload.sync_enabled and not row.local_folder:
            raise HTTPException(422, "Сначала укажите, в какую нашу папку складывать письма")
        row.sync_enabled = payload.sync_enabled

    await db.commit()
    await db.refresh(row)
    logger.info(
        "mailbox_folder_mapped",
        mailbox=row.mailbox,
        remote=row.remote_name,
        local=row.local_folder,
        sync=row.sync_enabled,
    )
    return FolderHealth(
        id=row.id,
        remote_name=row.remote_name,
        local_folder=row.local_folder,
        sync_enabled=row.sync_enabled,
        last_sync_at=row.last_sync_at,
        sync_error=row.sync_error,
        uid_validity=row.uid_validity,
    )


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
            oauth_configured=p.oauth_provider in configured_providers
            if p.oauth_provider
            else False,
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
    db.add(
        AuditLog(
            user_id=current_user.sub,
            action="mailbox.sweep_consent",
            entity_type="mailbox",
            details={"address": cfg.name, "sweep_enabled": payload.sweep_enabled},
        )
    )
    await db.commit()
    logger.info("mailbox_sweep_consent", user=current_user.sub, enabled=payload.sweep_enabled)
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
    logger.info(
        "mailbox_sync_triggered_self", name=cfg.name, user=current_user.sub, task_id=task.id
    )
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
    existing = await db.execute(select(MailboxConfig).where(MailboxConfig.name == payload.name))
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
        smtp_password_encrypted=encrypt_password(payload.smtp_password)
        if payload.smtp_password
        else None,
        smtp_use_tls=payload.smtp_use_tls,
        smtp_from_address=payload.smtp_from_address,
        smtp_from_name=payload.smtp_from_name,
        default_doc_type=payload.default_doc_type,
        assigned_role=_validate_assigned_role(payload.assigned_role),
        ingress_allowed_senders=payload.ingress_allowed_senders,
        auto_process_attachments=payload.auto_process_attachments,
        auto_approve_invoices=payload.auto_approve_invoices,
        agent_triage_mode=_validate_triage_mode(payload.agent_triage_mode),
        body_retention_days=_validate_retention(payload.body_retention_days),
        auto_send_enabled=payload.auto_send_enabled,
        auto_send_max_per_day=payload.auto_send_max_per_day,
        max_attachment_mb=payload.max_attachment_mb,
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
    # Ф9 — for the tri-state overrides NULL is a real value ("наследовать
    # общую политику"), and exclude_none would make it impossible to ever set
    # one back. Anything the client explicitly sent stays, null included.
    for field in ("auto_send_enabled", "auto_send_max_per_day", "max_attachment_mb"):
        if field in payload.model_fields_set:
            update_data[field] = getattr(payload, field)
    if "assigned_role" in update_data:
        update_data["assigned_role"] = _validate_assigned_role(update_data["assigned_role"])
    if "agent_triage_mode" in update_data:
        update_data["agent_triage_mode"] = _validate_triage_mode(update_data["agent_triage_mode"])
    if "body_retention_days" in update_data:
        # Ф8 — shortening retention destroys correspondence on the next nightly
        # run, so it is logged as an explicit decision with who made it.
        new_days = _validate_retention(update_data["body_retention_days"])
        if new_days != cfg.body_retention_days:
            logger.warning(
                "mailbox_body_retention_changed",
                mailbox=cfg.name,
                before=cfg.body_retention_days,
                after=new_days,
                actor=getattr(_admin, "sub", None),
            )
        update_data["body_retention_days"] = new_days
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
            name=cfg.name,
            provider=cfg.oauth_provider,
            reason=reason,
        )

    await db.delete(cfg)
    await db.commit()


@router.post("/configs/{mailbox_id}/test", response_model=MailboxTestResult)
async def test_mailbox(
    mailbox_id: uuid.UUID,
    send_test_to: str | None = None,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> MailboxTestResult:
    """Skill: mailbox.test — Test IMAP and SMTP connectivity.

    With ``send_test_to`` it also delivers a real message through this
    mailbox's SMTP: authentication succeeding tells you nothing about whether
    the relay will accept a message from this address.
    """
    cfg = await db.get(MailboxConfig, mailbox_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Mailbox not found")

    imap_ok = False
    imap_error: str | None = None
    smtp_ok: bool | None = None
    smtp_error: str | None = None
    message_count: int | None = None
    test_send_ok: bool | None = None
    test_send_error: str | None = None
    test_send_to: str | None = None

    access_token: str | None = None
    if cfg.auth_method == "oauth2":
        from app.domain import oauth_mail

        try:
            access_token = await oauth_mail.get_valid_access_token(db, cfg)
        except (oauth_mail.MailboxNotOAuthConnected, oauth_mail.OAuthAppNotConfigured) as e:
            return MailboxTestResult(imap_ok=False, smtp_ok=None, imap_error=str(e))
        except Exception as e:
            return MailboxTestResult(
                imap_ok=False, smtp_ok=None, imap_error=f"Не удалось обновить OAuth-токен: {e}"
            )

    # Test IMAP
    try:
        import imaplib

        from app.config import settings as _settings

        # SMTP below already had a timeout; IMAP did not, so "проверить
        # подключение" could hang the request indefinitely.
        _t = _settings.imap_timeout_seconds
        if cfg.imap_ssl:
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=_t)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=_t)
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
            import ssl

            if cfg.smtp_use_tls:
                srv = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port or 587, timeout=10)
                # Explicit verifying context — matches the real send path in
                # app/tasks/email_sender.py instead of relying on the default.
                srv.starttls(context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port or 465, timeout=10)
            if access_token:
                from app.domain.oauth_mail import xoauth2_base64

                code, resp = srv.docmd(
                    "AUTH", "XOAUTH2 " + xoauth2_base64(cfg.smtp_user, access_token)
                )
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, resp)
            else:
                smtp_password = decrypt_password(cfg.smtp_password_encrypted or "")
                srv.login(cfg.smtp_user, smtp_password)
            if send_test_to:
                test_send_to = send_test_to
                try:
                    from email.message import EmailMessage as _Msg
                    from email.utils import formatdate, make_msgid

                    msg = _Msg()
                    msg["From"] = cfg.smtp_from_address or cfg.smtp_user
                    msg["To"] = send_test_to
                    msg["Subject"] = f"Проверка ящика «{cfg.display_name or cfg.name}»"
                    msg["Date"] = formatdate(localtime=True)
                    msg["Message-ID"] = make_msgid()
                    # An automated probe must say so, or it comes back through
                    # the rules engine as a letter to answer.
                    msg["Auto-Submitted"] = "auto-generated"
                    msg.set_content(
                        "Это тестовое письмо из рабочего пространства.\n"
                        f"Ящик: {cfg.name} · SMTP: {cfg.smtp_host}:{cfg.smtp_port}\n"
                        "Если письмо пришло — отправка работает."
                    )
                    srv.send_message(msg)
                    test_send_ok = True
                except Exception as e:
                    test_send_ok = False
                    test_send_error = str(e)
                    logger.warning("mailbox_test_send_failed", name=cfg.name, error=str(e))
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
        test_send_ok=test_send_ok,
        test_send_error=test_send_error,
        test_send_to=test_send_to,
    )
