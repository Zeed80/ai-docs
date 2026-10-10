"""E35: downloads are bounded and labeled, never a local path; uploads come
only from in-memory files, into a file input."""

from __future__ import annotations

import http.server
import threading
import time

import pytest
from fastapi.testclient import TestClient

import server

PAGES = {
    "/page": """<html><body>
      <a id=pdf href='/file.pdf' download>PDF</a>
      <a id=evil href='/evil'>Злое имя</a>
      <a id=huge href='/huge'>Огромный</a>
      <a id=slow href='/slow'>Бесконечный</a>
      <form><input id=f type=file name=f><input id=t name=t placeholder='Текст'></form>
    </body></html>""",
}


class _Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/file.pdf":
            body = b"%PDF-1.4 test"
            self._file(body, 'attachment; filename="report.pdf"')
        elif self.path == "/evil":
            self._file(b"MZ\x90\x00", 'attachment; filename="../../etc/cron.d/x.exe"')
        elif self.path == "/huge":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="big.bin"')
            self.end_headers()
            try:
                for _ in range(4000):
                    self.wfile.write(b"\0" * 65536)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="slow.bin"')
            self.end_headers()
            try:
                for _ in range(600):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = PAGES.get(self.path, "<html>ok</html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    def _file(self, body, disposition):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        yield test_client


def _session(client, site, owner="wo:e35"):
    started = client.post(
        "/desktop/start",
        json={"url": site + "/page", "allowed_hosts": ["127.0.0.1"], "owner": owner},
    ).json()
    sid = started["session_id"]

    def act(action, **kw):
        return client.post(
            "/desktop/action", json={"session_id": sid, "owner": owner, "action": action, **kw}
        ).json()

    return sid, act


def _click(act, name, wait_ms=1500):
    snap = act("observe")
    ref = next(e["ref"] for e in snap["elements"] if e["name"] == name)
    return act("click", ref=ref, revision=snap["revision"], wait_ms=wait_ms)


def _wait_downloads(act, count, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        items = act("downloads")["downloads"]
        if len(items) >= count and all(d["status"] != "running" for d in items):
            return items
        time.sleep(0.3)
    return act("downloads")["downloads"]


def test_a_download_is_kept_in_memory_with_its_digest_and_type(client, site):
    _sid, act = _session(client, site)
    _click(act, "PDF")
    [item] = _wait_downloads(act, 1)
    assert item["status"] == "done" and item["sniffed_type"] == "application/pdf"
    assert item["name"] == "report.pdf" and item["size"] == 13
    taken = act("take_download", download_id=item["id"])
    assert taken["ok"] and taken["download"]["sha256"] == item["sha256"]


def test_a_hostile_file_name_is_only_a_label(client, site):
    _sid, act = _session(client, site)
    _click(act, "Злое имя")
    [item] = _wait_downloads(act, 1)
    assert "/" not in item["name"] and ".." not in item["name"]
    assert item["sniffed_type"] == "application/x-msdownload"


def test_an_oversized_or_endless_download_is_cut(client, site, monkeypatch):
    import egress

    monkeypatch.setattr(server, "DOWNLOAD_MAX_BYTES", 1024 * 1024)
    monkeypatch.setattr(server, "DOWNLOAD_MAX_SECONDS", 3)
    monkeypatch.setattr(egress, "MAX_RESPONSE_BYTES", 4 * 1024 * 1024)
    _sid, act = _session(client, site)
    _click(act, "Огромный")
    _click(act, "Бесконечный")
    items = {d["name"]: d for d in _wait_downloads(act, 2, seconds=20)}
    # Cut by the proxy at 4 MB or refused over 1 MB: never kept.
    assert items["big.bin"]["status"] in {"refused", "failed"}
    assert "content_b64" not in items["big.bin"]
    assert (items["slow.bin"]["status"], items["slow.bin"]["reason"]) == ("refused", "too_slow")


def test_another_sessions_download_id_names_nothing(client, site):
    _sid, act = _session(client, site, owner="wo:alice")
    _click(act, "PDF")
    [item] = _wait_downloads(act, 1)
    _sid2, bob = _session(client, site, owner="wo:bob")
    assert bob("take_download", download_id=item["id"]) == {
        "ok": False,
        "error": "download_not_found",
    }


def test_upload_takes_in_memory_files_into_a_file_input_only(client, site):
    sid, act = _session(client, site)
    snap = act("observe")
    file_ref = next(e["ref"] for e in snap["elements"] if e["type"] == "file")
    text_ref = next(e["ref"] for e in snap["elements"] if e["name"] == "Текст")
    files = [{"name": "../секрет.txt", "content_b64": "aGVsbG8=", "mime_type": "text/plain"}]
    refused = act("upload", ref=text_ref, revision=snap["revision"], files=files)
    assert refused == {"ok": False, "error": "not_a_file_input"}
    done = act("upload", ref=file_ref, revision=snap["revision"], files=files)
    assert done["ok"] and done["files"] == ["______.txt"]
    page = server._sessions[sid]["page"]
    names = client.portal.call(
        page.evaluate, "() => [...document.getElementById('f').files].map(f => f.name)"
    )
    assert len(names) == 1 and "/" not in names[0] and ".." not in names[0]
