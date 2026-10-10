"""Security headers middleware — adds request ID, security headers, structured logging."""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger()

# The embedded live ComfyUI UI (studio Workflow tab) is a heavy third-party
# SPA we don't control the internals of — it needs `eval` (ICU message-format
# compilation in its i18n library), inline WASM (`data:` URIs) and `blob:`
# Web Workers to function at all, confirmed live: with the app's normal CSP
# applied, ComfyUI's own JS threw CSP violations on all three and never
# finished booting ("ComfyApp graph accessed before initialization"). CSP
# exists to contain OUR OWN app's content against XSS — that threat model
# doesn't apply to a self-hosted service we already trust and gate behind
# `get_current_user` in comfyui_proxy.py; skip it there rather than chase a
# looser policy that both changes with every ComfyUI frontend release and
# never guarantees full coverage of what its bundlers happen to need.
_CSP_EXEMPT_PATH_PREFIXES = ("/api/comfyui-proxy/",)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # A caller-supplied id is kept only if it looks like an id (E47):
        # it goes into every log line of the request.
        incoming = request.headers.get("X-Request-ID") or ""
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.monotonic()
        response: Response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        # SAMEORIGIN (not DENY) so the app can embed its own document/PDF
        # download responses in the review iframe; cross-origin framing
        # (clickjacking) is still blocked.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        from app.config import settings

        csp_exempt = any(request.url.path.startswith(p) for p in _CSP_EXEMPT_PATH_PREFIXES)
        # Найдено при приёмке (Правило 0): middleware перезаписывал CSP, уже
        # выставленный эндпоинтом, и более строгая политика выдачи вложений
        # (`default-src 'none'; sandbox`) молча заменялась на общеприложенческую
        # `script-src 'self'`. Ослаблять то, что эндпоинт ужесточил осознанно,
        # нельзя: обратное — можно и нужно.
        if (
            settings.csp_enabled
            and not csp_exempt
            and "Content-Security-Policy" not in response.headers
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss:; "
                "frame-src 'self' blob:; "
                # Chrome's built-in PDF viewer loads blob: PDFs via the object/
                # plugin context — without object-src it falls back to
                # default-src ('self') and the embedded PDF renders blank.
                "object-src 'self' blob:; "
                "frame-ancestors 'self';"
            )

        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
