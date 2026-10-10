"""E34: a brokered secret goes into one password field of the right origin
and never comes back — not in a snapshot, text, error or another answer."""

from __future__ import annotations

import http.server
import threading

import pytest
from fastapi.testclient import TestClient

import server

SECRET = "S3cr3t-Пароль!"

PAGE = """<html><body>
  <input id=user name=user placeholder='Логин'>
  <input id=pw type=password placeholder='Пароль'
    oninput="document.getElementById('echo').textContent='typed:'+this.value;
             document.getElementById('copy').value=this.value;">
  <input id=copy placeholder='Копия'>
  <span id=echo></span>
  <button>Войти</button>
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
def sites():
    a, b = _serve(), _serve()
    yield f"http://127.0.0.1:{a.server_address[1]}", f"http://127.0.0.1:{b.server_address[1]}"
    a.shutdown()
    b.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def _session(client, url):
    started = client.post(
        "/desktop/start", json={"url": url, "allowed_hosts": ["127.0.0.1"], "owner": "wo:e34"}
    ).json()
    assert started["ok"], started
    sid = started["session_id"]

    def act(action, **kw):
        return client.post(
            "/desktop/action",
            json={"session_id": sid, "owner": "wo:e34", "action": action, **kw},
        )

    return sid, act


def _field(snap, name):
    return next(e for e in snap["elements"] if e["name"] == name)


def test_the_secret_is_typed_and_never_comes_back(client, sites):
    site, _other = sites
    sid, act = _session(client, site + "/login")
    snap = act("observe").json()
    filled = act(
        "fill_secret",
        ref=_field(snap, "Пароль")["ref"],
        revision=snap["revision"],
        secret=SECRET,
        origin=site,
    )
    assert filled.json()["ok"], filled.json()
    page = server._sessions[sid]["page"]
    assert client.portal.call(page.input_value, "#pw") == SECRET  # really typed
    # The page repeats it in visible text and in another field.
    for answer in (act("observe"), act("read")):
        assert SECRET not in answer.text
    assert "typed:•••" in act("read").json()["text"]
    assert SECRET not in filled.text


def test_another_origin_gets_nothing(client, sites):
    site, other = sites
    sid, act = _session(client, site + "/login")
    snap = act("observe").json()
    refused = act(
        "fill_secret",
        ref=_field(snap, "Пароль")["ref"],
        revision=snap["revision"],
        secret=SECRET,
        origin=other,  # same host, other port: another origin
    ).json()
    assert refused == {"ok": False, "error": "origin_mismatch"}
    page = server._sessions[sid]["page"]
    assert client.portal.call(page.input_value, "#pw") == ""


def test_only_a_password_field_takes_it(client, sites):
    site, _other = sites
    sid, act = _session(client, site + "/login")
    snap = act("observe").json()
    refused = act(
        "fill_secret",
        ref=_field(snap, "Логин")["ref"],
        revision=snap["revision"],
        secret=SECRET,
        origin=site,
    ).json()
    assert refused == {"ok": False, "error": "not_a_password_field"}


def test_a_validation_error_does_not_echo_the_secret(client, sites):
    site, _other = sites
    sid, act = _session(client, site + "/login")
    bad = act("fill_secret", ref="x" * 100, revision=1, secret=SECRET, origin=site)
    assert bad.status_code == 422
    assert SECRET not in bad.text
    missing = client.post("/desktop/action", json={"secret": SECRET})
    assert missing.status_code == 422 and SECRET not in missing.text
