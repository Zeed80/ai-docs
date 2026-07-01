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


# Bridges a `postMessage` from our own parent page straight into ComfyUI's
# own internal load path — `window.comfyAPI.app.app` is the same App
# singleton ComfyUI itself calls `.loadApiJson(graph, name)` on when a user
# drags an API-format JSON file onto the canvas (confirmed by reading
# ComfyUI's own bundled JS: `if(this.isApiJson(e)){this.loadApiJson(e,r);
# return}` in its file-drop handler) — so this is not a hack layered on top,
# it's the exact same call ComfyUI makes for itself. Our stored
# ``ComfyWorkflow.graph`` is already in that API/prompt format, so no
# conversion is needed. Without this, "opening" a workflow only saves it to
# ComfyUI's userdata folder and the user still has to go find it via
# ComfyUI's own Workflow menu — this makes it appear on the canvas instantly.
#
# Served as an EXTERNAL same-origin script (not injected inline) because the
# app's global CSP is `script-src 'self'` — a real browser blocks inline
# `<script>` tags outright regardless of content (confirmed live: an inline
# version silently no-op'd, browser console showed a CSP violation that
# httpx-based testing never surfaces since it doesn't enforce CSP at all).
#
# `app.rootGraph` existing is NOT enough to call `loadApiJson` yet: ComfyUI
# registers its (and every custom node pack's) node types in a staggered,
# asynchronous process that keeps running for several seconds *after*
# `rootGraph` is already available — calling `loadApiJson` too early silently
# treats every node as unknown and adds nothing (confirmed live: identical
# call succeeded with 12s of extra wait, failed with none, no exception
# either time — `loadApiJson` doesn't surface "not ready yet" as an error).
# So this doesn't just poll for the function to exist — it calls it, checks
# whether the graph actually gained nodes, and retries the whole call if not.
_BRIDGE_JS = """
(function () {
  function getApp() {
    return window.comfyAPI && window.comfyAPI.app && window.comfyAPI.app.app;
  }
  window.addEventListener("message", function (ev) {
    if (ev.origin !== window.location.origin) return;
    if (!ev.data || ev.data.type !== "ai-docs-load-workflow") return;
    var graph = ev.data.graph, name = ev.data.name || "workflow";
    var expectedCount = Object.keys(graph || {}).length;
    (function tryLoad(retriesLeft) {
      var app = getApp();
      if (app && app.rootGraph && typeof app.loadApiJson === "function") {
        try {
          app.loadApiJson(graph, name);
          var gotCount = app.rootGraph._nodes ? app.rootGraph._nodes.length : 0;
          if (gotCount >= expectedCount && expectedCount > 0) return;
        } catch (e) {
          console.error("ai-docs-load-workflow failed", e);
        }
      }
      if (retriesLeft > 0) setTimeout(function () { tryLoad(retriesLeft - 1); }, 1000);
      else console.error("ai-docs-load-workflow: gave up after retries, node types may still be registering");
    })(25);
  });
})();
"""

_BRIDGE_JS_PATH = "__bridge.js"


def _inject_scaffolding(html: str) -> str:
    """So every relative request ComfyUI's JS makes resolves under our mount
    point instead of the page's own origin root — plus a same-origin
    `<script src>` for the postMessage bridge (see `_BRIDGE_JS`)."""
    import re

    tag = (
        f'<base href="{PROXY_MOUNT}/">'
        f'<script src="{PROXY_MOUNT}/{_BRIDGE_JS_PATH}"></script>'
    )
    if "<head>" in html:
        return html.replace("<head>", f"<head>{tag}", 1)
    new_html, n = re.subn(r"(<head[^>]*>)", rf"\1{tag}", html, count=1)
    return new_html if n else tag + html


@router.get(f"/{_BRIDGE_JS_PATH}")
async def bridge_js(user: UserInfo = Depends(get_current_user)) -> Response:
    return Response(content=_BRIDGE_JS, media_type="application/javascript")


# Registered BEFORE the catch-all below: Starlette matches HTTP routes in
# declaration order, and `/{path:path}` matches any suffix including
# `__bridge.js` — without this ordering the catch-all would shadow it and
# forward the request to ComfyUI instead of serving our own script.
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
        html = _inject_scaffolding(raw.decode("utf-8", errors="replace"))
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
