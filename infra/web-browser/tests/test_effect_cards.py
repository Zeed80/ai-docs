"""E36: a click with an effect runs only on the form state it was approved for.

The site counts submissions server-side: the assertion is that count.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import server

FORM = """<html><body><form action='/order' method='get'>
  <input name=item value='фреза'>
  <input name=qty value='1' placeholder='Количество'>
  <button type=submit>Заказать</button>
</form></body></html>"""


class _Shop(http.server.BaseHTTPRequestHandler):
    orders: list[dict] = []

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/order":
            _Shop.orders.append({k: v[0] for k, v in parse_qs(parsed.query).items()})
            body = "<html><body>Заказ принят</body></html>"
        else:
            body = FORM
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def shop():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Shop)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def session(client, shop):
    _Shop.orders.clear()
    started = client.post(
        "/desktop/start",
        json={"url": shop + "/form", "allowed_hosts": ["127.0.0.1"], "owner": "wo:e36"},
    ).json()
    sid = started["session_id"]

    def act(action, **kw):
        return client.post(
            "/desktop/action", json={"session_id": sid, "owner": "wo:e36", "action": action, **kw}
        ).json()

    return act


def _refs(snap):
    refs = {e["name"]: e["ref"] for e in snap["elements"]}
    refs["Количество"] = next(e["ref"] for e in snap["elements"] if e.get("value") == "1")
    return refs


def test_an_approved_card_submits_once(session):
    snap = session("observe")
    refs = _refs(snap)
    card = session("describe", ref=refs["Заказать"], revision=snap["revision"])["card"]
    assert card["form"]["method"] == "get"
    assert {f["name"]: f["value"] for f in card["form"]["fields"]} == {"item": "фреза", "qty": "1"}
    done = session("click", ref=refs["Заказать"], revision=snap["revision"], card=card, wait_ms=800)
    assert done["ok"], done
    assert _Shop.orders == [{"item": "фреза", "qty": "1"}]
    # The response was "lost": replaying the same approved arguments.
    again = session("click", ref=refs["Заказать"], revision=snap["revision"], card=card)
    assert again["error"] == "stale_revision"
    assert len(_Shop.orders) == 1


def test_a_form_changed_after_approval_is_not_submitted(session):
    snap = session("observe")
    refs = _refs(snap)
    card = session("describe", ref=refs["Заказать"], revision=snap["revision"])["card"]
    # The quantity changes after the card was approved (same page revision).
    assert session("type", ref=refs["Количество"], revision=snap["revision"], text="500")["ok"]
    refused = session("click", ref=refs["Заказать"], revision=snap["revision"], card=card)
    assert refused == {"ok": False, "error": "form_changed"}
    assert _Shop.orders == []


def test_a_forged_card_does_not_match_the_live_form(session):
    snap = session("observe")
    refs = _refs(snap)
    card = session("describe", ref=refs["Заказать"], revision=snap["revision"])["card"]
    session("type", ref=refs["Количество"], revision=snap["revision"], text="500")
    # The model shows the approver qty=1 while the page holds 500.
    forged = {**card}
    refused = session("click", ref=refs["Заказать"], revision=snap["revision"], card=forged)
    assert refused["error"] == "form_changed"
    assert _Shop.orders == []
