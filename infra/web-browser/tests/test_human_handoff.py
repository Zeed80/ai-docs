"""T3: the agent hands a session to its owner for MFA; the person types the
code by keyboard into the same page; the agent waits, never sees the code,
and observes afresh when the session comes back."""

from __future__ import annotations

import http.server
import threading

import pytest
from fastapi.testclient import TestClient

import server

CODE = "482913"

PAGE = """<html><body>
  <form action='/done'>
    <input id=code name=code placeholder='Код из SMS' autofocus
      oninput="document.getElementById('echo').textContent='код:'+this.value;">
    <span id=echo></span>
    <button>Подтвердить</button>
  </form>
  <a id=away href='http://example.invalid/'>Чужой сайт</a>
</body></html>"""


def _serve():
    class _Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture(scope="module")
def site():
    httpd = _serve()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def _session(client, url, owner="wo:t3"):
    started = client.post(
        "/desktop/start", json={"url": url, "allowed_hosts": ["127.0.0.1"], "owner": owner}
    ).json()
    assert started["ok"], started
    sid = started["session_id"]

    def act(action, **kw):
        return client.post(
            "/desktop/action", json={"session_id": sid, "owner": owner, "action": action, **kw}
        ).json()

    def human(op, **kw):
        return client.post(
            "/desktop/human", json={"session_id": sid, "owner": owner, "op": op, **kw}
        ).json()

    return sid, act, human


def test_a_person_types_the_code_and_the_agent_never_sees_it(client, site):
    sid, act, human = _session(client, site + "/mfa")
    before = act("observe")
    # Before the handoff the person may look but not act.
    assert human("view")["ok"] and human("type", text=CODE)["error"] == "session_with_agent"

    assert act("handoff", reason="Нужен код из SMS")["handed_off"] is True
    # The agent waits: every action is refused while the owner holds it.
    assert act("observe")["error"] == "session_with_owner"
    assert act("read")["error"] == "session_with_owner"

    field = next(e for e in before["elements"] if e["name"] == "Код из SMS")
    assert field
    view = human("click", x=60, y=20)
    assert view["ok"] and view["handoff"]["reason"] == "Нужен код из SMS"
    assert human("type", text=CODE)["ok"]
    assert human("key", key="Delete")["error"] == "key_not_allowed"

    back = human("return")
    assert back["ok"] and back["handoff"] is None
    # The old observe is stale: the page changed under the person's hands.
    stale = act("type", ref=field["ref"], revision=before["revision"], text="x")
    assert stale["error"] == "stale_revision"
    fresh = act("observe")
    assert "код:•••" in fresh["text"] and CODE not in str(fresh)
    assert CODE not in str(act("read"))


def test_a_person_cannot_act_in_another_owners_session(client, site):
    sid, act, _human = _session(client, site + "/mfa", owner="wo:a")
    act("handoff", reason="MFA")
    foreign = client.post(
        "/desktop/human",
        json={"session_id": sid, "owner": "wo:b", "op": "type", "text": CODE},
    ).json()
    assert foreign["error"] == "session_not_found"
    listed = client.post("/desktop/list-owner", json={"owner": "wo:a"}).json()["sessions"]
    assert [s["session_id"] for s in listed] == [sid] and listed[0]["handoff"]["reason"] == "MFA"
    assert client.post("/desktop/list-owner", json={"owner": "wo:b"}).json()["sessions"] == []


def test_returning_from_outside_the_allowlist_ends_the_session(client, site):
    sid, act, human = _session(client, site + "/mfa")
    act("handoff", reason="CAPTCHA")
    page = server._sessions[sid]["page"]
    # The person followed a link off the allowlist (the site's SSO, say).
    client.portal.call(page.goto, "about:blank")
    back = human("return")
    assert back == {"ok": False, "error": "navigation_left_allowlist", "closed": True}
    assert act("observe")["error"] == "session_not_found"
