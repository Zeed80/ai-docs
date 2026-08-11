"""Tests for the assembly BOM extraction Celery task (Фаза 4.1).

Covers: format dispatch in _render_drawing_sheet_png, persistence of
extracted BOM items as DrawingAssemblyBOM rows, idempotent re-run
(replace, not append), and the fail-closed "no raster available" path.
"""

import uuid

import pytest
from sqlalchemy import select

from app.ai.assembly_extractor import AssemblyBOMResult, BalloonAnnotation, BOMItem
from app.db.models import Drawing, DrawingAssemblyBOM, DrawingStatus
from app.tasks import drawing_analysis


@pytest.fixture
async def assembly_drawing(db_session):
    d = Drawing(
        filename="unit-sb.pdf",
        format="pdf",
        drawing_number="SB-001",
        status=DrawingStatus.analyzed,
        metadata_={"drawing_type": "assembly", "storage_path": "drawings/unit-sb.pdf"},
    )
    db_session.add(d)
    await db_session.commit()
    await db_session.refresh(d)
    return d


@pytest.fixture
def bound_session_factory(db_session, monkeypatch):
    """Point _get_session_factory() at db_session's own connection.

    _extract_assembly_bom_async opens its own sessions via the production
    _get_session_factory(); without this, it would use a separate physical
    connection that can't see db_session's not-yet-truly-committed rows
    (per-test rollback isolation) — same pattern as tests/ai/test_cad_trace_task.py.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    conn = db_session.bind

    def _factory():
        return AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)
    return _factory


# ── _render_drawing_sheet_png format dispatch ──────────────────────────────────


@pytest.mark.asyncio
async def test_render_sheet_png_svg(monkeypatch):
    async def _fake_svg_to_png(svg_content, width=2048):
        assert svg_content == "<svg/>"
        return b"PNG-FROM-SVG"

    monkeypatch.setattr(drawing_analysis, "_svg_to_png_bytes", _fake_svg_to_png)
    out = await drawing_analysis._render_drawing_sheet_png(b"<svg/>", "svg")
    assert out == b"PNG-FROM-SVG"


@pytest.mark.asyncio
async def test_render_sheet_png_pdf(monkeypatch):
    async def _fake_pdf_to_png(file_bytes, page_index=0, dpi=200):
        return b"PNG-FROM-PDF"

    monkeypatch.setattr(drawing_analysis, "_pdf_to_png_bytes", _fake_pdf_to_png)
    out = await drawing_analysis._render_drawing_sheet_png(b"%PDF-1.4", "pdf")
    assert out == b"PNG-FROM-PDF"


@pytest.mark.asyncio
async def test_render_sheet_png_raster(monkeypatch):
    async def _fake_normalize(file_bytes, fmt):
        assert fmt == "png"
        return b"PNG-NORMALIZED"

    monkeypatch.setattr(drawing_analysis, "_normalize_raster_to_png", _fake_normalize)
    out = await drawing_analysis._render_drawing_sheet_png(b"\x89PNG", "png")
    assert out == b"PNG-NORMALIZED"


@pytest.mark.asyncio
async def test_render_sheet_png_dxf(monkeypatch):
    async def _fake_parse_dxf(file_bytes, filename):
        return "<svg>dxf</svg>", [], "text"

    async def _fake_svg_to_png(svg_content, width=2048):
        return b"PNG-FROM-DXF"

    monkeypatch.setattr(drawing_analysis, "_parse_dxf", _fake_parse_dxf)
    monkeypatch.setattr(drawing_analysis, "_svg_to_png_bytes", _fake_svg_to_png)
    out = await drawing_analysis._render_drawing_sheet_png(b"DXF-BYTES", "dxf")
    assert out == b"PNG-FROM-DXF"


@pytest.mark.asyncio
async def test_render_sheet_png_dwg_conversion_fails_returns_none(monkeypatch):
    async def _fake_convert(file_bytes):
        return None

    monkeypatch.setattr(drawing_analysis, "_convert_dwg_to_dxf", _fake_convert)
    out = await drawing_analysis._render_drawing_sheet_png(b"DWG-BYTES", "dwg")
    assert out is None


@pytest.mark.asyncio
async def test_render_sheet_png_unsupported_format_returns_none():
    out = await drawing_analysis._render_drawing_sheet_png(b"???", "step")
    assert out is None


# ── _extract_assembly_bom_async ────────────────────────────────────────────────


def _fake_bom_result() -> AssemblyBOMResult:
    return AssemblyBOMResult(
        items=[
            BOMItem(
                item_no=1,
                designation="Вал",
                quantity=1.0,
                unit="шт",
                material="Сталь 45",
                drawing_number="ДП-001",
                confidence=0.9,
                balloon_coords=[{"x": 10, "y": 20, "r": 15}],
            ),
            BOMItem(
                item_no=2,
                designation="Подшипник 6205",
                quantity=2.0,
                unit="шт",
                confidence=0.8,
            ),
        ],
        balloons=[BalloonAnnotation(item_no=1, x=10, y=20, radius=15)],
        table_bbox=(100, 0, 400, 200),
        confidence=0.85,
    )


@pytest.mark.asyncio
async def test_extract_assembly_bom_async_persists_items(
    monkeypatch, assembly_drawing, db_session, bound_session_factory
):
    async def _fake_load_file(drawing):
        return b"FAKE-PDF-BYTES"

    async def _fake_render(file_bytes, fmt):
        return b"FAKE-PNG"

    async def _fake_extract(image_bytes, router=None, drawing=None, allow_cloud=False):
        assert image_bytes == b"FAKE-PNG"
        return _fake_bom_result()

    monkeypatch.setattr(drawing_analysis, "_load_drawing_file", _fake_load_file)
    monkeypatch.setattr(drawing_analysis, "_render_drawing_sheet_png", _fake_render)
    monkeypatch.setattr("app.ai.assembly_extractor.extract_assembly_bom", _fake_extract)

    out = await drawing_analysis._extract_assembly_bom_async(str(assembly_drawing.id))

    assert out["items_count"] == 2
    assert out["balloons_count"] == 1
    assert out["table_bbox"] == [100, 0, 400, 200]

    result = await db_session.execute(
        select(DrawingAssemblyBOM)
        .where(DrawingAssemblyBOM.drawing_id == assembly_drawing.id)
        .order_by(DrawingAssemblyBOM.item_no)
    )
    rows = result.scalars().all()
    assert len(rows) == 2
    assert rows[0].designation == "Вал"
    assert rows[0].balloon_coords == [{"x": 10, "y": 20, "r": 15}]
    assert rows[1].designation == "Подшипник 6205"


@pytest.mark.asyncio
async def test_extract_assembly_bom_async_rerun_replaces_rows(
    monkeypatch, assembly_drawing, db_session, bound_session_factory
):
    async def _fake_load_file(drawing):
        return b"FAKE-PDF-BYTES"

    async def _fake_render(file_bytes, fmt):
        return b"FAKE-PNG"

    monkeypatch.setattr(drawing_analysis, "_load_drawing_file", _fake_load_file)
    monkeypatch.setattr(drawing_analysis, "_render_drawing_sheet_png", _fake_render)

    async def _fake_extract_first(image_bytes, router=None, drawing=None, allow_cloud=False):
        return _fake_bom_result()

    monkeypatch.setattr("app.ai.assembly_extractor.extract_assembly_bom", _fake_extract_first)
    await drawing_analysis._extract_assembly_bom_async(str(assembly_drawing.id))

    async def _fake_extract_second(image_bytes, router=None, drawing=None, allow_cloud=False):
        return AssemblyBOMResult(
            items=[BOMItem(item_no=1, designation="Только гайка", quantity=4.0, confidence=0.7)],
        )

    monkeypatch.setattr("app.ai.assembly_extractor.extract_assembly_bom", _fake_extract_second)
    out = await drawing_analysis._extract_assembly_bom_async(str(assembly_drawing.id))
    assert out["items_count"] == 1

    result = await db_session.execute(
        select(DrawingAssemblyBOM).where(DrawingAssemblyBOM.drawing_id == assembly_drawing.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].designation == "Только гайка"


@pytest.mark.asyncio
async def test_extract_assembly_bom_async_no_raster_is_fail_closed(
    monkeypatch, assembly_drawing, db_session, bound_session_factory
):
    async def _fake_load_file(drawing):
        return b"FAKE-BYTES"

    async def _fake_render(file_bytes, fmt):
        return None

    monkeypatch.setattr(drawing_analysis, "_load_drawing_file", _fake_load_file)
    monkeypatch.setattr(drawing_analysis, "_render_drawing_sheet_png", _fake_render)

    out = await drawing_analysis._extract_assembly_bom_async(str(assembly_drawing.id))
    assert "error" in out

    result = await db_session.execute(
        select(DrawingAssemblyBOM).where(DrawingAssemblyBOM.drawing_id == assembly_drawing.id)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_extract_assembly_bom_async_drawing_not_found(db_session, bound_session_factory):
    out = await drawing_analysis._extract_assembly_bom_async(str(uuid.uuid4()))
    assert out == {"error": "Drawing not found"}
