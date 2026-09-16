"""Масштаб чертежа по листу: основная надпись ЕСКД как линейка бумаги.

Основная надпись (ГОСТ 2.104, форма 1) — 185 × 55 мм на любом формате. Её
ширина в пикселях даёт пиксели на миллиметр БУМАГИ, система координат
проверенного вида — пиксели на миллиметр ДЕТАЛИ; их отношение — масштаб
чертежа, которым можно подтвердить масштаб из штампа. Живая втулка part_03:
штамп 2107 px → 11,39 px/мм, разрез 45,83 px/мм → 4,02 — «4:1», как в штампе.

Рамка листа не обязательно самая крайняя линия: у увеличенного листа край
бумаги тоже линия. Кандидаты рамки — все длинные основные линии, выбор — тот,
у которого нашёлся штамп.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_STAMP_WIDTH_MM = 185.0
_STAMP_HEIGHT_MM = 55.0
_STAMP_RATIO_TOLERANCE = 0.04
# Масштаб ГОСТ 2.302 принимается, если измеренный в пределах этой доли.
_SCALE_TOLERANCE = 0.04
_GOST_SCALES = (
    (1, 1),
    (1, 2),
    (1, 2.5),
    (1, 4),
    (1, 5),
    (1, 10),
    (1, 15),
    (1, 20),
    (1, 25),
    (1, 40),
    (1, 50),
    (1, 75),
    (1, 100),
    (1, 200),
    (1, 400),
    (1, 500),
    (1, 1000),
    (2, 1),
    (2.5, 1),
    (4, 1),
    (5, 1),
    (10, 1),
    (20, 1),
    (40, 1),
    (50, 1),
    (100, 1),
)


@dataclass(frozen=True)
class TitleBlock:
    bbox_px: tuple[float, float, float, float]
    paper_px_per_mm: float


def locate_title_block(gray: Any) -> TitleBlock | None:
    """Основная надпись у правого нижнего угла внутренней рамки или ``None``."""
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.flange_outline import _main_lines
    from app.ai.cad_recognize.verifiers.plate_frame import _lines

    gray = np.asarray(gray)
    height, width = gray.shape
    line = main_line_px(gray)
    if line <= 0:
        return None
    thick = _main_lines(gray, line) > 0
    min_length = max(6, int(0.03 * min(height, width)))
    horizontal = _lines(thick, min_length, axis=0)
    vertical = _lines(thick, min_length, axis=1)
    near = 3.0 * line
    frames_x = [v for v in vertical if v.end - v.start > 0.5 * height and v.position > 0.5 * width]
    frames_y = [
        h for h in horizontal if h.end - h.start > 0.5 * width and h.position > 0.5 * height
    ]
    best: tuple[float, TitleBlock] | None = None
    for right in frames_x:
        for bottom in frames_y:
            x_right, y_bottom = right.position, bottom.position
            for top in horizontal:
                if abs(top.end - x_right) > near or top.position >= y_bottom - min_length:
                    continue
                stamp_height = y_bottom - top.position
                for left in vertical:
                    if abs(left.end - y_bottom) > near or abs(left.start - top.position) > near:
                        continue
                    if abs(top.start - left.position) > near:
                        continue
                    stamp_width = x_right - left.position
                    ratio = stamp_width / stamp_height
                    error = abs(ratio / (_STAMP_WIDTH_MM / _STAMP_HEIGHT_MM) - 1.0)
                    if error > _STAMP_RATIO_TOLERANCE:
                        continue
                    found = TitleBlock(
                        bbox_px=(left.position, top.position, x_right, y_bottom),
                        paper_px_per_mm=(
                            stamp_width / _STAMP_WIDTH_MM + stamp_height / _STAMP_HEIGHT_MM
                        )
                        / 2.0,
                    )
                    # Самый крупный подходящий прямоугольник — штамп, а не его графы.
                    if best is None or stamp_width > best[0]:
                        best = (stamp_width, found)
    return best[1] if best else None


def drawing_scale(view_mm_per_px: float, block: TitleBlock) -> dict[str, Any]:
    """Масштаб чертежа: отношение пикселей на мм детали к пикселям на мм бумаги."""
    ratio = (1.0 / float(view_mm_per_px)) / block.paper_px_per_mm
    label = None
    for model, paper in _GOST_SCALES:
        value = model / paper
        if abs(ratio / value - 1.0) <= _SCALE_TOLERANCE:
            label = f"{model:g}:{paper:g}"
            break
    return {
        "stamp_bbox_px": [round(float(v), 1) for v in block.bbox_px],
        "paper_px_per_mm": round(block.paper_px_per_mm, 3),
        "view_px_per_mm": round(1.0 / float(view_mm_per_px), 3),
        "ratio": round(ratio, 3),
        "label": label,
    }


def same_scale(stated: Any, label: str | None) -> bool:
    """Масштаб штампа («4:1», «1 : 2», «М 2:1») совпадает с измеренным."""
    import re

    if not label:
        return False
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)", str(stated or ""))
    if not match:
        return False
    model, paper = (float(value.replace(",", ".")) for value in match.groups())
    want_model, want_paper = (float(value) for value in label.split(":"))
    return abs(model / paper - want_model / want_paper) <= 1e-6
