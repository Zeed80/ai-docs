"""Масштаб чертежа по листу: основная надпись ЕСКД 185 × 55 мм — линейка бумаги."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.sheet_scale import (
    drawing_scale,
    locate_title_block,
    same_scale,
)

PAPER = 8.0  # px на мм бумаги: A3 420 × 297
LINE = 6


def _sheet() -> np.ndarray:
    width, height = int(420 * PAPER), int(297 * PAPER)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle([2, 2, width - 3, height - 3], outline=0, width=3)  # край бумаги
    left, top, right, bottom = 20 * PAPER, 5 * PAPER, width - 5 * PAPER, height - 5 * PAPER
    draw.rectangle([left, top, right, bottom], outline=0, width=LINE)  # рамка
    stamp_left, stamp_top = right - 185 * PAPER, bottom - 55 * PAPER
    draw.line([(stamp_left, stamp_top), (right, stamp_top)], fill=0, width=LINE)
    draw.line([(stamp_left, stamp_top), (stamp_left, bottom)], fill=0, width=LINE)
    for k in range(1, 4):  # графы штампа
        y = stamp_top + k * 11 * PAPER
        draw.line([(stamp_left + 65 * PAPER, y), (right, y)], fill=0, width=LINE)
    draw.line(
        [(stamp_left + 65 * PAPER, stamp_top), (stamp_left + 65 * PAPER, bottom)],
        fill=0,
        width=LINE,
    )
    # Деталь: основная линия в виде.
    draw.rectangle([600, 500, 1600, 900], outline=0, width=LINE)
    return np.asarray(image)


def test_the_title_block_gives_the_paper_scale_and_confirms_the_stated_drawing_scale():
    block = locate_title_block(_sheet())

    assert block is not None
    assert abs(block.paper_px_per_mm - PAPER) / PAPER < 0.02
    # Вид 4:1 — 32 px на мм детали.
    measured = drawing_scale(1.0 / (4 * PAPER), block)
    assert measured["label"] == "4:1"
    assert same_scale("4:1", measured["label"]) and same_scale("М 4 : 1", "4:1")
    assert not same_scale("2:1", measured["label"])


def test_a_sheet_without_a_title_block_gives_no_scale():
    image = np.full((1200, 1600), 255, np.uint8)
    image[400:406, 200:1400] = 0
    assert locate_title_block(image) is None


def test_a_confirmed_scale_is_evidence_in_the_graph_and_a_mismatch_is_a_note():
    from app.ai.cad_emg_compat import legacy_spec_as_low_assurance
    from app.ai.cad_recognize.verifiers.stage import attach_scale_evidence
    from app.services.engineering_model_graph import verify_graph

    spec = {"title_block": {"scale": "4:1"}, "main_view": {"outer": []}}
    report = {
        "sheet_scale": {
            "stamp_bbox_px": [1880.0, 1896.0, 3320.0, 2336.0],
            "paper_px_per_mm": 8.0,
            "view_px_per_mm": 32.0,
            "ratio": 4.0,
            "label": "4:1",
            "stated": "4:1",
            "agrees": True,
        }
    }

    def scale_codes(payload):
        graph = legacy_spec_as_low_assurance(payload, graph_id="g", source_uri="minio://sheet.png")
        _state, issues = verify_graph(graph)
        return [i["code"] for i in issues if i["code"].startswith("drawing_scale")]

    assert scale_codes(spec) == ["drawing_scale_evidence_missing"]
    assert scale_codes(attach_scale_evidence(spec, report)) == []
    disagree = {"sheet_scale": {**report["sheet_scale"], "label": "2:1", "agrees": False}}
    assert attach_scale_evidence(spec, disagree) == spec
