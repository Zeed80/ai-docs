"""Контур пластины по листу: Г-образная планка из сторон вдоль осей и сопряжений."""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.plate_contour import plate_labels, propose_contour

PX = 12.0  # px/мм
X0, Y0 = 400.0, 1800.0  # левый нижний угол детали на листе (y вниз)
LINE = 7


def _p(u: float, v: float) -> tuple[float, float]:
    return X0 + u * PX, Y0 - v * PX


def _arc(draw, centre, radius, start_deg, end_deg):
    cx, cy = _p(*centre)
    r = radius * PX
    # PIL: углы по часовой от оси x в экранных координатах (y вниз).
    draw.arc([cx - r, cy - r, cx + r, cy + r], -end_deg, -start_deg, fill=0, width=LINE)


def _planka() -> np.ndarray:
    """Планка part_04: полка 90 × 30 с концом R16, стойка 20 × 100 с R10, внутренний R25."""
    image = Image.new("L", (2600, 2600), 255)
    draw = ImageDraw.Draw(image)
    lines = [
        ((0, 0), (90, 0)),
        ((90, 0), (90, 100)),
        ((90, 100), (80, 100)),
        ((70, 90), (70, 55)),
        ((45, 30), (16, 30)),
        ((0, 14), (0, 0)),
    ]
    for a, b in lines:
        draw.line([_p(*a), _p(*b)], fill=0, width=LINE)
    _arc(draw, (80, 90), 10, 90, 180)  # R10: (80,100) → (70,90)
    _arc(draw, (45, 55), 25, 270, 360)  # R25: (70,55) → (45,30), вогнутый
    _arc(draw, (16, 14), 16, 90, 180)  # R16: (16,30) → (0,14)
    for u, v, d in ((16, 14, 16), (80, 14, 10), (80, 90, 10)):
        cx, cy = _p(u, v)
        r = d / 2 * PX
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=LINE)
        # Тонкая осевая, пересекающая кромку, — как на листе.
        draw.line([(cx, cy - r - 60), (cx, cy + r + 200)], fill=0, width=2)
    # Тонкие размерные линии ниже детали.
    for v in (-12, -22):
        draw.line([_p(0, v), _p(90, v)], fill=0, width=2)
    return np.asarray(image)


SPEC = {
    "dimensions": [
        {"value": v}
        for v in (
            "20",
            "76",
            "100",
            "30",
            "14",
            "64",
            "10",
            "90",
            "R10",
            "R25",
            "S3",
            "R16",
            "φ16",
            "φ10",
        )
    ]
}


def test_labels_of_a_plate_are_split_by_meaning():
    labels = plate_labels(
        {
            "dimensions": [
                {"value": v}
                for v in ("90", "R10", "S3", "φ16", "Скругление у левого отверстия: R16")
            ]
        }
    )

    assert labels["axial"] == [90.0]
    assert labels["radii"] == [10.0]  # пояснение ридера не в счёт
    assert labels["thickness"] == [3.0]
    assert labels["diameters"] == [16.0]


def test_an_l_shaped_plate_is_assembled_from_the_sheet_and_its_labels():
    proposal, why = propose_contour(_planka(), SPEC, read_holes=2)

    assert proposal is not None, why
    profile = proposal.profile
    assert profile["shape"] == "sketch"
    assert (profile["width_mm"], profile["height_mm"], profile["thickness_mm"]) == (
        90.0,
        100.0,
        3.0,
    )
    holes = sorted((h["diameter_mm"], h["center_x_mm"], h["center_y_mm"]) for h in profile["holes"])
    assert holes == [(10.0, 80.0, 14.0), (10.0, 80.0, 90.0), (16.0, 16.0, 14.0)]
    sketch = profile["sketch"]
    assert sketch[-1]["to"] == (0.0, 0.0)
    arcs = sorted(round(math.dist(s["to"], s["center"]), 3) for s in sketch if s["kind"] == "arc")
    assert arcs == [10.0, 16.0, 25.0]
    vertices = {s["to"] for s in sketch if s["kind"] == "line"}
    assert {(90.0, 0.0), (90.0, 100.0)} <= vertices


def test_a_plate_whose_sides_have_no_labels_gives_no_contour():
    spec = {"dimensions": [{"value": v} for v in ("90", "100", "R10", "R25", "R16", "φ16", "φ10")]}

    proposal, why = propose_contour(_planka(), spec)

    assert proposal is None
    assert "не объясняется" in why


def _read_and_report(statuses):
    spec = {
        "main_view": {
            "profile": {
                "shape": "rectangle",
                "width_mm": 90.0,
                "height_mm": 100.0,
                "holes": [
                    {"center_x_mm": -15.0, "center_y_mm": -20.0, "diameter_mm": 16.0},
                    {"center_x_mm": 19.0, "center_y_mm": -36.0, "diameter_mm": 10.0},
                ],
            }
        },
        **SPEC,
    }
    proposal, why = propose_contour(_planka(), SPEC, read_holes=2)
    assert proposal is not None, why
    report = {
        "items": [
            {"kind": "plate_hole", "path": f"main_view.profile.holes[{i}]", "status": status}
            for i, status in enumerate(statuses)
        ],
        "contour_proposal": {"profile": proposal.profile, "side_error_mm": 0.3},
    }
    return spec, report


def test_a_sheet_contour_replaces_a_plate_read_as_a_rectangle_and_builds():
    """Живая планка: прочитана прямоугольником, отверстия «на заявленном x нет»."""
    from app.ai.cad_recognize.verifiers.reconcile import apply_contour, contour_decision
    from app.ai.cad_solid import feature_tree_from_spec

    spec, report = _read_and_report(["unmeasurable", "unmeasurable"])

    decision = contour_decision(spec, report)

    assert decision is not None and "R10, R16, R25" in decision["reason"]
    fixed = apply_contour(spec, decision)
    profile = fixed["main_view"]["profile"]
    assert profile["shape"] == "sketch" and len(profile["holes"]) == 3
    candidate = feature_tree_from_spec(fixed)
    assert candidate is not None, "контур по листу не собирается в дерево операций"
    kinds = [feature.kind for feature in candidate.features]
    assert kinds.count("extrude") == 1 and kinds.count("hole") == 3, kinds


def test_a_plate_whose_holes_the_sheet_confirmed_keeps_its_reading():
    from app.ai.cad_recognize.verifiers.reconcile import contour_decision

    spec, report = _read_and_report(["confirmed", "confirmed"])

    assert contour_decision(spec, report) is None
