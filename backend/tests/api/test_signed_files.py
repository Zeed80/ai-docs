from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skip(reason="/api/files/signed/* endpoint not yet implemented")


@pytest.mark.asyncio
async def test_document_signed_download_url_flow(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Signed files"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("readme.txt", b"hello signed file", "text/plain")},
    )
    document = upload_response.json()

    signed_response = await client.post(f"/api/documents/{document['id']}/download-url")

    assert signed_response.status_code == 200
    signed = signed_response.json()
    assert signed["url"].startswith("/api/files/signed/")
    assert signed["filename"] == "readme.txt"

    download_response = await client.get(signed["url"])
    assert download_response.status_code == 200
    assert download_response.content == b"hello signed file"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "signed_file_url_created" in event_types


@pytest.mark.asyncio
async def test_tampered_signed_download_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/api/files/signed/tampered.token")

    assert response.status_code == 403
