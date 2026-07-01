"""Authenticated reverse proxy for the live ComfyUI web UI.

ComfyUI runs on a separate on-prem GPU node, reachable only from the backend's
own network (its base_url is an internal host, e.g. ``host-gateway:8188`` —
never resolvable from a user's browser). This proxy lets an already-
authenticated ``/studio`` session load ComfyUI's own node-graph editor in an
iframe without exposing that node directly to the internet: no new domain/TLS
is needed because the iframe stays same-origin, so the existing httpOnly
``access_token`` cookie is sent automatically and ``get_current_user`` covers
it exactly like any other endpoint.

ComfyUI's frontend (as served) uses root-relative asset/API paths — bare
``user.css``, ``./assets/...``, ``api/userdata/...`` — rather than absolute-
from-domain-root ones, so injecting ``<base href="...">`` into its index.html
is enough for every request it makes to resolve correctly under our prefix.
No JS rewriting is needed (and would be fragile against upstream updates).
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, WebSocket
from starlette.responses import Response, StreamingResponse

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo

logger = structlog.get_logger()
router = APIRouter()

PROXY_MOUNT = "/api/comfyui-proxy"

# Headers that must not be forwarded verbatim in either direction — they
# describe THIS hop's transport (chunking, keep-alive, the wrong Host), not
# the resource itself; letting them through corrupts the proxied response
# (stale Content-Length after we rewrite HTML, wrong Host on the upstream
# request, etc).
_STRIP_REQUEST_HEADERS = {"host", "content-length", "connection"}
_STRIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "connection", "transfer-encoding",
    "keep-alive", "x-frame-options", "content-security-policy",
}


def _comfyui_base_url() -> str:
    from app.ai.comfyui_client import ComfyUIClient

    return ComfyUIClient.from_registry().base_url


def _inject_base_href(html: str) -> str:
    """So every relative request ComfyUI's JS makes resolves under our mount
    point instead of the page's own origin root."""
    import re

    tag = f'<base href="{PROXY_MOUNT}/">'
    if "<head>" in html:
        return html.replace("<head>", f"<head>{tag}", 1)
    new_html, n = re.subn(r"(<head[^>]*>)", rf"\1{tag}", html, count=1)
    return new_html if n else tag + html


@router.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def proxy(
    path: str,
    request: Request,
    user: UserInfo = Depends(get_current_user),
):
    base_url = _comfyui_base_url()
    upstream_url = f"{base_url}/{path}"
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
    }

    client = httpx.AsyncClient(timeout=60.0)
    try:
        req = client.build_request(
            request.method, upstream_url, params=request.query_params, content=body, headers=headers
        )
        upstream_resp = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning("comfyui_proxy_unreachable", url=upstream_url, error=str(exc))
        return Response(
            content="ComfyUI сервер сейчас недоступен.", status_code=502, media_type="text/plain"
        )

    content_type = upstream_resp.headers.get("content-type", "")
    resp_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS
    }

    if "text/html" in content_type:
        raw = await upstream_resp.aread()
        await upstream_resp.aclose()
        await client.aclose()
        html = _inject_base_href(raw.decode("utf-8", errors="replace"))
        return Response(
            content=html.encode("utf-8"),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=content_type,
        )

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(), status_code=upstream_resp.status_code, headers=resp_headers, media_type=content_type
    )


@router.websocket("/ws")
async def proxy_ws(
    websocket: WebSocket, user: UserInfo = Depends(get_current_user)
) -> None:
    """Bridges the browser's ComfyUI-frontend WebSocket (live queue/progress
    events) to the real ComfyUI node's ``/ws``. ``get_current_user`` works
    for both HTTP and WS connections (it takes the base ``HTTPConnection``,
    see ``app.auth.jwt``) — FastAPI closes the socket with an auth error
    automatically if the dependency raises."""
    await websocket.accept()

    import websockets

    base_url = _comfyui_base_url()
    ws_scheme = "wss" if base_url.startswith("https://") else "ws"
    ws_url = ws_scheme + "://" + base_url.split("://", 1)[1] + "/ws"
    query = str(websocket.url.query)
    if query:
        ws_url = f"{ws_url}?{query}"

    try:
        async with websockets.connect(ws_url, max_size=None) as upstream:

            async def client_to_upstream() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, (bytes, bytearray)):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as exc:  # noqa: BLE001 — best-effort bridge, never crash the server
        logger.debug("comfyui_proxy_ws_closed", error=str(exc))
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
