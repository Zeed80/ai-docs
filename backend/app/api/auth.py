"""Auth API — OIDC login/logout, /me endpoint."""

import secrets

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.config import settings

router = APIRouter()
logger = structlog.get_logger()

# In-memory state store for PKCE/CSRF (production: use Redis)
_state_store: dict[str, str] = {}


@router.get("/me", response_model=UserInfo)
async def me(user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """Return current user info. Dev mode returns dev user."""
    return user


@router.get("/login")
async def login(redirect_uri: str = Query(default="http://localhost:3000/auth/callback")) -> RedirectResponse:
    """Redirect to Authentik OIDC authorization endpoint."""
    if not settings.auth_enabled:
        return RedirectResponse(url="/")

    state = secrets.token_urlsafe(32)
    _state_store[state] = redirect_uri

    params = {
        "response_type": "code",
        "client_id": settings.oauth_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email groups",
        "state": state,
    }
    from urllib.parse import urlencode
    auth_url = (
        f"{settings.authentik_url}/application/o/{settings.authentik_slug}/authorize/"
        f"?{urlencode(params)}"
    )
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def callback(
    code: str = Query(...),
    state: str = Query(...),
) -> dict:
    """Exchange OIDC code for tokens. Frontend handles redirect."""
    if state not in _state_store:
        raise HTTPException(status_code=400, detail="Invalid state")

    import httpx
    token_url = f"{settings.authentik_url}/application/o/token/"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(token_url, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.oauth_client_id,
            "client_secret": settings.oauth_client_secret,
            "redirect_uri": _state_store.pop(state),
        })
        resp.raise_for_status()
        tokens = resp.json()

    logger.info("oidc_callback_success")
    return {"access_token": tokens["access_token"], "token_type": "bearer"}


@router.post("/logout")
async def logout(response: Response) -> dict:
    """Clear session. Frontend should delete stored token."""
    response.delete_cookie("session")
    return {"status": "logged_out"}
