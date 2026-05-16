"""Rate limiting middleware — sliding window via Redis."""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger()

_LOGIN_PATHS = {"/api/auth/login", "/api/auth/callback"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        from app.config import settings

        path = request.url.path

        if path in _LOGIN_PATHS:
            limit = settings.rate_limit_login_per_minute
            key_suffix = "login"
        elif path.startswith("/api/"):
            limit = settings.rate_limit_api_per_minute
            key_suffix = "api"
        else:
            return await call_next(request)

        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

        try:
            from app.utils.redis_client import get_async_redis

            r = get_async_redis()
            window = 60
            bucket = int(time.time()) // window
            key = f"rate:{key_suffix}:{client_ip}:{bucket}"
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, window * 2)
            if count > limit:
                logger.warning(
                    "rate_limit_exceeded",
                    ip=client_ip,
                    path=path,
                    count=count,
                )
                return JSONResponse(
                    {"detail": "Too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(window)},
                )
        except Exception:
            pass  # Redis unavailable → fail open

        return await call_next(request)
