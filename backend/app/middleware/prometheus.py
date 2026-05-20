"""Prometheus metrics middleware — records HTTP request counts and durations."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        from app.core import metrics

        # Normalise path to avoid high cardinality (strip UUIDs and numeric IDs)
        path = request.url.path
        for segment in path.split("/"):
            if _looks_like_id(segment):
                path = path.replace(segment, "{id}", 1)

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        method = request.method
        status = str(response.status_code)

        metrics.http_requests_total.labels(
            method=method, path=path, status=status
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=method, path=path
        ).observe(duration)

        return response


def _looks_like_id(segment: str) -> bool:
    if not segment:
        return False
    # UUID pattern
    if len(segment) == 36 and segment.count("-") == 4:
        return True
    # Pure numeric ID
    return segment.isdigit()
