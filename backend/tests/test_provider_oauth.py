"""Sign-in through the provider's site for cloud providers (app/ai/provider_oauth.py)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import urllib.parse

import httpx
import pytest

from app.ai import provider_oauth as po
from app.ai.schemas import ProviderKind

_RealAsyncClient = httpx.AsyncClient
_RealClient = httpx.Client


def _route(monkeypatch, handler):
    """Every httpx client in provider_oauth goes through ``handler``."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        po.httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=transport, **kw)
    )
    monkeypatch.setattr(po.httpx, "Client", lambda **kw: _RealClient(transport=transport, **kw))


def _jwt(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _form(request: httpx.Request) -> dict:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def test_only_the_five_providers_offer_sign_in():
    assert set(po.SPECS) == {"openrouter", "minimax", "moonshot", "openai", "xai"}
    for excluded in ("anthropic", "gemini", "dashscope", "ollama_cloud", "deepseek"):
        assert po.spec_for(excluded) is None


def test_kimi_device_flow_start_and_poll(monkeypatch):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/api/oauth/device_authorization":
            assert _form(request)["client_id"] == po.KIMI_CLIENT_ID
            assert request.headers["X-Msh-Platform"] == "kimi_cli"
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://www.kimi.com/code/authorize",
                    "verification_uri_complete": "https://www.kimi.com/code/authorize?user_code=ABCD",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        form = _form(request)
        assert form["grant_type"] == po.DEVICE_CODE_GRANT and form["device_code"] == "dc"
        if len([c for c in calls if c == "/api/oauth/token"]) == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 900}
        )

    _route(monkeypatch, handler)
    started = asyncio.run(po.start("moonshot", "inst-1", origin="https://site"))
    assert started["type"] == "device" and started["user_code"] == "ABCD-EFGH"
    assert started["verification_uri"].endswith("user_code=ABCD")

    status, state, _ = asyncio.run(po.poll(started["flow_id"]))
    assert status == "pending" and state is None
    status, state, _ = asyncio.run(po.poll(started["flow_id"]))
    assert status == "success"
    assert state["access"] == "at" and state["refresh"] == "rt"
    assert state["expires_at"] > time.time() + 800


def test_minimax_state_mismatch_is_refused(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "user_code": "U",
                "verification_uri": "https://account.minimax.io/verify",
                "expired_in": int((time.time() + 600) * 1000),
                "state": "someone-else",
            },
        )

    _route(monkeypatch, handler)
    with pytest.raises(po.OAuthError, match="state"):
        asyncio.run(po.start("minimax", "i", origin="https://site"))


def test_minimax_user_code_flow_uses_pkce_and_absolute_expiry(monkeypatch):
    seen = {}

    def handler(request):
        form = _form(request)
        if request.url.path == "/oauth2/device/code":
            seen["challenge"] = form["code_challenge"]
            assert form["scope"] == po.MINIMAX_SCOPE
            return httpx.Response(
                200,
                json={
                    "user_code": "U-1",
                    "verification_uri": "https://account.minimax.io/verify",
                    "expired_in": int((time.time() + 600) * 1000),
                    "interval": 2000,
                    "state": form["state"],
                },
            )
        assert form["grant_type"] == po.MINIMAX_USER_CODE_GRANT
        verifier = form["code_verifier"]
        import hashlib

        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        assert challenge.decode() == seen["challenge"]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "mm-at",
                "refresh_token": "mm-rt",
                "expired_in": int((time.time() + 3600) * 1000),
            },
        )

    _route(monkeypatch, handler)
    started = asyncio.run(po.start("minimax", "i", origin="https://site", region="global"))
    assert started["interval"] == 2  # milliseconds normalised
    status, state, _ = asyncio.run(po.poll(started["flow_id"]))
    assert status == "success"
    assert state["base_url"] == "https://api.minimax.io/anthropic"
    assert 3500 < state["expires_at"] - time.time() < 3700


def test_codex_device_flow(monkeypatch):
    def handler(request):
        if request.url.path == "/api/accounts/deviceauth/usercode":
            return httpx.Response(
                200, json={"user_code": "CODE-1", "device_auth_id": "da", "interval": 5}
            )
        if request.url.path == "/api/accounts/deviceauth/token":
            return httpx.Response(200, json={"authorization_code": "ac", "code_verifier": "cv"})
        form = _form(request)
        assert form["redirect_uri"] == "https://auth.openai.com/deviceauth/callback"
        assert form["code_verifier"] == "cv"
        return httpx.Response(200, json={"access_token": _jwt({"exp": 9e9}), "refresh_token": "r"})

    _route(monkeypatch, handler)
    started = asyncio.run(po.start("openai", "i", origin="https://site"))
    assert started["verification_uri"] == "https://auth.openai.com/codex/device"
    status, state, _ = asyncio.run(po.poll(started["flow_id"]))
    assert status == "success" and state["refresh"] == "r"


def test_codex_pending_is_403(monkeypatch):
    def handler(request):
        if request.url.path.endswith("usercode"):
            return httpx.Response(200, json={"user_code": "C", "device_auth_id": "d"})
        return httpx.Response(403)

    _route(monkeypatch, handler)
    started = asyncio.run(po.start("openai", "i", origin="https://site"))
    assert asyncio.run(po.poll(started["flow_id"]))[0] == "pending"


def test_xai_endpoints_must_stay_on_x_ai(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "device_authorization_endpoint": "https://evil.example/device",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            },
        )

    _route(monkeypatch, handler)
    with pytest.raises(po.OAuthError, match="x.ai"):
        asyncio.run(po.start("xai", "i", origin="https://site"))


def test_openrouter_redirect_and_key_exchange(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["code"] == "the-code" and body["code_challenge_method"] == "S256"
        return httpx.Response(200, json={"key": "sk-or-v1-user"})

    _route(monkeypatch, handler)
    started = asyncio.run(po.start("openrouter", "i", origin="https://ptsai.ru"))
    assert started["type"] == "redirect"
    url = httpx.URL(started["authorize_url"])
    assert url.host == "openrouter.ai" and url.params["code_challenge_method"] == "S256"
    callback = url.params["callback_url"]
    assert callback.startswith("https://ptsai.ru/api/providers/oauth/openrouter/callback?state=")
    flow = po.load_flow(started["flow_id"])
    assert asyncio.run(po.exchange_openrouter_code(flow, "the-code")) == "sk-or-v1-user"


def test_expiring_token_is_refreshed_once_and_persisted(monkeypatch):
    refreshes = []
    persisted = []

    def handler(request):
        refreshes.append(_form(request)["refresh_token"])
        time.sleep(0.3)
        return httpx.Response(
            200, json={"access_token": "new-at", "refresh_token": "rt-2", "expires_in": 3600}
        )

    _route(monkeypatch, handler)
    monkeypatch.setattr(po, "persist_state_sync", lambda iid, st: persisted.append(st))
    instance = f"inst-{time.time()}"
    blob = po.encode_state(
        {"access": "old", "refresh": "rt-1", "expires_at": time.time() + 10,
         "token_endpoint": "https://auth.x.ai/oauth2/token"}
    )  # fmt: skip

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(po.runtime_credentials(instance, "xai", blob))
        )
        for _ in range(3)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # The rotating refresh token went out once; everyone got the new access token.
    assert refreshes == ["rt-1"]
    assert {r.token for r in results} == {"new-at"}
    assert persisted and persisted[0]["refresh"] == "rt-2"
    assert results[0].api == "responses" and results[0].base_url == "https://api.x.ai/v1"


def test_dead_refresh_token_is_a_clear_error(monkeypatch):
    _route(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    blob = po.encode_state({"access": "a", "refresh": "r", "expires_at": time.time() - 5})
    with pytest.raises(po.OAuthError, match="войдите заново"):
        po.runtime_credentials(f"dead-{time.time()}", "openai", blob)


def test_codex_headers_come_from_the_jwt():
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc-1",
                "chatgpt_data_residency": "eu",
            }
        }
    )
    headers = po.codex_headers(token)
    assert headers["ChatGPT-Account-ID"] == "acc-1"
    assert headers["x-openai-internal-codex-residency"] == "eu"
    assert headers["originator"] == po.CODEX_ORIGINATOR


def test_registry_resolves_a_signed_in_node(monkeypatch):
    from app.ai import provider_registry as pr

    state = {"access": "kimi-at", "refresh": "r", "expires_at": time.time() + 3600}
    row = {
        "id": f"row-{time.time()}",
        "kind": "moonshot",
        "name": "moonshot (default)",
        "auth_mode": "oauth",
        "oauth_encrypted": po.encode_state(state),
        "extra": {},
    }
    node = pr._row_to_resolved(row)
    assert node.api_key == "kimi-at"
    assert node.base_url == "https://api.kimi.com/coding/v1"
    assert node.extra["api"] == "openai" and node.extra["oauth"] is True
    assert node.extra["headers"]["X-Msh-Platform"] == "kimi_cli"
    assert node.is_local is False

    row["oauth_encrypted"] = None
    row["id"] = f"empty-{time.time()}"
    assert pr._row_to_resolved(row) is None  # no sign-in: not routable


def test_chat_messages_become_responses_items():
    from app.ai.providers.openai_responses import chat_to_responses, chat_tools_to_responses

    instructions, items = chat_to_responses(
        [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Сколько счетов?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "invoices", "arguments": {"status": "new"}}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"total": 3}'},
        ]
    )
    assert instructions == "Be brief."
    assert items[0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "Сколько счетов?"}],
    }
    assert items[1] == {
        "type": "function_call", "call_id": "c1", "name": "invoices", "arguments": '{"status": "new"}'
    }  # fmt: skip
    assert items[2] == {"type": "function_call_output", "call_id": "c1", "output": '{"total": 3}'}
    tools = chat_tools_to_responses(
        [{"type": "function", "function": {"name": "invoices", "parameters": {"type": "object"}}}]
    )
    assert tools == [
        {
            "type": "function",
            "name": "invoices",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]


def test_responses_stream_folds_text_and_tool_calls():
    from app.ai.providers.openai_responses import ResponsesStream, build_body

    s = ResponsesStream()
    s.feed({"type": "response.output_text.delta", "delta": "Всего "})
    s.feed({"type": "response.output_text.delta", "delta": "3"})
    s.feed(
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c9", "name": "f", "arguments": "{}"},
        }
    )
    s.feed({"type": "response.completed", "response": {"usage": {"input_tokens": 5}}})
    assert "".join(s.text) == "Всего 3"
    assert s.tool_calls()[0]["id"] == "c9" and s.usage == {"input_tokens": 5}

    body = build_body(model="m", instructions="i", items=[], max_output_tokens=10, codex=True)
    assert body["store"] is False and body["stream"] is True and "max_output_tokens" not in body


def test_anthropic_provider_bearer_mode_for_minimax():
    from app.ai.providers.anthropic_provider import AnthropicProvider
    from app.ai.schemas import ProviderConfig

    cfg = ProviderConfig(
        kind=ProviderKind.MINIMAX, base_url="https://api.minimax.io/anthropic", api_key="tok"
    )
    p = AnthropicProvider(cfg, bearer_auth=True)
    h = p._headers()
    assert h["Authorization"] == "Bearer tok" and "x-api-key" not in h
    assert p._base() == "https://api.minimax.io/anthropic/v1"
    assert p.kind == ProviderKind.MINIMAX


def test_direct_helpers_refuse_a_non_chat_completions_node(monkeypatch):
    from types import SimpleNamespace

    from app.ai import model_resolver

    monkeypatch.setattr(
        model_resolver,
        "_resolved_node",
        lambda provider: SimpleNamespace(extra={"api": "responses"}),
    )
    with pytest.raises(RuntimeError, match="основной маршрутизатор"):
        model_resolver.require_chat_completions("openai")
    monkeypatch.setattr(
        model_resolver, "_resolved_node", lambda provider: SimpleNamespace(extra={"api": "openai"})
    )
    model_resolver.require_chat_completions("moonshot")  # Kimi Code speaks Chat Completions


@pytest.mark.asyncio
async def test_api_sign_in_saves_tokens_and_never_returns_them(client, monkeypatch):
    created = await client.post(
        "/api/providers", json={"kind": "moonshot", "name": f"kimi-{time.time()}"}
    )
    assert created.status_code == 200, created.text
    inst_id = created.json()["id"]

    async def fake_start(kind, instance_id, *, origin, region=None):
        po._save_flow("flow-x", {"kind": kind, "instance_id": instance_id, "origin": origin}, 600)
        return {"flow_id": "flow-x", "type": "device", "user_code": "AB-CD",
                "verification_uri": "https://kimi", "expires_at": time.time() + 600, "interval": 5}  # fmt: skip

    async def fake_poll(flow_id):
        return "success", {"access": "SECRET-AT", "refresh": "SECRET-RT",
                           "expires_at": time.time() + 3600, "account": "me@kimi"}, None  # fmt: skip

    monkeypatch.setattr(po, "start", fake_start)
    monkeypatch.setattr(po, "poll", fake_poll)
    r = await client.post(f"/api/providers/{inst_id}/oauth/start", json={"origin": "https://site"})
    assert r.status_code == 200 and r.json()["user_code"] == "AB-CD"
    r = await client.post("/api/providers/oauth/flows/flow-x/poll")
    body = r.json()
    assert body["status"] == "success"
    assert body["instance"]["auth_mode"] == "oauth"
    assert body["instance"]["oauth"]["account"] == "me@kimi"
    assert "SECRET" not in r.text

    listed = await client.get("/api/providers")
    assert "SECRET" not in listed.text
    kimi = next(k for k in listed.json()["known_kinds"] if k["kind"] == "moonshot")
    assert kimi["oauth"]["flow"] == "device"
    assert (
        next(k for k in listed.json()["known_kinds"] if k["kind"] == "anthropic")["oauth"] is None
    )

    out = await client.post(f"/api/providers/{inst_id}/oauth/logout")
    assert out.json()["auth_mode"] == "api_key" and out.json()["oauth"] == {}


@pytest.mark.asyncio
async def test_api_openrouter_callback_saves_the_key(client, monkeypatch):
    created = await client.post(
        "/api/providers", json={"kind": "openrouter", "name": f"or-{time.time()}"}
    )
    inst_id = created.json()["id"]
    po._save_flow(
        "or-flow",
        {
            "kind": "openrouter",
            "instance_id": inst_id,
            "origin": "https://ptsai.ru",
            "verifier": "v",
        },
        600,
    )

    async def fake_exchange(flow, code):
        assert code == "c0de"
        return "sk-or-v1-user"

    monkeypatch.setattr(po, "exchange_openrouter_code", fake_exchange)
    r = await client.get(
        "/api/providers/oauth/openrouter/callback",
        params={"state": "or-flow", "code": "c0de"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == (
        "https://ptsai.ru/settings/models?tab=overview&oauth=openrouter&oauth_ok=1"
    )
    listed = await client.get("/api/providers")
    row = next(i for i in listed.json()["instances"] if i["id"] == inst_id)
    assert row["api_key_set"] and "sk-or-v1-user" not in listed.text
    assert row["extra"]["key_source"] == "openrouter_oauth"


def test_minimax_resource_url_with_v1_is_not_doubled(monkeypatch):
    # MiniMax answers the token request with resource_url ".../anthropic/v1".
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path != "/anthropic/v1/messages":
            return httpx.Response(404, text="404 page not found")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    _route(monkeypatch, handler)
    rt = po.runtime_for(
        "minimax", {"access": "t", "base_url": "https://api.minimax.io/anthropic/v1"}
    )
    assert rt.base_url == "https://api.minimax.io/anthropic"
    models = asyncio.run(po.list_models("minimax", rt))
    assert models and seen == ["https://api.minimax.io/anthropic/v1/messages"]
