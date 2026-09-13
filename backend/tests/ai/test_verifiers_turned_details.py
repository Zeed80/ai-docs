"""Канавка у уступа и фаска на торце вала: проверка по главному виду (Ф3.0c)."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import Hypothesis, verify
from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame

# Вал Ø30×30 → Ø20×40 → Ø25×30 при 5 px/мм; левый торец x=200, ось y=500.
PX = 5.0
X0, AXIS = 200.0, 500.0
MAIN = 6
# Канавка у уступа 70 на ступени Ø20: 67…70, глубина 1 (дно Ø18);
# фаска 2×45° на левом торце.
GROOVE = {"axial_position_mm": 68.5, "width_mm": 3.0, "depth_mm": 1.0}
CHAMFER = {"size_mm": 2.0, "location": "left_end"}


def _sheet(*, main: int = MAIN) -> np.ndarray:
    image = Image.new("L", (2800, 2000), 255)
    draw = ImageDraw.Draw(image)

    def x(mm: float) -> float:
        return X0 + mm * PX

    def edges(u0: float, u1: float, radius_mm: float) -> None:
        r = radius_mm * PX
        draw.line([(x(u0), AXIS - r), (x(u1), AXIS - r)], fill=0, width=main)
        draw.line([(x(u0), AXIS + r), (x(u1), AXIS + r)], fill=0, width=main)

    def face(u: float, low_mm: float, high_mm: float) -> None:
        for sign in (-1, 1):
            draw.line(
                [(x(u), AXIS + sign * low_mm * PX), (x(u), AXIS + sign * high_mm * PX)],
                fill=0,
                width=main,
            )

    # Ступень 1 с фаской 2×45° на левом торце: торец укорочен, линия фаски —
    # вертикаль через всю высоту на 2 мм от торца, косые кромки.
    face(0.0, 0.0, 13.0)
    draw.line([(x(2.0), AXIS - 75), (x(2.0), AXIS + 75)], fill=0, width=main)
    for sign in (-1, 1):
        draw.line([(x(0.0), AXIS + sign * 65), (x(2.0), AXIS + sign * 75)], fill=0, width=main)
    edges(2.0, 30.0, 15.0)
    face(30.0, 10.0, 15.0)
    # Ступень 2 с канавкой у уступа 70.
    edges(30.0, 67.0, 10.0)
    # Стенки канавки и уступ — вертикали через весь диаметр: круговая кромка
    # кольцевой грани проецируется на главный вид целиком (так рисует ядро).
    face(67.0, 0.0, 10.0)
    edges(67.0, 70.0, 9.0)
    face(70.0, 0.0, 12.5)
    edges(70.0, 100.0, 12.5)
    face(100.0, 0.0, 12.5)
    return np.asarray(image)


def _frame_and_profile(sheet: np.ndarray):
    located = locate_shaft_frame(sheet, 100.0)
    assert located is not None
    return located


def test_a_groove_at_the_shoulder_is_confirmed_with_width_and_depth():
    sheet = _sheet()
    frame, profile = _frame_and_profile(sheet)
    verdict = verify(Hypothesis("groove", "main_view.grooves[0]", GROOVE), frame, (sheet, profile))

    assert verdict.status == "confirmed", (verdict.reason, verdict.measured)
    assert abs(verdict.measured["width_mm"] - 3.0) <= 0.3
    assert abs(verdict.measured["depth_mm"] - 1.0) <= 0.25
    assert abs(verdict.measured["axial_position_mm"] - 68.5) <= 0.3


def test_a_groove_drawn_off_its_label_is_not_refuted_but_measured():
    """ГОСТ 2.305: канавку допускается изображать не в масштабе — замер надпись
    не опровергает, а даёт справку и «не измеримо» с причиной."""
    sheet = _sheet()
    frame, profile = _frame_and_profile(sheet)
    for field, delta in (("width_mm", 1.0), ("depth_mm", 0.6)):
        read = {**GROOVE, field: GROOVE[field] + delta}
        verdict = verify(Hypothesis("groove", "g", read), frame, (sheet, profile))

        assert verdict.status == "unmeasurable", (field, verdict.measured)
        assert "не в масштабе" in verdict.reason
        assert abs(verdict.measured[field] - GROOVE[field]) <= 0.3, (field, verdict.measured)


def test_an_end_chamfer_is_confirmed_and_one_drawn_off_its_label_is_not_refuted():
    """Живой z4-r4: «1,6×45°» и «3×45°» нарисованы линиями в 1 и 1,6 мм — надписи
    верны, изображение условное (ГОСТ 2.305), опровергать их нельзя."""
    sheet = _sheet()
    frame, profile = _frame_and_profile(sheet)
    confirmed = verify(Hypothesis("chamfer", "c", CHAMFER), frame, (sheet, profile))
    other = verify(Hypothesis("chamfer", "c", {**CHAMFER, "size_mm": 1.0}), frame, (sheet, profile))

    assert confirmed.status == "confirmed", (confirmed.reason, confirmed.measured)
    assert abs(confirmed.measured["size_mm"] - 2.0) <= 0.3
    assert other.status == "unmeasurable"
    assert "не в масштабе" in other.reason
    assert abs(other.measured["size_mm"] - 2.0) <= 0.3


def test_a_coarse_sheet_is_unmeasurable_not_a_guess():
    sheet = _sheet(main=3)
    located = locate_shaft_frame(sheet, 100.0)
    frame, profile = located if located else (None, None)
    for kind, read in (("groove", GROOVE), ("chamfer", CHAMFER)):
        verdict = verify(Hypothesis(kind, kind, read), frame, (sheet, profile))
        assert verdict.status == "unmeasurable", (kind, verdict.measured)


def test_the_stage_checks_grooves_and_chamfers_of_a_shaft():
    from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

    buffer = io.BytesIO()
    Image.fromarray(_sheet()).save(buffer, format="PNG")
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 30.0},
                {"diameter_mm": 20.0, "length_mm": 40.0},
                {"diameter_mm": 25.0, "length_mm": 30.0},
            ],
            "grooves": [{**GROOVE, "kind": "relief", "width_mm": 2.0}],  # на листе 3
            "chamfers": [{**CHAMFER, "angle_deg": 45.0}],
        }
    }
    report = verify_spec_against_sheet(buffer.getvalue(), spec)
    by_kind = {
        item["kind"]: item for item in report["items"] if item["kind"] in ("groove", "chamfer")
    }

    # Канавка на листе 3 мм при надписи 2 — справка, а не опровержение.
    assert by_kind["groove"]["status"] == "unmeasurable", by_kind["groove"]
    assert abs(by_kind["groove"]["measured"]["width_mm"] - 3.0) <= 0.3
    assert by_kind["chamfer"]["status"] == "confirmed", by_kind["chamfer"]
    assert not any(note.startswith("канавка 1") for note in report["notes"])
