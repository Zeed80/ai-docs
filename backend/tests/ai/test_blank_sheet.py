"""ГОСТ 2.301/2.104 frame+stamp geometry for the blank-sheet entry point (Ф5.5)."""

from __future__ import annotations

from app.ai.cad_ir.blank_sheet import TB_H_MM, TB_W_MM, frame_and_title_block_entities
from app.ai.cad_ir.schema import Segment, TextEntity


def test_frame_produces_closed_rectangle_segments() -> None:
    entities = frame_and_title_block_entities(297, 210, 4.0)
    segments = [e for e in entities if isinstance(e, Segment)]
    # 4 sheet border + 4 stamp border + 6 horizontal grid + 2 vertical grid
    assert len(segments) == 4 + 4 + 6 + 2


def test_frame_segments_stay_within_sheet_bounds() -> None:
    w_mm, h_mm, px_per_mm = 297, 210, 4.0
    entities = frame_and_title_block_entities(w_mm, h_mm, px_per_mm)
    for e in entities:
        if not isinstance(e, Segment):
            continue
        for p in (e.p1, e.p2):
            assert 0 <= p.x <= w_mm * px_per_mm
            assert 0 <= p.y <= h_mm * px_per_mm


def test_labels_only_added_when_provided() -> None:
    empty = frame_and_title_block_entities(297, 210, 4.0)
    assert not any(isinstance(e, TextEntity) for e in empty)

    filled = frame_and_title_block_entities(
        297, 210, 4.0, name="Вал", designation="АБВГ.001", company="Завод"
    )
    texts = {e.text for e in filled if isinstance(e, TextEntity)}
    assert texts == {"Вал", "АБВГ.001", "Завод"}


def test_all_entities_are_human_approved() -> None:
    entities = frame_and_title_block_entities(297, 210, 4.0, name="X")
    assert all(e.origin == "human" and e.assurance == "human_approved" for e in entities)


def test_title_block_dimensions_match_gost_2104_form1() -> None:
    assert TB_W_MM == 185.0
    assert TB_H_MM == 55.0
