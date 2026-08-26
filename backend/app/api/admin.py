"""Admin API — user management, audit logs, API keys, system status."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user, require_role
from app.auth.models import ROLE_PERMISSIONS, UserInfo, UserRole
from app.db.models import ApiKey, AuditLog, Department, MailboxConfig, User
from app.db.session import get_db
from app.utils.crypto import encrypt_password
from app.domain.admin import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyListResponse,
    ApiKeyOut,
    AuditLogListResponse,
    AuditLogOut,
    DepartmentCreate,
    DepartmentListResponse,
    DepartmentOut,
    DepartmentUpdate,
    IntegrationAuthentikOut,
    IntegrationAuthentikUpdate,
    IntegrationMailServerOut,
    IntegrationMailServerSaved,
    IntegrationMailServerUpdate,
    IntegrationTestResult,
    OAuthAppOut,
    OAuthAppUpdate,
    PermissionMatrixOut,
    SetPasswordRequest,
    SystemStatusOut,
    UserCreate,
    UserListResponse,
    UserMailboxCreate,
    UserMailboxOut,
    UserMailboxProvisionedOut,
    UserMailboxRevoke,
    UserMailboxSweepUpdate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = structlog.get_logger()

_admin_dep = Depends(require_role(UserRole.admin))


# ── User management ───────────────────────────────────────────────────────────


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """Pre-provision a user: create in our DB and in Authentik (if API token is set)."""
    import uuid as _uuid

    from app.config import settings

    valid_roles = {r.value for r in UserRole}
    if payload.role not in valid_roles:
        raise HTTPException(status_code=422, detail=f"Invalid role: {payload.role}")

    if payload.password and len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    existing = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    preferred_username = payload.preferred_username or payload.email.split("@")[0]

    # Try to provision in Authentik first (get real sub or fall back to local:UUID)
    authentik_pk: int | None = None
    sub = f"local:{_uuid.uuid4()}"
    from app.services.integration_config import get_authentik_token
    if settings.auth_enabled and get_authentik_token():
        try:
            from app.services.authentik_api import provision_user
            authentik_pk = await provision_user(
                email=payload.email,
                username=preferred_username,
                name=payload.name,
                password=payload.password or None,
            )
            # Authentik uses hashed sub — we store the PK as local ref for now;
            # the real `sub` gets updated on first OIDC login via upsert_user.
            sub = f"authentik:{authentik_pk}"
        except Exception as exc:
            logger.warning("authentik_provision_failed", error=str(exc))
            # Continue — user is created locally, will sync on first login

    user = User(
        sub=sub,
        email=payload.email,
        name=payload.name,
        preferred_username=preferred_username,
        role=payload.role,
        is_active=True,
    )
    db.add(user)

    log = AuditLog(
        user_id=admin.sub,
        action="admin.create_user",
        entity_type="user",
        details={
            "email": payload.email,
            "role": payload.role,
            "authentik_pk": authentik_pk,
        },
    )
    db.add(log)
    await db.commit()
    await db.refresh(user)

    logger.info("admin_create_user", admin=admin.sub, email=payload.email, authentik_pk=authentik_pk)
    return UserOut.model_validate(user)


@router.post("/users/{user_sub}/set-password", status_code=204)
async def set_user_password(
    user_sub: str,
    payload: SetPasswordRequest,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Set or reset password for a user via Authentik API."""
    from app.config import settings
    from app.services.integration_config import get_authentik_token

    if not settings.auth_enabled or not get_authentik_token():
        raise HTTPException(status_code=503, detail="Authentik API not configured")

    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    from app.services.authentik_api import find_user_by_email, set_password

    authentik_pk = None
    # For users with sub=authentik:{pk} we have the PK already
    if user_sub.startswith("authentik:"):
        try:
            authentik_pk = int(user_sub.split(":", 1)[1])
        except ValueError:
            pass
    if authentik_pk is None:
        authentik_pk = await find_user_by_email(user.email)

    if authentik_pk is None:
        raise HTTPException(status_code=404, detail="User not found in Authentik; they must log in via SSO first")

    try:
        await set_password(authentik_pk, payload.password)
    except Exception as exc:
        logger.error("set_password_failed", error=str(exc), target=user_sub)
        raise HTTPException(status_code=502, detail=f"Authentik API error: {exc}")

    log = AuditLog(
        user_id=admin.sub,
        action="admin.set_password",
        entity_type="user",
        details={"target_sub": user_sub},
    )
    db.add(log)
    await db.commit()
    logger.info("admin_set_password", admin=admin.sub, target=user_sub)


# ── Personal mailbox provisioning ───────────────────────────────────────────
# A personal @<domain> mailbox is a MailboxConfig row with owner_sub set and
# mailbox_type="personal" (backend/app/db/models.py) — it's swept by the same
# triage sweep as shared mailboxes (procurement/accounting/…) and works with
# every existing email skill unmodified. Provisioning is admin-only, direct
# (not agent-gated) — same trust level as set_user_password above.

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _suggest_local_part(user: User) -> str:
    import re

    source = user.preferred_username or user.name or user.email.split("@")[0]
    translit = "".join(_TRANSLIT.get(ch, ch) for ch in source.lower())
    slug = re.sub(r"[^a-z0-9]+", ".", translit).strip(".")
    return slug or "user"


async def _personal_mailbox(db: AsyncSession, user_sub: str, *, active_only: bool = True):
    """The user's personal mailbox row.

    ``active_only`` matters: a revoked mailbox stays in the table (audit trail,
    and a deactivated Mailcow mailbox still exists server-side). Provisioning
    must not be blocked by such a row — it reuses or replaces it — while
    password reset and the self-service view must only ever see a live one.
    """
    conditions = [
        MailboxConfig.owner_sub == user_sub,
        MailboxConfig.mailbox_type == "personal",
    ]
    if active_only:
        conditions.append(MailboxConfig.is_active == True)  # noqa: E712
    return (await db.execute(select(MailboxConfig).where(*conditions))).scalar_one_or_none()


@router.get("/users/{user_sub}/mailbox", response_model=UserMailboxOut)
async def get_user_mailbox(
    user_sub: str,
    _admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserMailboxOut:
    cfg = await _personal_mailbox(db, user_sub)
    if cfg is None:
        return UserMailboxOut()
    from app.services.integration_config import get_mail_server_config

    mail_cfg = await get_mail_server_config()
    return UserMailboxOut(
        address=cfg.name, is_active=cfg.is_active, webmail_url=mail_cfg.webmail_url,
        last_sync_at=cfg.last_sync_at, sync_error=cfg.sync_error,
        sweep_enabled=cfg.sweep_enabled, quota_mb=cfg.quota_mb,
    )


@router.post("/users/{user_sub}/mailbox", response_model=UserMailboxProvisionedOut, status_code=201)
async def provision_user_mailbox(
    user_sub: str,
    payload: UserMailboxCreate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserMailboxProvisionedOut:
    """Create a personal @<domain> mailbox for a user via the Mailcow API."""
    from app.services import mailcow_api
    from app.services.integration_config import get_mail_server_config

    user = (await db.execute(select(User).where(User.sub == user_sub))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if await _personal_mailbox(db, user_sub) is not None:
        raise HTTPException(status_code=409, detail="У пользователя уже есть личный ящик")

    # A previously revoked mailbox leaves an inactive row behind; drop it so the
    # user can be issued a mailbox again (otherwise revocation was a one-way door).
    stale = await _personal_mailbox(db, user_sub, active_only=False)
    if stale is not None:
        await db.delete(stale)
        await db.flush()

    mail_cfg = await get_mail_server_config()
    if not mail_cfg.configured:
        raise HTTPException(status_code=503, detail="Mail server is not configured (см. /api/admin/integrations/mail-server)")

    local_part = (payload.local_part or _suggest_local_part(user)).strip().lower()
    if not local_part:
        raise HTTPException(status_code=422, detail="local_part is required")

    try:
        available = await mailcow_api.check_local_part_available(local_part, mail_cfg.mail_domain)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=mailcow_api.explain_api_failure(exc))
    if not available:
        raise HTTPException(status_code=409, detail=f"{local_part}@{mail_cfg.mail_domain} уже занят")

    full_address = f"{local_part}@{mail_cfg.mail_domain}"
    password = secrets.token_urlsafe(18)
    quota_mb = payload.quota_mb or mail_cfg.default_quota_mb

    try:
        await mailcow_api.create_mailbox(
            local_part=local_part, domain=mail_cfg.mail_domain, password=password,
            full_name=user.name or user.preferred_username, quota_mb=quota_mb,
        )
    except Exception as exc:
        logger.error("mailcow_provision_failed", error=str(exc), user_sub=user_sub)
        raise HTTPException(status_code=502, detail=mailcow_api.explain_api_failure(exc))

    cfg = MailboxConfig(
        name=full_address,
        display_name=f"{user.name} — личная почта",
        owner_sub=user_sub,
        mailbox_type="personal",
        imap_host=mail_cfg.imap_host,
        imap_port=mail_cfg.imap_port,
        imap_user=full_address,
        imap_password_encrypted=encrypt_password(password),
        imap_ssl=True,
        smtp_host=mail_cfg.smtp_host,
        smtp_port=mail_cfg.smtp_port,
        smtp_user=full_address,
        smtp_password_encrypted=encrypt_password(password),
        # 465 is the implicit-TLS/SMTPS submission port (SMTP_SSL from the
        # first byte) — STARTTLS must not be attempted there, or every send
        # fails with a handshake error. 587 (and anything else) is submission
        # with STARTTLS. The mail-server integration page only exposes a port,
        # not a TLS-mode toggle, so this has to be derived, not hardcoded.
        smtp_use_tls=mail_cfg.smtp_port != 465,
        smtp_from_address=full_address,
        smtp_from_name=user.name,
        is_active=True,
        quota_mb=quota_mb,
        # Consent, not a side effect: the AI does not read this mailbox until its
        # owner turns the sweep on in /settings (app/tasks/email_triage.py).
        sweep_enabled=False,
    )
    db.add(cfg)
    db.add(AuditLog(
        user_id=admin.sub, action="admin.provision_mailbox", entity_type="user",
        details={"target_sub": user_sub, "address": full_address, "quota_mb": quota_mb},
    ))
    await db.commit()
    logger.info("admin_provision_mailbox", admin=admin.sub, target=user_sub, address=full_address)
    return UserMailboxProvisionedOut(address=full_address, generated_password=password)


@router.post("/users/{user_sub}/mailbox/reset-password", response_model=UserMailboxProvisionedOut)
async def reset_user_mailbox_password(
    user_sub: str,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserMailboxProvisionedOut:
    from app.services import mailcow_api

    cfg = await _personal_mailbox(db, user_sub)
    if cfg is None:
        raise HTTPException(status_code=404, detail="У пользователя нет активного личного ящика")

    password = secrets.token_urlsafe(18)
    try:
        await mailcow_api.edit_mailbox_password(cfg.name, password)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=mailcow_api.explain_api_failure(exc))

    cfg.imap_password_encrypted = encrypt_password(password)
    cfg.smtp_password_encrypted = encrypt_password(password)
    db.add(AuditLog(
        user_id=admin.sub, action="admin.reset_mailbox_password", entity_type="user",
        details={"target_sub": user_sub, "address": cfg.name},
    ))
    await db.commit()
    logger.info("admin_reset_mailbox_password", admin=admin.sub, target=user_sub)
    return UserMailboxProvisionedOut(address=cfg.name, generated_password=password)


@router.post("/users/{user_sub}/mailbox/revoke", status_code=204)
async def revoke_user_mailbox(
    user_sub: str,
    payload: UserMailboxRevoke,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a personal mailbox.

    Two very different operations behind one word, so they are separated:
      * default — deactivate (Mailcow `active=0`, sweep off, our row inactive).
        Nothing is lost; the right move when someone leaves and their mail may
        still be needed.
      * delete_on_server=True — destroy the mailbox and every message in it.
        Irreversible, so the caller must echo the exact address.
    """
    from app.services import mailcow_api

    cfg = await _personal_mailbox(db, user_sub)
    if cfg is None:
        raise HTTPException(status_code=404, detail="У пользователя нет активного личного ящика")

    if payload.delete_on_server:
        if (payload.confirm_address or "").strip().lower() != cfg.name.lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Для безвозвратного удаления повторите адрес ящика в confirm_address. "
                    "Вся переписка будет уничтожена."
                ),
            )
        try:
            await mailcow_api.delete_mailbox(cfg.name)
        except Exception as exc:
            logger.warning("mailcow_delete_failed", error=str(exc), address=cfg.name)
            raise HTTPException(status_code=502, detail=mailcow_api.explain_api_failure(exc))
        action = "admin.delete_mailbox"
    else:
        try:
            await mailcow_api.set_mailbox_active(cfg.name, active=False)
        except Exception as exc:
            logger.warning("mailcow_deactivate_failed", error=str(exc), address=cfg.name)
            raise HTTPException(status_code=502, detail=mailcow_api.explain_api_failure(exc))
        action = "admin.deactivate_mailbox"

    cfg.is_active = False
    cfg.sweep_enabled = False
    db.add(AuditLog(
        user_id=admin.sub, action=action, entity_type="user",
        details={
            "target_sub": user_sub,
            "address": cfg.name,
            "deleted_on_server": payload.delete_on_server,
        },
    ))
    await db.commit()
    logger.info(
        "admin_revoke_mailbox", admin=admin.sub, target=user_sub,
        address=cfg.name, deleted=payload.delete_on_server,
    )


@router.patch("/users/{user_sub}/mailbox/sweep", response_model=UserMailboxOut)
async def set_user_mailbox_sweep(
    user_sub: str,
    payload: UserMailboxSweepUpdate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserMailboxOut:
    """Turn AI triage of a personal mailbox on/off (admin side of the consent)."""
    cfg = await _personal_mailbox(db, user_sub)
    if cfg is None:
        raise HTTPException(status_code=404, detail="У пользователя нет активного личного ящика")

    cfg.sweep_enabled = payload.sweep_enabled
    db.add(AuditLog(
        user_id=admin.sub, action="admin.mailbox_sweep", entity_type="user",
        details={"target_sub": user_sub, "address": cfg.name, "sweep_enabled": payload.sweep_enabled},
    ))
    await db.commit()
    logger.info(
        "admin_mailbox_sweep", admin=admin.sub, target=user_sub, enabled=payload.sweep_enabled
    )
    from app.services.integration_config import get_mail_server_config

    mail_cfg = await get_mail_server_config()
    return UserMailboxOut(
        address=cfg.name, is_active=cfg.is_active, webmail_url=mail_cfg.webmail_url,
        last_sync_at=cfg.last_sync_at, sync_error=cfg.sync_error,
        sweep_enabled=cfg.sweep_enabled, quota_mb=cfg.quota_mb,
    )


@router.get("/users", response_model=UserListResponse)
async def list_users(
    role: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="Search by name or email"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserListResponse:
    stmt = select(User)
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            (func.lower(User.name).like(like)) | (func.lower(User.email).like(like))
        )

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    stmt = stmt.order_by(User.name).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    return UserListResponse(items=[UserOut.model_validate(u) for u in rows], total=total)


@router.get("/users/{user_sub}", response_model=UserOut)
async def get_user(
    user_sub: str,
    _user: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut.model_validate(user)


@router.post("/users/{user_sub}/login-qr")
async def create_user_login_qr(
    user_sub: str,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: mint a single-use QR-login token for a chosen user.

    The mobile app scans it (login screen → "Войти по QR-коду") and is signed in
    AS that user — handy for multi-user devices and testing. The backend mints its
    own session token (it has no Authentik token for other users); the token is
    relayed via /api/auth/qr-login/redeem. Admin-only and audited.
    """
    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="User is deactivated")

    from app.auth.jwt import current_session_epoch, mint_local_session
    from app.config import settings as _settings
    from app.utils.redis_client import get_async_redis

    # Session lifetime once logged in is short & configurable (impersonation);
    # QR token validity (time to scan) is 5 min. The epoch lets the admin revoke.
    session_ttl = max(60, _settings.qr_login_session_ttl_minutes * 60)
    epoch = await current_session_epoch(user.sub)
    session_jwt = mint_local_session(
        sub=user.sub,
        email=user.email,
        name=user.name,
        preferred_username=user.preferred_username,
        groups=[],  # role is resolved from DB (users.role) at verify time
        ttl_seconds=session_ttl,
        session_epoch=epoch,
    )
    qr_token = secrets.token_urlsafe(32)
    qr_ttl = 300
    r = get_async_redis()
    await r.setex(f"qrlogin:{qr_token}", qr_ttl, session_jwt)

    db.add(
        AuditLog(
            user_id=admin.sub,
            action="admin.user_login_qr",
            entity_type="user",
            details={"target_sub": user.sub, "email": user.email},
        )
    )
    await db.commit()

    logger.info("admin_user_login_qr", admin=admin.sub, target=user.sub)
    return {"token": qr_token, "expires_in": qr_ttl, "session_ttl": session_ttl}


@router.post("/users/{user_sub}/revoke-sessions")
async def revoke_user_login_sessions(
    user_sub: str,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin: revoke all QR-login (backend-minted) sessions for a user immediately.

    Bumps the user's session epoch so every outstanding local session token stops
    validating. Does not affect normal Authentik SSO sessions. Audited.
    """
    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    from app.auth.jwt import revoke_user_sessions

    epoch = await revoke_user_sessions(user.sub)

    db.add(
        AuditLog(
            user_id=admin.sub,
            action="admin.revoke_user_sessions",
            entity_type="user",
            details={"target_sub": user.sub, "new_epoch": epoch},
        )
    )
    await db.commit()
    logger.info("admin_revoke_user_sessions", admin=admin.sub, target=user.sub, epoch=epoch)
    return {"revoked": True, "epoch": epoch}


@router.patch("/users/{user_sub}", response_model=UserOut)
async def update_user(
    user_sub: str,
    payload: UserUpdate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.name is not None:
        user.name = payload.name.strip() or user.name

    if payload.role is not None:
        valid_roles = {r.value for r in UserRole}
        if payload.role not in valid_roles:
            raise HTTPException(status_code=422, detail=f"Invalid role: {payload.role}")
        # Prevent demoting the last admin
        if user.role == "admin" and payload.role != "admin":
            if user_sub == admin.sub:
                raise HTTPException(status_code=400, detail="Cannot remove your own admin role")
            from app.config import settings
            admin_count_result = await db.execute(
                select(func.count()).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            admin_count = admin_count_result.scalar() or 0
            if admin_count <= settings.min_admin_count:
                raise HTTPException(status_code=400, detail="Cannot remove the last admin")
        user.role = payload.role
    if payload.is_active is not None:
        # Prevent deactivating the last admin
        if not payload.is_active and user.role == "admin":
            from app.config import settings
            admin_count_result = await db.execute(
                select(func.count()).where(User.role == "admin", User.is_active == True)  # noqa: E712
            )
            admin_count = admin_count_result.scalar() or 0
            if admin_count <= settings.min_admin_count:
                raise HTTPException(status_code=400, detail="Cannot deactivate the last admin")
        user.is_active = payload.is_active
        if not payload.is_active:
            # A deactivated employee must stop receiving mail and stop being read
            # by the agent. Nothing is deleted — a full revoke stays a separate,
            # explicit action (POST .../mailbox/revoke).
            mailbox = await _personal_mailbox(db, user_sub)
            if mailbox is not None:
                from app.services import mailcow_api

                try:
                    await mailcow_api.set_mailbox_active(mailbox.name, active=False)
                except Exception as exc:  # noqa: BLE001
                    # The user record still gets deactivated — but say so loudly,
                    # a silently live mailbox is a real security gap.
                    logger.error(
                        "mailbox_deactivate_failed_on_user_deactivate",
                        address=mailbox.name, error=str(exc),
                    )
                mailbox.sweep_enabled = False
                mailbox.is_active = False
                db.add(AuditLog(
                    user_id=admin.sub, action="admin.deactivate_mailbox", entity_type="user",
                    details={"target_sub": user_sub, "address": mailbox.name,
                             "reason": "user deactivated"},
                ))
    if payload.preferences is not None:
        user.preferences = payload.preferences
    if "section_access" in payload.model_fields_set:
        # Explicit null clears the grant (→ base sections only); a list replaces
        # it. Unknown/non-assignable keys are dropped so we never persist stray
        # values (admin-only, base, or renamed sections).
        from app.domain.sections import validate_section_keys

        user.section_access = (
            validate_section_keys(payload.section_access)
            if payload.section_access is not None
            else None
        )

    # Org fields: applied only when explicitly present in the request body, so an
    # explicit null clears the field while an absent field is left untouched.
    fields_set = payload.model_fields_set
    if "title" in fields_set:
        user.title = payload.title
    if "manager_sub" in fields_set:
        if payload.manager_sub == user.sub:
            raise HTTPException(status_code=400, detail="A user cannot be their own manager")
        user.manager_sub = payload.manager_sub
    if "department_id" in fields_set:
        if payload.department_id is not None:
            from app.db.models import Department

            exists = await db.execute(
                select(Department.id).where(Department.id == payload.department_id)
            )
            if exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Department not found")
        user.department_id = payload.department_id

    await db.commit()
    await db.refresh(user)

    log = AuditLog(
        user_id=admin.sub,
        action="admin.update_user",
        entity_type="user",
        entity_id=user.id,
        details={"target_sub": user_sub, "changes": payload.model_dump(mode="json", exclude_none=True)},
    )
    db.add(log)
    await db.commit()

    # Role/active changes must take effect immediately, not after JWT expiry.
    from app.auth.jwt import invalidate_active_cache

    await invalidate_active_cache(user_sub)

    logger.info("admin_update_user", admin=admin.sub, target=user_sub)
    return UserOut.model_validate(user)


@router.post("/users/{user_sub}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_sub: str,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    result = await db.execute(select(User).where(User.sub == user_sub))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.sub == admin.sub:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    user.is_active = False
    log = AuditLog(
        user_id=admin.sub,
        action="admin.deactivate_user",
        entity_type="user",
        entity_id=user.id,
        details={"target_sub": user_sub},
    )
    db.add(log)
    await db.commit()
    await db.refresh(user)

    # Revoke access now (bounded by cache TTL otherwise).
    from app.auth.jwt import invalidate_active_cache

    await invalidate_active_cache(user_sub)

    logger.info("admin_deactivate_user", admin=admin.sub, target=user_sub)
    return UserOut.model_validate(user)


# ── Departments ───────────────────────────────────────────────────────────────


@router.get("/departments", response_model=DepartmentListResponse)
async def list_departments(
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> DepartmentListResponse:
    rows = (await db.execute(select(Department).order_by(Department.name))).scalars().all()
    return DepartmentListResponse(
        items=[DepartmentOut.model_validate(d) for d in rows], total=len(rows)
    )


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    payload: DepartmentCreate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> DepartmentOut:
    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=422, detail="code is required")
    existing = await db.execute(select(Department.id).where(Department.code == code))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Department code already exists: {code}")
    if payload.parent_id is not None:
        parent = await db.execute(select(Department.id).where(Department.id == payload.parent_id))
        if parent.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Parent department not found")

    dept = Department(name=payload.name.strip(), code=code, parent_id=payload.parent_id)
    db.add(dept)
    await db.commit()
    await db.refresh(dept)
    db.add(
        AuditLog(
            user_id=admin.sub,
            action="admin.create_department",
            entity_type="department",
            entity_id=dept.id,
            details={"code": code, "name": dept.name},
        )
    )
    await db.commit()
    logger.info("admin_create_department", admin=admin.sub, code=code)
    return DepartmentOut.model_validate(dept)


@router.patch("/departments/{department_id}", response_model=DepartmentOut)
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> DepartmentOut:
    dept = (
        await db.execute(select(Department).where(Department.id == department_id))
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(status_code=404, detail="Department not found")

    if payload.name is not None:
        dept.name = payload.name.strip() or dept.name
    if payload.code is not None:
        code = payload.code.strip()
        clash = await db.execute(
            select(Department.id).where(Department.code == code, Department.id != department_id)
        )
        if clash.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"Department code already exists: {code}")
        dept.code = code
    if "parent_id" in payload.model_fields_set:
        if payload.parent_id == department_id:
            raise HTTPException(status_code=400, detail="A department cannot be its own parent")
        dept.parent_id = payload.parent_id

    await db.commit()
    await db.refresh(dept)
    logger.info("admin_update_department", admin=admin.sub, department_id=str(department_id))
    return DepartmentOut.model_validate(dept)


@router.delete("/departments/{department_id}", status_code=204)
async def delete_department(
    department_id: uuid.UUID,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> None:
    dept = (
        await db.execute(select(Department).where(Department.id == department_id))
    ).scalar_one_or_none()
    if dept is None:
        raise HTTPException(status_code=404, detail="Department not found")

    # Block deletion while users or child departments still reference it.
    in_use = await db.execute(
        select(func.count()).where(User.department_id == department_id)
    )
    if (in_use.scalar() or 0) > 0:
        raise HTTPException(status_code=409, detail="Department still has assigned users")
    has_children = await db.execute(
        select(func.count()).where(Department.parent_id == department_id)
    )
    if (has_children.scalar() or 0) > 0:
        raise HTTPException(status_code=409, detail="Department has sub-departments")

    await db.delete(dept)
    db.add(
        AuditLog(
            user_id=admin.sub,
            action="admin.delete_department",
            entity_type="department",
            entity_id=department_id,
            details={"code": dept.code},
        )
    )
    await db.commit()
    logger.info("admin_delete_department", admin=admin.sub, department_id=str(department_id))


# ── Integrations: Authentik ─────────────────────────────────────────────────


def _authentik_admin_url(external_url: str) -> str:
    base = (external_url or "").rstrip("/")
    return f"{base}/if/admin/" if base else ""


@router.get("/integrations/authentik", response_model=IntegrationAuthentikOut)
async def get_authentik_integration(
    admin: UserInfo = _admin_dep,
) -> IntegrationAuthentikOut:
    from app.config import settings
    from app.services.integration_config import get_authentik_external_url, get_authentik_token, mask_token

    token = get_authentik_token()
    ext = get_authentik_external_url()
    return IntegrationAuthentikOut(
        auth_enabled=settings.auth_enabled,
        external_url=ext,
        admin_url=_authentik_admin_url(ext),
        token_set=bool(token),
        token_hint=mask_token(token),
    )


@router.put("/integrations/authentik", response_model=IntegrationAuthentikOut)
async def update_authentik_integration(
    payload: IntegrationAuthentikUpdate,
    admin: UserInfo = _admin_dep,
) -> IntegrationAuthentikOut:
    from app.services.integration_config import (
        set_authentik_external_url,
        set_authentik_token,
    )

    fields = payload.model_fields_set
    if "api_token" in fields:
        set_authentik_token((payload.api_token or "").strip() or None)
    if "external_url" in fields:
        set_authentik_external_url((payload.external_url or "").strip() or None)

    logger.info(
        "admin_update_authentik_integration",
        admin=admin.sub,
        token_changed="api_token" in fields,
        url_changed="external_url" in fields,
    )
    return await get_authentik_integration(admin=admin)


@router.post("/integrations/authentik/test", response_model=IntegrationTestResult)
async def test_authentik_integration(
    admin: UserInfo = _admin_dep,
) -> IntegrationTestResult:
    """Validate the stored token by calling the Authentik API."""
    from app.config import settings
    from app.services.integration_config import get_authentik_token

    if not get_authentik_token():
        return IntegrationTestResult(ok=False, detail="API-токен не задан")

    import httpx

    from app.services.authentik_api import _base, _headers

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_base()}/core/users/", params={"page_size": 1}, headers=_headers()
            )
        if r.status_code == 200:
            return IntegrationTestResult(ok=True, detail="Соединение успешно, токен валиден")
        if r.status_code in (401, 403):
            return IntegrationTestResult(ok=False, detail="Токен отклонён Authentik (401/403)")
        return IntegrationTestResult(ok=False, detail=f"Authentik вернул HTTP {r.status_code}")
    except Exception as exc:
        return IntegrationTestResult(ok=False, detail=f"Ошибка соединения: {exc}")


# ── Mail server (Mailcow) integration ───────────────────────────────────────
# Connection settings for the self-hosted mail server (see infra/installer/
# install-mailcow.sh). Stored in mail_server_config (Postgres, singleton row);
# api_key encrypted at rest with app.ai.secret_box, same pattern as
# provider_instances API keys — never returned to clients, only masked.


async def _get_mail_server_row(db: AsyncSession):
    from app.db.models import MailServerConfig

    return (
        await db.execute(select(MailServerConfig).where(MailServerConfig.singleton_key == "default"))
    ).scalar_one_or_none()


@router.get("/integrations/mail-server", response_model=IntegrationMailServerOut)
async def get_mail_server_integration(
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> IntegrationMailServerOut:
    from app.ai.secret_box import decrypt, mask

    row = await _get_mail_server_row(db)
    if row is None:
        return IntegrationMailServerOut(
            configured=False, api_key_set=False, api_key_hint="", imap_port=993, smtp_port=465,
            default_quota_mb=1024,
        )
    api_key = decrypt(row.api_key_encrypted)
    return IntegrationMailServerOut(
        configured=bool(row.api_url and api_key and row.mail_domain),
        api_url=row.api_url,
        api_key_set=bool(api_key),
        api_key_hint=mask(api_key),
        mail_domain=row.mail_domain,
        webmail_url=row.webmail_url,
        imap_host=row.imap_host,
        imap_port=row.imap_port,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        default_quota_mb=row.default_quota_mb,
    )


def _normalize_api_url(raw: str) -> str:
    """Validate + normalise the Mailcow base URL.

    Typos here surface much later as an opaque connection error during
    provisioning, so reject them at save time: scheme required, no path, no
    trailing slash (the client appends /api/v1/...).
    """
    from urllib.parse import urlparse

    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="API URL должен начинаться с http:// или https:// и содержать хост, например https://mail.example.com",
        )
    if parsed.path not in ("", "/"):
        raise HTTPException(
            status_code=422,
            detail=f"Уберите путь из API URL — нужен только адрес сервера: {parsed.scheme}://{parsed.netloc}",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


@router.put("/integrations/mail-server", response_model=IntegrationMailServerSaved)
async def update_mail_server_integration(
    payload: IntegrationMailServerUpdate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> IntegrationMailServerOut:
    from app.ai.secret_box import encrypt
    from app.db.models import MailServerConfig

    row = await _get_mail_server_row(db)
    if row is None:
        row = MailServerConfig(singleton_key="default")
        db.add(row)

    fields = payload.model_fields_set
    if "api_url" in fields:
        row.api_url = _normalize_api_url(payload.api_url or "") or None
    if "api_key" in fields:
        row.api_key_encrypted = encrypt((payload.api_key or "").strip()) if payload.api_key else None
    if "mail_domain" in fields:
        row.mail_domain = (payload.mail_domain or "").strip().lower() or None
    if "webmail_url" in fields:
        row.webmail_url = (payload.webmail_url or "").strip() or None
    if "imap_host" in fields:
        row.imap_host = (payload.imap_host or "").strip() or None
    if "imap_port" in fields and payload.imap_port:
        row.imap_port = payload.imap_port
    if "smtp_host" in fields:
        row.smtp_host = (payload.smtp_host or "").strip() or None
    if "smtp_port" in fields and payload.smtp_port:
        row.smtp_port = payload.smtp_port
    if "default_quota_mb" in fields and payload.default_quota_mb is not None:
        row.default_quota_mb = payload.default_quota_mb
    row.updated_by = admin.sub

    await db.commit()
    logger.info("admin_update_mail_server_integration", admin=admin.sub)

    saved = await get_mail_server_integration(admin=admin, db=db)
    result = IntegrationMailServerSaved(**saved.model_dump())
    if payload.verify:
        from app.services import mailcow_api

        ok, detail = await mailcow_api.test_connection()
        result.verified = ok
        result.verify_detail = detail
    return result


@router.post("/integrations/mail-server/test", response_model=IntegrationTestResult)
async def test_mail_server_integration(
    admin: UserInfo = _admin_dep,
) -> IntegrationTestResult:
    from app.services import mailcow_api

    ok, detail = await mailcow_api.test_connection()
    return IntegrationTestResult(ok=ok, detail=detail)


# ── OAuth apps (Gmail / Microsoft 365 mailbox auth) ──────────────────────────
# Registered once per provider here; each mailbox then runs its own consent
# flow against it (app/api/oauth.py) to get its own refresh token — see
# app/domain/oauth_mail.py for why plain passwords stopped working at all.

_OAUTH_PROVIDERS = ("google", "microsoft")


def _oauth_app_out(provider: str, row) -> OAuthAppOut:
    from app.ai.secret_box import decrypt, mask

    secret = decrypt(row.client_secret_encrypted) if row else ""
    return OAuthAppOut(
        provider=provider,
        client_id=row.client_id if row else None,
        client_secret_set=bool(secret),
        client_secret_hint=mask(secret),
        redirect_uri=row.redirect_uri if row else None,
        configured=bool(row and row.client_id and secret and row.redirect_uri),
    )


@router.get("/integrations/oauth-apps", response_model=list[OAuthAppOut])
async def list_oauth_apps(
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> list[OAuthAppOut]:
    from app.db.models import OAuthAppConfig

    rows = {
        row.provider: row
        for row in (await db.execute(select(OAuthAppConfig))).scalars().all()
    }
    return [_oauth_app_out(p, rows.get(p)) for p in _OAUTH_PROVIDERS]


@router.put("/integrations/oauth-apps/{provider}", response_model=OAuthAppOut)
async def update_oauth_app(
    provider: str,
    payload: OAuthAppUpdate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> OAuthAppOut:
    from app.ai.secret_box import encrypt
    from app.db.models import OAuthAppConfig

    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")

    row = (
        await db.execute(select(OAuthAppConfig).where(OAuthAppConfig.provider == provider))
    ).scalar_one_or_none()
    if row is None:
        row = OAuthAppConfig(provider=provider)
        db.add(row)

    fields = payload.model_fields_set
    if "client_id" in fields:
        row.client_id = (payload.client_id or "").strip() or None
    if "client_secret" in fields:
        row.client_secret_encrypted = encrypt(payload.client_secret.strip()) if payload.client_secret else None
    if "redirect_uri" in fields:
        row.redirect_uri = (payload.redirect_uri or "").strip().rstrip("/") or None
    row.updated_by = admin.sub

    await db.commit()
    logger.info("admin_update_oauth_app", admin=admin.sub, provider=provider)
    return _oauth_app_out(provider, row)


# ── Permission matrix ─────────────────────────────────────────────────────────


@router.get("/permissions", response_model=PermissionMatrixOut)
async def get_permission_matrix(
    _user: UserInfo = _admin_dep,
) -> PermissionMatrixOut:
    matrix = {
        role.value: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()
    }
    return PermissionMatrixOut(matrix=matrix)


# ── Section access catalog ────────────────────────────────────────────────────


@router.get("/sections/catalog")
async def get_sections_catalog(_user: UserInfo = _admin_dep) -> dict:
    """Return the workspace section tree an admin can grant per user.

    Admin-only entries (e.g. Администрирование) are excluded — those stay gated
    by role, not by the section allowlist.
    """
    from app.domain.sections import SECTION_CATALOG

    groups = []
    for group in SECTION_CATALOG:
        items = [
            {"key": item.key, "label": item.label, "href": item.href}
            for item in group.items
            if not item.admin_only
        ]
        if items:
            groups.append({"key": group.key, "label": group.label, "items": items})
    return {"groups": groups}


# ── Audit log viewer ──────────────────────────────────────────────────────────


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    user_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    from_dt: datetime | None = Query(default=None),
    to_dt: datetime | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _user: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> AuditLogListResponse:
    stmt = select(AuditLog)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if from_dt:
        stmt = stmt.where(AuditLog.timestamp >= from_dt)
    if to_dt:
        stmt = stmt.where(AuditLog.timestamp <= to_dt)

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    stmt = stmt.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    return AuditLogListResponse(
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total,
    )


# ── API keys ──────────────────────────────────────────────────────────────────


@router.get("/api-keys", response_model=ApiKeyListResponse)
async def list_api_keys(
    _user: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyListResponse:
    rows = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    return ApiKeyListResponse(
        items=[ApiKeyOut.model_validate(k) for k in rows],
        total=len(rows),
    )


@router.post("/api-keys", response_model=ApiKeyCreatedOut)
async def create_api_key(
    payload: ApiKeyCreate,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreatedOut:
    raw_key = secrets.token_urlsafe(40)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    api_key = ApiKey(
        key_hash=key_hash,
        name=payload.name,
        user_sub=admin.sub,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
    )
    db.add(api_key)

    log = AuditLog(
        user_id=admin.sub,
        action="admin.create_api_key",
        entity_type="api_key",
        details={"name": payload.name, "scopes": payload.scopes},
    )
    db.add(log)
    await db.commit()
    await db.refresh(api_key)

    logger.info("admin_create_api_key", admin=admin.sub, key_name=payload.name)
    out = ApiKeyOut.model_validate(api_key)
    return ApiKeyCreatedOut(**out.model_dump(), raw_key=raw_key)


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    admin: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    key.is_active = False
    log = AuditLog(
        user_id=admin.sub,
        action="admin.revoke_api_key",
        entity_type="api_key",
        entity_id=key_id,
        details={"name": key.name},
    )
    db.add(log)
    await db.commit()
    logger.info("admin_revoke_api_key", admin=admin.sub, key_id=str(key_id))


# ── System status ─────────────────────────────────────────────────────────────


@router.get("/system-status", response_model=SystemStatusOut)
async def system_status(
    _user: UserInfo = _admin_dep,
    db: AsyncSession = Depends(get_db),
) -> SystemStatusOut:
    # DB check
    try:
        await db.execute(select(func.now()))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    # Redis check
    try:
        from app.utils.redis_client import get_async_redis
        await get_async_redis().ping()
        redis_status = "ok"
    except Exception as exc:
        redis_status = f"error: {exc}"

    # Celery check
    try:
        from app.tasks.celery_app import celery_app

        inspect = celery_app.control.inspect(timeout=2)
        workers = inspect.ping()
        celery_status = "ok" if workers else "no_workers"
    except Exception as exc:
        celery_status = f"error: {exc}"

    # AI providers
    ai_providers: dict[str, str] = {}
    try:
        from app.api.health import ai_health

        health_result = await ai_health()
        for provider, info in health_result.get("providers", {}).items():
            if info.get("ok"):
                ai_providers[provider] = "ok"
            elif info.get("skipped"):
                ai_providers[provider] = "skipped"
            else:
                ai_providers[provider] = f"error: {info.get('error', 'unknown')}"
    except Exception as exc:
        ai_providers["error"] = str(exc)

    # Counts
    active_users = (
        await db.execute(select(func.count(User.id)).where(User.is_active == True))  # noqa: E712
    ).scalar_one()

    from app.db.models import Approval, ApprovalStatus

    pending_approvals = (
        await db.execute(
            select(func.count(Approval.id)).where(Approval.status == ApprovalStatus.pending)
        )
    ).scalar_one()

    return SystemStatusOut(
        db=db_status,
        redis=redis_status,
        celery=celery_status,
        ai_providers=ai_providers,
        active_users_count=active_users,
        pending_approvals_count=pending_approvals,
    )
