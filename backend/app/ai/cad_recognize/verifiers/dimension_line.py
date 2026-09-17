"""Проверяльщик размерной линии: её длина по чернилам рядом с подписью.

Измерение — `axial_dimensions._span_from_ink` (эксперимент E2: 95–100 % верных
на лестнице деградации от 300 до 75 dpi, 92 % на фото после выпрямления).
Гипотеза: подпись с рамкой и прочитанное число; вердикт — длина линии в
пикселях и, если у вида есть масштаб, в миллиметрах.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск сравнения прочитанного с измеренным: 2 % или 2 px — как в харнессе E2.
_RELATIVE = 0.02
_FLOOR_PX = 2.0
# Рамка повёрнутой подписи выше, чем шире, во столько раз.
_ROTATED_LABEL = 1.2


@register("dimension_line", min_feature_px=8.0)
def verify_dimension_line(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``sheet`` — маска чернил листа (`axial_dimensions._ink_rows`)."""
    from app.ai.cad_recognize.axial_dimensions import _span_from_ink, _vertical_span_from_ink

    if hypothesis.region_px is None:
        return Verdict(status="unmeasurable", reason="у подписи нет рамки на листе")
    x0, y0, x1, y1 = hypothesis.region_px
    # Вертикальный размер: подпись повёрнута — рамка выше, чем шире (или
    # ориентацию назвала гипотеза). Высота плана пластины, вертикальные
    # цепочки — раньше не мерились вовсе: проверяльщик сканировал строки.
    orientation = hypothesis.expected.get("orientation")
    vertical = orientation == "vertical" or (
        orientation is None and (y1 - y0) > _ROTATED_LABEL * (x1 - x0)
    )
    if vertical:
        unit = max(4.0, x1 - x0)
        line = _vertical_span_from_ink(sheet, [x0, y0, x1, y1], unit)
    else:
        unit = max(4.0, y1 - y0)
        line = _span_from_ink(sheet, [x0, y0, x1, y1], unit)
    if line is None:
        return Verdict(status="unmeasurable", reason="рядом с подписью нет размерной линии")
    span_px = float(line[3] - line[1]) if vertical else float(line[2] - line[0])
    measured = {"span_px": round(span_px, 2)}
    anchors = ((float(line[0]), float(line[1])), (float(line[2]), float(line[3])))
    bbox = (min(x0, line[0]), min(y0, line[1]), max(x1, line[2]), max(y1, line[3]))
    expected = hypothesis.expected.get("value_mm")
    if frame is None or expected is None:
        return Verdict(
            status="unmeasurable",
            measured=measured,
            evidence_bbox_px=bbox,
            anchors_px=anchors,
            reason="нет масштаба вида — длина линии измерена, но сравнить не с чем",
        )
    scale = frame.scale_v if vertical else frame.mm_per_px
    measured["value_mm"] = round(span_px * scale, 3)
    expected_px = expected / scale
    agree = abs(span_px - expected_px) <= max(_FLOOR_PX, _RELATIVE * expected_px)
    return Verdict(
        status="confirmed" if agree else "refuted",
        measured=measured,
        evidence_bbox_px=bbox,
        anchors_px=anchors,
        reason="" if agree else f"линия {measured['value_mm']:g} мм, подпись {expected:g} мм",
    )
