"""Стадия проверки: спек пластины против листа, без эталона."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

# План 80×50 мм при 10 px/мм, левый нижний угол — (100, 600) px.
PX = 10.0
X0, Y0 = 100.0, 600.0
# (x от левой, y от нижней кромки, Ø) — как на листе plate-1 корпуса.
HOLES = [(18.0, 23.0, 5.5), (39.0, 19.0, 11.0), (52.0, 32.0, 6.6), (64.0, 26.0, 5.5)]


def _png(*, plan: bool = True) -> bytes:
    image = Image.new("L", (1000, 800), 255)
    draw = ImageDraw.Draw(image)
    if plan:
        x1, y1 = X0 + 80 * PX, Y0 - 50 * PX
        # Кромки — серединой линии на контуре, основной толщины.
        for a, b in (((X0, Y0), (x1, Y0)), ((X0, y1), (x1, y1))):
            draw.line([a, b], fill=0, width=5)
        for a, b in (((X0, Y0), (X0, y1)), ((x1, Y0), (x1, y1))):
            draw.line([a, b], fill=0, width=5)
    for x, y, d in HOLES:
        cx, cy, r = X0 + x * PX, Y0 - y * PX, d / 2 * PX
        draw.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], outline=0, width=4)
        draw.line([(cx, cy), (cx, 40)], fill=0, width=2)  # выносная координаты
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _spec(holes) -> dict:
    return {
        "main_view": {
            "profile": {
                "shape": "rectangle",
                "width_mm": 80.0,
                "height_mm": 50.0,
                # Спек хранит центры от середины пластины.
                "holes": [
                    {"center_x_mm": x - 40.0, "center_y_mm": y - 25.0, "diameter_mm": d}
                    for x, y, d in holes
                ],
            }
        }
    }


def test_read_holes_are_confirmed_and_a_swapped_y_is_refuted_with_a_note():
    read = list(HOLES)
    read[2] = (52.0, 26.0, 6.6)  # plate-1: y переставлен с соседним

    report = verify_spec_against_sheet(_png(), _spec(read))

    statuses = [item["status"] for item in report["items"]]
    assert statuses == ["confirmed", "confirmed", "refuted", "confirmed"], report
    refuted = report["items"][2]
    # Прочитанное не заменено, измеренное — в координатах спека.
    assert refuted["read"]["center_y_mm"] == 1.0
    assert abs(refuted["measured"]["center_y_mm"] - 7.0) <= 0.3
    assert report["summary"]["refuted"] == 1
    assert len(report["notes"]) == 1 and "отверстие 3" in report["notes"][0]


def test_without_the_plan_on_the_sheet_every_hole_is_unmeasurable_with_a_reason():
    report = verify_spec_against_sheet(_png(plan=False), _spec(HOLES))

    assert [item["status"] for item in report["items"]] == ["unmeasurable"] * 4
    assert report["summary"]["reason"] == "план пластины на листе не найден"
    assert report["notes"] == []


def test_verdicts_reach_the_graph_per_field_of_each_hole():
    """Путь продукта: id признаков → граф сборки → вердикт по x, y и Ø отдельно."""
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_recognize.spec_vectorize import assign_stable_feature_ids
    from app.ai.cad_recognize.verifiers.stage import apply_verification
    from app.ai.cad_solid import feature_tree_from_spec

    read = list(HOLES)
    read[2] = (52.0, 26.0, 6.6)  # y переставлен, Ø верен
    spec = _spec(read)
    spec["main_view"]["profile"]["thickness_mm"] = 10.0
    assign_stable_feature_ids(spec)
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    graph = spec_feature_tree_as_graph(spec, candidate, graph_id="image-generation:verify")

    report = verify_spec_against_sheet(_png(), spec)
    graph, written = apply_verification(graph, report, pass_id="verify-test")

    assert written == 12  # 4 отверстия × (x, y, Ø)
    active = {
        item.supersedes_assertion_id: item
        for item in graph.assertions
        if item.state == "active" and item.supersedes_assertion_id
    }
    prefix = "assertion:feature:0:profile.holes:2:param:"
    assert active[f"{prefix}center_y_mm"].assurance == "contradicted"
    assert active[f"{prefix}center_x_mm"].assurance == "corroborated"
    assert active[f"{prefix}diameter_mm"].assurance == "corroborated"
    (measured,) = [
        item for item in graph.assertions if item.id.startswith(f"{prefix}center_y_mm@measured")
    ]
    assert abs(measured.value.value - 7.0) <= 0.3


def test_no_checkable_hypothesis_leaves_the_stage_without_a_verdict():
    """Контракт стадии (план, P1.3): каждое проверяемое — ровно один вердикт.

    Молчаливых потерь нет: и найденная система координат, и не найденная дают
    по элементу на каждое отверстие пластины, окружность болтов и центральное
    отверстие — со статусом из трёх возможных.
    """
    flange = _flange_spec()
    flange["main_view"]["profile"]["holes"] = [
        {"center_x_mm": 0.0, "center_y_mm": 0.0, "diameter_mm": 25.0}
    ]
    blank = io.BytesIO()
    Image.new("L", (800, 600), 255).save(blank, format="PNG")
    cases = [
        (_png(), _spec(HOLES), 4),
        (_png(plan=False), _spec(HOLES), 4),
        (_flange_png(), flange, 2),
        (blank.getvalue(), flange, 2),
    ]
    for image, spec, expected in cases:
        report = verify_spec_against_sheet(image, spec)
        assert len(report["items"]) == expected, report["summary"]
        assert {item["status"] for item in report["items"]} <= {
            "confirmed",
            "refuted",
            "unmeasurable",
        }
        assert report["summary"]["checked"] == expected
        assert sum(report["summary"][s] for s in ("confirmed", "refuted", "unmeasurable")) == (
            expected
        )


def _shaft_png(*, keyway: bool = False) -> bytes:
    """Вал Ø30×30 → Ø20×40 → Ø25×30 при 5 px/мм на листе размера настоящего.

    ``keyway`` — паз 40…60 × 6 на средней ступени, лицом (капсула).
    """
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)
    x, axis, previous = 200.0, 500.0, 0.0
    for diameter, length in ((30.0, 30.0), (20.0, 40.0), (25.0, 30.0)):
        r = diameter / 2 * 5.0
        x_end = x + length * 5.0
        draw.line([(x, axis - r), (x_end, axis - r)], fill=0, width=6)
        draw.line([(x, axis + r), (x_end, axis + r)], fill=0, width=6)
        if previous == 0.0:
            draw.line([(x, axis - r), (x, axis + r)], fill=0, width=6)
        else:
            low, high = min(previous, r), max(previous, r)
            draw.line([(x, axis - high), (x, axis - low)], fill=0, width=6)
            draw.line([(x, axis + low), (x, axis + high)], fill=0, width=6)
        previous, x = r, x_end
    draw.line([(x, axis - previous), (x, axis + previous)], fill=0, width=6)
    if keyway:
        h, left, right = 15.0, 200.0 + 43.0 * 5.0, 200.0 + 57.0 * 5.0
        draw.line([(left, axis - h), (right, axis - h)], fill=0, width=6)
        draw.line([(left, axis + h), (right, axis + h)], fill=0, width=6)
        r = h + 3.0
        draw.arc([left - r, axis - r, left + r, axis + r], 90, 270, fill=0, width=6)
        draw.arc([right - r, axis - r, right + r, axis + r], -90, 90, fill=0, width=6)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_shaft_is_checked_step_by_step_and_only_the_wrong_diameter_is_refuted():
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_recognize.spec_vectorize import assign_stable_feature_ids
    from app.ai.cad_recognize.verifiers.stage import apply_verification
    from app.ai.cad_solid import feature_tree_from_spec

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 22.0, "length_mm": 40.0},  # на листе Ø20
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ]
        }
    }
    assign_stable_feature_ids(spec)
    report = verify_spec_against_sheet(_shaft_png(), spec)

    assert [item["kind"] for item in report["items"]] == ["shaft_step"] * 3
    assert [item["status"] for item in report["items"]] == [
        "confirmed",
        "refuted",
        "confirmed",
    ], report
    assert abs(report["items"][1]["measured"]["diameter_mm"] - 20.0) <= 0.3
    assert "ступень 2" in report["notes"][0]

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    graph = spec_feature_tree_as_graph(spec, candidate, graph_id="image-generation:shaft")
    graph, written = apply_verification(graph, report, pass_id="verify-shaft")

    assert written == 6  # 3 ступени × (Ø, длина)
    active = {
        item.supersedes_assertion_id: item.assurance
        for item in graph.assertions
        if item.state == "active" and item.supersedes_assertion_id
    }
    assert active["assertion:feature:0:outer:1:param:diameter_mm"] == "contradicted"
    assert active["assertion:feature:0:outer:1:param:length_mm"] == "corroborated"
    assert active["assertion:feature:0:outer:0:param:diameter_mm"] == "corroborated"


def test_a_reading_the_sheet_does_not_confirm_gets_a_profile_assembled_from_the_sheet():
    # Ридер ошибся почти во всём (как на живом z4-r4), но надписи выписал.
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 28.0, "length_mm": 45.0},
                {"diameter_mm": 22.0, "length_mm": 25.0},
                {"diameter_mm": 25.0, "length_mm": 38.0},
            ]
        },
        "dimensions": [{"value": v} for v in ("30", "40", "30", "100", "Ø30", "Ø20", "Ø25")],
    }
    report = verify_spec_against_sheet(_shaft_png(), spec)

    assert report["profile_proposal"]["steps"] == [
        {"diameter_mm": 30.0, "length_mm": 30.0},
        {"diameter_mm": 20.0, "length_mm": 40.0},
        {"diameter_mm": 25.0, "length_mm": 30.0},
    ]


def test_a_reading_matching_the_sheet_profile_is_confirmed_without_a_proposal():
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ]
        },
        "dimensions": [{"value": v} for v in ("30", "40", "30", "100", "Ø30", "Ø20", "Ø25")],
    }
    report = verify_spec_against_sheet(_shaft_png(), spec)

    assert [item["status"] for item in report["items"]] == ["confirmed"] * 3
    assert "profile_proposal" not in report


def test_a_keyway_is_checked_and_only_its_wrong_length_is_contradicted_in_the_graph():
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_recognize.spec_vectorize import assign_stable_feature_ids
    from app.ai.cad_recognize.verifiers.stage import apply_verification
    from app.ai.cad_solid import feature_tree_from_spec

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            # На листе паз 40…60: длина прочитана неверно.
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 23.0, "width_mm": 6.0, "depth_mm": 3.5}
            ],
        }
    }
    assign_stable_feature_ids(spec)
    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    keyways = [item for item in report["items"] if item["kind"] == "keyway"]
    assert len(keyways) == 1
    item = keyways[0]
    assert item["status"] == "refuted", item
    assert abs(item["measured"]["length_mm"] - 20.0) <= 0.5
    assert any(note.startswith("паз 1") for note in report["notes"])

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    graph = spec_feature_tree_as_graph(spec, candidate, graph_id="image-generation:keyway")
    graph, _written = apply_verification(graph, report, pass_id="verify-keyway")
    active = {
        assertion.supersedes_assertion_id: assertion.assurance
        for assertion in graph.assertions
        if assertion.state == "active" and assertion.supersedes_assertion_id
    }
    prefix = f"assertion:feature:{item['feature_id']}:param:"
    assert active[prefix + "length_mm"] == "contradicted"
    assert active[prefix + "width_mm"] == "corroborated"
    assert active[prefix + "axial_start_mm"] == "corroborated"


def test_a_keyway_read_where_there_is_none_does_not_hide_the_step_under_it():
    """Живой shaft-1: паз прочитан на ступени без паза — её неверный Ø не проверялся."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 26.0, "length_mm": 40.0},  # на листе Ø20, паза нет
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0, "depth_mm": 3.5}
            ],
        }
    }
    report = verify_spec_against_sheet(_shaft_png(), spec)

    by_kind = {item["kind"]: item for item in report["items"] if item["kind"] == "keyway"}
    assert by_kind["keyway"]["status"] == "unmeasurable"
    steps = [item for item in report["items"] if item["kind"] == "shaft_step"]
    assert steps[1]["status"] == "refuted", steps[1]
    assert abs(steps[1]["measured"]["diameter_mm"] - 20.0) <= 0.3


def test_a_spec_without_checkable_elements_has_nothing_to_check():
    report = verify_spec_against_sheet(_png(), {"main_view": {"profile": {"shape": "circle"}}})

    assert report["items"] == []
    assert report["summary"]["checked"] == 0


def _flange_png() -> bytes:
    """Фланец Ø100 при 6 px/мм, 6 отв. Ø6,6 на Ø60 с фазой 15°."""
    import math

    image = Image.new("L", (1300, 1000), 255)
    draw = ImageDraw.Draw(image)
    cx, cy = 600.0, 500.0

    def ring(x, y, r, width):
        half = width / 2
        draw.ellipse(
            [x - r - half, y - r - half, x + r + half, y + r + half], outline=0, width=width
        )

    ring(cx, cy, 300, 4)
    ring(cx, cy, 180, 2)
    for index in range(6):
        angle = math.radians(15.0 + 60.0 * index)
        ring(cx + 180 * math.cos(angle), cy - 180 * math.sin(angle), 19.8, 4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _flange_spec() -> dict:
    return {
        "main_view": {
            "profile": {
                "shape": "circle",
                "diameter_mm": 100.0,
                "thickness_mm": 12.0,
                # Ридер: углового размера на листе нет — фаза 0°.
                "hole_patterns": [
                    {
                        "kind": "bolt_circle",
                        "count": 6,
                        "bolt_circle_diameter_mm": 60.0,
                        "hole_diameter_mm": 6.6,
                        "start_angle_deg": 0.0,
                    }
                ],
            }
        }
    }


def test_a_flange_bolt_circle_is_checked_and_only_the_phase_is_refuted_in_the_graph():
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_recognize.spec_vectorize import assign_stable_feature_ids
    from app.ai.cad_recognize.verifiers.stage import apply_verification
    from app.ai.cad_solid import feature_tree_from_spec

    spec = _flange_spec()
    assign_stable_feature_ids(spec)
    report = verify_spec_against_sheet(_flange_png(), spec)

    (item,) = report["items"]
    assert item["kind"] == "bolt_circle" and item["status"] == "refuted", item
    assert abs(item["measured"]["start_angle_deg"] - 15.0) <= 1.0
    assert "фаза 15°" in report["notes"][0]

    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    graph = spec_feature_tree_as_graph(spec, candidate, graph_id="image-generation:flange")
    graph, written = apply_verification(graph, report, pass_id="verify-flange")

    assert written == 4  # число, PCD, Ø, фаза
    active = {
        item.supersedes_assertion_id: item.assurance
        for item in graph.assertions
        if item.state == "active" and item.supersedes_assertion_id
    }
    prefix = "assertion:feature:0:profile.hole_patterns:0:param:"
    assert active[f"{prefix}start_angle_deg"] == "contradicted"
    assert active[f"{prefix}count"] == "corroborated"
    assert active[f"{prefix}bolt_circle_diameter_mm"] == "corroborated"
    assert active[f"{prefix}hole_diameter_mm"] == "corroborated"
    # Масштаб вида — утверждением со свидетельством: в пути «по описанию» его не было.
    from app.domain.emg_predicates import PREDICATE

    (scale,) = [
        item
        for item in graph.assertions
        if item.predicate == PREDICATE.SCALE_MM_PER_PX and item.state == "active"
    ]
    assert abs(scale.value.value - 1 / 6.0) <= 0.002
    assert scale.origin == "traced" and scale.evidence_ids
