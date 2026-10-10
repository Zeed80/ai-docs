"""E31: a desktop session belongs to its owner, ends on its own and has limits.

Real Chromium against a local synthetic site. Run inside the service image:

    docker run --rm -v $PWD/infra/web-browser:/app -w /app infra-web-browser \
        python -m pytest tests -q
"""

from __future__ import annotations

import http.server
import threading
import time

import pytest
from fastapi.testclient import TestClient

import server


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"<html><body><h1>secret of the owner</h1><a id=go href='/two'>go</a></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", f"sid=owner-{self.path.strip('/') or 'root'}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def _start(client, site, owner):
    return client.post(
        "/desktop/start", json={"url": site, "allowed_hosts": ["127.0.0.1"], "owner": owner}
    ).json()


def _act(client, session_id, owner, action="read"):
    return client.post(
        "/desktop/action", json={"session_id": session_id, "owner": owner, "action": action}
    ).json()


def test_bob_cannot_read_click_or_close_alices_session_by_its_id(client, site):
    alice = _start(client, site, "wo:alice")
    assert alice["ok"]
    for action in ("read", "screenshot", "close"):
        assert _act(client, alice["session_id"], "wo:bob", action) == {
            "ok": False,
            "error": "session_not_found",
        }
    read = _act(client, alice["session_id"], "wo:alice")
    assert read["ok"] and "secret of the owner" in read["text"]


def test_a_new_session_does_not_inherit_cookies(client, site):
    first = _start(client, site, "wo:a")
    context = server._sessions[first["session_id"]]["context"]
    client.portal.call(context.add_cookies, [{"name": "alice_only", "value": "1", "url": site}])
    second = _start(client, site, "wo:b")
    fresh = server._sessions[second["session_id"]]["context"]
    assert "alice_only" not in {c["name"] for c in client.portal.call(fresh.cookies)}
    assert "alice_only" in {c["name"] for c in client.portal.call(context.cookies)}


def test_idle_and_absolute_limits_end_a_session(client, site, monkeypatch):
    started = _start(client, site, "wo:t")
    session = server._sessions[started["session_id"]]
    session["last_used"] = time.monotonic() - server.SESSION_IDLE_SECONDS - 1
    assert _act(client, started["session_id"], "wo:t")["error"] == "session_expired"
    assert started["session_id"] not in server._sessions

    other = _start(client, site, "wo:t")
    server._sessions[other["session_id"]]["created"] = (
        time.monotonic() - server.SESSION_MAX_SECONDS - 1
    )
    assert client.portal.call(server._sweep_expired) == 1
    assert other["session_id"] not in server._sessions


def test_session_limits_per_owner_and_overall(client, site, monkeypatch):
    monkeypatch.setattr(server, "MAX_SESSIONS_PER_OWNER", 1)
    assert _start(client, site, "wo:limit")["ok"]
    assert _start(client, site, "wo:limit")["error"] == "owner_session_limit"
    monkeypatch.setattr(server, "MAX_SESSIONS", len(server._sessions))
    assert _start(client, site, "wo:another")["error"] == "session_limit"


def test_revoking_an_owner_closes_all_of_its_sessions(client, site):
    a = _start(client, site, "wo:revoked")
    keep = _start(client, site, "wo:kept")
    closed = client.post("/desktop/close-owner", json={"owner": "wo:revoked"}).json()
    assert closed == {"ok": True, "closed": 1}
    assert _act(client, a["session_id"], "wo:revoked")["error"] == "session_not_found"
    assert _act(client, keep["session_id"], "wo:kept")["ok"]


def test_an_action_waiting_on_a_closing_session_does_not_run(client, site):
    started = _start(client, site, "wo:race")
    sid = started["session_id"]

    async def race():
        import asyncio

        session = server._sessions[sid]
        async with session["lock"]:
            pending = asyncio.create_task(
                server.desktop_action(
                    server.DesktopActionRequest(session_id=sid, owner="wo:race", action="read")
                )
            )
            await asyncio.sleep(0.05)
            await server._close_session(sid)
        return await pending

    assert client.portal.call(race) == {"ok": False, "error": "session_not_found"}
