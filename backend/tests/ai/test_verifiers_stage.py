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


def test_a_spec_without_plate_holes_has_nothing_to_check():
    report = verify_spec_against_sheet(_png(), {"main_view": {"profile": {"shape": "circle"}}})

    assert report["items"] == []
    assert report["summary"]["checked"] == 0
