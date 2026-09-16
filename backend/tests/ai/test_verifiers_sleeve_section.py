"""Втулка по листу: разрез (профиль, расточка, грани фланца) + вид с торца + надписи."""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.sleeve_section import (
    locate_sleeve_section,
    propose_sleeve,
)

PX = 30.0
AXIS = 700.0
X0 = 250.0
LINE = 7
THIN = 3
END = (1950.0, 700.0)

OUTER = [(15.0, 0.0, 12.0), (16.0, 12.0, 18.0), (15.0, 18.0, 24.0)]
BORE = [(13.0, 0.0, 4.0), (11.0, 4.0, 20.0), (13.0, 20.0, 24.0)]


def _x(mm: float) -> float:
    return X0 + mm * PX


def _y(mm: float) -> float:
    return AXIS - mm * PX


def _sheet(*, bore: bool = True) -> np.ndarray:
    image = Image.new("L", (2600, 1500), 255)
    draw = ImageDraw.Draw(image)
    # Разрез: наружные линии (под фланцем 16…18 их нет), расточка.
    for d, a, b in OUTER:
        for sign in (1, -1):
            segments = [(a, min(b, 16.0)), (max(a, 18.0), b)] if a < 18 < b or a == 12 else [(a, b)]
            for s, e in segments:
                if e > s:
                    draw.line(
                        [(_x(s), _y(sign * d / 2)), (_x(e), _y(sign * d / 2))], fill=0, width=LINE
                    )
    if bore:
        for d, a, b in BORE:
            for sign in (1, -1):
                draw.line(
                    [(_x(a), _y(sign * d / 2)), (_x(b), _y(sign * d / 2))], fill=0, width=LINE
                )
        # Выносная размера расточки продолжает её линию за торец — тонкой.
        draw.line([(_x(-8), _y(6.5)), (_x(0), _y(6.5))], fill=0, width=THIN)
    inner = {0.0: 6.5, 4.0: 6.5, 12.0: 5.5, 18.0: 5.5, 20.0: 6.5, 24.0: 6.5} if bore else {}
    # Торцы и уступы — стенка между наружной линией и расточкой.
    for station, (r_out_a, r_out_b) in {
        0.0: (7.5, 7.5),
        12.0: (7.5, 8.0),
        18.0: (8.0, 7.5),
        24.0: (7.5, 7.5),
    }.items():
        r_in = inner.get(station, 0.0)
        for sign in (1, -1):
            draw.line(
                [(_x(station), _y(sign * r_in)), (_x(station), _y(sign * max(r_out_a, r_out_b)))],
                fill=0,
                width=LINE,
            )
    if bore:
        for station, (a, b) in {4.0: (6.5, 5.5), 20.0: (5.5, 6.5)}.items():
            for sign in (1, -1):
                draw.line(
                    [(_x(station), _y(sign * a)), (_x(station), _y(sign * b))], fill=0, width=LINE
                )
    # Фланец 16…18: сверху лыска 9,5, снизу круг 14,5.
    for station in (16.0, 18.0):
        draw.line([(_x(station), _y(9.5)), (_x(station), _y(-14.5))], fill=0, width=LINE)
    for level in (9.5, -14.5):
        draw.line([(_x(16.0), _y(level)), (_x(18.0), _y(level))], fill=0, width=LINE)
    draw.line([(_x(-2), AXIS), (_x(26), AXIS)], fill=0, width=2)
    # Вид с торца: круг Ø29 с тремя лысками, окружности тела и расточки, отверстия.
    points = []
    for k in range(2400):
        t = 2 * math.pi * k / 2400
        reach = 14.5
        for j in range(3):
            c = math.cos(t - math.radians(90 + 120 * j))
            if c > 1e-9:
                reach = min(reach, 9.5 / c)
        points.append((END[0] + reach * PX * math.cos(t), END[1] - reach * PX * math.sin(t)))
    draw.line(points + points[:1], fill=0, width=LINE, joint="curve")
    for d in (16.0, 15.0, 13.0, 11.0) if bore else (16.0, 15.0):
        r = d / 2 * PX
        draw.ellipse([END[0] - r, END[1] - r, END[0] + r, END[1] + r], outline=0, width=LINE)
    for k in range(3):
        a = math.radians(30 + 120 * k)
        x, y = END[0] + 11 * PX * math.cos(a), END[1] - 11 * PX * math.sin(a)
        r = 1.25 * PX
        draw.ellipse([x - r, y - r, x + r, y + r], outline=0, width=LINE)
    return np.asarray(image)


SPEC = {
    "dimensions": [
        {"value": v}
        for v in (
            "4",
            "6",
            "Ø16js7",
            "Ø15",
            "Ø13H7",
            "Ø11",
            "4",
            "2",
            "6",
            "24",
            "Ø22±0,1",
            "Ø29",
            "24",
            "Ø2,5",
            "3 отв.",
            "0,01",
        )
    ]
}


def test_a_sleeve_with_a_flange_is_assembled_from_its_section_end_view_and_labels():
    proposal, why = propose_sleeve(_sheet(), SPEC)

    assert proposal is not None, why
    assert [(s["diameter_mm"], s["length_mm"]) for s in proposal.outer] == [
        (15.0, 12.0),
        (16.0, 6.0),
        (15.0, 6.0),
    ]
    assert [(s["diameter_mm"], s["length_mm"]) for s in proposal.bore] == [
        (13.0, 4.0),
        (11.0, 16.0),
        (13.0, 4.0),
    ]
    flange = proposal.flange
    assert flange is not None, proposal.notes
    assert (flange["axial_start_mm"], flange["thickness_mm"]) == (16.0, 2.0)
    outline = flange["outline"]
    assert (outline["diameter_mm"], outline["flats"], outline["flat_distance_mm"]) == (29.0, 3, 9.5)
    (pattern,) = flange["profile"]["hole_patterns"]
    assert (pattern["count"], pattern["hole_diameter_mm"], pattern["bolt_circle_diameter_mm"]) == (
        3,
        2.5,
        22.0,
    )
    assert abs(pattern["start_angle_deg"] - 30.0) <= 1.0


def test_a_part_without_a_bore_at_its_ends_is_not_a_sleeve():
    assert locate_sleeve_section(_sheet(bore=False), (15.0, 16.0, 29.0)) is None


def _read_and_report(statuses):
    proposal, why = propose_sleeve(_sheet(), SPEC)
    assert proposal is not None, why
    spec = {
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": 16.0, "length_mm": 4.0},
                {"diameter_mm": 15.0, "length_mm": 2.0},
                {"diameter_mm": 29.0, "length_mm": 6.0},
            ],
        },
        "unresolved": [
            "расточка: расточка длиной 24 мм длиннее детали (14 мм)",
            "малые элементы: поперечное отверстие Ø22 указано, но не локализовано",
            "PMI: 4 рамок с неразличимым знаком или значением",
        ],
        **SPEC,
    }
    report = {
        "items": [
            {"kind": "shaft_step", "path": f"main_view.outer[{i}]", "status": status}
            for i, status in enumerate(statuses)
        ],
        "sleeve_proposal": proposal.as_payload(),
    }
    return spec, report


def test_a_sleeve_from_the_sheet_replaces_a_wrong_reading_and_builds():
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
    from app.ai.cad_recognize.verifiers.reconcile import apply_sleeve, sleeve_decision
    from app.ai.cad_solid import feature_tree_from_spec

    spec, report = _read_and_report(["unmeasurable"] * 3)

    decision = sleeve_decision(spec, report)

    assert decision is not None and "3 лысками" in decision["reason"]
    fixed = apply_sleeve(spec, decision)
    assert fixed["unresolved"] == ["PMI: 4 рамок с неразличимым знаком или значением"]
    validated = EngineeringDrawingSpec.model_validate(fixed).model_dump(mode="json")
    candidate = feature_tree_from_spec(validated)
    assert candidate is not None
    kinds = [feature.kind for feature in candidate.features]
    assert kinds.count("revolve") == 1 and kinds.count("boss") == 1 and kinds.count("pocket") == 3


def test_a_sleeve_whose_steps_the_sheet_confirmed_keeps_its_reading():
    from app.ai.cad_recognize.verifiers.reconcile import sleeve_decision

    spec, report = _read_and_report(["confirmed"] * 3)

    assert sleeve_decision(spec, report) is None
