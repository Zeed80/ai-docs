"""Контракт проверяльщиков (план, Ф1): вердикт на каждую гипотезу."""

from __future__ import annotations

import numpy as np

from app.ai.cad_recognize.verifiers import Hypothesis, ViewFrame, registered_kinds, verify


def test_a_view_frame_round_trips_millimetres_and_pixels():
    frame = ViewFrame(bbox_px=(100, 100, 900, 500), mm_per_px=0.5, origin_px=(150.0, 450.0))

    x, y = frame.to_px(20.0, 10.0)
    assert (x, y) == (190.0, 430.0)  # v вверх — на листе y меньше
    assert frame.to_mm(x, y) == (20.0, 10.0)
    assert frame.roi_px(0.0, 0.0, 100.0) == (100, 250.0, 350.0, 500)  # обрезано рамкой вида


def test_an_unknown_kind_is_unmeasurable_not_silent():
    verdict = verify(Hypothesis(kind="no-such-thing", path="x"), None, None)
    assert verdict.status == "unmeasurable" and "нет проверяльщика" in verdict.reason


def test_below_the_measurability_threshold_there_is_no_guess():
    verdict = verify(
        Hypothesis(kind="dimension_line", path="x", expected={"feature_px": 5.0}), None, None
    )
    assert verdict.status == "unmeasurable" and "порога" in verdict.reason


def _dimension_sheet() -> np.ndarray:
    """Размерная линия 250 px между выносными на строке 200, подпись над ней."""
    from PIL import Image, ImageDraw

    from app.ai.cad_recognize.axial_dimensions import _ink_rows

    image = Image.new("L", (800, 500), 255)
    draw = ImageDraw.Draw(image)
    draw.line([(100, 200), (400, 200)], fill=0, width=3)
    for x in (150, 400):
        draw.line([(x, 177), (x, 480)], fill=0, width=3)
    for tip, back in ((150, 191), (400, 359)):
        draw.polygon([(tip, 200), (back, 189), (back, 211)], fill=0)
    return _ink_rows(image)


def test_the_dimension_line_verifier_confirms_and_refutes_by_scale():
    sheet = _dimension_sheet()
    frame = ViewFrame(bbox_px=(0, 0, 800, 500), mm_per_px=0.2, origin_px=(0.0, 400.0))
    label = (262.0, 150.0, 288.0, 181.0)

    right = verify(Hypothesis("dimension_line", "dims/0", {"value_mm": 50.0}, label), frame, sheet)
    wrong = verify(Hypothesis("dimension_line", "dims/0", {"value_mm": 65.0}, label), frame, sheet)

    assert right.status == "confirmed" and abs(right.measured["value_mm"] - 50.0) < 1.0
    assert wrong.status == "refuted" and "65" in wrong.reason
    assert right.as_payload()["anchors_px"]  # готово к свидетельству в графе


def test_the_registry_knows_its_built_in_verifiers():
    assert "dimension_line" in registered_kinds()
