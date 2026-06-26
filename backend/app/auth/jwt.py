"""JWT verification — Authentik OIDC tokens.

In development (AUTH_ENABLED=false) every request is treated as the dev user.
In production, Bearer token or httpOnly cookie is verified against Authentik's JWKS endpoint.
Service accounts can authenticate via X-API-Key header.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import structlog
from fastapi import Cookie, Depends, HTTPException, status
from starlette.requests import HTTPConnection

from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

# JWKS cache — Authentik keys rotate rarely; re-fetching per-request adds
# unnecessary latency and hammers Authentik under load.
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_jwks_lock = asyncio.Lock()
_JWKS_TTL = 600.0  # 10 minutes


async def _get_jwks() -> dict:
    """Return cached JWKS, refreshing when the TTL has expired."""
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_fetched_at) < _JWKS_TTL:
        return _jwks_cache
    async with _jwks_lock:
        # Double-check after acquiring the lock to avoid thundering herd
        now = time.monotonic()
        if _jwks_cache is not None and (now - _jwks_fetched_at) < _JWKS_TTL:
            return _jwks_cache
        import httpx
        jwks_uri = (
            f"{settings.authentik_url}/application/o"
            f"/{settings.authentik_slug}/jwks/"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_fetched_at = time.monotonic()
        return _jwks_cache

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


# ── Locally-minted session tokens ─────────────────────────────────────────────
# For QR-login provisioning (admin issues a login for any user) we can't relay an
# Authentik JWT we don't possess, so the backend mints its own HS256 session token
# signed with app_secret_key. _verify_token accepts these alongside Authentik's
# RS256 tokens. Marked by iss+token_use so they can't be confused with anything else.

_LOCAL_ISS = "sveta-local"
_LOCAL_TOKEN_USE = "local_session"
_LSEPOCH_PREFIX = "auth:lsepoch:"      # per-user local-session epoch (bump = revoke all)
_LSREVOKED_PREFIX = "auth:lsrevoked:"  # per-jti denylist (single-session revoke)


def _session_key() -> str:
    """Signing secret for local session tokens (dedicated key, app_secret_key fallback)."""
    return settings.session_signing_key or settings.app_secret_key


async def current_session_epoch(sub: str) -> int:
    """Current local-session epoch for a user (0 if none). Fail-open to 0."""
    try:
        from app.utils.redis_client import get_async_redis

        val = await get_async_redis().get(f"{_LSEPOCH_PREFIX}{sub}")
        return int(val) if val else 0
    except Exception:
        return 0


async def revoke_user_sessions(sub: str) -> int:
    """Invalidate ALL outstanding local sessions for a user by bumping the epoch."""
    from app.utils.redis_client import get_async_redis

    return int(await get_async_redis().incr(f"{_LSEPOCH_PREFIX}{sub}"))


async def revoke_session_jti(jti: str, ttl_seconds: int = 86400) -> None:
    """Revoke a single local session by its jti (denylist until it would expire)."""
    try:
        from app.utils.redis_client import get_async_redis

        await get_async_redis().setex(f"{_LSREVOKED_PREFIX}{jti}", max(ttl_seconds, 60), "1")
    except Exception:  # pragma: no cover - best effort
        pass


async def _is_local_session_revoked(claims: dict) -> bool:
    """True if this local session was revoked (by jti denylist or epoch bump).

    Fail-open (not revoked) on infra errors so a Redis outage can't lock users out.
    """
    try:
        from app.utils.redis_client import get_async_redis

        r = get_async_redis()
        jti = claims.get("jti")
        if jti and await r.get(f"{_LSREVOKED_PREFIX}{jti}"):
            return True
        cur = await r.get(f"{_LSEPOCH_PREFIX}{claims.get('sub')}")
        if cur is not None and int(cur) > int(claims.get("epoch", 0)):
            return True
    except Exception:
        return False
    return False


def mint_local_session(
    *,
    sub: str,
    email: str = "",
    name: str = "",
    preferred_username: str = "",
    groups: list[str] | None = None,
    ttl_seconds: int = 3600,
    session_epoch: int = 0,
) -> str:
    """Mint a backend-signed (HS256) session token for `sub`. Used by QR-login.

    Carries a `jti` (single-session revoke) and an `epoch` (revoke-all-for-user).
    Pass the user's current epoch (see current_session_epoch) so a later bump
    invalidates this token.
    """
    import secrets as _secrets
    import time

    from jose import jwt

    now = int(time.time())
    claims = {
        "sub": sub,
        "email": email,
        "name": name or preferred_username or email,
        "preferred_username": preferred_username,
        "groups": groups or [],
        "iss": _LOCAL_ISS,
        "token_use": _LOCAL_TOKEN_USE,
        "jti": _secrets.token_urlsafe(12),
        "epoch": session_epoch,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, _session_key(), algorithm="HS256")


async def _verify_local_session(token: str) -> UserInfo:
    """Verify a backend-minted HS256 session token (see mint_local_session)."""
    try:
        from jose import jwt

        claims = jwt.decode(
            token,
            _session_key(),
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        if claims.get("iss") != _LOCAL_ISS or claims.get("token_use") != _LOCAL_TOKEN_USE:
            raise ValueError("not a local session token")

        if await _is_local_session_revoked(claims):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )

        groups: list[str] = claims.get("groups", [])
        roles = _groups_to_roles(groups)

        await _assert_user_active(claims["sub"])

        db_role = await _db_role_for_sub(claims["sub"])
        if db_role is not None and db_role not in roles:
            roles = [db_role, *roles]

        return UserInfo(
            sub=claims["sub"],
            email=claims.get("email", ""),
            name=claims.get("name", claims.get("preferred_username", "")),
            preferred_username=claims.get("preferred_username", ""),
            roles=roles,
            groups=groups,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("local_session_verification_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _verify_token(token: str) -> UserInfo:
    """Verify JWT and extract claims.

    Backend-minted session tokens (HS256, see mint_local_session) take the local
    path; everything else is verified as an Authentik RS256 token against JWKS.
    """
    try:
        from jose import jwt

        try:
            _alg = jwt.get_unverified_header(token).get("alg")
        except Exception:
            _alg = None
        if _alg == "HS256":
            return await _verify_local_session(token)

        jwks = await _get_jwks()

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

        # Merge the DB role (bootstrap admin / admin-granted roles) on top of the
        # SSO-group roles so app-managed RBAC takes effect even when SSO groups
        # don't carry it. Union → DB can only add privileges, never silently drop
        # an SSO grant.
        db_role = await _db_role_for_sub(claims["sub"])
        if db_role is not None and db_role not in roles:
            roles = [db_role, *roles]

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
_ROLE_CACHE_TTL = 45  # seconds
_ROLE_CACHE_PREFIX = "auth:role:"


async def invalidate_active_cache(sub: str) -> None:
    """Drop the cached active-status and role for a user so a change takes effect
    immediately. Call after activating/deactivating or changing a user's role.
    Best-effort — ignores Redis errors.
    """
    try:
        from app.utils.redis_client import get_async_redis

        redis = get_async_redis()
        await redis.delete(f"{_ACTIVE_CACHE_PREFIX}{sub}")
        await redis.delete(f"{_ROLE_CACHE_PREFIX}{sub}")
    except Exception:  # pragma: no cover - cache invalidation is best-effort
        pass


async def _db_role_for_sub(sub: str) -> UserRole | None:
    """Return the app role stored in DB for this user (authoritative for RBAC).

    ``users.role`` is synced on every login from SSO groups + the
    INITIAL_ADMIN_EMAIL bootstrap promote + admin grants, so it must be honoured
    at request time — otherwise the first admin (promoted in DB but not in any
    SSO ``admins`` group) is stuck as viewer. Cached briefly; fail-open (None).
    """
    cache_key = f"{_ROLE_CACHE_PREFIX}{sub}"
    redis = None
    try:
        from app.utils.redis_client import get_async_redis

        redis = get_async_redis()
        cached = await redis.get(cache_key)
        if cached:
            try:
                return UserRole(cached)
            except ValueError:
                return None
    except Exception:
        redis = None

    try:
        from sqlalchemy import select

        from app.db.models import User
        from app.db.session import _get_session_factory

        async with _get_session_factory()() as db:
            row = (
                await db.execute(select(User.role).where(User.sub == sub))
            ).scalar_one_or_none()
    except Exception as e:
        logger.warning("role_lookup_db_failed", error=str(e))
        return None

    role: UserRole | None = None
    if row:
        try:
            role = UserRole(row)
        except ValueError:
            role = None
    if redis is not None and role is not None:
        try:
            await redis.set(cache_key, role.value, ex=_ROLE_CACHE_TTL)
        except Exception:  # pragma: no cover
            pass
    return role


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


def is_service_account(user: UserInfo) -> bool:
    """The internal agent service identity must not stand in for a human reviewer."""
    return user.sub == "agent-service" or "agents" in (user.groups or [])


def require_human_role(*roles: UserRole):
    """Like ``require_role`` but rejects the agent service account.

    Decisions that approve/run the agent's own proposals (task decide/run,
    memory promotion/source decide) are the human-in-the-loop boundary. The
    agent authenticates as admin via the service key, so a plain role check is
    not enough — these endpoints must be reachable only by a real operator.
    """
    role_check = require_role(*roles)

    async def check(user: UserInfo = Depends(role_check)) -> UserInfo:
        if is_service_account(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "This decision requires a human operator; the agent service "
                    "account cannot approve or run its own proposals."
                ),
            )
        return user

    return check
