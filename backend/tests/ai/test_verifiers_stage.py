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

    steps = report["profile_proposal"]["steps"]
    assert all(len(step["bbox_px"]) == 4 for step in steps)
    assert [{k: step[k] for k in ("diameter_mm", "length_mm")} for step in steps] == [
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
    assert scale.origin == "observed" and scale.evidence_ids
    from app.services.engineering_model_graph import verify_graph

    _state, issues = verify_graph(graph)
    assert not [i for i in issues if i["code"] == "trace_verification_incomplete"]


def test_a_keyway_read_with_width_and_depth_swapped_is_still_found():
    """Живой z4-r4: паз 4 × 8 вместо 8 × 4 — по прочитанной ширине не находился."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            # На листе паз 40…60 × 6 (ГОСТ 23360 для Ø20: 6 × 3,5).
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 3.0, "depth_mm": 6.0}
            ],
        }
    }
    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    item = next(item for item in report["items"] if item["kind"] == "keyway")
    assert item["status"] == "refuted", item
    assert abs(item["measured"]["width_mm"] - 6.0) <= 0.3
    assert "переставлены" in item["reason"]


def test_a_keyway_found_on_the_sheet_gets_evidence_of_where_it_was_found():
    """Живой z4-r4: гейт держал найденный проверкой паз «без evidence»."""
    from app.ai.cad_recognize.verifiers.stage import attach_sheet_evidence

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0, "depth_mm": 3.5}
            ],
            "chamfers": [
                {"size_mm": 1.0, "location": "left_end", "evidence": [{"bbox": [1, 2, 3, 4]}]}
            ],
        }
    }
    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    item = next(item for item in report["items"] if item["kind"] == "keyway")
    assert item["status"] == "confirmed" and len(item["evidence_bbox_px"]) == 4
    fixed = attach_sheet_evidence(spec, report)
    evidence = fixed["main_view"]["keyways"][0]["evidence"]
    assert evidence[0]["bbox"] == item["evidence_bbox_px"]
    # Прочитанное свидетельство не заменяется; исходный спек не тронут.
    assert fixed["main_view"]["chamfers"][0]["evidence"] == [{"bbox": [1, 2, 3, 4]}]
    assert "evidence" not in spec["main_view"]["keyways"][0]


def test_the_step_under_a_keyway_found_on_the_sheet_is_not_measured_for_its_diameter():
    """Пролёты пазов не доходили до проверки профиля (с 551e257f): Ø ступени
    под пазом мерился по контуру паза, хотя должен был не мериться вовсе."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 22.0, "length_mm": 40.0},  # на листе Ø20, под пазом
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0, "depth_mm": 3.5}
            ],
        }
    }
    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    step = [item for item in report["items"] if item["kind"] == "shaft_step"][1]
    assert "diameter_mm" not in step["measured"], step
    assert step["status"] != "refuted", step


def test_a_keyway_the_reader_missed_is_proposed_from_the_sheet():
    """Живой z4-r4: второй паз ридер не выписал — проверка находит его сама."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ]
        }
    }
    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    proposals = report["keyway_proposals"]
    assert len(proposals) == 1
    found = proposals[0]
    assert found["step_index"] == 1
    assert abs(found["axial_start_mm"] - 40.0) <= 0.6 and abs(found["length_mm"] - 20.0) <= 0.6
    assert found["standard_mm"] == [6.0, 3.5]  # ГОСТ 23360 для Ø20


def test_no_keyway_is_proposed_where_the_sheet_has_none():
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ]
        }
    }
    report = verify_spec_against_sheet(_shaft_png(), spec)

    assert "keyway_proposals" not in report


def _sleeve_like_spec(views):
    return {
        "main_view": {
            "outer": [
                {"id": "0:outer:0", "diameter_mm": 15.0, "length_mm": 12.0},
                {"id": "0:outer:1", "diameter_mm": 16.0, "length_mm": 6.0},
            ],
            "bore": [{"id": "0:bore:0", "diameter_mm": 11.0, "length_mm": 18.0}],
            "flanges": [{"id": "0:flanges:0", "axial_start_mm": 4.0, "thickness_mm": 2.0}],
        },
        "views": views,
    }


def test_features_confirmed_on_the_sheet_are_shown_on_their_view():
    """Все механические сборки — `mechanical_feature_without_view`: рёбра
    represented_by строятся из features_shown вида, а его никто не заполнял."""
    from app.ai.cad_emg_compat import native_feature_graph_additions
    from app.ai.cad_recognize.verifiers.stage import attach_verified_views

    report = {
        "items": [
            {"kind": "shaft_step", "feature_id": "0:outer:0", "status": "confirmed"},
            {"kind": "shaft_step", "feature_id": "0:outer:1", "status": "confirmed"},
        ],
        "sleeve_confirmed": True,
        "frame": {"bbox_px": [10.0, 20.0, 300.0, 200.0]},
    }
    section = {"kind": "section", "view_id": "A-A", "label": "A-A", "body_index": 0}

    spec = attach_verified_views(_sleeve_like_spec([section]), report)

    (view,) = spec["views"]
    assert view["features_shown"] == ["0:outer:0", "0:outer:1", "0:bore:0", "0:flanges:0"]
    _nodes, edges, _assertions = native_feature_graph_additions(spec)
    linked = {edge.source_id for edge in edges if edge.type == "represented_by"}
    assert linked == {
        "feature:0:outer:0",
        "feature:0:outer:1",
        "feature:0:bore:0",
        "feature:0:flanges:0",
    }

    # Ридер видов не выписал — вид с рамкой проверки.
    created = attach_verified_views(_sleeve_like_spec([]), report)
    (view,) = created["views"]
    assert view["evidence"][0]["bbox"] == [10.0, 20.0, 300.0, 200.0]
    assert len(view["features_shown"]) == 4

    # Фланец измерен и на виде с торца — тот же объект на двух видах (уровень 6).
    from app.services.engineering_model_graph import verify_graph  # noqa: F401

    with_end = attach_verified_views(
        _sleeve_like_spec([section]),
        {**report, "sleeve_end_view": {"bbox_px": [400.0, 20.0, 700.0, 320.0]}},
    )
    assert [v["view_id"] for v in with_end["views"]] == ["A-A", "sheet-verified-end"]
    _nodes, edges, _assertions = native_feature_graph_additions(with_end)
    same = [edge for edge in edges if edge.type == "same_object_across_views"]
    assert [(e.id, e.source_id, e.target_id) for e in same] == [
        (
            "same:feature:0:flanges:0:A-A:sheet-verified-end",
            "view:A-A",
            "view:sheet-verified-end",
        )
    ]
    # Граф целиком проходит валидацию (живая втулка fac0e881: ребро с
    # незарегистрированным extension роняло весь прогон).
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_ir.feature_tree import FeatureTreeCandidate

    graph = spec_feature_tree_as_graph(
        with_end, FeatureTreeCandidate(features=[], score=0.5, label="t"), graph_id="g"
    )
    _state, issues = verify_graph(graph)
    assert "cross_view_not_available" not in [issue["code"] for issue in issues]

    # Несколько сечений без главного вида (живой z4-r4: Б-Б, А-А) — не на
    # сечение, а на вид с рамкой проверки; фаска «не измерима», но найдена;
    # паз, найденный по листу, — тоже на виде.
    spec_z4 = _sleeve_like_spec(
        [
            {"kind": "section", "view_id": "B-B", "body_index": 0},
            {"kind": "section", "view_id": "A-A", "body_index": 0},
        ]
    )
    spec_z4["main_view"]["chamfers"] = [{"id": "0:chamfers:0"}]
    spec_z4["main_view"]["keyways"] = [{"id": "sheet:keyways:1"}]
    report_z4 = {
        "items": [
            {"kind": "shaft_step", "feature_id": "0:outer:0", "status": "confirmed"},
            {
                "kind": "chamfer",
                "feature_id": "0:chamfers:0",
                "status": "unmeasurable",
                "measured": {"size_mm": 1.1},
            },
            {"kind": "chamfer", "feature_id": "0:chamfers:9", "status": "unmeasurable"},
        ],
        "frame": {"bbox_px": [1.0, 2.0, 3.0, 4.0]},
    }
    placed = attach_verified_views(spec_z4, report_z4)
    by_id = {view["view_id"]: view for view in placed["views"]}
    assert not by_id["B-B"].get("features_shown")
    assert by_id["sheet-verified"]["features_shown"] == [
        "0:outer:0",
        "0:chamfers:0",
        "sheet:keyways:1",
    ]

    # Не подтверждённое — без вида.
    doubtful = {"items": [{"kind": "shaft_step", "feature_id": "0:outer:0", "status": "refuted"}]}
    assert attach_verified_views(_sleeve_like_spec([section]), doubtful)["views"] == [section]


def test_a_step_whose_diameter_is_hidden_by_a_keyway_is_not_confirmed_by_its_length():
    """Корпус v9: Ø +1,5 на ступени под пазом «подтверждался» — Ø под пазом по
    виду не мерится, а совпала одна длина. Такая ступень — «не измерима»."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 21.5, "length_mm": 40.0},  # на листе Ø20
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            "keyways": [
                {"axial_start_mm": 40.0, "length_mm": 20.0, "width_mm": 6.0, "depth_mm": 3.5}
            ],
        }
    }

    report = verify_spec_against_sheet(_shaft_png(keyway=True), spec)

    steps = [item for item in report["items"] if item["kind"] == "shaft_step"]
    assert steps[1]["status"] == "unmeasurable", steps[1]
    assert "паз" in steps[1]["reason"]
    assert steps[0]["status"] == "confirmed" and steps[2]["status"] == "confirmed"


def _housing_png(thickness_px: int = 120, gap: int = 60) -> bytes:
    """Лист корпуса: план 300×200 px, под ним вид спереди, справа вид слева.

    Линии рисуются поштучно: у `rectangle` с толстой обводкой середина линии
    уходит внутрь, и «толщина» оказывалась на 0,7 мм меньше нарисованной —
    это артефакт теста, а не замера (на листах корпуса ошибка 0,08 мм).
    """
    import io

    from PIL import Image, ImageDraw

    image = Image.new("L", (900, 700), 255)
    draw = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1, width=3):
        for a, b in (((x0, y0), (x1, y0)), ((x0, y1), (x1, y1))):
            draw.line([a, b], fill=0, width=width)
        for a, b in (((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))):
            draw.line([a, b], fill=0, width=width)

    box(100, 100, 400, 300)
    # Вид спереди под планом: ширина та же, высота — толщина; прилив слева
    # делает его ШИРЕ плана, как на настоящем листе.
    top = 360
    box(100, top, 400, top + thickness_px)
    box(60, top + 30, 100, top + 70)
    # Вид слева справа от плана: высота та же, ширина — толщина.
    left = 460
    box(left, 100, left + thickness_px, 300)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_thickness_of_a_housing_is_measured_by_its_views_not_read():
    """Базовая линия на корпусах: толщина прочитана неверно на КАЖДОМ листе —
    на листе три вида, и число приписывается не тому."""
    # План 300 × 200 px при 150 × 100 мм — 0,5 мм/px; толщина 120 px = 60 мм.
    profile = {"shape": "rectangle", "width_mm": 150.0, "height_mm": 100.0, "thickness_mm": 60.0}
    png = _housing_png()

    report = verify_spec_against_sheet(png, {"main_view": {"profile": profile}})

    item = next(entry for entry in report["items"] if entry["kind"] == "plate_thickness")
    assert item["status"] == "confirmed"
    assert abs(item["measured"]["thickness_mm"] - 60.0) <= 0.5
    assert report["housing_views"]["front_bbox_px"] and report["housing_views"]["side_bbox_px"]

    wrong = verify_spec_against_sheet(
        png, {"main_view": {"profile": {**profile, "thickness_mm": 20.0}}}
    )
    refuted = next(entry for entry in wrong["items"] if entry["kind"] == "plate_thickness")
    assert refuted["status"] == "refuted"
    assert abs(refuted["measured"]["thickness_mm"] - 60.0) <= 0.5
    assert "прочитано 20" in refuted["reason"]


def test_views_that_disagree_about_the_thickness_measure_nothing():
    """Вид спереди и вид слева разной толщины — это не деталь, а совпадение."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("L", (900, 700), 255)
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in ((100, 100, 400, 300), (100, 360, 400, 480), (460, 100, 530, 300)):
        for a, b in (((x0, y0), (x1, y0)), ((x0, y1), (x1, y1))):
            draw.line([a, b], fill=0, width=3)
        for a, b in (((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))):
            draw.line([a, b], fill=0, width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    report = verify_spec_against_sheet(
        buffer.getvalue(),
        {
            "main_view": {
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 150.0,
                    "height_mm": 100.0,
                    "thickness_mm": 60.0,
                }
            }
        },
    )

    item = next(entry for entry in report["items"] if entry["kind"] == "plate_thickness")
    assert item["status"] == "unmeasurable"
    assert "не согласны" in item["reason"]


def _housing_with_features_png() -> bytes:
    """Лист корпуса: полость на плане и прилив Ø на виде спереди."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("L", (900, 700), 255)
    draw = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        for a, b in (((x0, y0), (x1, y0)), ((x0, y1), (x1, y1))):
            draw.line([a, b], fill=0, width=3)
        for a, b in (((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))):
            draw.line([a, b], fill=0, width=3)

    box(100, 100, 400, 300)  # план 300 × 200 px = 150 × 100 мм
    box(160, 140, 340, 260)  # полость 180 × 120 px = 90 × 60 мм в центре
    box(100, 360, 400, 480)  # вид спереди: толщина 120 px = 60 мм
    box(460, 100, 580, 300)  # вид слева
    # Прилив Ø40 мм = 80 px; середина вида спереди — x=250, центр на 25 мм
    # правее неё (25 мм = 50 px при 0,5 мм/px).
    draw.ellipse((300 - 40, 420 - 40, 300 + 40, 420 + 40), outline=0, width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


_HOUSING_PROFILE = {
    "shape": "rectangle",
    "width_mm": 150.0,
    "height_mm": 100.0,
    "thickness_mm": 60.0,
}


def test_wall_features_are_measured_on_their_own_view():
    """Базовая линия на корпусах: размер модель берёт верно, а положение
    приписывает не тому виду (полость: центр (−42, −42) вместо (0, 0))."""
    spec = {
        "main_view": {
            "profile": {
                **_HOUSING_PROFILE,
                "wall_features": [
                    {
                        "kind": "pocket",
                        "on_plane": "top",
                        "profile": "rectangle",
                        "width_mm": 90.0,
                        "height_mm": 60.0,
                        "depth_mm": 40.0,
                        "center_u_mm": 0.0,
                        "center_v_mm": 0.0,
                    },
                    {
                        "kind": "boss",
                        "on_plane": "front",
                        "profile": "circle",
                        "diameter_mm": 40.0,
                        "depth_mm": 8.0,
                        "center_u_mm": 25.0,
                        "center_v_mm": 0.0,
                    },
                ],
            }
        }
    }

    report = verify_spec_against_sheet(_housing_with_features_png(), spec)

    items = [item for item in report["items"] if item["kind"] == "wall_feature"]
    assert [item["status"] for item in items] == ["confirmed", "confirmed"]
    # Ø нарисованного эллипса меряется по середине штриха — с запасом на него.
    assert abs(items[1]["measured"]["diameter_mm"] - 40.0) <= 2.0
    assert abs(items[1]["measured"]["center_u_mm"] - 25.0) <= 1.5


def test_a_wall_feature_read_in_the_wrong_place_is_refuted_with_the_measurement():
    spec = {
        "main_view": {
            "profile": {
                **_HOUSING_PROFILE,
                "wall_features": [
                    {
                        "kind": "boss",
                        "on_plane": "front",
                        "profile": "circle",
                        "diameter_mm": 40.0,
                        "depth_mm": 8.0,
                        # Ридер приписал прилив другому месту грани.
                        "center_u_mm": -20.0,
                        "center_v_mm": 0.0,
                    }
                ],
            }
        }
    }

    report = verify_spec_against_sheet(_housing_with_features_png(), spec)

    item = next(entry for entry in report["items"] if entry["kind"] == "wall_feature")
    assert item["status"] == "refuted"
    assert abs(item["measured"]["center_u_mm"] - 25.0) <= 1.5


def test_a_measurement_that_lands_on_another_feature_of_the_face_is_unmeasurable():
    """E28: расхождение, указывающее на ДРУГОЙ объект, не опровергает чтение."""
    from app.ai.cad_recognize.verifiers.wall_feature import wall_feature_verdict

    read = {"diameter_mm": 30.0, "center_u_mm": -10.0, "center_v_mm": 0.0}
    far = wall_feature_verdict(read, {"diameter_mm": 16.0, "center_u_mm": 20.0, "center_v_mm": 0.0})
    assert far["status"] == "unmeasurable"
    assert "другой элемент" in far["reason"]

    near = wall_feature_verdict(
        read, {"diameter_mm": 30.2, "center_u_mm": -9.8, "center_v_mm": 0.1}, line_mm=0.5
    )
    assert near["status"] == "confirmed"


def test_a_confirmed_wall_feature_is_shown_on_both_of_its_views():
    """Ф5: элемент грани виден на своём виде (размер и место) и на соседнем с
    ребра (глубина) — это межвидовое соответствие корпуса."""
    from app.ai.cad_emg_compat import native_feature_graph_additions
    from app.ai.cad_recognize.verifiers.stage import attach_verified_views

    spec = {
        "main_view": {
            "profile": {
                **_HOUSING_PROFILE,
                "wall_features": [
                    {
                        "id": "0:wall:0",
                        "kind": "boss",
                        "on_plane": "front",
                        "profile": "circle",
                        "diameter_mm": 40.0,
                        "depth_mm": 8.0,
                        "center_u_mm": 25.0,
                        "center_v_mm": 0.0,
                    }
                ],
            }
        },
        "views": [],
    }
    report = {
        "items": [
            {"kind": "wall_feature", "feature_id": "0:wall:0", "status": "confirmed"},
        ],
        "housing_views": {
            "plan_bbox_px": [100.0, 100.0, 400.0, 300.0],
            "front_bbox_px": [100.0, 360.0, 400.0, 480.0],
            "side_bbox_px": [460.0, 100.0, 580.0, 300.0],
            "thickness_mm": 60.0,
        },
        "frame": {"bbox_px": [100.0, 100.0, 400.0, 300.0]},
    }

    placed = attach_verified_views(spec, report)

    by_id = {view["view_id"]: view for view in placed["views"]}
    assert by_id["sheet-verified-front"]["features_shown"] == ["0:wall:0"]
    assert by_id["sheet-verified-plan"]["features_shown"] == ["0:wall:0"]
    _nodes, edges, _assertions = native_feature_graph_additions(placed)
    same = [edge for edge in edges if edge.type == "same_object_across_views"]
    assert len(same) == 1


def test_a_front_wall_feature_is_not_measured_on_the_section_below_the_plan():
    """Под планом корпуса с полостью — разрез: он снимает переднюю стенку, и
    замер находил зеркальный прилив задней, «опровергая» верное чтение
    (корпус seed 8: (50; 10) «найден» в (−56; −8))."""
    import io

    from PIL import Image

    from app.ai.cad_recognize.verifiers.stage import _wall_features_on_sheet

    buffer = io.BytesIO()
    Image.new("L", (400, 300), 255).save(buffer, format="PNG")
    profile = {
        "width_mm": 150.0,
        "height_mm": 80.0,
        "thickness_mm": 50.0,
        "wall_features": [
            {
                "kind": "pocket",
                "on_plane": "top",
                "profile": "rectangle",
                "width_mm": 120.0,
                "height_mm": 50.0,
                "depth_mm": 42.0,
                "center_u_mm": 0.0,
                "center_v_mm": 0.0,
            },
            {
                "kind": "boss",
                "on_plane": "front",
                "profile": "circle",
                "diameter_mm": 20.0,
                "depth_mm": 10.0,
                "center_u_mm": 50.0,
                "center_v_mm": 10.0,
            },
        ],
    }
    # Под планом — разрез: штриховка под 45°.
    from PIL import ImageDraw

    image = Image.new("L", (400, 300), 255)
    draw = ImageDraw.Draw(image)
    for x in range(20, 300, 12):
        draw.line([(x, 280), (x + 80, 200)], fill=0, width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    report = {
        "items": [],
        "frame": {"mm_per_px": 0.5},
        "housing_views": {
            "plan_bbox_px": [10, 10, 310, 170],
            "front_bbox_px": [10, 190, 310, 290],
            "side_bbox_px": [330, 10, 390, 170],
        },
    }
    _wall_features_on_sheet(buffer.getvalue(), profile, report)

    front = next(i for i in report["items"] if i["path"].endswith("[1]"))
    assert front["status"] == "unmeasurable" and "разрез" in front["reason"]


def test_a_side_view_edge_continued_by_witness_lines_is_still_an_edge():
    """Выносные размеров над видом слева продолжают его кромку по той же
    вертикали: линия выходила длиннее 1,6 высоты вида и отбрасывалась как рамка
    листа — вид слева не находился на 4 корпусах из 12."""
    from app.ai.cad_recognize.verifiers.housing_views import _levels_right
    from app.ai.cad_recognize.verifiers.plate_frame import _Line

    # План: y 100…300; вид слева: кромки x = 500 и 600.
    edge = _Line(position=500.0, start=-250.0, end=300.0)  # кромка + выносные сверху
    other = _Line(position=600.0, start=100.0, end=300.0)
    frame = _Line(position=900.0, start=0.0, end=2000.0)  # рамка листа
    columns = _levels_right([edge, other, frame], 100.0, 300.0, 400.0, 300.0, 200.0)

    assert 500.0 in columns and 600.0 in columns
