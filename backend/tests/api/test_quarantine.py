from __future__ import annotations


def test_disallowed_upload_extension_is_quarantined(client) -> None:
    case_response = client.post("/api/cases", json={"title": "Quarantine"})
    case = case_response.json()

    upload_response = client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("payload.exe", b"MZ suspicious", "application/octet-stream")},
    )

    assert upload_response.status_code == 201
    document = upload_response.json()
    assert document["status"] == "suspicious"
    assert "/quarantine/" in document["storage_path"]

    process_response = client.post(f"/api/documents/{document['id']}/process")
    assert process_response.status_code == 409

    audit_response = client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "document_quarantined" in event_types
