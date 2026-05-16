"""Auth API — OIDC login/logout, /me, /users endpoints."""

from __future__ import annotations

import secrets
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
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
    redirect_uri: str = Query(default="http://localhost:3000/auth/callback"),
    next: str = Query(default="/inbox"),
) -> RedirectResponse:
    """Redirect to Authentik OIDC authorization endpoint."""
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
    auth_url = (
        f"{settings.authentik_url}/application/o/{settings.authentik_slug}/authorize/"
        f"?{urlencode(params)}"
    )
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

    is_production = settings.app_env == "production"
    cookie_opts = dict(
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=3600,
    )

    frontend_base = _frontend_base_from_uri(redirect_uri)
    resp = RedirectResponse(url=f"{frontend_base}{_next_path}", status_code=302)
    resp.set_cookie(key="access_token", value=access_token, path="/", **cookie_opts)
    return resp


@router.post("/logout")
async def logout(response: Response) -> dict:
    """Clear auth cookie."""
    response.delete_cookie("access_token", path="/")
    return {"status": "logged_out"}


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
