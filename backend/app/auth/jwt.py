"""JWT verification — Authentik OIDC tokens.

In development (AUTH_ENABLED=false) every request is treated as the dev user.
In production, Bearer token is verified against Authentik's JWKS endpoint.
"""

from __future__ import annotations

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

_bearer = HTTPBearer(auto_error=False)

# Dev user injected when auth is disabled
_DEV_USER = UserInfo(
    sub="dev-user",
    email="dev@localhost",
    name="Dev User",
    preferred_username="dev",
    roles=[UserRole.admin],
    groups=["admins"],
)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserInfo:
    """FastAPI dependency — returns current user or raises 401."""
    if not settings.auth_enabled:
        return _DEV_USER

    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = credentials.credentials
    return await _verify_token(token)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserInfo | None:
    """Same as get_current_user but returns None instead of raising."""
    if not settings.auth_enabled:
        return _DEV_USER
    if credentials is None:
        return None
    try:
        return await _verify_token(credentials.credentials)
    except HTTPException:
        return None


async def _verify_token(token: str) -> UserInfo:
    """Verify JWT against Authentik JWKS and extract claims."""
    try:
        from jose import JWTError, jwt

        # Fetch JWKS from Authentik (cached by jose internally)
        jwks_uri = f"{settings.authentik_url}/application/o/{settings.authentik_slug}/.well-known/jwks.json"

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

        # Map Authentik groups to roles
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
            return user  # admin bypasses all checks
        for role in roles:
            if role in user.roles:
                return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Required role: {[r.value for r in roles]}",
        )
    return check
