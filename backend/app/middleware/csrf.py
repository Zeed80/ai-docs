"""CSRF protection — double-submit cookie pattern."""

from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The embedded ComfyUI UI (studio Workflow tab) makes its own POST/PUT calls
# (settings, userdata saves, custom-node extensions) straight from ComfyUI's
# own JS — it has no way to attach our `X-CSRF-Token` header, it doesn't know
# this app's custom scheme exists. Confirmed live: without this exemption
# ComfyUI's own internal requests get a blanket 403 and it never finishes
# initializing. This isn't a bare CSRF hole: `access_token` is
# `SameSite=Lax` (see app/api/auth.py) — cross-*site* state-changing requests
# already don't carry the cookie at all, which is the actual threat this
# middleware defends against; this route's own auth (get_current_user in
# comfyui_proxy.py) still gates it the same as every other endpoint.
_EXEMPT_PATH_PREFIXES = ("/api/auth/", "/api/comfyui-proxy/")


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        from app.config import settings

        path = request.url.path

        if request.method in _SAFE_METHODS:
            response = await call_next(request)
            if not request.cookies.get("csrf_token"):
                token = secrets.token_urlsafe(32)
                response.set_cookie(
                    "csrf_token",
                    token,
                    httponly=False,  # JS must read it for double-submit
                    secure=settings.app_env == "production",
                    samesite="strict",
                    max_age=86400,
                    path="/",
                )
            return response

        # Exempt auth flow endpoints
        if any(path.startswith(p) for p in _EXEMPT_PATH_PREFIXES):
            return await call_next(request)

        # Bearer-token requests (API/service accounts) bypass CSRF
        if request.headers.get("Authorization", "").startswith("Bearer "):
            return await call_next(request)

        # API-key requests also bypass CSRF
        if request.headers.get("X-API-Key"):
            return await call_next(request)

        # Dev mode: auth is disabled, skip CSRF entirely
        if not settings.auth_enabled:
            return await call_next(request)

        csrf_cookie = request.cookies.get("csrf_token", "")
        csrf_header = request.headers.get("X-CSRF-Token", "")

        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(
                {"detail": "CSRF validation failed"},
                status_code=403,
            )

        return await call_next(request)
