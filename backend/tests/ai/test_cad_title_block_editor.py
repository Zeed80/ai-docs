"""Structured основная надпись editor (C3)."""

from __future__ import annotations

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import SheetInfo
from app.ai.cad_ir.title_block import apply_title_block, stamp_region_px


def _sheet_ir(with_frame=False) -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=int(297 * 4), image_height=int(210 * 4)),
        scale=0.25,  # 4 px/mm
        scale_source="sheet_format",
        sheet=SheetInfo(format="A4", width_mm=297.0, height_mm=210.0, frame=with_frame),
    )


def _tb_texts(ir):
    return [e for e in ir.entities if e.type == "text" and "title_block_text" in (e.evidence or [])]


def test_apply_stores_fields_and_renders_labels():
    ir = _sheet_ir()
    n = apply_title_block(ir, {"designation": "АБВГ.001", "name": "Вал", "material": "Сталь 45"})
    assert n > 0
    assert ir.sheet.title_block["fields"]["designation"] == "АБВГ.001"
    texts = {e.text for e in _tb_texts(ir)}
    assert "АБВГ.001" in texts
    assert "Вал" in texts
    assert "Сталь 45" in texts


def test_apply_creates_frame_when_missing():
    ir = _sheet_ir(with_frame=False)
    apply_title_block(ir, {"name": "Деталь"})
    assert ir.sheet.frame is True
    frame_lines = [e for e in ir.entities if "title_block_frame" in (e.evidence or [])]
    assert len(frame_lines) >= 8  # sheet border (4) + stamp box (4) + grid


def test_reapply_replaces_labels_not_accumulate():
    ir = _sheet_ir()
    apply_title_block(ir, {"name": "Первое", "designation": "X1"})
    first = len(_tb_texts(ir))
    apply_title_block(ir, {"name": "Второе"})
    texts = {e.text for e in _tb_texts(ir)}
    assert "Первое" not in texts  # replaced
    assert "Второе" in texts
    assert "X1" in texts  # merged, not lost
    assert len(_tb_texts(ir)) <= first + 2  # stable, not doubled


def test_reapply_does_not_duplicate_frame():
    ir = _sheet_ir()
    apply_title_block(ir, {"name": "A"})
    frame1 = sum(1 for e in ir.entities if "title_block_frame" in (e.evidence or []))
    apply_title_block(ir, {"name": "B"})
    frame2 = sum(1 for e in ir.entities if "title_block_frame" in (e.evidence or []))
    assert frame1 == frame2


def test_mass_and_scale_rendered():
    ir = _sheet_ir()
    apply_title_block(ir, {"name": "X", "mass_kg": 2.5, "scale": "1:2"})
    texts = {e.text for e in _tb_texts(ir)}
    assert "2.5" in texts
    assert "1:2" in texts
    assert ir.sheet.title_block["scale"] == "1:2"


def test_labels_land_inside_stamp_region():
    ir = _sheet_ir()
    apply_title_block(ir, {"name": "Вал", "designation": "АБВГ.001"})
    region = stamp_region_px(ir)
    assert region is not None
    x0, y0, x1, y1 = region
    for e in _tb_texts(ir):
        assert x0 - 1 <= e.position.x <= x1 + 1
        assert y0 - 1 <= e.position.y <= y1 + 1


def test_unknown_fields_ignored():
    ir = _sheet_ir()
    apply_title_block(ir, {"name": "X", "bogus": "hack"})
    assert "bogus" not in ir.sheet.title_block["fields"]


def test_generated_stamp_is_eskd_text_height_clean():
    """The stamp renderer must use nominal ГОСТ 2.304 heights — the generated
    основная надпись must not trip the ESKD_TEXT_HEIGHT check it enforces."""
    from app.ai.cad_validate import validate_ir

    ir = _sheet_ir()
    apply_title_block(ir, {
        "name": "Вал", "designation": "АБВГ.001", "material": "Сталь 45",
        "scale": "1:2", "developer": "Иванов",
    })
    label_ids = {e.id for e in _tb_texts(ir)}
    report = validate_ir(ir)
    offenders = [
        i for i in report.issues
        if i.code == "ESKD_TEXT_HEIGHT" and set(i.entity_ids) & label_ids
    ]
    assert offenders == []
