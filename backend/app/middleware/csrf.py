"""CSRF protection — double-submit cookie pattern."""

from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_EXEMPT_PATH_PREFIXES = ("/api/auth/",)


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

        csrf_cookie = request.cookies.get("csrf_token", "")
        csrf_header = request.headers.get("X-CSRF-Token", "")

        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            return JSONResponse(
                {"detail": "CSRF validation failed"},
                status_code=403,
            )

        return await call_next(request)
