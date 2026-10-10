"""E35: browser files cross the artifact broker in both directions."""

from __future__ import annotations

import base64
import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.db.models import WorkArtifact

PDF = b"%PDF-1.4 e35"


class _Browser:
    def __init__(self, download: dict | None = None):
        self.payloads: list[dict] = []
        self.download = download

    def install(self, monkeypatch):
        import httpx

        browser = self

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                browser.payloads.append(json)
                body = {"ok": True, "session_id": "s-1", "url": "https://shop.example/"}
                if json and json.get("action") == "take_download":
                    body.update(browser.download or {})
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr("app.api.computer_use.httpx.AsyncClient", _Client)


async def _order(client, actions):
    order = (await client.post("/api/work-orders", json={"objective": "Files"})).json()["id"]
    grant = await client.post(
        f"/api/work-orders/{order}/computer-grants",
        json={"actions": actions, "allowed_hosts": ["shop.example"], "reason": "t"},
    )
    assert grant.status_code == 201, grant.text
    return order


def _download(data=PDF, name="report.pdf"):
    return {
        "download": {
            "id": "d1",
            "name": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "url": "https://shop.example/report.pdf",
            "sniffed_type": "application/pdf",
        },
        "content_b64": base64.b64encode(data).decode(),
    }


@pytest.mark.asyncio
async def test_a_download_becomes_an_artifact_with_provenance(client, db_session, monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(
        "app.storage.upload_file", lambda data, path, ct: stored.update({path: data})
    )
    _Browser(_download()).install(monkeypatch)
    order = await _order(client, ["desktop_save_download"])
    resp = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "desktop_save_download",
            "work_order_id": order,
            "target": "s-1",
            "arguments": {"download_id": "d1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert "content_b64" not in resp.text
    artifact = await db_session.get(
        WorkArtifact, uuid.UUID(resp.json()["result"]["artifact"]["id"])
    )
    assert artifact.artifact_type == "browser_download"
    assert artifact.metadata_["source_url"] == "https://shop.example/report.pdf"
    assert list(stored) == [f"browser-downloads/{order}/{artifact.content_hash}"]


@pytest.mark.asyncio
async def test_an_executable_download_is_refused(client, monkeypatch):
    monkeypatch.setattr("app.storage.upload_file", lambda *a: None)
    _Browser(_download(b"MZ\x90\x00", "setup.exe")).install(monkeypatch)
    order = await _order(client, ["desktop_save_download"])
    resp = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "desktop_save_download",
            "work_order_id": order,
            "target": "s-1",
            "arguments": {"download_id": "d1"},
        },
    )
    assert resp.status_code == 409 and "executable" in resp.text


@pytest.mark.asyncio
async def test_upload_takes_only_the_owners_live_artifact(client, db_session, monkeypatch):
    monkeypatch.setattr("app.storage.download_file", lambda path: PDF)
    browser = _Browser()
    browser.install(monkeypatch)
    order = await _order(client, ["desktop_upload"])
    artifact = WorkArtifact(
        work_order_id=uuid.UUID(order),
        artifact_type="upload",
        name="in.pdf",
        uri="x/in.pdf",
        content_hash=hashlib.sha256(PDF).hexdigest(),
    )
    db_session.add(artifact)
    await db_session.commit()
    ref = {"artifact_id": str(artifact.id), "sha256": artifact.content_hash, "name": "in.pdf"}

    def upload(inputs):
        return client.post(
            "/api/computer-use/execute",
            json={
                "action": "desktop_upload",
                "work_order_id": order,
                "target": "s-1",
                "arguments": {"ref": "1:3", "revision": 1, "inputs": inputs},
            },
        )

    ok = await upload([ref])
    assert ok.status_code == 200, ok.text
    sent = browser.payloads[-1]
    assert sent["action"] == "upload"
    assert base64.b64decode(sent["files"][0]["content_b64"]) == PDF

    # Changed bytes (wrong digest) and a host path are refused.
    assert (await upload([{**ref, "sha256": "0" * 64}])).status_code == 403
    assert (await upload([{**ref, "name": "../../etc/passwd"}])).status_code == 403

    revoked = await client.post(f"/api/work-orders/{order}/artifacts/{artifact.id}/revoke")
    assert revoked.status_code == 200
    assert (await upload([ref])).status_code == 403


@pytest.mark.asyncio
async def test_another_owners_artifact_cannot_be_uploaded(client, db_session, monkeypatch):
    from app.db.models import WorkOrder

    monkeypatch.setattr("app.storage.download_file", lambda path: PDF)
    _Browser().install(monkeypatch)
    bob = WorkOrder(owner_key="bob", objective="Bob", source="api")
    db_session.add(bob)
    await db_session.flush()
    artifact = WorkArtifact(
        work_order_id=bob.id,
        artifact_type="upload",
        name="bob.pdf",
        uri="x/bob.pdf",
        content_hash=hashlib.sha256(PDF).hexdigest(),
    )
    db_session.add(artifact)
    await db_session.commit()
    order = await _order(client, ["desktop_upload"])
    resp = await client.post(
        "/api/computer-use/execute",
        json={
            "action": "desktop_upload",
            "work_order_id": order,
            "target": "s-1",
            "arguments": {
                "ref": "1:3",
                "revision": 1,
                "inputs": [
                    {
                        "artifact_id": str(artifact.id),
                        "sha256": artifact.content_hash,
                        "name": "bob.pdf",
                    }
                ],
            },
        },
    )
    assert resp.status_code == 403
    rows = (
        await db_session.scalars(select(WorkArtifact).where(WorkArtifact.name == "bob.pdf"))
    ).all()
    assert len(rows) == 1
