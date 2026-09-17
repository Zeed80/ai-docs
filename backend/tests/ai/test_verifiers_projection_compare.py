"""Проекция собранного тела вращения против проверенного вида листа (уровень 11)."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.projection_compare import compare_turned_projection

_ORIGIN = (100.0, 300.0)
_K = 0.1  # мм/px
_STEPS = [(20.0, 40.0), (30.0, 50.0), (20.0, 30.0)]  # Ø × длина


def _sheet(steps=_STEPS, line=6):
    image = Image.new("L", (1600, 600), 255)
    draw = ImageDraw.Draw(image)
    ox, oy = _ORIGIN
    x = ox
    previous = 0.0
    for diameter, length in steps:
        r = diameter / 2.0 / _K
        x1 = x + length / _K
        for sign in (-1, 1):
            draw.line([(x, oy + sign * r), (x1, oy + sign * r)], fill=0, width=line)
        top = max(r, previous)
        draw.line([(x, oy - top), (x, oy + top)], fill=0, width=line)
        previous = r
        x = x1
    draw.line([(x, oy - previous), (x, oy + previous)], fill=0, width=line)
    # Рамка листа — длинные основные линии для меры толщины.
    draw.rectangle([10, 10, 1590, 590], outline=0, width=line)
    return np.asarray(image)


def _front(steps=_STEPS):
    total = sum(length for _d, length in steps)
    u = -total / 2.0
    visible = []
    for diameter, length in steps:
        for sign in (-1, 1):
            visible.append(
                {
                    "type": "line",
                    "points": [[u, sign * diameter / 2], [u + length, sign * diameter / 2]],
                }
            )
        visible.append({"type": "line", "points": [[u, -diameter / 2], [u, diameter / 2]]})
        u += length
    return {
        "visible": visible,
        "bounds_mm": {"u_min": -total / 2.0, "u_max": total / 2.0, "v_min": -15, "v_max": 15},
    }


_FRAME = {"origin_px": list(_ORIGIN), "mm_per_px": _K, "mm_per_px_v": _K}
_SPEC = {"main_view": {"outer": [{"diameter_mm": d, "length_mm": length} for d, length in _STEPS]}}


def test_the_built_body_that_matches_the_sheet_passes():
    result = compare_turned_projection(_sheet(), _FRAME, _front(), _SPEC)

    assert result["status"] == "passed", result
    assert [s["diameter_mm"] for s in result["segments"]] == [20.0, 30.0, 20.0]
    assert all(item["offset_lines"] == 0.0 for item in result["shoulders"])


def test_a_wrong_diameter_is_caught():
    wrong = [(20.0, 40.0), (32.0, 50.0), (20.0, 30.0)]

    result = compare_turned_projection(_sheet(), _FRAME, _front(wrong), _SPEC)

    assert result["status"] == "failed"
    assert "Ø32" in result["reason"]


def test_a_shifted_shoulder_is_caught_though_the_envelope_mostly_agrees():
    """Корпус v9: огибающая сдвиг уступа на 3 мм не видела ни разу."""
    shifted = [(20.0, 43.0), (30.0, 47.0), (20.0, 30.0)]

    result = compare_turned_projection(_sheet(), _FRAME, _front(shifted), _SPEC)

    assert result["status"] == "failed"
    assert "уступ" in result["reason"]


def test_a_coarse_sheet_is_not_evidence():
    result = compare_turned_projection(_sheet(line=3), _FRAME, _front(), _SPEC)

    assert result["status"] == "unmeasurable"
    assert "грубый" in result["reason"]


def test_a_thread_is_not_compared():
    """ГОСТ 2.311: резьба изображается условно (z4-r4 рисует M18 как Ø16)."""
    drawn = [(16.0, 40.0), (30.0, 50.0), (20.0, 30.0)]
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 18.0, "length_mm": 40.0, "thread": {"designation": "M18"}},
                {"diameter_mm": 30.0, "length_mm": 50.0},
                {"diameter_mm": 20.0, "length_mm": 30.0},
            ]
        }
    }
    built = [(18.0, 40.0), (30.0, 50.0), (20.0, 30.0)]

    plain = compare_turned_projection(_sheet(drawn), _FRAME, _front(built), _SPEC)
    threaded = compare_turned_projection(_sheet(drawn), _FRAME, _front(built), spec)

    assert plain["status"] == "failed"
    assert threaded["status"] == "passed", threaded
    assert threaded["excluded"][0]["why"].startswith("резьба")


def test_without_a_verified_view_there_is_nothing_to_compare():
    assert compare_turned_projection(_sheet(), None, _front(), _SPEC)["status"] == "unmeasurable"


def test_a_passed_comparison_gives_the_graph_its_level_11_evidence():
    """Граф целиком: код projection_comparison_not_available снимается только при passed."""
    from app.ai.cad_emg_compat import projection_comparison_patch, spec_feature_tree_as_graph
    from app.ai.cad_ir.feature_tree import FeatureTreeCandidate
    from app.domain.engineering_model_graph import apply_graph_patch
    from app.services.engineering_model_graph import verify_graph

    graph = spec_feature_tree_as_graph(
        _SPEC, FeatureTreeCandidate(features=[], score=0.5, label="t"), graph_id="g"
    )
    codes = lambda g: [issue["code"] for issue in verify_graph(g)[1]]  # noqa: E731
    assert "projection_comparison_not_available" in codes(graph)

    failed = compare_turned_projection(
        _sheet(), _FRAME, _front([(20, 40), (32, 50), (20, 30)]), _SPEC
    )
    assert projection_comparison_patch(graph, failed, pass_id="p") is None

    passed = compare_turned_projection(_sheet(), _FRAME, _front(), _SPEC)
    patched = apply_graph_patch(graph, projection_comparison_patch(graph, passed, pass_id="p"))
    assert "projection_comparison_not_available" not in codes(patched)
