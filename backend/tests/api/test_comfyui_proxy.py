"""Tests for the authenticated ComfyUI reverse proxy (Workflow tab iframe)."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from app.api import comfyui_proxy


def test_inject_base_href_into_plain_head_tag():
    html = "<!doctype html><html><head><title>x</title></head><body></body></html>"
    out = comfyui_proxy._inject_base_href(html)
    assert '<base href="/api/comfyui-proxy/">' in out
    assert out.index("<base") < out.index("<title>")


def test_inject_base_href_into_head_with_attributes():
    html = '<html><head lang="en"><title>x</title></head></html>'
    out = comfyui_proxy._inject_base_href(html)
    assert '<base href="/api/comfyui-proxy/">' in out
    assert out.index('<head lang="en">') < out.index("<base")


def test_inject_base_href_falls_back_when_no_head_tag():
    html = "<div>no head here</div>"
    out = comfyui_proxy._inject_base_href(html)
    assert out.startswith('<base href="/api/comfyui-proxy/">')
    assert "no head here" in out


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
