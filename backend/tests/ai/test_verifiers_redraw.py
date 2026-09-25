"""Точечная перерисовка спорного места: принимается только проверкой (E30b).

«FLUX» здесь поддельный: возвращает вырез чистого листа, выдумывает линию
или у разных сидов рисует по-разному. Проверка — сечения (`section_outline`).
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

import tests.ai.test_verifiers_section_outline as sections
from app.ai.cad_recognize.verifiers.redraw import patch_disputed, precision
from app.ai.cad_recognize.verifiers.section_outline import verify_placed_on_sections

BODY = sections._body(sections._flat(90.0, 4.0), sections._hole(45.0, 8.0))
SPEC = {"main_view": BODY}


def _merged(sheet: np.ndarray) -> np.ndarray:
    """Хорда лыски слиплась со штрихом над ней в одну толстую полосу."""
    bad = sheet.copy()
    depth_px = (20.0 - 4.0) * sections.PX
    cv2.rectangle(bad, (270, int(420 - depth_px - 7)), (330, int(420 - depth_px + 2)), 0, -1)
    return bad


def _png(gray: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(gray).save(buffer, format="PNG")
    return buffer.getvalue()


def _verify(png: bytes, spec: dict) -> dict:
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    return {
        "items": verify_placed_on_sections(
            gray, sections.VIEW, 1.0 / sections.PX, spec["main_view"]
        )
    }


def _from(sheet: np.ndarray, source: np.ndarray):
    """«Перерисовка» — тот же вырез с листа ``sheet`` (по месту выреза)."""

    def redraw(crop: np.ndarray, seed: int) -> np.ndarray:
        result = cv2.matchTemplate(source, crop, cv2.TM_SQDIFF)
        _v, _m, (x, y), _M = cv2.minMaxLoc(result)
        h, w = crop.shape
        return sheet[y : y + h, x : x + w].copy()

    return redraw


def test_a_merged_flat_becomes_measurable_on_an_agreeing_redraw():
    clean, bad = sections._sheet(), _merged(sections._sheet())
    before = _verify(_png(bad), SPEC)
    assert before["items"][0]["status"] == "unmeasurable" and before["items"][0]["redraw_box"]

    patched, log = patch_disputed(
        _png(bad), SPEC, before, comfy_url="", redraw=_from(clean, bad), verify=_verify
    )

    assert log[0]["outcome"].startswith("принято"), log
    after = _verify(patched, SPEC)["items"]
    assert after[0]["status"] == "confirmed", after[0]
    assert abs(after[0]["measured"]["depth_mm"] - 4.0) < 0.6
    assert after[1]["status"] == "confirmed"  # соседнее сечение не тронуто


def test_a_redraw_that_invents_lines_is_refused():
    clean, bad = sections._sheet(), _merged(sections._sheet())
    inventive = clean.copy()
    cv2.line(inventive, (180, 470), (230, 540), 0, 3)  # линия на пустом поле выреза
    before = _verify(_png(bad), SPEC)

    patched, log = patch_disputed(
        _png(bad), SPEC, before, comfy_url="", redraw=_from(inventive, bad), verify=_verify
    )

    assert patched == _png(bad)
    assert "выдумала" in log[0]["outcome"], log


def test_seeds_that_disagree_are_refused():
    clean, bad = sections._sheet(), _merged(sections._sheet())
    shallow = sections._sheet()
    shallow[:, :600] = 255
    sections._section(shallow, 300, 420, flat=(90.0, 2.5))  # другой сид «нарисовал» мельче
    before = _verify(_png(bad), SPEC)
    first, second = _from(clean, bad), _from(shallow, bad)

    patched, log = patch_disputed(
        _png(bad),
        SPEC,
        before,
        comfy_url="",
        redraw=lambda crop, seed: (first if seed == 1 else second)(crop, seed),
        verify=_verify,
    )

    assert patched == _png(bad)
    assert not log[0].get("accepted"), log


def test_precision_counts_only_lines_inside_the_source_ink():
    thin = np.full((100, 100), 255, np.uint8)
    cv2.line(thin, (10, 50), (90, 50), 0, 2)
    thick = np.full((100, 100), 255, np.uint8)
    cv2.line(thick, (10, 50), (90, 50), 0, 9)
    assert precision(thick, thin, 6.0) > 0.97
    cv2.line(thin, (50, 5), (50, 45), 0, 2)
    assert precision(thick, thin, 6.0) < 0.8
