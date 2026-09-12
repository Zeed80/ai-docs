"""Проверяльщик фаски на торце вала: размер c по главному виду.

По ЕСКД фаска на продольном виде — вертикаль через всю высоту на
расстоянии c от торца. При малой фаске (0,5 мм на 300 dpi — 6 px) она
сливается с линией торца в одну толстую полосу, при большой стоит отдельно
— в обоих случаях внутренняя кромка чернил у торца отстоит от внешней на
c + толщину линии. Меряется в строках у оси (выше — косая линия фаски и
кромка), медианой по строкам. Нет фаски на листе — c ≈ 0, и прочитанная
фаска опровергается.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame


def chamfer_tolerance(mm_per_px: float) -> float:
    """Размеры фасок по ряду (0,5/1/1,6/2) отличаются от 0,4 мм; не меньше
    полутора пикселей листа."""
    return max(0.2, 1.5 * mm_per_px)


@register("chamfer", min_feature_px=2.0)
def verify_chamfer(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``size_mm``, ``location`` (``left_end``/``right_end``);
    ``sheet`` — ``(серый лист, ShaftProfile)`` того же вида."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    if frame is None or not sheet or sheet[1] is None:
        return Verdict(status="unmeasurable", reason="главный вид вала на листе не найден")
    gray, profile = np.asarray(sheet[0]), sheet[1]
    size = hypothesis.expected.get("size_mm")
    location = hypothesis.expected.get("location")
    if not isinstance(size, (int, float)) or location not in ("left_end", "right_end"):
        return Verdict(status="unmeasurable", reason="проверяется только фаска на торце")
    line_px = float(getattr(profile, "line_px", 0.0) or 0.0)
    if line_px < _MIN_LINE_PX:
        return Verdict(
            status="unmeasurable",
            reason=f"лист слишком грубый: основная линия {line_px:.1f} px (нужно от {_MIN_LINE_PX:g})",
        )
    scale = frame.mm_per_px
    at_left = location == "left_end"
    end = float(profile.x0 if at_left else profile.x1)
    inward = 1 if at_left else -1
    # Полувысота у торца — чуть внутри, за полосой фаски.
    radius = profile.half_at(end + inward * (2.5 * float(size) / scale + 3.0 * line_px))
    if radius is None:
        return Verdict(status="unmeasurable", reason="кромка у торца на виде не найдена")
    reach = 2.5 * float(size) / scale + 3.0 * line_px
    lo = int(end - 3.0 * line_px) if at_left else int(end - reach)
    hi = int(end + reach) + 1 if at_left else int(end + 3.0 * line_px) + 1
    lo, hi = max(0, lo), min(gray.shape[1], hi)
    axis = profile.axis_y
    rows = [
        y
        for y in range(int(axis - 0.5 * radius), int(axis + 0.5 * radius) + 1)
        if abs(y - axis) > 2 and 0 <= y < gray.shape[0]
    ]
    if hi - lo < 4 or not rows:
        return Verdict(status="unmeasurable", reason="торец вне листа")
    ink = _ink(np.ascontiguousarray(gray[int(min(rows)) : int(max(rows)) + 1, lo:hi]))
    spans = []
    for row in ink:
        columns = np.nonzero(row)[0]
        if columns.size == 0:
            continue
        runs = np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1)
        # От внешнего края внутрь: прогоны, начинающиеся в пределах фаски.
        if at_left:
            outer = float(runs[0][0])
            inner = max(float(r[-1]) for r in runs if r[0] - outer <= reach - 2.0 * line_px)
        else:
            outer = float(runs[-1][-1])
            inner = min(float(r[0]) for r in runs if outer - r[-1] <= reach - 2.0 * line_px)
        spans.append(abs(outer - inner) + 1.0)
    if len(spans) < 3:
        return Verdict(status="unmeasurable", reason="линия торца у оси не найдена")
    measured_size = max(0.0, (float(np.median(spans)) - line_px) * scale)
    measured = {"size_mm": round(measured_size, 3)}
    tolerance = chamfer_tolerance(scale)
    wrong = abs(measured_size - float(size)) > tolerance
    return Verdict(
        status="refuted" if wrong else "confirmed",
        measured=measured,
        evidence_bbox_px=(lo, axis - radius, hi, axis + radius),
        reason=(f"фаска {measured_size:.2g} мм, прочитано {float(size):g}" if wrong else ""),
    )
