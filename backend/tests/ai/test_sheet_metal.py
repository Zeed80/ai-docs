"""Листовая деталь без верстака (X4, E14): сечение с гибами, выдавленное на ширину.

Живое ядро (2026-09-18): уголок, швеллер, Z-профиль и короб — объём совпал с
формулой до сотых, развёртка по нейтральному слою с 3D — до 0,0000 мм.
"""

from __future__ import annotations

import math

import pytest

from app.ai.sheet_metal import bent_section, developed_length, section_area


def _polygon_area(sketch: list[dict]) -> tuple[float, tuple[float, float]]:
    points = [(0.0, 0.0)]
    x0, y0 = 0.0, 0.0
    for segment in sketch:
        x1, y1 = segment["to"]
        if segment["kind"] == "arc":
            cx, cy = segment["center"]
            radius = math.hypot(x0 - cx, y0 - cy)
            a0 = math.atan2(y0 - cy, x0 - cx)
            sweep = math.atan2(y1 - cy, x1 - cx) - a0
            if segment["clockwise"]:
                while sweep >= 0:
                    sweep -= 2 * math.pi
            else:
                while sweep <= 0:
                    sweep += 2 * math.pi
            for step in range(1, 400):
                angle = a0 + sweep * step / 400
                points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        points.append((x1, y1))
        x0, y0 = x1, y1
    area = (
        abs(
            sum(
                points[i][0] * points[i + 1][1] - points[i + 1][0] * points[i][1]
                for i in range(len(points) - 1)
            )
        )
        / 2.0
    )
    return area, points[-1]


@pytest.mark.parametrize(
    ("flanges", "turns"),
    [
        ([40.0, 30.0], [1]),
        ([40.0, 30.0], [-1]),
        ([30.0, 50.0, 30.0], [1, -1]),
        ([20.0, 40.0, 60.0, 40.0], [1, 1, 1]),
    ],
)
def test_the_section_closes_and_its_area_is_the_formula(flanges, turns):
    sketch = bent_section(flanges, turns, 3.0, 2.0)

    area, last = _polygon_area(sketch)

    assert last == (0.0, 0.0)
    assert abs(area - section_area(flanges, len(turns), 3.0, 2.0)) <= 0.01


def test_the_developed_length_follows_the_neutral_layer():
    # Уголок 40 + 30, R3, t2: дуга по нейтральному слою K = 0,5 — π/2 · 4.
    assert abs(developed_length([40.0, 30.0], 1, 3.0, 2.0, 0.5) - (70.0 + math.pi * 2.0)) < 1e-9
    # Меньший K — меньшая дуга: K = 0,33 (часто для гиба R ≈ t).
    assert developed_length([40.0, 30.0], 1, 3.0, 2.0, 0.33) < developed_length(
        [40.0, 30.0], 1, 3.0, 2.0, 0.5
    )


def test_a_malformed_section_is_refused():
    with pytest.raises(ValueError, match="гибов"):
        bent_section([40.0, 30.0], [], 3.0, 2.0)
    with pytest.raises(ValueError, match="положительны"):
        bent_section([40.0, 0.0], [1], 3.0, 2.0)


def _channel_spec(sheet: dict | None = None) -> dict:
    return {
        "part": "Швеллер гнутый",
        "main_view": {
            "type": "листовая деталь",
            "sheet_metal": sheet
            or {
                "flanges_mm": [20.0, 40.0, 20.0],
                "turns": [1, 1],
                "radius_mm": 2.0,
                "thickness_mm": 2.0,
                "width_mm": 50.0,
            },
        },
    }


def test_a_read_sheet_metal_part_becomes_a_bent_section_extruded_by_width():
    """X4: листовая деталь в спеке — дерево операций с сечением и шириной.

    Раньше ни схема, ни дерево гибов не знали: деталь выпадала как «класс не
    определён» и строилась в лучшем случае прямоугольной пластиной."""
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
    from app.ai.cad_solid import feature_tree_from_spec

    spec = EngineeringDrawingSpec.model_validate(_channel_spec()).model_dump()
    tree = feature_tree_from_spec(spec)

    assert tree is not None
    [extrude] = tree.features
    assert extrude.kind == "extrude"
    assert extrude.params["depth_mm"] == 50.0
    assert any(segment["kind"] == "arc" for segment in extrude.params["sketch_profile"])
    area, _centroid = _polygon_area(extrude.params["sketch_profile"])
    assert area == pytest.approx(section_area([20.0, 40.0, 20.0], 2, 2.0, 2.0), rel=1e-3)
    assert {"sketch_profile", "depth_mm"} <= set(extrude.param_provenance)


def test_the_schema_refuses_a_bend_count_that_does_not_match_the_flanges():
    from pydantic import ValidationError

    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec

    bad = _channel_spec(
        {
            "flanges_mm": [20.0, 40.0, 20.0],
            "turns": [1],
            "radius_mm": 2.0,
            "thickness_mm": 2.0,
            "width_mm": 50.0,
        }
    )
    with pytest.raises(ValidationError):
        EngineeringDrawingSpec.model_validate(bad)


def test_the_consensus_keeps_an_agreed_sheet_metal_part_and_drops_a_disputed_one():
    """Та же семья молчаливых потерь, что фланцы и размещение: поле, которого
    консенсус не знает, выпадало без следа."""
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    merged = consensus_spec([_channel_spec(), _channel_spec(), _channel_spec()])
    assert merged["main_view"]["sheet_metal"]["flanges_mm"] == [20.0, 40.0, 20.0]

    other = dict(_channel_spec()["main_view"]["sheet_metal"], radius_mm=4.0)
    third = dict(other, radius_mm=6.0)
    split = consensus_spec([_channel_spec(), _channel_spec(other), _channel_spec(third)])
    assert "sheet_metal" not in split["main_view"]


def test_a_sheet_metal_part_counts_as_geometry():
    from app.ai.cad_recognize.spec_fragments import spec_has_geometry

    assert spec_has_geometry(_channel_spec())


def test_the_bent_section_survives_the_round_trip_through_the_graph():
    from app.ai.cad_emg_compat import feature_tree_from_graph, spec_feature_tree_as_graph
    from app.ai.cad_solid import feature_tree_from_spec

    spec = _channel_spec()
    graph = spec_feature_tree_as_graph(spec, feature_tree_from_spec(spec), graph_id="g")
    rebuilt = feature_tree_from_graph(graph, target_id="preview")

    [extrude] = [f for f in rebuilt.features if f.kind == "extrude"]
    assert extrude.params["depth_mm"] == 50.0
    assert sum(1 for s in extrude.params["sketch_profile"] if s["kind"] == "arc") == 4


def _taken(*numbers: float):
    return lambda value: value if any(abs(value - n) < 1e-6 for n in numbers) else None


def test_the_reader_turns_outer_flange_sizes_into_straight_runs():
    """Лист ставит полку по наружной поверхности; схема хранит прямой участок
    между гибами — минус R + s на каждый прилегающий гиб."""
    from app.ai.cad_recognize.spec_fragments import sheet_metal_from_answer

    answer = {
        "shape": "channel",
        "flanges_mm": [19, 48, 16],
        "radius_mm": 2,
        "thickness_mm": 2,
        "width_mm": 50,
    }
    sheet = sheet_metal_from_answer(answer, _taken(19, 48, 16, 2, 50))

    assert sheet == {
        "flanges_mm": [15.0, 40.0, 12.0],
        "turns": [1, 1],
        "radius_mm": 2.0,
        "thickness_mm": 2.0,
        "width_mm": 50.0,
    }


def test_the_reader_builds_nothing_from_a_number_the_sheet_does_not_state():
    from app.ai.cad_recognize.spec_fragments import sheet_metal_from_answer

    notes: list[str] = []
    answer = {
        "shape": "angle",
        "flanges_mm": [45, 25],
        "radius_mm": 2.5,
        "thickness_mm": 2.5,
        "width_mm": 30,
    }
    assert sheet_metal_from_answer(answer, _taken(45, 25, 2.5), notes) is None
    assert notes == ["листовая деталь не построена: ширина"]


def test_explicit_turns_must_match_the_flanges():
    from app.ai.cad_recognize.spec_fragments import sheet_metal_from_answer

    answer = {
        "shape": "other",
        "flanges_mm": [20, 30, 20],
        "turns": ["left"],
        "radius_mm": 2,
        "thickness_mm": 2,
        "width_mm": 40,
    }
    notes: list[str] = []
    assert sheet_metal_from_answer(answer, _taken(20, 30, 2, 40), notes) is None
    assert "число гибов не сходится с числом полок" in notes[0]


def test_the_operator_can_say_the_part_is_sheet_metal():
    from app.ai.cad_digitization_type import resolve_digitization_type
    from app.ai.cad_recognize.spec_fragments import _apply_operator_kind, _kind_prompt

    decision = resolve_digitization_type("sheet_metal_part")
    assert decision.spec_redraw_supported
    assert "kind=sheet_metal" in _kind_prompt("sheet_metal_part")
    kind, notes = _apply_operator_kind("plate", "sheet_metal_part")
    assert kind == "sheet_metal" and notes


def test_a_bend_that_is_not_square_keeps_area_and_developed_length():
    """Гиб на 60° (угол между полками 120°): площадь — полки и сектор кольца,
    развёртка — дуга по нейтральному слою на этот угол. Живое ядро: уголок
    40/20 × 80 при R = s = 2,5 — 11 630,70 мм³, как по формуле."""
    flanges, turns, angles = [37.113249, 17.113249], [1], [60.0]
    sketch = bent_section(flanges, turns, 2.5, 2.5, angles)
    area, _ = _polygon_area(sketch)
    assert area == pytest.approx(section_area(flanges, 1, 2.5, 2.5, angles), rel=1e-3)
    assert section_area(flanges, 1, 2.5, 2.5, angles) * 80 == pytest.approx(11630.70, abs=0.05)
    assert developed_length(flanges, 1, 2.5, 2.5, 0.5, angles) == pytest.approx(
        sum(flanges) + math.radians(60) * 3.75
    )


def test_an_outer_flange_size_runs_to_the_virtual_sharp():
    """У гиба не на 90° полку проставляют до пересечения наружных поверхностей:
    прямой участок + (R + s)·tg(θ/2)."""
    from app.ai.sheet_metal import flange_spans

    spans = flange_spans([37.113249, 17.113249], [1], 2.5, 2.5, [60.0])
    assert [round(span["value"], 3) for span in spans] == [40.0, 20.0]
    assert spans[1]["axis"] == "aligned"


def test_the_reader_turns_the_angle_between_flanges_into_the_bend_angle():
    from app.ai.cad_recognize.spec_fragments import sheet_metal_from_answer

    answer = {
        "shape": "angle",
        "flanges_mm": [40, 20],
        "angles_deg": [120],
        "radius_mm": 2.5,
        "thickness_mm": 2.5,
        "width_mm": 80,
    }
    sheet = sheet_metal_from_answer(answer, _taken(40, 20, 120, 2.5, 80))
    assert sheet["bend_angles_deg"] == [60.0]
    assert sheet["flanges_mm"] == pytest.approx([37.113249, 17.113249], abs=1e-6)
