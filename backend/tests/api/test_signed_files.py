from __future__ import annotations


def test_document_signed_download_url_flow(client) -> None:
    case_response = client.post("/api/cases", json={"title": "Signed files"})
    case = case_response.json()
    upload_response = client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("readme.txt", b"hello signed file", "text/plain")},
    )
    document = upload_response.json()

    signed_response = client.post(f"/api/documents/{document['id']}/download-url")

    assert signed_response.status_code == 200
    signed = signed_response.json()
    assert signed["url"].startswith("/api/files/signed/")
    assert signed["filename"] == "readme.txt"

    download_response = client.get(signed["url"])
    assert download_response.status_code == 200
    assert download_response.content == b"hello signed file"

    audit_response = client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "signed_file_url_created" in event_types


def test_tampered_signed_download_token_is_rejected(client) -> None:
    response = client.get("/api/files/signed/tampered.token")

    assert response.status_code == 403
