from __future__ import annotations

from fastapi import Depends, WebSocket

from app.auth.jwt import get_current_user_optional
from app.auth.models import UserInfo
from app.config import settings


def to_user_key(user: UserInfo | None) -> str:
    if user is None:
        return "anonymous"
    if user.sub:
        return user.sub
    if user.preferred_username:
        return user.preferred_username
    if user.email:
        return user.email
    return "anonymous"


async def get_user_key(user: UserInfo | None = Depends(get_current_user_optional)) -> str:
    return to_user_key(user)


async def get_ws_user_key(ws: WebSocket) -> str:
    """Resolve user key for a WebSocket connection.

    Mirrors get_current_user_optional logic:
    Priority: X-API-Key header → httpOnly cookie → Bearer header.
    Browsers send cookies automatically with same-origin WS, so this
    correctly identifies the same authenticated user as REST endpoints.
    """
    if not settings.auth_enabled:
        return "dev-user"

    from app.auth.jwt import _extract_bearer, _verify_api_key, _verify_token

    # Service-account API key header
    api_key_raw = ws.headers.get("x-api-key") or ws.headers.get("X-API-Key")
    if api_key_raw:
        try:
            user = await _verify_api_key(api_key_raw)
            return to_user_key(user)
        except Exception:
            pass

    # Cookie (sent automatically by browsers with same-origin WS connections)
    cookie_token = ws.cookies.get("access_token")
    # Bearer header (for non-browser clients / API integrations)
    bearer = _extract_bearer(ws)
    token = cookie_token or bearer

    if not token:
        return "anonymous"

    try:
        user = await _verify_token(token)
        return to_user_key(user)
    except Exception:
        return "anonymous"
