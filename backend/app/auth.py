from __future__ import annotations

from typing import Any

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.config import Settings, get_settings
from backend.app.domain.schemas import AuthUserRead


bearer_scheme = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "technologist": {
        "case:read",
        "case:write",
        "document:read",
        "document:write",
        "drawing:analyze",
        "email:draft",
        "agent:read",
        "agent:run",
    },
    "accountant": {
        "case:read",
        "document:read",
        "invoice:read",
        "invoice:export",
        "email:draft",
    },
}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthUserRead:
    settings = get_settings()
    if settings.auth_local_bypass:
        return AuthUserRead(
            subject="local-dev",
            email="local-dev@example.local",
            name="Local developer",
            roles=["admin"],
            auth_mode="local_bypass",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is required",
        )
    return validate_oidc_token(credentials.credentials, settings)


def require_permission(permission: str) -> Callable[[AuthUserRead], Awaitable[AuthUserRead]]:
    async def dependency(user: AuthUserRead = Depends(get_current_user)) -> AuthUserRead:
        if not has_permission(user.roles, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission}",
            )
        return user

    return dependency


def has_permission(roles: list[str], permission: str) -> bool:
    for role in roles:
        permissions = ROLE_PERMISSIONS.get(role, set())
        if "*" in permissions or permission in permissions:
            return True
    return False


def permissions_for_roles(roles: list[str]) -> list[str]:
    permissions: set[str] = set()
    for role in roles:
        role_permissions = ROLE_PERMISSIONS.get(role, set())
        if "*" in role_permissions:
            return ["*"]
        permissions.update(role_permissions)
    return sorted(permissions)


def validate_oidc_token(token: str, settings: Settings) -> AuthUserRead:
    if not settings.oidc_issuer_url or not settings.oidc_audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC_ISSUER_URL and OIDC_AUDIENCE must be configured",
        )
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PyJWT[crypto] is required for OIDC JWT validation",
        ) from exc

    jwks_url = settings.oidc_jwks_url or settings.oidc_issuer_url.rstrip("/") + "/protocol/openid-connect/certs"
    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer_url,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC token validation failed",
        ) from exc
    return _user_from_claims(claims)


def _user_from_claims(claims: dict[str, Any]) -> AuthUserRead:
    resource_roles = []
    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        for client in resource_access.values():
            roles = client.get("roles") if isinstance(client, dict) else None
            if isinstance(roles, list):
                resource_roles.extend(str(role) for role in roles)
    realm_access = claims.get("realm_access", {})
    realm_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    return AuthUserRead(
        subject=str(claims.get("sub", "")),
        email=claims.get("email"),
        name=claims.get("name") or claims.get("preferred_username"),
        roles=sorted({str(role) for role in [*realm_roles, *resource_roles]}),
        auth_mode="oidc",
    )
