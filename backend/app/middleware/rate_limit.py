"""Rate limiting middleware — sliding window via Redis, in-memory fallback for login."""

from __future__ import annotations

import collections
import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger()

_LOGIN_PATHS = {"/api/auth/login", "/api/auth/callback"}

# In-memory fallback: tracks (ip, bucket) → count for login paths when Redis is down.
# Deque caps at 4096 entries so it never grows unbounded.
_FALLBACK_LOGIN_LIMIT = 10  # requests per minute per IP
_fallback_counters: dict[tuple[str, int], int] = {}
_fallback_keys: collections.deque[tuple[str, int]] = collections.deque(maxlen=4096)


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

        # limit=0 means disabled (useful in test/dev environments)
        if limit == 0:
            return await call_next(request)

        # Only trust X-Forwarded-For when behind a known trusted proxy (Traefik/nginx).
        # Without this guard, clients can spoof arbitrary IPs and bypass rate limiting.
        _xff = request.headers.get("X-Forwarded-For", "")
        if _xff and settings.trusted_proxy:
            client_ip = _xff.split(",")[0].strip() or "unknown"
        else:
            client_ip = request.client.host if request.client else "unknown"

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
        except Exception as exc:
            logger.error("rate_limit_redis_unavailable", error=str(exc), path=path)
            # In-memory fallback — enforce only on login paths to prevent brute-force
            if path in _LOGIN_PATHS:
                bucket = int(time.time()) // 60
                fb_key = (client_ip, bucket)
                if fb_key not in _fallback_counters:
                    _fallback_keys.append(fb_key)
                    _fallback_counters[fb_key] = 0
                _fallback_counters[fb_key] += 1
                # Evict stale buckets when deque wraps around
                while len(_fallback_keys) == _fallback_keys.maxlen:
                    old = _fallback_keys[0]
                    if old[1] < bucket - 1:
                        _fallback_counters.pop(old, None)
                        _fallback_keys.popleft()
                    else:
                        break
                if _fallback_counters[fb_key] > _FALLBACK_LOGIN_LIMIT:
                    logger.warning(
                        "rate_limit_exceeded_fallback",
                        ip=client_ip,
                        path=path,
                        count=_fallback_counters[fb_key],
                    )
                    return JSONResponse(
                        {"detail": "Too many requests"},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )

        return await call_next(request)
