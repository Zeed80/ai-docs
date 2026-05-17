from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skip(reason="/api/cases endpoint not yet implemented")


@pytest.mark.asyncio
async def test_case_document_audit_and_ai_flow(client: AsyncClient) -> None:
    create_response = await client.post(
        "/api/cases",
        json={
            "title": "Shaft RFQ",
            "description": "Need process plan and supplier quote",
            "customer_name": "ACME",
        },
    )
    assert create_response.status_code == 201
    case = create_response.json()
    assert case["title"] == "Shaft RFQ"
    assert case["document_count"] == 0

    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("invoice.txt", b"Invoice #1 total 1000", "text/plain")},
    )
    assert upload_response.status_code == 201
    document = upload_response.json()
    assert document["filename"] == "invoice.txt"
    assert document["status"] == "uploaded"

    list_response = await client.get(f"/api/cases/{case['id']}/documents")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    classify_response = await client.post(f"/api/documents/{document['id']}/classify", json={})
    assert classify_response.status_code == 200
    classified = classify_response.json()["document"]
    assert classified["status"] == "classified"
    assert classified["document_type"] == "invoice"

    extract_response = await client.post(
        f"/api/documents/{document['id']}/extract",
        json={"extraction_goal": "Extract invoice data as JSON."},
    )
    assert extract_response.status_code == 200
    extracted = extract_response.json()["document"]
    assert extracted["status"] == "extracted"
    assert "ACME" in extract_response.json()["ai_text"]

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    assert audit_response.status_code == 200
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "case_created" in event_types
    assert "document_uploaded" in event_types
    assert "document_classified" in event_types
    assert "document_extracted" in event_types


@pytest.mark.asyncio
async def test_missing_case_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/cases/missing")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_document_processing_text_flow(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Invoice processing"})
    assert case_response.status_code == 201
    case = case_response.json()

    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("invoice.md", b"# Invoice\nSupplier: ACME\nTotal: 1000", "text/markdown")},
    )
    assert upload_response.status_code == 201
    document = upload_response.json()

    process_response = await client.post(f"/api/documents/{document['id']}/process")
    assert process_response.status_code == 200
    task = process_response.json()
    assert task["status"] == "pending"
    assert task["task_type"] == "document.process"

    run_response = await client.post(f"/api/tasks/{task['id']}/run")
    assert run_response.status_code == 200
    task = run_response.json()
    assert task["status"] == "completed"
    assert task["result"]["processing_status"] == "completed"
    assert task["result"]["parser_name"] == "text.md"

    document_response = await client.get(f"/api/documents/{document['id']}")
    processed = document_response.json()
    assert processed["status"] == "processed"
    assert processed["extraction_result"]["structured"]["fields"][0]["name"] == "supplier"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "task_job_created" in event_types
    assert "task_job_completed" in event_types
    assert "document_processing_started" in event_types
    assert "document_processing_completed" in event_types


@pytest.mark.asyncio
async def test_document_processing_unsupported_file_is_safe(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "CAD fallback"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("part.step", b"ISO-10303-21;", "application/step")},
    )
    document = upload_response.json()

    process_response = await client.post(f"/api/documents/{document['id']}/process")

    assert process_response.status_code == 200
    task = process_response.json()
    run_response = await client.post(f"/api/tasks/{task['id']}/run")
    assert run_response.status_code == 200
    task = run_response.json()
    assert task["status"] == "completed"
    assert task["result"]["processing_status"] == "unsupported"
    document_response = await client.get(f"/api/documents/{document['id']}")
    assert document_response.json()["status"] == "needs_review"


@pytest.mark.asyncio
async def test_document_processing_image_ocr_fallback_creates_artifact(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Scanned invoice"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("scan.png", b"not-a-real-image-but-stored-preview", "image/png")},
    )
    document = upload_response.json()

    process_response = await client.post(f"/api/documents/{document['id']}/process")

    assert process_response.status_code == 200
    task = process_response.json()
    run_response = await client.post(f"/api/tasks/{task['id']}/run")
    assert run_response.status_code == 200
    task = run_response.json()
    assert task["status"] == "completed"
    assert task["result"]["parser_name"] == "image_placeholder+ocr"
    document_response = await client.get(f"/api/documents/{document['id']}")
    processed = document_response.json()
    assert processed["artifacts"][0]["content_type"] == "image/png"
    assert processed["extraction_result"]["structured"]["document_type"] == "invoice"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "document_artifact_created" in event_types


@pytest.mark.asyncio
async def test_drawing_analysis_creates_drawing_and_features(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Shaft manufacturing case"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={
            "file": (
                "shaft_drawing.txt",
                b"Drawing DRW-001 rev A. Material Steel 40X. Outer diameter 25 h7.",
                "text/plain",
            )
        },
    )
    document = upload_response.json()

    analysis_response = await client.post(f"/api/documents/{document['id']}/drawing-analysis")

    assert analysis_response.status_code == 200
    payload = analysis_response.json()
    assert payload["drawing"]["title"] == "Shaft drawing"
    assert payload["drawing"]["drawing_number"] == "DRW-001"
    assert payload["drawing"]["features"][0]["feature_type"] == "diameter"
    assert payload["analysis"]["questions"] == ["Confirm heat treatment requirement"]

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "drawing_analyzed" in event_types


@pytest.mark.asyncio
async def test_customer_question_draft_requires_approval_and_is_audited(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Shaft clarifications"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={
            "file": (
                "shaft_drawing.txt",
                b"Drawing DRW-001 rev A. Material Steel 40X. Outer diameter 25 h7.",
                "text/plain",
            )
        },
    )
    document = upload_response.json()
    analysis_response = await client.post(f"/api/documents/{document['id']}/drawing-analysis")
    drawing = analysis_response.json()["drawing"]

    draft_response = await client.post(f"/api/drawings/{drawing['id']}/customer-question-draft")

    assert draft_response.status_code == 200
    draft = draft_response.json()["draft"]
    assert draft["approval_required"] is True
    assert "термообработке" in draft["body"]
    assert draft["questions"]

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "customer_question_drafted" in event_types


@pytest.mark.asyncio
async def test_invoice_extraction_creates_supplier_invoice_lines_and_audit(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Invoice case"})
    case = case_response.json()
    upload_response = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={
            "file": (
                "invoice.txt",
                (
                    b"Supplier ACME Tools INN 7726314000 KPP 507401001 "
                    b"Invoice INV-100 total 1200 VAT 200"
                ),
                "text/plain",
            )
        },
    )
    document = upload_response.json()

    extraction_response = await client.post(f"/api/documents/{document['id']}/invoice-extraction")

    assert extraction_response.status_code == 200
    payload = extraction_response.json()
    assert payload["invoice"]["supplier"]["name"] == "ACME Tools"
    assert payload["invoice"]["invoice_number"] == "INV-100"
    assert payload["invoice"]["arithmetic_ok"] == "ok"
    assert payload["invoice"]["duplicate_status"] == "unique"
    assert payload["invoice"]["lines"][0]["sku"] == "EM-D10"
    assert payload["checks"]["arithmetic_ok"] is True
    assert payload["anomaly_card"]["severity"] == "low"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "invoice_extracted" in event_types
    assert "invoice_anomaly_created" in event_types


@pytest.mark.asyncio
async def test_invoice_extraction_flags_duplicate_supplier_number(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Duplicate invoice case"})
    case = case_response.json()

    async def upload_invoice(name: str) -> dict:
        response = await client.post(
            f"/api/cases/{case['id']}/documents",
            files={
                "file": (
                    name,
                    f"ACME Tools invoice INV-100 {name}".encode(),
                    "text/plain",
                )
            },
        )
        return response.json()

    first = await upload_invoice("invoice-a.txt")
    second = await upload_invoice("invoice-b.txt")

    first_response = await client.post(f"/api/documents/{first['id']}/invoice-extraction")
    second_response = await client.post(f"/api/documents/{second['id']}/invoice-extraction")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["invoice"]["duplicate_status"] == "duplicate_supplier_number"
    assert second_response.json()["checks"]["duplicate_by_supplier_number"] is True


@pytest.mark.asyncio
async def test_invoice_supplier_requisites_diff_and_exports(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Invoice exports"})
    case = case_response.json()

    first_upload = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("invoice-original.txt", b"ACME Tools Test Bank INV-100", "text/plain")},
    )
    first_response = await client.post(
        f"/api/documents/{first_upload.json()['id']}/invoice-extraction"
    )
    first_invoice = first_response.json()["invoice"]

    second_upload = await client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("invoice-changed.txt", b"ACME Tools Changed Bank INV-100", "text/plain")},
    )
    second_response = await client.post(
        f"/api/documents/{second_upload.json()['id']}/invoice-extraction"
    )

    assert second_response.status_code == 200
    assert second_response.json()["checks"]["supplier_requisites_diff"] == ["bank_details changed"]

    export_response = await client.post(f"/api/invoices/{first_invoice['id']}/export.xlsx")
    assert export_response.status_code == 200
    assert export_response.json()["artifact"]["artifact_type"] == "invoice_excel_export"

    onec_response = await client.post(f"/api/invoices/{first_invoice['id']}/1c-export")
    assert onec_response.status_code == 200
    assert onec_response.json()["approval_required"] is True
    assert onec_response.json()["payload"]["invoice"]["number"] == "INV-100"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "supplier_requisites_diff_detected" in event_types
    assert "invoice_excel_exported" in event_types
    assert "onec_export_prepared" in event_types


@pytest.mark.asyncio
async def test_email_workspace_thread_draft_and_send_gate(client: AsyncClient) -> None:
    case_response = await client.post("/api/cases", json={"title": "Email case"})
    case = case_response.json()

    thread_response = await client.post(
        "/api/email/threads",
        json={
            "case_id": case["id"],
            "subject": "RFQ shaft",
            "external_thread_id": "thread-1",
            "message": {
                "sender": "customer@example.com",
                "recipients": ["tech@example.local"],
                "subject": "RFQ shaft",
                "body_text": "Please quote shaft production",
                "external_message_id": "message-1",
            },
        },
    )

    assert thread_response.status_code == 201
    thread = thread_response.json()
    assert thread["case_id"] == case["id"]
    assert len(thread["messages"]) == 1
    assert thread["messages"][0]["sender"] == "customer@example.com"

    poll_response = await client.post(f"/api/email/imap/poll?case_id={case['id']}")
    assert poll_response.status_code == 200
    assert poll_response.json()["status"] == "placeholder"

    draft_response = await client.post(
        "/api/email/drafts",
        json={
            "thread_id": thread["id"],
            "case_id": case["id"],
            "to": ["customer@example.com"],
            "subject": "Re: RFQ shaft",
            "body_text": "Просим подтвердить реквизиты и условия оплаты.",
        },
    )
    assert draft_response.status_code == 201
    draft = draft_response.json()
    assert draft["approval_required"] is True
    assert draft["status"] == "needs_approval"
    assert "contains_financial_or_requisites_terms" in draft["risk"]["signals"]

    send_response = await client.post(f"/api/email/drafts/{draft['id']}/send")
    assert send_response.status_code == 200
    assert send_response.json()["status"] == "blocked_for_approval"
    assert send_response.json()["draft"]["status"] == "blocked_for_approval"

    audit_response = await client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "email_thread_created" in event_types
    assert "email_message_ingested" in event_types
    assert "email_draft_created" in event_types
    assert "email_send_blocked_for_approval" in event_types
