"""W3: reading is free, sending is not — decided by facts of the DOM.

Real Chrome against local pages: a catalog with a search form and filters,
a contact form, a login form and a payment form.
"""

from __future__ import annotations

import http.server
import threading
from urllib.parse import parse_qs, urlparse

import profiles
import pytest
import server
from fastapi.testclient import TestClient

PAGES = {
    "/catalog": """<html><body>
<a href='/catalog?page=2'>Следующая страница</a>
<button aria-expanded='false' onclick="this.setAttribute('aria-expanded','true')">Характеристики</button>
<form action='/catalog' method='get'>
  <input type='search' name='q' aria-label='Поиск по каталогу'>
  <select name='sort' aria-label='Сортировка'><option value='p'>По цене</option><option value='n'>По названию</option></select>
</form>
<form action='/contact' method='post'>
  <input name='company' aria-label='Компания'>
  <input type='email' name='email' aria-label='Почта'>
  <select name='topic' aria-label='Тема'><option value='a'>Вопрос</option><option value='b'>Заказ</option></select>
  <textarea name='text' aria-label='Сообщение'></textarea>
  <button aria-label='Отправить'>Отправить</button>
  <button type='button' aria-label='Отправить скриптом' onclick='this.form.submit()'>Отправить скриптом</button>
</form>
<form action='/login' method='post'>
  <input name='user' aria-label='Логин'><input type='password' name='pw' aria-label='Пароль'>
  <button aria-label='Войти'>Войти</button>
</form>
<form action='/pay' method='post'>
  <input name='card' autocomplete='cc-number' aria-label='Номер карты'>
  <button aria-label='Оплатить'>Оплатить</button>
</form>
</body></html>""",
}


class _Site(http.server.BaseHTTPRequestHandler):
    posts: list[tuple[str, str]] = []

    def _send(self, body: str) -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/catalog" and url.query:
            q = parse_qs(url.query)
            return self._send(
                f"<html><body><h1>Результаты: {q.get('q', [''])[0]} {q.get('page', [''])[0]}</h1></body></html>"
            )
        self._send(PAGES.get(url.path, "<html><body>404</body></html>"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Site.posts.append((self.path, self.rfile.read(length).decode()))
        self._send("<html><body>Принято</body></html>")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "ROOT", tmp_path)
    with TestClient(server.app) as test_client:
        yield test_client


class _S:
    def __init__(self, client, site, hosts=("127.0.0.1",)):
        self.client = client
        started = client.post(
            "/desktop/start",
            json={"url": site + "/catalog", "allowed_hosts": list(hosts), "owner": "wo:w3"},
        ).json()
        assert started["ok"], started
        self.sid = started["session_id"]

    def act(self, action, **kw):
        return self.client.post(
            "/desktop/action",
            json={"session_id": self.sid, "owner": "wo:w3", "action": action, **kw},
        ).json()

    def ref(self, name):
        snap = self.act("observe")
        return {
            "ref": next(e["ref"] for e in snap["elements"] if e["name"] == name),
            "revision": snap["revision"],
        }


def test_links_toggles_and_search_are_reading(client, site):
    s = _S(client, site)
    assert s.act("open", **s.ref("Характеристики"))["ok"]
    assert s.act("search", **s.ref("Поиск по каталогу"), text="насос КМ", submit=True)["ok"]
    assert "Результаты: насос КМ" in s.act("read")["text"]
    s.act("back")
    assert s.act("open", **s.ref("Следующая страница"))["ok"]
    assert "Результаты: 2" in s.act("read")["text"]


@pytest.mark.parametrize(
    ("action", "name", "cls", "extra"),
    [
        ("open", "Отправить", "submit", {}),
        ("open", "Отправить скриптом", "submit", {}),  # the form decides, not type=button
        ("open", "Войти", "login", {}),
        ("open", "Оплатить", "transact", {}),
        ("search", "Сообщение", "submit", {"text": "привет"}),
        ("search", "Почта", "submit", {"text": "a@b.c"}),
        ("search", "Компания", "submit", {"text": "ООО"}),
        ("search", "Номер карты", "transact", {"text": "4111"}),
        ("select", "Тема", "submit", {"text": "Заказ"}),
    ],
)
def test_sending_needs_an_approval_card(client, site, action, name, cls, extra):
    _Site.posts.clear()
    s = _S(client, site)
    answer = s.act(action, **s.ref(name), **extra)
    assert answer["ok"] is False and answer["error"] == "needs_approval"
    assert answer["class"] == cls and answer["card"]["class"] == cls
    assert _Site.posts == []


def test_a_filter_select_is_reading_and_enter_in_a_data_form_is_not(client, site):
    s = _S(client, site)
    assert s.act("select", **s.ref("Сортировка"), text="По названию")["selected"] == ["n"]
    # Focus a field of the contact form (approved type), then Enter is its submit.
    typed = s.act("type", **s.ref("Компания"), text="ООО Ромашка")
    assert typed["ok"]
    pressed = s.act("press", key="Enter")
    assert pressed["error"] == "needs_approval" and pressed["class"] == "submit"
    assert s.act("press", key="Tab")["ok"]


def test_an_approved_submit_reports_what_left_the_page(client, site):
    _Site.posts.clear()
    s = _S(client, site)
    s.act("type", **s.ref("Компания"), text="ООО Ромашка")
    target = s.ref("Отправить")
    card = s.act("describe", **target)["card"]
    assert card["class"] == "submit"
    clicked = s.act("click", **target, card=card, wait_ms=500)
    assert clicked["ok"]
    assert any(w["carries_typed_text"] and w["url"].endswith("/contact") for w in clicked["sent"])
    assert _Site.posts and _Site.posts[0][0] == "/contact"


def test_a_read_grant_session_may_go_to_any_public_site(client, site):
    s = _S(client, site, hosts=("*",))
    assert s.act("navigate", url=site + "/catalog?page=3")["ok"]


def test_a_narrow_grant_does_not_act_on_a_page_outside_its_hosts(client, site):
    s = _S(client, site, hosts=("*",))
    target = s.ref("Отправить")
    answer = s.act("describe", **target, grant_hosts=["supplier.example"])
    assert answer["error"] == "page_outside_grant"
    assert s.act("describe", **target, grant_hosts=["127.0.0.1"])["ok"]
