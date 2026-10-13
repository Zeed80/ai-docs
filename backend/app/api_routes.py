"""Every API route of the app with its full path, whatever FastAPI's layout.

FastAPI 0.141 keeps an included router lazily (``_IncludedRouter`` with
``original_router`` and an ``include_context`` prefix) instead of copying its
routes into ``app.routes``. Code that walked ``app.routes`` saw 6 of ~1000
routes and silently found nothing: query-only arguments of POST endpoints went
into the JSON body and were dropped, the planner lost the response shapes and
the id check had no types (live 2026-10-13). Walk the tree instead.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi.routing import APIRoute


def _walk(routes: list[Any], prefix: str = ""):
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            context = getattr(route, "include_context", None)
            yield from _walk(
                route.original_router.routes, prefix + (getattr(context, "prefix", "") or "")
            )
        elif type(route).__name__ != "Mount" and hasattr(route, "routes"):
            yield from _walk(route.routes, prefix)


@lru_cache(maxsize=1)
def api_routes() -> tuple[tuple[str, APIRoute], ...]:
    """(full path, route) for every API route of the live app."""
    from app.main import app

    return tuple(_walk(app.routes))
