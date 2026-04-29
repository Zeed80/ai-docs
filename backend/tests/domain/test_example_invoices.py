from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.domain.models import Document, ProcessingJobStatus
from backend.app.tasks.document_processing import extract_text


def test_example_invoice_pdf_text_layer_smoke() -> None:
    pytest.importorskip("fitz")
    path = Path("example-invoices/Xoffmann № ПРЗ2419587 от 30 мая 2024 г.pdf")
    if not path.exists():
        pytest.skip("Local example-invoices dataset is not present")
    document = Document(
        case_id="example-case",
        filename=path.name,
        content_type="application/pdf",
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        storage_path=str(path),
    )

    result = extract_text(document)

    assert result.status == ProcessingJobStatus.COMPLETED
    assert result.parser_name == "pdf_text_layer"
    assert "Счет" in result.text
    assert "ПРЗ2419587" in result.text
