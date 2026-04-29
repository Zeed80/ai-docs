from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from backend.app.domain.models import Document, ProcessingJobStatus
from backend.app.tasks.document_processing import extract_text


def test_extract_text_supported_plain_formats(tmp_path: Path) -> None:
    samples = {
        "note.txt": "plain manufacturing note",
        "table.csv": "item,qty\nbolt,10",
        "payload.json": '{"supplier": "ACME", "total": 1000}',
        "data.xml": "<invoice><supplier>ACME</supplier></invoice>",
    }

    for filename, content in samples.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        document = Document(
            case_id="case-id",
            filename=filename,
            content_type="text/plain",
            sha256="0" * 64,
            size_bytes=len(content),
            storage_path=str(path),
        )

        result = extract_text(document)

        assert result.status == ProcessingJobStatus.COMPLETED
        assert result.parser_name == f"text{path.suffix}"
        assert result.text == content


def test_extract_text_unsupported_cad_fallback(tmp_path: Path) -> None:
    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21;", encoding="utf-8")
    document = Document(
        case_id="case-id",
        filename="part.step",
        content_type="application/step",
        sha256="0" * 64,
        size_bytes=14,
        storage_path=str(path),
    )

    result = extract_text(document)

    assert result.status == ProcessingJobStatus.UNSUPPORTED
    assert result.parser_name == "step_header"
    assert result.unsupported_reason is not None


def test_extract_text_step_header_without_geometry_backend(tmp_path: Path) -> None:
    path = tmp_path / "part.step"
    path.write_text(
        "\n".join(
            [
                "ISO-10303-21;",
                "HEADER;",
                "FILE_DESCRIPTION(('example'),'2;1');",
                "FILE_NAME('shaft.step','2026-04-24',('ACME'),('TPO'),'','','');",
                "FILE_SCHEMA(('AP214'));",
                "ENDSEC;",
                "DATA;",
                "#1=CARTESIAN_POINT('',(0.,0.,0.));",
                "#2=DIRECTION('',(0.,0.,1.));",
                "ENDSEC;",
                "END-ISO-10303-21;",
            ]
        ),
        encoding="utf-8",
    )
    document = Document(
        case_id="case-id",
        filename="part.step",
        content_type="model/step",
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        storage_path=str(path),
    )

    result = extract_text(document)

    assert result.status == ProcessingJobStatus.COMPLETED
    assert result.parser_name == "step_header"
    assert "Full geometry analysis requires FreeCAD/pythonOCC backend" in result.text
    assert "CARTESIAN_POINT" in result.text


def test_extract_text_dxf_without_ezdxf_is_safe(tmp_path: Path, monkeypatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ezdxf":
            raise ImportError("blocked in test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    path = tmp_path / "part.dxf"
    path.write_text("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    document = Document(
        case_id="case-id",
        filename="part.dxf",
        content_type="image/vnd.dxf",
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        storage_path=str(path),
    )

    result = extract_text(document)

    assert result.status == ProcessingJobStatus.UNSUPPORTED
    assert result.parser_name == "dxf_unavailable"


def test_extract_text_xlsx_when_openpyxl_is_available(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "invoice.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Supplier", "Total"])
    sheet.append(["ACME", 1000])
    workbook.save(path)

    document = Document(
        case_id="case-id",
        filename="invoice.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        storage_path=str(path),
    )

    result = extract_text(document)

    assert result.status == ProcessingJobStatus.COMPLETED
    assert result.parser_name == "xlsx"
    assert "ACME" in result.text
    assert "1000" in result.text
