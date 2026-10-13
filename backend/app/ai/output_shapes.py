"""T2: what each capability action returns, for the planner.

The planner saw every action's parameters but not its answer, and guessed
``${steps.X.output.result...}`` paths: ``result.supplier_id`` where the answer
was ``result.results[0].id``, ``results`` where ``invoices.list`` has
``items`` (live 2026-10-10). The shape comes from the endpoint's own
``response_model`` — the source of truth for the answer, as the Pydantic
schemas are for the parameters — through the same method and path the
capability gateway proxies to. A list answer reaches a step as
``{items, total}`` (the gateway wraps it), and is described that way.
"""

from __future__ import annotations

import typing
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

_MAX_FIELDS = 10


def _model_of(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _list_item(annotation: Any) -> Any | None:
    origin = typing.get_origin(annotation)
    if origin in (list, tuple, set):
        args = typing.get_args(annotation)
        return args[0] if args else None
    return None


def _strip_optional(annotation: Any) -> Any:
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if typing.get_origin(annotation) in (
        typing.Union,
        getattr(__import__("types"), "UnionType", None),
    ):
        return args[0] if len(args) == 1 else annotation
    return annotation


def _fields(model: type[BaseModel], depth: int) -> str:
    parts: list[str] = []
    for name, field in list(model.model_fields.items())[:_MAX_FIELDS]:
        annotation = _strip_optional(field.annotation)
        item = _list_item(annotation)
        nested = _model_of(item) if item is not None else _model_of(annotation)
        if depth > 0 and nested is not None:
            inner = _fields(nested, depth - 1)
            parts.append(f"{name}:[{inner}]" if item is not None else f"{name}:{inner}")
        else:
            parts.append(name)
    more = len(model.model_fields) > _MAX_FIELDS
    return "{" + ",".join(parts) + (",…" if more else "") + "}"


def describe(response_model: Any) -> str | None:
    """A compact shape like ``{items:[{id,name,…}],total}``; None if unknown."""
    if response_model is None:
        return None
    item = _list_item(response_model)
    if item is not None:
        model = _model_of(item)
        return f"{{items:[{_fields(model, 1) if model else 'value'}],total}}"
    model = _model_of(_strip_optional(response_model))
    return _fields(model, 1) if model is not None else None


@lru_cache(maxsize=1)
def action_output_shapes() -> dict[str, str]:
    """``capability.action`` → shape, for every action whose endpoint declares
    a response model. Built once per process from the live routes."""
    from fastapi.routing import APIRoute

    from app.ai.tool_catalog import TOOLS
    from app.main import app

    routes: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or ():
                routes[(method, route.path)] = route.response_model
    shapes: dict[str, str] = {}
    for name, tool in TOOLS.items():
        shape = describe(routes.get((tool.method, tool.path)))
        if shape:
            shapes[name] = shape
    return shapes


@lru_cache(maxsize=1)
def action_uuid_path_params() -> dict[str, frozenset[str]]:
    """``capability.action`` → its path parameters the endpoint types as UUID.

    E52: "Утверди счёт EV-8401" (a number, not an id, of an invoice the
    employee could not see) went to the owner as an approval card; the
    endpoint would have refused it anyway. Built once from the live routes.
    """
    import uuid as _uuid

    from fastapi.routing import APIRoute

    from app.ai.tool_catalog import TOOLS
    from app.main import app

    routes: dict[tuple[str, str], frozenset[str]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            ids = frozenset(
                p.name for p in route.dependant.path_params if p.field_info.annotation is _uuid.UUID
            )
            for method in route.methods or ():
                routes[(method, route.path)] = ids
    return {name: routes.get((tool.method, tool.path), frozenset()) for name, tool in TOOLS.items()}
