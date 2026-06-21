"""Auth API — OIDC login/logout, /me, /users endpoints."""

from __future__ import annotations

import secrets
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.config import settings
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


# ── OAuth state helpers (Redis-backed, TTL 600s) ──────────────────────────────


async def _store_state(state: str, redirect_uri: str) -> None:
    from app.utils.redis_client import get_async_redis
    r = get_async_redis()
    await r.setex(f"oauth_state:{state}", 600, redirect_uri)


async def _pop_state(state: str) -> str | None:
    from app.utils.redis_client import get_async_redis
    r = get_async_redis()
    key = f"oauth_state:{state}"
    pipe = r.pipeline()
    pipe.get(key)
    pipe.delete(key)
    result = await pipe.execute()
    return result[0]  # None if state not found or expired


# ── Auth endpoints ────────────────────────────────────────────────────────────


@router.get("/me", response_model=UserInfo)
async def me(user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """Return current user info."""
    return user


def _frontend_base_from_uri(redirect_uri: str) -> str:
    """Extract frontend origin from redirect_uri (e.g. http://192.168.1.246:3000)."""
    from urllib.parse import urlparse
    parsed = urlparse(redirect_uri)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return settings.frontend_url


@router.get("/login")
async def login(
    redirect_uri: str = Query(default="http://localhost/auth/callback"),
    next: str = Query(default="/inbox"),
) -> RedirectResponse:
    """Redirect to Authentik OIDC authorization endpoint."""
    # Validate redirect_uri origin against allowed frontends to prevent open-redirect
    # token theft (especially in dev mode where a dev-token cookie is issued directly).
    if settings.auth_enabled:
        from urllib.parse import urlparse as _up
        _req_netloc = _up(redirect_uri).netloc
        _allowed = {_up(settings.frontend_url).netloc}
        if _req_netloc and _req_netloc not in _allowed:
            raise HTTPException(status_code=400, detail="redirect_uri origin is not allowed")

    if not settings.auth_enabled:
        # Dev mode: set cookie and redirect back to caller's origin + next path
        base = _frontend_base_from_uri(redirect_uri)
        # Sanitize next to prevent open redirect
        if not next.startswith("/"):
            next = "/inbox"
        resp = RedirectResponse(url=f"{base}{next}", status_code=302)
        resp.set_cookie(
            key="access_token",
            value="dev-token",
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=86400,
            path="/",
        )
        return resp

    from urllib.parse import urlencode

    # Encode `next` into state so it survives the OAuth round-trip
    state = secrets.token_urlsafe(32)
    await _store_state(state, redirect_uri)
    # Also store next under a derived key
    try:
        from app.utils.redis_client import get_async_redis
        await get_async_redis().setex(
            f"oauth_next:{state}", 600, next if next.startswith("/") else "/inbox"
        )
    except Exception:
        pass

    params = {
        "response_type": "code",
        "client_id": settings.oauth_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email groups",
        "state": state,
    }
    # Derive the Authentik authorize base URL from redirect_uri (built by the browser
    # from window.location.origin). This makes the flow work regardless of how the user
    # reaches the server: direct, SSH tunnel, LAN IP, or custom domain — Authentik OAuth
    # paths are all proxied through Traefik on the same host:port as the frontend.
    # Authentik 2024.12+: authorize endpoint is /application/o/authorize/ (no app slug).
    _ext = _frontend_base_from_uri(redirect_uri)
    auth_url = f"{_ext}/application/o/authorize/?{urlencode(params)}"
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Exchange OIDC code for token, set httpOnly cookie, redirect to frontend."""
    redirect_uri = await _pop_state(state)
    if redirect_uri is None:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    import httpx

    token_url = f"{settings.authentik_url}/application/o/token/"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.oauth_client_id,
                "client_secret": settings.oauth_client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        tokens = resp.json()

    access_token = tokens["access_token"]

    # Verify token and upsert user into DB
    from app.auth.jwt import _verify_token
    from app.auth.user_service import upsert_user

    try:
        user_info = await _verify_token(access_token)
        await upsert_user(db, user_info)
        await db.commit()
    except Exception as exc:
        logger.warning("user_upsert_failed", error=str(exc))
        await db.rollback()

    # Retrieve next-path stored during login (default to /inbox)
    try:
        from app.utils.redis_client import get_async_redis
        _r = get_async_redis()
        _nkey = f"oauth_next:{state}"
        _pipe = _r.pipeline()
        _pipe.get(_nkey)
        _pipe.delete(_nkey)
        _next_path = ((await _pipe.execute())[0] or "/inbox")
    except Exception:
        _next_path = "/inbox"
    if not _next_path.startswith("/"):
        _next_path = "/inbox"

    logger.info("oidc_callback_success")

    # Determine cookie lifetime from token's 'expires_in' claim (or exp - iat),
    # falling back to a generous 8 hours for development.
    _token_ttl = int(tokens.get("expires_in", 0))
    if not _token_ttl:
        # Parse exp from JWT directly (no signature check needed here — already verified above)
        try:
            import base64 as _b64
            import json as _json
            _payload_b64 = access_token.split(".")[1]
            _payload_b64 += "=" * (-len(_payload_b64) % 4)
            _claims = _json.loads(_b64.urlsafe_b64decode(_payload_b64))
            import time as _time
            _token_ttl = max(0, int(_claims.get("exp", 0)) - int(_time.time()))
        except Exception:
            pass
    # Default to 8 hours if we couldn't determine expiry from the token
    _cookie_max_age = _token_ttl if _token_ttl > 60 else 28800

    is_production = settings.app_env == "production"
    cookie_opts = dict(
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=_cookie_max_age,
    )

    frontend_base = _frontend_base_from_uri(redirect_uri)
    resp = RedirectResponse(url=f"{frontend_base}{_next_path}", status_code=302)
    resp.set_cookie(key="access_token", value=access_token, path="/", **cookie_opts)
    return resp


# ── QR login (authenticated desktop → mobile, passwordless) ───────────────────
# Flow: an authenticated desktop calls /qr-login/create → mints a DURABLE session
# token for the caller and stores it under a short-lived, single-use QR token. The
# mobile app scans it and calls /qr-login/redeem → backend sets that session as the
# device's httpOnly cookie (long-lived, so the phone stays logged in). The session
# JWT lives only server-side (Redis) until redeemed — it never appears in the QR.
# Revoke all of a user's QR sessions via /api/admin/users/{sub}/revoke-sessions.

_QR_TTL_SECONDS = 120  # time to scan the QR; NOT the session lifetime


def _token_max_age(access_token: str) -> int:
    """Cookie lifetime from the JWT's exp claim (fallback 8h)."""
    try:
        import base64 as _b64
        import json as _json
        import time as _time
        p = access_token.split(".")[1]
        p += "=" * (-len(p) % 4)
        claims = _json.loads(_b64.urlsafe_b64decode(p))
        ttl = int(claims.get("exp", 0)) - int(_time.time())
        return ttl if ttl > 60 else 28800
    except Exception:
        return 28800


class QrRedeemRequest(BaseModel):
    token: str


@router.post("/qr-login/create")
async def qr_login_create(
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Mint a durable session for the caller and stash it under a single-use QR token.

    The scanning phone gets a long-lived session (see qr_login_session_ttl_minutes),
    so it stays logged in. Role/active status are still re-checked from the DB on
    every request, and the session can be revoked via the admin endpoint.
    """
    from app.auth.jwt import current_session_epoch, mint_local_session
    from app.utils.redis_client import get_async_redis

    ttl_seconds = max(3600, settings.qr_login_session_ttl_minutes * 60)
    epoch = await current_session_epoch(user.sub)
    session_jwt = mint_local_session(
        sub=user.sub,
        email=user.email,
        name=user.name,
        preferred_username=user.preferred_username,
        groups=[],  # role resolved from DB at verify time
        ttl_seconds=ttl_seconds,
        session_epoch=epoch,
    )
    r = get_async_redis()
    token = secrets.token_urlsafe(32)
    await r.setex(f"qrlogin:{token}", _QR_TTL_SECONDS, session_jwt)
    logger.info("qr_login_created", user=user.sub)
    return {"token": token, "expires_in": _QR_TTL_SECONDS}


@router.post("/qr-login/redeem")
async def qr_login_redeem(payload: QrRedeemRequest) -> Response:
    """Redeem a QR-login token: set the mobile device's session cookie. Public."""
    from app.utils.redis_client import get_async_redis
    r = get_async_redis()
    key = f"qrlogin:{payload.token}"
    pipe = r.pipeline()
    pipe.get(key)
    pipe.delete(key)  # single-use
    session_jwt = (await pipe.execute())[0]
    if not session_jwt:
        raise HTTPException(status_code=400, detail="QR-код недействителен или истёк")

    # Make sure the relayed session is still valid (not expired/revoked).
    from app.auth.jwt import _verify_token
    try:
        await _verify_token(session_jwt)
    except Exception:
        raise HTTPException(status_code=400, detail="Сессия истекла — обновите QR-код")

    is_production = settings.app_env == "production"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="access_token",
        value=session_jwt,
        path="/",
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=_token_max_age(session_jwt),
    )
    logger.info("qr_login_redeemed")
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    origin: str = Query(default=""),
) -> dict:
    """Clear auth cookie and return Authentik end-session URL for RP-initiated logout.

    `origin` should be window.location.origin from the browser — this ensures the
    returned URL works regardless of port (direct, SSH tunnel, LAN IP, etc.).
    Falls back to the Referer header, then to authentik_external_url.
    """
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("csrf_token", path="/")

    if settings.auth_enabled:
        from urllib.parse import urlencode

        # Derive base from: explicit origin param → Referer header → configured external URL
        base = (
            origin.rstrip("/")
            or _frontend_base_from_uri(request.headers.get("referer", ""))
            or settings.authentik_external_url.rstrip("/")
            or "http://localhost"
        )
        post_logout = f"{base}/auth/login"
        params = urlencode({"post_logout_redirect_uri": post_logout})
        logout_url = f"{base}/application/o/{settings.authentik_slug}/end-session/?{params}"
    else:
        logout_url = "/auth/login"

    return {"status": "logged_out", "logout_url": logout_url}


# ── User directory (for approval assignment) ──────────────────────────────────


@router.get("/users")
async def user_directory(
    role: str | None = Query(default=None),
    _user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List active users — used for approval assignment dropdowns."""
    from app.db.models import User

    stmt = select(User).where(User.is_active == True)  # noqa: E712
    if role:
        stmt = stmt.where(User.role == role)
    stmt = stmt.order_by(User.name)

    result = await db.execute(stmt)
    users = result.scalars().all()

    return [
        {
            "sub": u.sub,
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        }
        for u in users
    ]
