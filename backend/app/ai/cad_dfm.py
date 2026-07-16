"""F4: deterministic DFM (design-for-manufacturability) checks over a CAD IR
drawing.

Every check is geometry + reference data, no LLM: standard drill series,
minimum tool radii, wall/web thickness. Findings are ADVISORY for the
technologist (severity warn) except physically unmakeable cases (error).
All thresholds are in millimetres, so a confirmed metric scale is required —
running DFM on pixel guesses would produce noise, not engineering advice.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.ai.cad_ir.schema import Arc, CadIR, Circle, Segment

# ГОСТ 885 (сокращённый ряд): диаметры спиральных свёрл, мм.
STANDARD_DRILLS_MM = [
    1.0, 1.2, 1.5, 1.8, 2.0, 2.2, 2.5, 2.8, 3.0, 3.2, 3.5, 3.8,
    4.0, 4.2, 4.5, 4.8, 5.0, 5.2, 5.5, 5.8, 6.0, 6.5, 7.0, 7.5,
    8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 12.0, 13.0, 14.0, 15.0,
    16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 24.0, 25.0, 26.0,
    28.0, 30.0, 32.0, 34.0, 36.0, 38.0, 40.0, 42.0, 45.0, 48.0, 50.0,
]
# Метрическая резьба (ГОСТ 8724, основной ряд): номинал → крупный шаг.
METRIC_THREADS = {
    3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75,
    14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 24: 3.0, 30: 3.5, 36: 4.0,
    42: 4.5, 48: 5.0,
}

MIN_DRILL_MM = 1.0
MIN_TOOL_RADIUS_MM = 0.5
MIN_WALL_MM = 1.5
MIN_HOLE_BRIDGE_MM = 1.0
_DRILL_MATCH_TOL_MM = 0.06


@dataclass
class DfmFinding:
    code: str
    severity: str  # error | warn
    message: str
    recommendation: str
    entity_ids: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def _mm(value_px: float, scale: float) -> float:
    return value_px * scale


def _check_holes(ir: CadIR, scale: float) -> list[DfmFinding]:
    findings: list[DfmFinding] = []
    circles = [e for e in ir.entities if isinstance(e, Circle) and not e.construction]
    for circle in circles:
        diameter = _mm(circle.radius * 2, scale)
        if diameter < MIN_DRILL_MM:
            findings.append(DfmFinding(
                code="DFM_SMALL_HOLE", severity="error",
                message=f"Отверстие Ø{diameter:.2f} мм меньше минимального сверла {MIN_DRILL_MM} мм",
                recommendation="Увеличьте диаметр или согласуйте спец. операцию (лазер/ЭЭО)",
                entity_ids=[circle.id], evidence={"diameter_mm": diameter},
            ))
            continue
        if diameter <= STANDARD_DRILLS_MM[-1] and not any(
            abs(diameter - drill) <= _DRILL_MATCH_TOL_MM for drill in STANDARD_DRILLS_MM
        ):
            nearest = min(STANDARD_DRILLS_MM, key=lambda drill: abs(drill - diameter))
            findings.append(DfmFinding(
                code="DFM_NONSTANDARD_DRILL", severity="warn",
                message=f"Ø{diameter:.2f} мм не входит в стандартный ряд свёрл (ГОСТ 885)",
                recommendation=f"Ближайшее стандартное сверло {nearest:g} мм; иначе — сверление + расточка/развёртка",
                entity_ids=[circle.id], evidence={"diameter_mm": diameter, "nearest_standard_mm": nearest},
            ))
    # перемычки между соседними отверстиями
    for i, first in enumerate(circles):
        for second in circles[i + 1:]:
            gap_px = math.hypot(
                first.center.x - second.center.x, first.center.y - second.center.y
            ) - first.radius - second.radius
            gap = _mm(gap_px, scale)
            if 0 <= gap < MIN_HOLE_BRIDGE_MM:
                findings.append(DfmFinding(
                    code="DFM_THIN_HOLE_BRIDGE", severity="warn",
                    message=f"Перемычка между отверстиями {gap:.2f} мм — риск разрыва при сверлении",
                    recommendation=f"Разнесите отверстия (перемычка ≥ {MIN_HOLE_BRIDGE_MM} мм) или измените порядок обработки",
                    entity_ids=[first.id, second.id], evidence={"bridge_mm": gap},
                ))
    return findings


def _check_internal_radii(ir: CadIR, scale: float) -> list[DfmFinding]:
    findings: list[DfmFinding] = []
    for arc in (e for e in ir.entities if isinstance(e, Arc) and not e.construction):
        if arc.line_class != "contour":
            continue
        radius = _mm(arc.radius, scale)
        if radius < MIN_TOOL_RADIUS_MM:
            findings.append(DfmFinding(
                code="DFM_SMALL_INTERNAL_RADIUS", severity="warn",
                message=f"Радиус контура {radius:.2f} мм меньше минимального радиуса фрезы {MIN_TOOL_RADIUS_MM} мм",
                recommendation="Увеличьте радиус или предусмотрите ЭЭО/протяжку для острого угла",
                entity_ids=[arc.id], evidence={"radius_mm": radius},
            ))
    return findings


def _segments_parallel_gap_px(a: Segment, b: Segment) -> tuple[float, float] | None:
    """(perpendicular gap, projection overlap) between two near-parallel
    segments — the wall-thickness candidate. None when not comparable."""
    ax, ay = a.p2.x - a.p1.x, a.p2.y - a.p1.y
    bx, by = b.p2.x - b.p1.x, b.p2.y - b.p1.y
    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
    if la < 1e-6 or lb < 1e-6:
        return None
    cross = abs(ax * by - ay * bx) / (la * lb)
    if cross > 0.05:  # ~3° — not parallel
        return None
    # projection overlap along a's direction
    ux, uy = ax / la, ay / la
    ta = sorted([0.0, la])
    tb = sorted([
        (b.p1.x - a.p1.x) * ux + (b.p1.y - a.p1.y) * uy,
        (b.p2.x - a.p1.x) * ux + (b.p2.y - a.p1.y) * uy,
    ])
    overlap = min(ta[1], tb[1]) - max(ta[0], tb[0])
    if overlap <= max(la, lb) * 0.3:  # require meaningful shared span
        return None
    nx, ny = -uy, ux
    gap = abs((b.p1.x - a.p1.x) * nx + (b.p1.y - a.p1.y) * ny)
    return gap, overlap


def _check_thin_walls(ir: CadIR, scale: float) -> list[DfmFinding]:
    findings: list[DfmFinding] = []
    segments = [
        e for e in ir.entities
        if isinstance(e, Segment) and not e.construction and e.line_class == "contour"
    ]
    reported: set[tuple[str, str]] = set()
    for i, first in enumerate(segments):
        for second in segments[i + 1:]:
            pair = _segments_parallel_gap_px(first, second)
            if pair is None:
                continue
            gap_px, overlap_px = pair
            # A wall is a sustained feature: require ≥5 mm of shared span so
            # hatching strokes and dimension-line fragments don't register.
            if _mm(overlap_px, scale) < 5.0:
                continue
            wall = _mm(gap_px, scale)
            # The lower cutoff (0.5 mm) filters digitization artifacts: a
            # recognized drawing often carries doubled strokes 0.2-0.4 mm
            # apart, which are the same line, not a wall. A real machined
            # wall thinner than 0.5 mm is practically nonexistent on the
            # drawings this system sees, so the band below is noise.
            if 0.5 < wall < MIN_WALL_MM:
                key = (first.id, second.id)
                if key in reported:
                    continue
                reported.add(key)
                findings.append(DfmFinding(
                    code="DFM_THIN_WALL", severity="warn",
                    message=f"Стенка {wall:.2f} мм тоньше рекомендуемого минимума {MIN_WALL_MM} мм",
                    recommendation="Утолщите стенку или согласуйте с технологом режимы/крепление",
                    entity_ids=[first.id, second.id], evidence={"wall_mm": wall},
                ))
    return findings


_THREAD_RE = re.compile(r"[МM]\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×]\s*(\d+(?:[.,]\d+)?))?", re.IGNORECASE)


def _check_threads(ir: CadIR) -> list[DfmFinding]:
    findings: list[DfmFinding] = []
    for entity in ir.entities:
        if getattr(entity, "type", None) != "annotation" or getattr(entity, "kind", None) != "thread":
            continue
        raw = (entity.value or "").strip()
        match = _THREAD_RE.search(raw)
        if not match:
            continue
        nominal = float(match.group(1).replace(",", "."))
        if nominal != int(nominal) or int(nominal) not in METRIC_THREADS:
            findings.append(DfmFinding(
                code="DFM_THREAD_NONSTANDARD", severity="warn",
                message=f"Резьба {raw!r}: номинал M{nominal:g} вне основного ряда ГОСТ 8724",
                recommendation="Проверьте необходимость нестандартной резьбы — метчик придётся заказывать",
                entity_ids=[entity.id], evidence={"nominal_mm": nominal},
            ))
            continue
        if match.group(2):
            pitch = float(match.group(2).replace(",", "."))
            coarse = METRIC_THREADS[int(nominal)]
            if abs(pitch - coarse) > 1e-9 and pitch > coarse:
                findings.append(DfmFinding(
                    code="DFM_THREAD_PITCH", severity="warn",
                    message=f"Резьба {raw!r}: шаг {pitch:g} больше крупного {coarse:g} для M{nominal:g}",
                    recommendation="Крупный шаг — максимум для номинала; проверьте обозначение",
                    entity_ids=[entity.id], evidence={"pitch_mm": pitch, "coarse_mm": coarse},
                ))
    return findings


def check_dfm(ir: CadIR) -> list[DfmFinding]:
    """Run every DFM check. Requires a confirmed metric scale — millimetre
    thresholds over pixel guesses would be noise, not engineering advice."""
    if ir.scale is None or ir.scale_source is None:
        raise ValueError("DFM-проверка требует подтверждённого масштаба (мм/px)")
    scale = ir.scale
    findings: list[DfmFinding] = []
    findings.extend(_check_holes(ir, scale))
    findings.extend(_check_internal_radii(ir, scale))
    findings.extend(_check_thin_walls(ir, scale))
    findings.extend(_check_threads(ir))
    return findings
