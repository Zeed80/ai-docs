"""E33: the browser cannot reach a forbidden address, whatever the page tries.

Site A (127.0.0.1, explicitly allowed for the tests) tries every way out to
an "internal" server B on 127.0.0.2. The assertion is B's own connection
count — zero — not the text of a refusal.
"""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading

import pytest
from fastapi.testclient import TestClient

import egress
import server


class _Counting(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Counting.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"internal secret")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def internal():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.2", 0), _Counting)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(scope="module")
def site(internal):
    b = f"http://127.0.0.2:{internal}"
    decimal = f"http://2130706434:{internal}"
    pages = {
        "/attack": f"""<html><body>
          <img src="{b}/img">
          <iframe src="{b}/frame"></iframe>
          <img src="{decimal}/decimal">
          <img src="http://0x7f000002:{internal}/hex">
          <img src="http://[::ffff:127.0.0.2]:{internal}/mapped">
          <img src="http://127.0.0.1@127.0.0.2:{internal}/userinfo">
          <img src="http://169.254.169.254/latest/meta-data/">
          <script>
            fetch("{b}/fetch").catch(() => {{}});
            try {{ new WebSocket("ws://127.0.0.2:{internal}/ws"); }} catch (e) {{}}
            if (navigator.serviceWorker) navigator.serviceWorker.register("/sw.js").catch(() => {{}});
          </script>
          <p id=done>loaded</p></body></html>""",
    }

    class _Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{b}/redirected")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages.get(self.path, "<html>ok</html>").encode())

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def test_no_way_from_a_page_reaches_an_internal_address(client, site, internal):
    _Counting.hits.clear()
    started = client.post(
        "/desktop/start",
        json={"url": site + "/attack", "allowed_hosts": ["127.0.0.1"], "owner": "wo:e33"},
    ).json()
    assert started["ok"], started
    sid = started["session_id"]
    client.post(
        "/desktop/action",
        json={"session_id": sid, "owner": "wo:e33", "action": "read", "wait_ms": 2000},
    )
    assert _Counting.hits == []
    reasons = {reason for _h, _p, reason in egress.blocked_log}
    assert "address_not_allowed" in reasons
    blocked_hosts = {host for host, _p, _r in egress.blocked_log}
    assert "169.254.169.254" in blocked_hosts


def test_a_redirect_into_the_internal_network_is_not_followed(client, site, internal):
    _Counting.hits.clear()
    fetched = client.post("/fetch", json={"url": site + "/redirect"}).json()
    assert _Counting.hits == []
    assert "internal secret" not in (fetched.get("text") or "")


def test_a_direct_fetch_of_an_internal_address_reaches_nothing(client, internal):
    _Counting.hits.clear()
    client.post("/fetch", json={"url": f"http://127.0.0.2:{internal}/direct"})
    assert _Counting.hits == []


def test_rebinding_a_name_that_also_points_inside_is_refused(monkeypatch):
    async def resolver(host, port, type=0):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", resolver)
        with pytest.raises(PermissionError):
            await egress.resolve_checked("rebind.example", 443)

    asyncio.run(run())


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.2",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
def test_internal_ranges_are_closed(address):
    assert not egress.address_allowed(address, 443)


def test_public_addresses_on_web_ports_are_open():
    assert egress.address_allowed("93.184.216.34", 443)
    assert egress.address_allowed("2606:4700:4700::1111", 80)
    assert not egress.address_allowed("93.184.216.34", 22)


def test_the_pdf_download_path_is_held_too(client, internal):
    """/fetch downloads PDFs with the context's request client, not a page."""
    _Counting.hits.clear()
    client.post("/fetch", json={"url": f"http://127.0.0.2:{internal}/catalog.pdf"})
    assert _Counting.hits == []
