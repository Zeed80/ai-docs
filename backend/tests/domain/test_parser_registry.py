"""Unit tests for the shared document parser registry (app.ai.parsers).

Covers the single source of truth used by both the live Celery extraction
pipeline and the agent execution path. OCR is intentionally not exercised here
(images / scanned PDFs only set ``needs_ocr``).
"""

from __future__ import annotations

import pytest

from app.ai.parsers import ParsedDocument, parse_document


def test_plain_text_and_csv() -> None:
    r = parse_document(b"hello invoice 123", "note.txt", "text/plain")
    assert r.text == "hello invoice 123"
    assert r.parser_name == "text.txt"
    assert r.needs_ocr is False

    c = parse_document(b"item,qty\nbolt,10", "t.csv", "text/csv")
    assert "bolt" in c.text
    assert c.parser_name == "text.csv"


def test_empty_input_is_safe() -> None:
    r = parse_document(b"", "x.pdf", "application/pdf")
    assert isinstance(r, ParsedDocument)
    assert r.text == ""
    assert r.parser_name == "empty_input"


def test_image_flags_needs_ocr_without_text() -> None:
    r = parse_document(b"\x89PNG\r\n\x1a\n fake", "scan.png", "image/png")
    assert r.text == ""
    assert r.needs_ocr is True
    assert r.parser_name == "image"


def test_binary_doc_does_not_produce_garbage() -> None:
    # Legacy .doc / unparseable binary must not decode into replacement-char mush.
    content = b"\x00\x01\x02\xff\xfe\xd0\xcf binary \x00\x00"
    r = parse_document(content, "legacy.doc", "application/msword")
    assert r.text == ""
    assert r.needs_ocr is False
    assert r.parser_name.startswith("unsupported")


def test_unknown_extension_with_real_text_falls_back() -> None:
    r = parse_document("Счёт №123 на оплату".encode(), "weird.abc", None)
    assert "Счёт №123" in r.text
    assert r.parser_name == "fallback_text.abc"


def test_eml_extracts_headers_body_and_attachments() -> None:
    eml = (
        b"From: a@b.com\r\n"
        b"To: c@d.com\r\n"
        b"Subject: Test Invoice\r\n"
        b'Content-Type: multipart/mixed; boundary="XX"\r\n\r\n'
        b"--XX\r\nContent-Type: text/plain\r\n\r\nBody text here.\r\n"
        b"--XX\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="inv.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--XX--\r\n"
    )
    r = parse_document(eml, "mail.eml", "message/rfc822")
    assert r.parser_name == "eml"
    assert "Test Invoice" in r.text
    assert "Body text here." in r.text
    attachments = r.meta.get("attachments") or []
    assert [a["filename"] for a in attachments] == ["inv.pdf"]
    assert isinstance(attachments[0]["content"], bytes)


def test_dxf_text_entities() -> None:
    pytest.importorskip("ezdxf")
    import ezdxf

    doc = ezdxf.new()
    doc.modelspace().add_text("DETAIL-42")
    import io

    buf = io.StringIO()
    doc.write(buf)
    r = parse_document(buf.getvalue().encode("utf-8"), "part.dxf", "image/vnd.dxf")
    assert r.parser_name == "dxf"
    assert "DETAIL-42" in r.text


def test_xlsx_rows_are_pipe_joined() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    import io

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Supplier", "Total"])
    ws.append(["ACME", 1000])
    buf = io.BytesIO()
    wb.save(buf)
    r = parse_document(
        buf.getvalue(),
        "invoice.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert r.parser_name == "xlsx"
    assert "ACME" in r.text
    assert "1000" in r.text


def test_docx_paragraphs_and_tables() -> None:
    docx = pytest.importorskip("docx")
    import io

    d = docx.Document()
    d.add_paragraph("Уважаемый поставщик")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Поз"
    table.rows[0].cells[1].text = "1"
    buf = io.BytesIO()
    d.save(buf)
    r = parse_document(
        buf.getvalue(),
        "letter.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert r.parser_name == "docx"
    assert "Уважаемый поставщик" in r.text


def test_pdf_text_layer_extracted() -> None:
    fitz = pytest.importorskip("fitz")

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "INVOICE NUMBER 555")
    content = doc.tobytes()
    doc.close()

    r = parse_document(content, "doc.pdf", "application/pdf")
    assert r.parser_name == "pdf_text_layer"
    assert r.needs_ocr is False
    assert "INVOICE NUMBER 555" in r.text
    assert r.page_count == 1


def test_pdf_without_text_layer_flags_ocr() -> None:
    fitz = pytest.importorskip("fitz")

    doc = fitz.open()
    doc.new_page()  # blank page, no text
    content = doc.tobytes()
    doc.close()

    r = parse_document(content, "blank.pdf", "application/pdf")
    assert r.needs_ocr is True
    assert r.parser_name == "pdf_scanned"
    assert r.text == ""
