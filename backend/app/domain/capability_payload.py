"""How the capability gateway turns an agent's arguments into a request body.

Shared by the gateway (``capability_router.dispatch_capability``) and by the
agent, which must sign the exact body a recipient will receive when a
work-budget handoff travels through the gateway.
"""

from __future__ import annotations

from typing import Any


def capability_proxy_body(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments minus action/reason, with nested filters/body flattened."""
    body = dict(args)
    body.pop("action", None)
    body.pop("reason", None)
    flatten_capability_body(body)
    return body


def flatten_capability_body(body: dict[str, Any]) -> None:
    """Flatten nested ``filters`` and ``body`` into top-level args, in place."""
    if "filters" in body and isinstance(body["filters"], dict):
        body.update(body.pop("filters"))
    if "body" in body and isinstance(body["body"], dict):
        body.update(body.pop("body"))
