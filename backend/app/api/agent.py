"""Retired compatibility boundary for the former agent WebSocket lifecycle."""

import structlog
from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

router = APIRouter()
logger = structlog.get_logger()

_RETIREMENT_REASON = (
    "WebSocket chat lifecycle retired. Use POST /api/agent/chat-runs and the "
    "durable run, event, checkpoint, resume, and WorkOrder cancel endpoints."
)
_FALLBACK_CLOSE_REASON = "Agent WebSocket retired; use durable HTTP chat"


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket) -> None:
    """Reject the retired lifecycle before accepting or starting any model work.

    Starlette can return an HTTP response during the WebSocket handshake when
    the ASGI server advertises ``websocket.http.response``. Older servers do
    not have that extension, so they get a policy close instead. Both paths
    happen before ``accept`` and cannot create, resume, or cancel a chat turn.
    """

    logger.warning("ws_chat_retired", client=ws.client)
    response = JSONResponse(
        status_code=410,
        content={
            "code": "agent_ws_retired",
            "detail": _RETIREMENT_REASON,
            "replacement": "/api/agent/chat-runs",
        },
        headers={
            "Cache-Control": "no-store",
            "Deprecation": "true",
            "Link": '</api/agent/chat-runs>; rel="successor-version"',
        },
    )
    try:
        await ws.send_denial_response(response)
    except RuntimeError:
        # RFC 6455 leaves at most 123 UTF-8 bytes for an application close
        # reason (125-byte control frame minus the two-byte status code).
        # The complete migration detail is available in the 410 JSON response.
        await ws.close(code=1008, reason=_FALLBACK_CLOSE_REASON)
