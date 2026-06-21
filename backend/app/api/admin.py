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
from app.db.models import ApiKey, AuditLog, Department, User
from app.db.session import get_db
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
    IntegrationTestResult,
    PermissionMatrixOut,
    SetPasswordRequest,
    SystemStatusOut,
    UserCreate,
    UserListResponse,
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
    if payload.preferences is not None:
        user.preferences = payload.preferences

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


# ── Permission matrix ─────────────────────────────────────────────────────────


@router.get("/permissions", response_model=PermissionMatrixOut)
async def get_permission_matrix(
    _user: UserInfo = _admin_dep,
) -> PermissionMatrixOut:
    matrix = {
        role.value: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()
    }
    return PermissionMatrixOut(matrix=matrix)


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
