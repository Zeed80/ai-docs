"""W2: a browser sites take for a returning person, not a fresh bot.

Real Chrome in a window on Xvfb against local synthetic sites. Run inside the
service image:

    docker run --rm -v $PWD/infra/web-browser:/app -w /app infra-web-browser \
        sh -c 'pip install -q --user pytest httpx; python -m pytest tests -q'
"""

from __future__ import annotations

import http.server
import re
import socket
import socketserver
import threading
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import challenges
import profiles
import pytest
import server
from fastapi.testclient import TestClient

ARTICLE = "Каталог насосов КМ: подача 50 м3/ч, напор 50 м. " * 60

INPUT_PAGE = """<html><body style='height:3000px'>
<form action='/search'><input id=q name=q aria-label='Поиск'></form>
<select id=s aria-label='Сорт' onchange="note('sel='+this.value)">
  <option value=a>Альфа</option><option value=b>Бета</option></select>
<button id=b style='position:absolute;left:100px;top:300px;width:200px;height:60px'>Кнопка</button>
<div id=log></div>
<script>
let keys = 0, moves = 0, click = null;
const out = () => document.getElementById('log').textContent =
  `keys=${keys} moves=${moves} click=${click} y=${Math.round(scrollY)}`;
function note(t) { document.getElementById('log').dataset.note = t; document.title = t; }
document.getElementById('q').addEventListener('keydown', () => { keys++; out(); });
document.addEventListener('mousemove', () => { moves++; out(); });
document.getElementById('b').addEventListener('click', e => { click = e.clientX + ',' + e.clientY; out(); });
addEventListener('scroll', out);
setTimeout(() => { const p = document.createElement('p'); p.textContent = 'поздний текст'; document.body.appendChild(p); }, 800);
</script></body></html>"""

WALL = """<html><head><title>Just a moment...</title></head><body>
<div id='challenge-form'>Checking your browser</div>{script}</body></html>"""
CLEARING = "<script>setTimeout(() => { document.cookie = 'cf_clearance=1; path=/'; location.reload(); }, 1200)</script>"


class _Site(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, cookie: str | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        if url.path == "/visits":
            n = int(jar["visits"].value) + 1 if "visits" in jar else 1
            return self._send(
                200,
                f"<html><body><p>visit {n}</p>{ARTICLE}</body></html>",
                f"visits={n}; Path=/; Max-Age=3600",
            )
        if url.path == "/cf":
            if "cf_clearance" in jar:
                return self._send(
                    200,
                    f"<html><head><title>Каталог</title></head><body><article>{ARTICLE}</article></body></html>",
                )
            return self._send(503, WALL.format(script=CLEARING))
        if url.path == "/wall":
            return self._send(503, WALL.format(script=""))
        if url.path == "/cf-block":
            return self._send(
                403,
                "<html><head><title>Attention Required! | Cloudflare</title></head><body>"
                "<div id='cf-error-details'>Sorry, you have been blocked</div></body></html>",
            )
        if url.path == "/own-check":
            # A site's own check (Wildberries: 498 «Проверяем браузер»).
            if "passed" in jar:
                return self._send(200, f"<html><body><article>{ARTICLE}</article></body></html>")
            return self._send(
                498,
                "<html><body>Проверяем браузер<script>setTimeout(() => {"
                "document.cookie='passed=1; path=/'; location.reload(); }, 1000)</script></body></html>",
            )
        if url.path == "/refused":
            return self._send(403, "<html><body>Access denied</body></html>")
        if url.path == "/search":
            q = parse_qs(url.query).get("q", [""])[0]
            return self._send(200, f"<html><body><h1>Найдено: {q}</h1></body></html>")
        if url.path == "/input":
            return self._send(200, INPUT_PAGE)
        return self._send(404, "<html><body>404</body></html>")

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


def _fetch(client, url, **kw):
    return client.post("/fetch", json={"url": url, **kw}).json()


# ── kept profiles ────────────────────────────────────────────────────────────


def test_a_profile_keeps_cookies_between_reads_and_owners_do_not_share(client, site):
    first = _fetch(client, site + "/visits", profile_owner="alice")
    second = _fetch(client, site + "/visits", profile_owner="alice")
    assert "visit 1" in first["text"] and "visit 2" in second["text"]
    # Another person, and a read without a profile, are first-time visitors.
    assert "visit 1" in _fetch(client, site + "/visits", profile_owner="bob")["text"]
    assert "visit 1" in _fetch(client, site + "/visits")["text"]

    listed = client.post("/profiles/list", json={"owner": "alice"}).json()["profiles"]
    assert [p["name"] for p in listed] == ["reader"] and listed[0]["size_bytes"] > 0
    assert client.post("/profiles/list", json={"owner": "carol"}).json()["profiles"] == []

    assert client.post("/profiles/delete", json={"owner": "alice", "name": "reader"}).json()[
        "deleted"
    ]
    assert "visit 1" in _fetch(client, site + "/visits", profile_owner="alice")["text"]


def test_a_profile_name_cannot_walk_out_of_its_directory(client, site):
    answer = _fetch(client, site + "/visits", profile_owner="alice", profile="../bob")
    assert answer["diagnostics"] == ["profile_error:bad_profile_name"]


# ── upstream proxy ───────────────────────────────────────────────────────────


class _Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.seen: list[tuple[str, str]] = []
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)


class _UpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        first, *lines = head.split(b"\r\n\r\n")[0].decode().split("\r\n")
        auth = next(
            (
                l.split(":", 1)[1].strip()
                for l in lines
                if l.lower().startswith("proxy-authorization")
            ),
            "",
        )
        target = first.split(" ")[1]
        self.server.seen.append((target, auth))
        host, port = target.rsplit(":", 1)
        upstream = socket.create_connection((host.strip("[]"), int(port)))
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def pipe(a, b):
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                b.close()

        threading.Thread(target=pipe, args=(upstream, self.request), daemon=True).start()
        pipe(self.request, upstream)


def test_an_upstream_proxy_gets_only_checked_addresses(client, site):
    upstream = _Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = {
        "id": "res-1",
        "server": f"http://127.0.0.1:{upstream.server_address[1]}",
        "username": "u",
        "password": "pw",
    }
    try:
        read = _fetch(client, site + "/visits", proxy=proxy)
        assert "visit 1" in read["text"]
        port = urlparse(site).port
        assert ("127.0.0.1:%d" % port, "Basic dTpwdw==") in upstream.seen
        # A private address is refused by egress before the upstream hears of
        # it. (Chrome's own background requests reach it too: count by target.)
        _fetch(client, "http://10.255.255.1/", proxy=proxy)
        assert not [t for t, _ in upstream.seen if t.startswith("10.")]
    finally:
        upstream.shutdown()


# ── bot walls ────────────────────────────────────────────────────────────────


def test_a_js_check_that_clears_is_waited_out_and_read(client, site):
    read = _fetch(client, site + "/cf")
    assert "challenge_passed:cloudflare" in read["diagnostics"]
    assert "bot_challenge_detected" not in read["diagnostics"]
    assert "подача 50 м3/ч" in read["text"]


def test_a_wall_that_stays_is_reported_with_its_kind(client, site, monkeypatch):
    monkeypatch.setattr(challenges, "CLEARANCE_WAIT_S", 2.0)
    read = _fetch(client, site + "/wall")
    assert "bot_challenge_detected" in read["diagnostics"]
    assert "challenge:cloudflare" in read["diagnostics"]


def test_a_cloudflare_block_is_not_waited_for_and_not_called_passed(client, site):
    read = _fetch(client, site + "/cf-block")
    assert "challenge:cloudflare_block" in read["diagnostics"]
    assert not any(d.startswith("challenge_passed") for d in read["diagnostics"])


def test_a_sites_own_check_is_waited_out_and_the_final_status_is_reported(client, site):
    read = _fetch(client, site + "/own-check")
    assert "challenge_passed:unknown_check" in read["diagnostics"]
    assert read["status"] == 200 and "подача 50 м3/ч" in read["text"]


def test_a_plain_refusal_is_not_passed(client, site, monkeypatch):
    monkeypatch.setattr(challenges, "CLEARANCE_WAIT_S", 2.0)
    read = _fetch(client, site + "/refused")
    assert read["status"] == 403
    assert not any(d.startswith("challenge_passed") for d in read["diagnostics"])


def test_an_observe_names_the_wall_for_a_handoff(client, site, monkeypatch):
    monkeypatch.setattr(challenges, "CLEARANCE_WAIT_S", 1.0)
    s = _Session(client, site, "/wall")
    assert s.started["challenge"] == {"kind": "cloudflare", "wall": True, "cleared": False}
    assert s.act("observe")["challenge"]["kind"] == "cloudflare"


# ── human input and the new actions ──────────────────────────────────────────


class _Session:
    def __init__(self, client, site, path, **kw):
        self.client = client
        self.started = client.post(
            "/desktop/start",
            json={"url": site + path, "allowed_hosts": ["127.0.0.1"], "owner": "wo:w2", **kw},
        ).json()
        assert self.started["ok"], self.started
        self.sid = self.started["session_id"]

    def act(self, action, **kw):
        return self.client.post(
            "/desktop/action",
            json={"session_id": self.sid, "owner": "wo:w2", "action": action, **kw},
        ).json()

    def ref(self, name):
        snap = self.act("observe")
        return next(e["ref"] for e in snap["elements"] if e["name"] == name), snap["revision"]


def _log(s) -> str:
    return re.search(r"keys=\d+ moves=\d+ click=\S+ y=\d+", s.act("read")["text"]).group(0)


def test_typing_is_key_by_key_and_a_click_travels_to_a_point_inside(client, site):
    s = _Session(client, site, "/input")
    ref, rev = s.ref("Поиск")
    assert s.act("type", ref=ref, revision=rev, text="Насос 80")["ok"]
    keys = int(re.search(r"keys=(\d+)", _log(s)).group(1))
    # Every character a key press, Cyrillic too (plus select-all and erase).
    assert keys >= len("Насос 80") + 2
    assert "Насос 80" in str(s.act("observe")["elements"])

    ref, rev = s.ref("Кнопка")
    assert s.act("click", ref=ref, revision=rev)["ok"]
    log = _log(s)
    moves = int(re.search(r"moves=(\d+)", log).group(1))
    x, y = map(int, re.search(r"click=(\d+),(\d+)", log).groups())
    assert moves >= 8  # a path, not a jump
    assert 100 < x < 300 and 300 < y < 360  # inside the button
    assert (x, y) != (200, 330)  # not its exact centre


def test_scroll_press_select_wait_and_back(client, site):
    s = _Session(client, site, "/input")
    assert s.act("scroll", dy=800)["ok"]
    assert int(re.search(r"y=(\d+)", _log(s)).group(1)) >= 600

    ref, rev = s.ref("Сорт")
    assert s.act("select", ref=ref, revision=rev, text="Бета")["selected"] == ["b"]

    assert s.act("wait_for", text="поздний текст", wait_ms=3000)["found"] is True
    assert s.act("wait_for", text="никогда", wait_ms=500)["found"] is False

    ref, rev = s.ref("Поиск")
    s.act("type", ref=ref, revision=rev, text="фреза")
    pressed = s.act("press", key="Enter")
    assert pressed["ok"] and "/search?q=" in pressed["url"]
    assert "Найдено: фреза" in s.act("read")["text"]
    assert s.act("press", key="Control+L")["error"] == "key_not_allowed"

    back = s.act("back")
    assert back["ok"] and back["url"].endswith("/input"), back
    assert s.act("forward")["url"].startswith(site + "/search")


def test_a_session_in_a_profile_shares_its_cookies_and_closes_only_its_tabs(client, site):
    _fetch(client, site + "/visits", profile_owner="alice")
    s = _Session(client, site, "/visits", profile_owner="alice")
    assert "visit 2" in s.act("read")["text"]
    assert s.act("close")["closed"]
    # The profile outlives the session: the next read is the third visit.
    assert "visit 3" in _fetch(client, site + "/visits", profile_owner="alice")["text"]


# ── self-check ───────────────────────────────────────────────────────────────


def test_selftest_reports_what_sites_see(client, site):
    report = client.post("/selftest", json={"url": site + "/input"}).json()
    probe = report["probe"]
    assert report["ok"] and report["headless"] is False
    assert probe["webdriver"] is False
    assert "Headless" not in probe["user_agent"]
    assert probe["timezone"] == "Europe/Moscow"
    assert probe["languages"][0].startswith("ru")
    # WebGL exists and does not name the VM renderer.
    assert probe["webgl"] and "SwiftShader" not in str(probe["webgl"]["renderer"])
