"""Сечение гнутой детали по листу (X4): форма сечения — без чтения.

Корпус (24 листа 300 dpi): верное чтение — 24/24 «подтверждено», перевёрнутый
гиб (швеллер ↔ Z) — 19/19 опровергнуто, потерянная полка — 19/19, неверный
угол — 24/24 опровергнуто.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from app.ai.cad_recognize.verifiers.bent_section import (
    bent_section_verdict,
    measure_bent_section,
)
from app.ai.sheet_metal import bent_section


def _outline(flanges_mm, turns, radius_mm, thickness_mm, angles=None, scale=8.0):
    """Лист: сечение основной линией, рядом прямоугольник вида и тонкий размер."""
    sheet = np.full((900, 1400), 255, np.uint8)
    x, y = 0.0, 0.0
    points = [(0.0, 0.0)]
    for segment in bent_section(flanges_mm, turns, radius_mm, thickness_mm, angles):
        if segment["kind"] == "arc":
            cx, cy = segment["center"]
            a0 = math.atan2(y - cy, x - cx)
            a1 = math.atan2(segment["to"][1] - cy, segment["to"][0] - cx)
            sweep = (a1 - a0) % (2 * math.pi)
            if segment["clockwise"]:
                sweep -= 2 * math.pi
            r = math.hypot(x - cx, y - cy)
            for k in range(1, 13):
                a = a0 + sweep * k / 12
                points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        else:
            points.append(tuple(segment["to"]))
        x, y = segment["to"]
    pts = np.array([[200 + px * scale, 700 - py * scale] for px, py in points], np.int32)
    cv2.polylines(sheet, [pts], True, 0, 6)
    cv2.rectangle(sheet, (1000, 200), (1300, 600), 0, 6)
    cv2.line(sheet, (150, 780), (700, 780), 0, 2)
    return sheet


CHANNEL = {"flanges_mm": [30.0, 40.0, 30.0], "turns": [1, 1], "radius_mm": 2.0, "thickness_mm": 3.0}
ZED = {**CHANNEL, "turns": [1, -1]}


def test_the_section_shape_is_measured_from_the_sheet():
    measured = measure_bent_section(_outline(**CHANNEL))

    assert measured is not None
    assert len(measured["turns"]) == 2 and measured["turns"][0] == measured["turns"][1]
    assert all(a is not None and abs(a - 90.0) < 3.0 for a in measured["angles_deg"])


def test_a_channel_read_as_z_is_refuted_and_a_right_read_confirmed():
    measured = measure_bent_section(_outline(**CHANNEL))

    assert bent_section_verdict(CHANNEL, measured)["status"] == "confirmed"
    verdict = bent_section_verdict(ZED, measured)
    assert verdict["status"] == "refuted" and "направления" in verdict["reason"]
    lost = {**CHANNEL, "flanges_mm": [30.0, 40.0], "turns": [1]}
    assert bent_section_verdict(lost, measured)["status"] == "refuted"


def test_a_bend_angle_is_checked_when_the_flanges_are_long_enough():
    angle = {"flanges_mm": [40.0, 40.0], "turns": [1], "radius_mm": 2.0, "thickness_mm": 3.0}
    sheet = _outline(**angle, angles=[60.0])
    measured = measure_bent_section(sheet)

    assert measured["angles_deg"][0] == pytest.approx(60.0, abs=2.0)
    assert bent_section_verdict({**angle, "bend_angles_deg": [60.0]}, measured)["status"] == (
        "confirmed"
    )
    assert bent_section_verdict(angle, measured)["status"] == "refuted"


def test_an_unmeasured_angle_is_not_confirmed():
    measured = {"turns": [1], "angles_deg": [None], "flanges_px": [1.0, 1.0]}
    read = {"turns": [1], "bend_angles_deg": [60.0]}

    assert bent_section_verdict(read, measured)["status"] == "unmeasurable"


def test_the_reconciliation_takes_the_turns_from_the_sheet_only_when_the_count_agrees():
    from app.ai.cad_recognize.verifiers.reconcile import apply_bent_section, bent_section_decision

    spec = {"main_view": {"sheet_metal": dict(ZED)}}
    report = {
        "items": [
            {
                "kind": "bent_section",
                "status": "refuted",
                "reason": "направления гибов на листе другие",
                "measured": {"bends": 2, "turns": [-1, -1], "angles_deg": [89.4, 90.6]},
            }
        ]
    }
    decision = bent_section_decision(spec, report)
    assert decision["action"] == "adopt"
    fixed = apply_bent_section(spec, decision)
    assert fixed["main_view"]["sheet_metal"]["turns"] == [-1, -1]
    assert "bend_angles_deg" not in fixed["main_view"]["sheet_metal"]

    report["items"][0]["measured"]["turns"] = [1, 1, 1]
    report["items"][0]["measured"]["angles_deg"] = [90.0, 90.0, 90.0]
    assert bent_section_decision(spec, report)["action"] == "ask_human"
