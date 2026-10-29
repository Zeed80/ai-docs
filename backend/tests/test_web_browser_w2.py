"""W2 on the backend side: every read and session names the person's kept
profile, its bound proxy and a persona that agrees with that proxy."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.ai import web_search_config as wsc
from app.api import web_search as ws


def _proxy(pid: str, **kw) -> wsc.BrowserProxy:
    return wsc.BrowserProxy(id=pid, server=f"http://{pid}.example:8000", **kw)


def test_without_proxies_the_profile_goes_direct_with_the_default_persona():
    fields = ws.browser_profile_fields("alice", cfg=wsc.WebSearchConfig())
    assert fields == {
        "profile_owner": "alice",
        "profile": "reader",
        "persona": {"locale": "ru-RU", "timezone": "Europe/Moscow"},
    }


def test_a_profile_keeps_its_proxy_and_takes_its_persona():
    cfg = wsc.WebSearchConfig(
        proxies=[
            _proxy("de", username="u", password="p", locale="de-DE", timezone="Europe/Berlin"),
            _proxy("ru"),
            _proxy("off", enabled=False),
        ]
    )
    first = ws.browser_profile_fields("alice", cfg=cfg)
    assert ws.browser_profile_fields("alice", cfg=cfg) == first
    assert first["proxy"]["id"] in {"de", "ru"}
    if first["proxy"]["id"] == "de":
        assert first["persona"] == {"locale": "de-DE", "timezone": "Europe/Berlin"}
        assert first["proxy"]["username"] == "u" and first["proxy"]["password"] == "p"
    chosen = {ws.browser_profile_fields(f"user-{i}", cfg=cfg)["proxy"]["id"] for i in range(40)}
    assert chosen == {"de", "ru"}  # spread over the enabled pool, never the disabled one


@pytest.mark.asyncio
async def test_a_read_uses_the_owners_profile_and_its_cache_is_not_shared(monkeypatch):
    from app.ai import actor_context

    cfg = wsc.WebSearchConfig(per_host_interval_s=0)
    monkeypatch.setattr(ws.wsc, "get_config", lambda: cfg)
    sent: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": 200, "title": "T", "text": "страница " + sent[-1]["profile_owner"]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", _Client)
    url = f"https://shared-{uuid.uuid4().hex}.example/account"
    for who in ("alice", "bob", "alice"):
        actor_context.set_acting_user(who)
        page = await ws.fetch_page(ws.WebFetchRequest(url=url))
        assert page.text == "страница alice" if who == "alice" else "страница bob"
    actor_context.set_acting_user(None)
    # alice's second read came from her cache; bob never saw her copy.
    assert [s["profile_owner"] for s in sent] == ["alice", "bob"]
    assert all(s["profile"] == "reader" for s in sent)


@pytest.mark.asyncio
async def test_a_challenge_kind_reaches_the_agent(monkeypatch):
    cfg = wsc.WebSearchConfig(per_host_interval_s=0)

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "status": 503,
                "text": "Just a moment",
                "diagnostics": ["bot_challenge_detected", "challenge:ddos_guard"],
            }

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", _Client)
    page = await ws._fetch_live(ws.WebFetchRequest(url="https://x.example/"), cfg, "alice")
    assert page.read_status == "blocked" and page.challenge == "ddos_guard"


@pytest.mark.asyncio
@pytest.mark.parametrize(("secret_ids", "profile"), [([], "reader"), ([uuid.uuid4()], "login")])
async def test_a_session_starts_in_the_persons_profile(monkeypatch, secret_ids, profile):
    from app.api import computer_use as cu

    monkeypatch.setattr(ws.wsc, "get_config", lambda: wsc.WebSearchConfig())
    sent: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "session_id": "s1"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(cu.httpx, "AsyncClient", _Client)
    grant = SimpleNamespace(
        allowed_hosts=["supplier.example"],
        work_order_id=uuid.uuid4(),
        granted_to="alice",
        secret_ids=secret_ids,
    )
    await cu._perform("desktop_start", "https://supplier.example/", {}, grant)
    assert sent[0]["owner"] == f"wo:{grant.work_order_id}"
    assert sent[0]["profile_owner"] == "alice" and sent[0]["profile"] == profile


@pytest.mark.asyncio
async def test_new_actions_pass_their_arguments(monkeypatch):
    from app.api import computer_use as cu

    monkeypatch.setattr(ws.wsc, "get_config", lambda: wsc.WebSearchConfig())
    sent: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(cu.httpx, "AsyncClient", _Client)
    grant = SimpleNamespace(
        allowed_hosts=["x.example"], work_order_id=uuid.uuid4(), granted_to="a", secret_ids=[]
    )
    await cu._perform("desktop_scroll", "s1", {"dy": -400}, grant)
    await cu._perform("desktop_press", "s1", {"key": "Enter"}, grant)
    await cu._perform("desktop_wait", "s1", {"text": "Готово", "wait_ms": 3000}, grant)
    assert [(s["action"], s.get("dy"), s.get("key")) for s in sent] == [
        ("scroll", -400, None),
        ("press", None, "Enter"),
        ("wait_for", None, None),
    ]
    assert sent[2]["text"] == "Готово" and sent[2]["wait_ms"] == 3000


def test_proxy_passwords_survive_a_save_and_locale_keeps_its_case():
    wsc.update_config(
        wsc.WebSearchConfigUpdate(
            proxies=[
                wsc.BrowserProxyUpdate(id="res", server="http://p.example:8000", password="s3cret")
            ],
            browser_locale="ru-RU",
            browser_timezone="Europe/Moscow",
        )
    )
    view = wsc.to_view(wsc.get_config())
    assert view.proxies[0].password_set and "s3cret" not in view.proxies[0].password_mask
    wsc.update_config(
        wsc.WebSearchConfigUpdate(
            proxies=[
                wsc.BrowserProxyUpdate(
                    id="res", server="http://p.example:8000", password=view.proxies[0].password_mask
                )
            ]
        )
    )
    cfg = wsc.get_config()
    assert cfg.proxies[0].password == "s3cret"
    assert (cfg.browser_locale, cfg.browser_timezone) == ("ru-RU", "Europe/Moscow")


def test_a_proxy_address_must_be_plain_http():
    with pytest.raises(ValueError):
        wsc.BrowserProxyUpdate(id="x", server="socks5://p.example:1080")
    with pytest.raises(ValueError):
        wsc.BrowserProxyUpdate(id="x", server="http://user:pw@p.example:8000")
