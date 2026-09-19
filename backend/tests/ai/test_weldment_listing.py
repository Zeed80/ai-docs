"""Пластины сварного узла против перечня на листе (X3)."""

from app.ai.cad_recognize.verifiers.reconcile import (
    apply_weldment_listing,
    listing_sizes,
    weldment_listing_check,
)


def _spec(rib: tuple[float, float, float], extra: list[str] | None = None) -> dict:
    values = ["80", "50", "5", "10", "Поз. 1 — Основание 80×80×10", "Поз. 2 — Ребро 1 80×50×5"]
    return {
        "dimensions": [{"value": v} for v in values + (extra or [])],
        "parts": [
            {"profile": {"width_mm": 80, "height_mm": 80, "thickness_mm": 10}},
            {"profile": dict(zip(("width_mm", "height_mm", "thickness_mm"), rib, strict=True))},
        ],
    }


def test_a_plate_misread_on_the_views_is_fixed_by_the_parts_list():
    """Живой узел: ребро 5 × 50 × 5 при «Поз. 2 — Ребро 1 80×50×5» — собрался молча."""
    spec = _spec((5.0, 50.0, 5.0))
    items = weldment_listing_check(spec)
    assert [i["status"] for i in items] == ["confirmed", "refuted"]
    fixed = apply_weldment_listing(spec, items)
    assert fixed["parts"][1]["profile"] == {
        "width_mm": 80.0,
        "height_mm": 50.0,
        "thickness_mm": 5.0,
    }


def test_the_same_sizes_in_another_order_are_confirmed():
    items = weldment_listing_check(_spec((50.0, 80.0, 5.0)))
    assert [i["status"] for i in items] == ["confirmed", "confirmed"]


def test_a_list_row_whose_numbers_are_not_on_the_views_goes_to_a_person():
    spec = _spec((5.0, 50.0, 5.0))
    spec["dimensions"] = [d for d in spec["dimensions"] if d["value"] != "80"]
    items = weldment_listing_check(spec)
    fixed = apply_weldment_listing(spec, items)
    assert fixed["parts"][1]["profile"]["width_mm"] == 5.0
    assert any("решение человеку" in note for note in fixed["unresolved"])


def test_a_retold_row_counts_once_and_a_contradiction_drops_the_position():
    spec = _spec((80.0, 50.0, 5.0), ["Поз. 2 — Ребро 1 (ребро жёсткости) размером 80 × 50 × 5."])
    assert listing_sizes(spec)[2] == (80.0, 50.0, 5.0)
    spec = _spec((80.0, 50.0, 5.0), ["Поз. 2 — Ребро 1 90×50×5"])
    assert 2 not in listing_sizes(spec)
