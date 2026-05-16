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
from app.db.models import ApiKey, AuditLog, User
from app.db.session import get_db
from app.domain.admin import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyListResponse,
    ApiKeyOut,
    AuditLogListResponse,
    AuditLogOut,
    PermissionMatrixOut,
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
    """Manually create a user record (pre-provisioning before first OIDC login)."""
    import uuid as _uuid

    valid_roles = {r.value for r in UserRole}
    if payload.role not in valid_roles:
        raise HTTPException(status_code=422, detail=f"Invalid role: {payload.role}")

    # Check email uniqueness
    existing = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    preferred_username = payload.preferred_username or payload.email.split("@")[0]
    user = User(
        sub=f"local:{_uuid.uuid4()}",
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
        details={"email": payload.email, "role": payload.role},
    )
    db.add(log)
    await db.commit()
    await db.refresh(user)

    logger.info("admin_create_user", admin=admin.sub, email=payload.email)
    return UserOut.model_validate(user)


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

    await db.commit()
    await db.refresh(user)

    log = AuditLog(
        user_id=admin.sub,
        action="admin.update_user",
        entity_type="user",
        entity_id=user.id,
        details={"target_sub": user_sub, "changes": payload.model_dump(exclude_none=True)},
    )
    db.add(log)
    await db.commit()

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

    logger.info("admin_deactivate_user", admin=admin.sub, target=user_sub)
    return UserOut.model_validate(user)


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
