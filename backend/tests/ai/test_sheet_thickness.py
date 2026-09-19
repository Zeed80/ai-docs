"""Толщина листа гнутой детали — по ширине сечения на листе (X4)."""

from app.ai.cad_recognize.verifiers.bent_section import sheet_thickness_check
from app.ai.cad_recognize.verifiers.reconcile import (
    apply_sheet_thickness,
    sheet_thickness_decision,
)

# Z-профиль 40 / 60 / 30 по наружной поверхности, R 2, s 2, 4 px/мм: по осевой
# 39 / 58 / 29 мм; полоса сечения — s × 4 + обводка 6 px.
ITEM = {
    "kind": "bent_section",
    "measured": {"flanges_px": [156.0, 232.0, 116.0], "band_precise_px": 14.0, "line_px": 6.0},
}


def _sheet(thickness: float) -> dict:
    reach = 2.0 + thickness  # (R + s)·tg 45°
    return {
        "flanges_mm": [40.0 - reach, 60.0 - 2 * reach, 30.0 - reach],
        "turns": [1, -1],
        "radius_mm": 2.0,
        "thickness_mm": thickness,
        "width_mm": 50.0,
    }


def test_the_right_thickness_is_confirmed_by_the_section():
    assert sheet_thickness_check(_sheet(2.0), ITEM)["status"] == "confirmed"


def test_a_misread_thickness_is_refuted_and_the_sheet_number_taken():
    """Живой Z-профиль: s прочитано 2 при 2,5 на листе — все полки на 0,5 мм мимо."""
    item = sheet_thickness_check(_sheet(1.5), ITEM)
    assert item["status"] == "refuted"
    assert abs(item["measured"]["thickness_mm"] - 2.0) < 0.15

    spec = {
        "main_view": {"sheet_metal": _sheet(1.5)},
        "dimensions": [{"value": "40"}, {"value": "60"}, {"value": "30"}, {"value": "s2"}],
    }
    decision = sheet_thickness_decision(spec, item)
    assert decision["action"] == "adopt" and decision["value"] == 2.0
    fixed = apply_sheet_thickness(spec, decision)["main_view"]["sheet_metal"]
    assert fixed["thickness_mm"] == 2.0
    assert fixed["flanges_mm"] == _sheet(2.0)["flanges_mm"]


def test_without_such_a_number_on_the_sheet_it_goes_to_a_person():
    item = sheet_thickness_check(_sheet(1.5), ITEM)
    spec = {"main_view": {"sheet_metal": _sheet(1.5)}, "dimensions": [{"value": "40"}]}
    assert sheet_thickness_decision(spec, item)["action"] == "ask_human"


def test_flanges_that_do_not_fit_the_section_are_not_measured():
    item = {**ITEM, "measured": {**ITEM["measured"], "flanges_px": [156.0, 100.0, 116.0]}}
    assert sheet_thickness_check(_sheet(2.0), item)["status"] == "unmeasurable"
