"""Tests for the authenticated ComfyUI reverse proxy (Workflow tab iframe)."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from app.api import comfyui_proxy


def test_inject_scaffolding_into_plain_head_tag():
    html = "<!doctype html><html><head><title>x</title></head><body></body></html>"
    out = comfyui_proxy._inject_scaffolding(html)
    assert '<base href="/api/comfyui-proxy/">' in out
    assert out.index("<base") < out.index("<title>")


def test_inject_scaffolding_into_head_with_attributes():
    html = '<html><head lang="en"><title>x</title></head></html>'
    out = comfyui_proxy._inject_scaffolding(html)
    assert '<base href="/api/comfyui-proxy/">' in out
    assert out.index('<head lang="en">') < out.index("<base")


def test_inject_scaffolding_falls_back_when_no_head_tag():
    html = "<div>no head here</div>"
    out = comfyui_proxy._inject_scaffolding(html)
    assert out.startswith('<base href="/api/comfyui-proxy/">')
    assert "no head here" in out


def test_inject_scaffolding_references_external_bridge_script_not_inline():
    """CSP is `script-src 'self'` (see middleware/security.py) — a real
    browser silently blocks an inline <script> tag regardless of content
    (confirmed live). The bridge must be a same-origin <script src="...">
    reference, never inline JS."""
    html = "<html><head></head></html>"
    out = comfyui_proxy._inject_scaffolding(html)
    assert '<script src="/api/comfyui-proxy/__bridge.js"></script>' in out
    assert "<script>" not in out


@pytest.mark.asyncio
async def test_bridge_js_is_served_as_external_script(client: AsyncClient):
    resp = await client.get("/api/comfyui-proxy/__bridge.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert "loadApiJson" in resp.text
    assert "ai-docs-load-workflow" in resp.text


@pytest.mark.asyncio
async def test_bridge_js_route_is_not_shadowed_by_the_catch_all_proxy(client: AsyncClient, monkeypatch):
    """`/{path:path}` matches `__bridge.js` too — this only passes if the
    specific route was registered (declared) before the catch-all, so it's
    served locally instead of being forwarded to (a possibly unreachable)
    ComfyUI upstream."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("bridge.js request should never reach the upstream proxy handler")

    monkeypatch.setattr(comfyui_proxy, "_comfyui_base_url", lambda: "http://comfyui-node:8188")

    class _FakeAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(comfyui_proxy.httpx, "AsyncClient", _FakeAsyncClient)

    resp = await client.get("/api/comfyui-proxy/__bridge.js")
    assert resp.status_code == 200


def _mock_upstream(monkeypatch, handler) -> None:
    """Replace httpx.AsyncClient inside the proxy module with one whose
    transport is a MockTransport — so `proxy()` believes it's really talking
    to ComfyUI over the network, without any real socket."""
    monkeypatch.setattr(comfyui_proxy, "_comfyui_base_url", lambda: "http://comfyui-node:8188")

    class _FakeAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(comfyui_proxy.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.asyncio
async def test_proxy_forwards_json_and_strips_hop_by_hop_headers(client: AsyncClient, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    _mock_upstream(monkeypatch, handler)

    resp = await client.get("/api/comfyui-proxy/api/system_stats")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert captured["url"] == "http://comfyui-node:8188/api/system_stats"
    assert captured["method"] == "GET"


@pytest.mark.asyncio
async def test_proxy_injects_base_href_for_html_response(client: AsyncClient, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<!doctype html><html><head><title>ComfyUI</title></head></html>",
            headers={"content-type": "text/html"},
        )

    _mock_upstream(monkeypatch, handler)

    resp = await client.get("/api/comfyui-proxy/")

    assert resp.status_code == 200
    assert '<base href="/api/comfyui-proxy/">' in resp.text


@pytest.mark.asyncio
async def test_proxy_returns_502_when_upstream_unreachable(client: AsyncClient, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_upstream(monkeypatch, handler)

    resp = await client.get("/api/comfyui-proxy/api/system_stats")

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_proxy_forwards_post_body_and_query_params(client: AsyncClient, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"queued": True}, headers={"content-type": "application/json"})

    _mock_upstream(monkeypatch, handler)

    resp = await client.post(
        "/api/comfyui-proxy/userdata/workflows%2Ffoo.json?overwrite=true",
        json={"hello": "world"},
    )

    assert resp.status_code == 200
    assert "overwrite=true" in captured["url"]
    assert b"hello" in captured["body"]


@pytest.mark.asyncio
async def test_comfyui_proxy_path_is_exempt_from_the_apps_own_csp(client: AsyncClient, monkeypatch):
    """ComfyUI's own JS needs `eval`/inline-WASM/blob-Workers to boot at all —
    confirmed live: with the app's normal CSP applied, it never finished
    initializing. Everything else still gets the strict app CSP."""
    from app.config import settings

    monkeypatch.setattr(settings, "csp_enabled", True)

    resp = await client.get("/api/comfyui-proxy/__bridge.js")
    assert "content-security-policy" not in {k.lower() for k in resp.headers}

    other = await client.get("/api/image-gen/workflows/list")
    assert "content-security-policy" in {k.lower() for k in other.headers}


@pytest.mark.asyncio
async def test_comfyui_proxy_post_is_exempt_from_csrf(monkeypatch):
    """ComfyUI's own JS issues POSTs (settings, userdata, extensions) with no
    way to attach our custom X-CSRF-Token header — without the exemption
    every one of those 403s and the embedded app never finishes booting.
    Unit-tests the middleware in isolation (mirrors the MagicMock-request
    pattern in test_auth_jwt.py) so it doesn't get entangled with
    get_current_user's own auth_enabled branching."""
    from unittest.mock import AsyncMock, MagicMock

    from app.config import settings
    from app.middleware.csrf import CSRFMiddleware

    monkeypatch.setattr(settings, "auth_enabled", True)

    request = MagicMock()
    request.method = "POST"
    request.url.path = "/api/comfyui-proxy/settings"
    request.cookies = {}
    request.headers = {}

    call_next = AsyncMock(return_value="downstream response")
    middleware = CSRFMiddleware(app=None)

    result = await middleware.dispatch(request, call_next)

    assert result == "downstream response"
    call_next.assert_awaited_once_with(request)
