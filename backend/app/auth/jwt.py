"""JWT verification — Authentik OIDC tokens.

In development (AUTH_ENABLED=false) every request is treated as the dev user.
In production, Bearer token or httpOnly cookie is verified against Authentik's JWKS endpoint.
Service accounts can authenticate via X-API-Key header.
"""

from __future__ import annotations

import hashlib

import structlog
from fastapi import Cookie, Depends, HTTPException, status
from starlette.requests import HTTPConnection

from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

_DEV_USER = UserInfo(
    sub="dev-user",
    email="dev@localhost",
    name="Dev User",
    preferred_username="dev",
    roles=[UserRole.admin],
    groups=["admins"],
)


def _extract_bearer(conn: HTTPConnection) -> str | None:
    """Extract Bearer token from Authorization header (works for HTTP and WS)."""
    auth = conn.headers.get("authorization", "") or conn.headers.get("Authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else None


async def get_current_user(
    conn: HTTPConnection,
    access_token: str | None = Cookie(default=None),
) -> UserInfo:
    """FastAPI dependency — returns current user or raises 401.

    Works for both HTTP requests and WebSocket connections.
    Priority: X-API-Key header → httpOnly cookie → Bearer token.
    """
    if not settings.auth_enabled:
        return _DEV_USER

    # Service-account API key
    api_key_raw = conn.headers.get("x-api-key") or conn.headers.get("X-API-Key")
    if api_key_raw:
        return await _verify_api_key(api_key_raw)

    # Cookie may also be in conn.cookies (WebSocket context)
    cookie_token = access_token or conn.cookies.get("access_token")
    token = cookie_token or _extract_bearer(conn)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return await _verify_token(token)


async def get_current_user_optional(
    conn: HTTPConnection,
    access_token: str | None = Cookie(default=None),
) -> UserInfo | None:
    """Same as get_current_user but returns None instead of raising."""
    if not settings.auth_enabled:
        return _DEV_USER
    api_key_raw = conn.headers.get("x-api-key") or conn.headers.get("X-API-Key")
    if api_key_raw:
        try:
            return await _verify_api_key(api_key_raw)
        except HTTPException:
            return None
    cookie_token = access_token or conn.cookies.get("access_token")
    token = cookie_token or _extract_bearer(conn)
    if not token:
        return None
    try:
        return await _verify_token(token)
    except HTTPException:
        return None


async def _verify_token(token: str) -> UserInfo:
    """Verify JWT against Authentik JWKS and extract claims."""
    try:
        from jose import jwt

        # Authentik 2024.12+: JWKS endpoint is /application/o/{slug}/jwks/
        jwks_uri = (
            f"{settings.authentik_url}/application/o"
            f"/{settings.authentik_slug}/jwks/"
        )

        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            jwks = resp.json()

        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=settings.oauth_client_id or None,
            options={"verify_aud": bool(settings.oauth_client_id)},
        )

        groups: list[str] = claims.get("groups", [])
        roles = _groups_to_roles(groups)

        await _assert_user_active(claims["sub"])

        return UserInfo(
            sub=claims["sub"],
            email=claims.get("email", ""),
            name=claims.get("name", claims.get("preferred_username", "")),
            preferred_username=claims.get("preferred_username", ""),
            roles=roles,
            groups=groups,
        )

    except Exception as e:
        logger.warning("jwt_verification_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _verify_api_key(raw_key: str) -> UserInfo:
    """Verify API key and return a minimal UserInfo for service accounts."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import ApiKey
    from app.db.session import _get_session_factory

    # Internal service-to-service key — no DB lookup, no expiry check.
    if settings.agent_service_key and raw_key == settings.agent_service_key:
        return UserInfo(
            sub="agent-service",
            email="agent@internal",
            name="AI Agent (Света)",
            preferred_username="agent",
            roles=[UserRole.admin],
            groups=["agents"],
        )

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)  # noqa: E712
        )
        key = result.scalar_one_or_none()

        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

        now = datetime.now(timezone.utc)
        if key.expires_at and key.expires_at < now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")

        key.last_used_at = now
        await db.commit()

    return UserInfo(
        sub=key.user_sub,
        email="service@internal",
        name=key.name,
        preferred_username=key.name,
        roles=[UserRole.viewer],
        groups=[],
    )


# Active-status cache: bridges the gap between deactivating a user and their JWT
# expiring. Short TTL keeps the DB load low while bounding stale access to seconds.
_ACTIVE_CACHE_TTL = 45  # seconds
_ACTIVE_CACHE_PREFIX = "auth:active:"


async def invalidate_active_cache(sub: str) -> None:
    """Drop the cached active-status for a user so a change takes effect immediately.

    Call after activating/deactivating a user. Best-effort — ignores Redis errors.
    """
    try:
        from app.utils.redis_client import get_async_redis

        await get_async_redis().delete(f"{_ACTIVE_CACHE_PREFIX}{sub}")
    except Exception:  # pragma: no cover - cache invalidation is best-effort
        pass


async def _assert_user_active(sub: str) -> None:
    """Reject tokens belonging to a deactivated user (is_active=False).

    Fail-open on infrastructure errors (Redis/DB) so a transient outage can never
    lock every user out. Unknown subs (not yet upserted) are treated as active.
    """
    cache_key = f"{_ACTIVE_CACHE_PREFIX}{sub}"
    redis = None
    try:
        from app.utils.redis_client import get_async_redis

        redis = get_async_redis()
        cached = await redis.get(cache_key)
        if cached == "1":
            return
        if cached == "0":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is deactivated",
            )
    except HTTPException:
        raise
    except Exception:
        redis = None  # Redis unavailable — fall back to DB without caching

    try:
        from sqlalchemy import select

        from app.db.models import User
        from app.db.session import _get_session_factory

        async with _get_session_factory()() as db:
            result = await db.execute(select(User.is_active).where(User.sub == sub))
            row = result.scalar_one_or_none()
    except Exception as e:
        logger.warning("active_check_db_failed", error=str(e))
        return  # fail-open

    is_active = True if row is None else bool(row)
    if redis is not None:
        try:
            await redis.set(cache_key, "1" if is_active else "0", ex=_ACTIVE_CACHE_TTL)
        except Exception:  # pragma: no cover
            pass
    if not is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )


def _groups_to_roles(groups: list[str]) -> list[UserRole]:
    """Map Authentik group names to app roles."""
    mapping = {
        "admins": UserRole.admin,
        "managers": UserRole.manager,
        "accountants": UserRole.accountant,
        "buyers": UserRole.buyer,
        "engineers": UserRole.engineer,
        "technologists": UserRole.technologist,
    }
    roles = []
    for g in groups:
        role = mapping.get(g.lower())
        if role:
            roles.append(role)
    return roles or [UserRole.viewer]


def require_role(*roles: UserRole):
    """Dependency factory: require at least one of the given roles."""

    async def check(user: UserInfo = Depends(get_current_user)) -> UserInfo:
        if UserRole.admin in user.roles:
            return user
        for role in roles:
            if role in user.roles:
                return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Required role: {[r.value for r in roles]}",
        )

    return check
