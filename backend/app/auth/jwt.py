"""JWT verification — Authentik OIDC tokens.

In development (AUTH_ENABLED=false) every request is treated as the dev user.
In production, Bearer token or httpOnly cookie is verified against Authentik's JWKS endpoint.
Service accounts can authenticate via X-API-Key header.
"""

from __future__ import annotations

import hashlib

import structlog
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

_bearer = HTTPBearer(auto_error=False)

_DEV_USER = UserInfo(
    sub="dev-user",
    email="dev@localhost",
    name="Dev User",
    preferred_username="dev",
    roles=[UserRole.admin],
    groups=["admins"],
)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    access_token: str | None = Cookie(default=None),
) -> UserInfo:
    """FastAPI dependency — returns current user or raises 401.

    Priority: X-API-Key header → httpOnly cookie → Bearer token.
    """
    if not settings.auth_enabled:
        return _DEV_USER

    # Service-account API key (creates its own short-lived session)
    api_key_raw = request.headers.get("X-API-Key")
    if api_key_raw:
        return await _verify_api_key(api_key_raw)

    token = access_token or (credentials.credentials if credentials else None)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return await _verify_token(token)


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    access_token: str | None = Cookie(default=None),
) -> UserInfo | None:
    """Same as get_current_user but returns None instead of raising."""
    if not settings.auth_enabled:
        return _DEV_USER
    api_key_raw = request.headers.get("X-API-Key")
    if api_key_raw:
        try:
            return await _verify_api_key(api_key_raw)
        except HTTPException:
            return None
    token = access_token or (credentials.credentials if credentials else None)
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

        jwks_uri = (
            f"{settings.authentik_url}/application/o"
            f"/{settings.authentik_slug}/.well-known/jwks.json"
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


def _groups_to_roles(groups: list[str]) -> list[UserRole]:
    """Map Authentik group names to app roles."""
    mapping = {
        "admins": UserRole.admin,
        "managers": UserRole.manager,
        "accountants": UserRole.accountant,
        "buyers": UserRole.buyer,
        "engineers": UserRole.engineer,
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
