"""Check a read spec against the drawing itself, before anything is built.

Consensus catches values that are unstable between passes. It cannot catch a
value that is stably WRONG — the spindle read 150+78+240+470 as section lengths
while 470 is the overall dimension, three times running. Two independent checks
catch that class, and neither needs a model:

1. Arithmetic the sheet itself asserts. Sections must sum to the overall size,
   a bore must fit inside its body, a bolt circle plus its holes must fit
   inside the flange. A drawing is a redundant document; the redundancy is
   free verification.

2. Proportions measured off the raster. The traced ink is in pixels and the
   spec in millimetres, so absolute comparison would need a calibration nobody
   has yet — but RATIOS are scale-free. A flange read as Ø560 outer with a Ø80
   bore must show two circles about 7:1 apart on the sheet; a read claiming a
   Ø18 bore implies 31:1, which the image simply does not contain.

Findings are advisory by default and never silently edit the spec: the caller
decides whether a contradiction blocks construction or goes to review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrossCheckFinding:
    code: str
    message: str
    # "error" — the sheet contradicts itself or the image; "warn" — suspicious.
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)


# A drawing rounds and a reader misreads a last digit; below this the numbers
# are treated as the same statement.
_RATIO_TOLERANCE = 0.03
_SUM_TOLERANCE = 0.02


def _sections(body: dict, key: str) -> list[dict]:
    return [s for s in (body.get(key) or []) if isinstance(s, dict)]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _stated_overall_length(spec: dict) -> float | None:
    """The largest plain-length callout the reader captured, if any."""
    values: list[float] = []
    for dimension in spec.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        text = str(dimension.get("value") or "").strip()
        if not text or text[:1] in ("Ø", "⌀", "M", "R", "м", "м"):
            continue
        try:
            values.append(float(text.replace(",", ".")))
        except ValueError:
            continue
    return max(values) if values else None


def check_spec_arithmetic(spec: dict) -> list[CrossCheckFinding]:
    """Contradictions the sheet's own numbers reveal, with no image involved."""
    findings: list[CrossCheckFinding] = []
    bodies = [spec.get("main_view") or {}, *(spec.get("parts") or [])]

    for index, body in enumerate(bodies):
        if not isinstance(body, dict):
            continue
        outer = _sections(body, "outer")
        lengths = [_num(s.get("length_mm")) for s in outer]
        diameters = [_num(s.get("diameter_mm")) for s in outer]
        known_lengths = [value for value in lengths if value]
        total = sum(known_lengths)

        # The classic chain misread: an overall dimension copied into a step.
        # Measured live on a spindle — 150+78+240+470 where 470 is the overall,
        # making the "part" twice its real length. One step longer than every
        # other step put together is the tell. It CAN be legitimate on a long
        # plain shaft, so this warns rather than blocks.
        if len(known_lengths) > 2 and total > 0:
            longest = max(known_lengths)
            rest = total - longest
            if longest >= rest * (1 - _SUM_TOLERANCE):
                position = lengths.index(longest)
                findings.append(CrossCheckFinding(
                    code="section_longer_than_all_others",
                    message=(
                        f"тело {index}: ступень {position} длиной {longest:g} мм "
                        f"длиннее всех остальных вместе ({rest:g} мм) — проверьте, "
                        "не прочитан ли вместо неё габаритный размер"
                    ),
                    severity="warn",
                    details={
                        "body": index, "section": position,
                        "value": longest, "others_total": rest,
                    },
                ))

        # When the sheet also stated an overall length, the sum must match it.
        overall = _stated_overall_length(spec)
        if overall and total and total > overall * (1 + _SUM_TOLERANCE):
            findings.append(CrossCheckFinding(
                code="sections_exceed_stated_overall",
                message=(
                    f"тело {index}: сумма ступеней {total:g} мм больше "
                    f"заявленного габарита {overall:g} мм"
                ),
                details={"body": index, "sum": total, "overall": overall},
            ))

        bore = _sections(body, "bore")
        max_outer = max((d for d in diameters if d), default=None)
        for position, section in enumerate(bore):
            bore_diameter = _num(section.get("diameter_mm"))
            if bore_diameter and max_outer and bore_diameter >= max_outer:
                findings.append(CrossCheckFinding(
                    code="bore_not_inside_body",
                    message=(
                        f"тело {index}: расточка Ø{bore_diameter:g} не меньше "
                        f"наибольшего наружного Ø{max_outer:g}"
                    ),
                    details={"body": index, "section": position},
                ))
        bore_total = sum(v for v in (_num(s.get("length_mm")) for s in bore) if v)
        if bore_total and total and bore_total > total * (1 + _SUM_TOLERANCE):
            findings.append(CrossCheckFinding(
                code="bore_longer_than_body",
                message=(
                    f"тело {index}: расточка длиной {bore_total:g} мм длиннее "
                    f"детали ({total:g} мм)"
                ),
                details={"body": index},
            ))

        profile = body.get("profile")
        if isinstance(profile, dict):
            findings.extend(_check_profile(profile, index))
    return findings


def _check_profile(profile: dict, body_index: int) -> list[CrossCheckFinding]:
    findings: list[CrossCheckFinding] = []
    diameter = _num(profile.get("diameter_mm"))
    width = _num(profile.get("width_mm"))
    height = _num(profile.get("height_mm"))
    half_width = (diameter / 2.0) if diameter else (
        min(width, height) / 2.0 if width and height else None
    )
    if half_width is None:
        return findings

    for position, hole in enumerate(profile.get("holes") or []):
        if not isinstance(hole, dict):
            continue
        x, y = _num(hole.get("center_x_mm")), _num(hole.get("center_y_mm"))
        hole_diameter = _num(hole.get("diameter_mm"))
        if x is None or y is None or not hole_diameter:
            continue
        if math.hypot(x, y) + hole_diameter / 2.0 > half_width * (1 + _SUM_TOLERANCE):
            findings.append(CrossCheckFinding(
                code="hole_outside_profile",
                message=(
                    f"тело {body_index}: отверстие Ø{hole_diameter:g} в точке "
                    f"({x:g}, {y:g}) выходит за контур"
                ),
                details={"body": body_index, "hole": position},
            ))

    for position, pattern in enumerate(profile.get("hole_patterns") or []):
        if not isinstance(pattern, dict):
            continue
        pcd = _num(pattern.get("bolt_circle_diameter_mm"))
        hole_diameter = _num(pattern.get("hole_diameter_mm"))
        if not pcd or not hole_diameter:
            continue
        if pcd / 2.0 + hole_diameter / 2.0 > half_width * (1 + _SUM_TOLERANCE):
            findings.append(CrossCheckFinding(
                code="bolt_circle_outside_profile",
                message=(
                    f"тело {body_index}: делительная окружность Ø{pcd:g} с "
                    f"отверстиями Ø{hole_diameter:g} не помещается в контур"
                ),
                details={"body": body_index, "pattern": position},
            ))
    return findings


def _spec_circle_diameters(spec: dict) -> list[float]:
    """Every diameter the spec claims is drawn as a circle on the sheet."""
    diameters: list[float] = []
    for body in [spec.get("main_view") or {}, *(spec.get("parts") or [])]:
        if not isinstance(body, dict):
            continue
        profile = body.get("profile")
        if isinstance(profile, dict):
            outer = _num(profile.get("diameter_mm"))
            if outer:
                diameters.append(outer)
            for hole in profile.get("holes") or []:
                value = _num(hole.get("diameter_mm")) if isinstance(hole, dict) else None
                if value:
                    diameters.append(value)
            for pattern in profile.get("hole_patterns") or []:
                if isinstance(pattern, dict):
                    value = _num(pattern.get("hole_diameter_mm"))
                    if value:
                        diameters.append(value)
    return sorted({round(value, 3) for value in diameters}, reverse=True)


def check_spec_against_raster(
    spec: dict, circle_radii_px: list[float]
) -> list[CrossCheckFinding]:
    """Compare the spec's diameter RATIOS with the ones measured on the sheet.

    Scale-free on purpose: nobody has calibrated mm-per-pixel at this point, and
    a ratio needs no calibration. Only the largest-to-smallest ratio is checked,
    because that is the one a misread bore or outline distorts most and the one
    least sensitive to a missed circle in the middle.
    """
    findings: list[CrossCheckFinding] = []
    stated = _spec_circle_diameters(spec)
    measured = sorted({round(r, 3) for r in circle_radii_px if r > 0}, reverse=True)
    if len(stated) < 2 or len(measured) < 2:
        return findings

    stated_ratio = stated[0] / stated[-1]
    measured_ratio = measured[0] / measured[-1]
    if stated_ratio <= 0 or measured_ratio <= 0:
        return findings
    relative = abs(stated_ratio - measured_ratio) / measured_ratio
    if relative > _RATIO_TOLERANCE * 5:
        findings.append(CrossCheckFinding(
            code="circle_ratio_mismatch",
            message=(
                "пропорции окружностей не сходятся с чертежом: по прочитанному "
                f"наибольший/наименьший = {stated_ratio:.2f}, по изображению "
                f"= {measured_ratio:.2f}"
            ),
            severity="error",
            details={
                "stated_ratio": round(stated_ratio, 3),
                "measured_ratio": round(measured_ratio, 3),
                "stated_diameters_mm": stated[:6],
                "measured_radii_px": measured[:6],
            },
        ))
    return findings


def measure_circle_radii(ink: Any) -> list[float]:
    """Radii of the circles the classical tracer finds on the sheet, in pixels.

    This is the auxiliary role tracing was kept for: it does not have to
    reconstruct the drawing, only to measure enough of it to contradict a bad
    reading.
    """
    from app.ai.cad_ir.schema import Circle
    from app.ai.cad_recognize.cv import CvRecognizer

    try:
        output = CvRecognizer().recognize(ink)
    except Exception:  # noqa: BLE001 — a cross-check must never break the run
        return []
    if output is None:
        return []
    return [
        float(entity.radius)
        for entity in output.entities
        if isinstance(entity, Circle) and entity.radius > 0
    ]


def cross_check_spec(spec: dict, ink: Any | None = None) -> dict[str, Any]:
    """Run every available check and report them in one reviewable block."""
    findings = check_spec_arithmetic(spec)
    measured: list[float] = []
    if ink is not None:
        measured = measure_circle_radii(ink)
        findings.extend(check_spec_against_raster(spec, measured))
    return {
        "findings": [
            {
                "code": f.code, "message": f.message,
                "severity": f.severity, "details": f.details,
            }
            for f in findings
        ],
        "errors": sum(1 for f in findings if f.severity == "error"),
        "measured_circles": len(measured),
    }
