from __future__ import annotations

from fastapi import Depends, WebSocket

from app.auth.jwt import get_current_user_optional
from app.auth.models import UserInfo


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
    # WebSocket auth is currently optional for the built-in panel;
    # keep behavior compatible and isolate chats by authenticated subject when available.
    auth_header = ws.headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return "anonymous"
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return "anonymous"
    try:
        # Reuse JWT validation logic; if verification fails, keep anonymous key.
        from app.auth.jwt import _verify_token

        user = await _verify_token(token)
        return to_user_key(user)
    except Exception:
        return "anonymous"
