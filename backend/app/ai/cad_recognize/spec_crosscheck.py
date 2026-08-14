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
import re
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
    """The largest plain-length callout the reader captured, if any.

    A callout is rarely a bare number: the sheet writes ``470h14``, and a strict
    ``float()`` used to drop exactly those — so on a drawing that toleranced its
    overall size this check silently did nothing. The NOMINAL is parsed out and
    the tolerance ignored; what still gets rejected is everything that is not a
    length at all (diameters, radii, threads, angles and chamfers), because
    mistaking one of those for the overall size is the failure this guards.
    """
    values: list[float] = []
    for dimension in spec.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        text = str(dimension.get("value") or "").strip()
        if not text or text[:1] in ("Ø", "⌀", "M", "R", "м"):
            continue
        # An angle or a chamfer ("45°", "1x45°", "2×45") is not a length.
        if "°" in text or re.search(r"\d\s*[x×]\s*\d", text):
            continue
        match = re.match(r"^\s*(\d+(?:[.,]\d+)?)", text)
        if match:
            values.append(float(match.group(1).replace(",", ".")))
    return max(values) if values else None


# A stamp value that IS the field's own caption means the reader transcribed an
# empty form instead of reading it. Measured live: a building floor plan came
# back with material="материал", scale="масштаб", name="наименование" — and the
# pipeline went on to build a 3-metre shaft from the building's grid spacings.
_STAMP_PLACEHOLDERS = {
    "материал", "масштаб", "наименование", "масса", "обозначение", "лист",
    "листов", "material", "scale", "name", "mass", "designation", "sheet",
}

# A turned part this pipeline can plan work for. Anything past these is not a
# machined component — it is the sheet being misread as one.
_MAX_TURNED_DIAMETER_MM = 2000.0
_MAX_PART_LENGTH_MM = 6000.0


def check_spec_plausibility(spec: dict) -> list[CrossCheckFinding]:
    """Is this a machined part at all, or a sheet mistaken for one?

    Both checks are exact rather than heuristic: a caption is a caption, and a
    three-metre-diameter turned section is not a part this pipeline builds.
    """
    findings: list[CrossCheckFinding] = []
    title = spec.get("title_block") or {}
    echoed = sorted(
        field for field, value in title.items()
        if str(value or "").strip().lower() in _STAMP_PLACEHOLDERS
    )
    if echoed:
        findings.append(CrossCheckFinding(
            code="stamp_placeholders_read_as_values",
            message=(
                "в штампе вместо значений прочитаны названия полей ("
                + ", ".join(echoed)
                + ") — лист прочитан неверно целиком"
            ),
            details={"fields": echoed},
        ))

    for index, body in enumerate([spec.get("main_view") or {}, *(spec.get("parts") or [])]):
        if not isinstance(body, dict):
            continue
        sections = [s for s in (body.get("outer") or []) if isinstance(s, dict)]
        diameters = [_num(s.get("diameter_mm")) for s in sections]
        biggest = max((d for d in diameters if d), default=0.0)
        if biggest > _MAX_TURNED_DIAMETER_MM:
            findings.append(CrossCheckFinding(
                code="implausible_turned_diameter",
                message=(
                    f"тело {index}: диаметр {biggest:g} мм не бывает у точёной "
                    "детали — вероятно, прочитаны размеры не той сущности"
                ),
                details={"body": index, "diameter_mm": biggest},
            ))
        total = sum(v for v in (_num(s.get("length_mm")) for s in sections) if v)
        if total > _MAX_PART_LENGTH_MM:
            findings.append(CrossCheckFinding(
                code="implausible_part_length",
                message=(
                    f"тело {index}: суммарная длина {total:g} мм слишком велика "
                    "для детали этого конвейера"
                ),
                details={"body": index, "length_mm": total},
            ))
    return findings


def check_spec_arithmetic(spec: dict) -> list[CrossCheckFinding]:
    """Contradictions the sheet's own numbers reveal, with no image involved."""
    findings: list[CrossCheckFinding] = check_spec_plausibility(spec)
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
        # Only when the sheet draws ONE body: the callouts are a single flat
        # list with no body attached, so on a multi-part sheet the largest
        # length belongs to whichever part is longest — comparing every body's
        # sum against it says nothing about the others.
        overall = _stated_overall_length(spec) if len(bodies) == 1 else None
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


_DIAMETER_FEATURE_LISTS = ("outer", "bore", "cross_holes")


def _index_body_diameters(bodies: list[Any]) -> dict[str, float]:
    """Every id-tagged diameter in the spec, by feature id.

    Scoped to the fields with an unambiguous single ``diameter_mm`` (profile
    steps, cross holes, prismatic holes) — bolt-circle/axial patterns state
    several different diameters (hole vs pitch-circle vs thread nominal) and
    are left for a follow-up rather than guessed at here.
    """
    index: dict[str, float] = {}
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for list_name in _DIAMETER_FEATURE_LISTS:
            for item in body.get(list_name) or []:
                if not isinstance(item, dict):
                    continue
                feature_id = item.get("id")
                value = _num(item.get("diameter_mm"))
                if feature_id and value:
                    index[str(feature_id)] = value
        profile = body.get("profile")
        if isinstance(profile, dict):
            for item in profile.get("holes") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                value = _num(item.get("diameter_mm"))
                if value:
                    index[str(item["id"])] = value
    return index


def spec_view_geometries(spec: dict) -> list[Any]:
    """Adapt a read spec into per-view ``cad_ir.correspondence.ViewGeometry``.

    A view's diameters come ONLY from features its OWN ``features_shown``
    names — never every diameter its body happens to have, which would make
    every view of one body trivially "correspond" to every other and carry
    no information. Until the reader populates ``features_shown``
    (``assign_stable_feature_ids`` gives every feature the id to be named
    by; nothing writes into ``features_shown`` yet — see
    ``ENGINEERING_MODEL_GRAPH_SPEC``/roadmap Фаза 1.2), this correctly
    returns geometry with nothing to match: silence, not a guessed link.
    """
    from app.ai.cad_ir.correspondence import ViewGeometry

    bodies = [spec.get("main_view") or {}, *(spec.get("parts") or [])]
    diameters_by_id = _index_body_diameters(bodies)

    views_out: list[ViewGeometry] = []
    for position, view in enumerate(spec.get("views") or []):
        if not isinstance(view, dict):
            continue
        shown = [str(item) for item in (view.get("features_shown") or []) if item]
        diameters: list[float] = []
        feature_ids: list[str | None] = []
        for feature_id in shown:
            value = diameters_by_id.get(feature_id)
            if value is None:
                continue
            diameters.append(value)
            feature_ids.append(feature_id)
        if not diameters:
            continue
        label = str(
            view.get("view_id") or view.get("label")
            or f"{view.get('kind', 'view')}:{position}"
        )
        views_out.append(ViewGeometry(
            label=label, projection=str(view.get("kind") or ""),
            diameters_mm=diameters, diameter_feature_ids=feature_ids,
        ))
    return views_out


def check_view_correspondence(spec: dict) -> dict[str, Any]:
    """Cross-view diameter agreement between explicitly-named features.

    The deterministic half of ``same_object_across_views`` evidence — the
    other half is a view directly naming a feature id in ``features_shown``,
    consumed as-is by the native EMG graph builder. This function corroborates
    that naming (two views that both claim to show the same feature actually
    state a matching diameter) rather than inventing a link neither view's own
    data states. Findings here are ``details``-only advisory data, not
    ``CrossCheckFinding`` severities — a missing correspondence is not itself
    an error, only a contradiction (an ``issue``) is.
    """
    from app.ai.cad_ir.correspondence import build_correspondence_graph

    views = spec_view_geometries(spec)
    graph = build_correspondence_graph(views)
    return {
        "correspondences": [
            {
                "kind": c.kind,
                "views": list(c.views),
                "detail": c.detail,
                "confidence": c.confidence,
                "feature_ids": list(c.feature_ids) if c.feature_ids else None,
            }
            for c in graph.correspondences
        ],
        "issues": list(graph.issues),
    }


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
        # Silence here is NOT confirmation, and reporting it as "no errors"
        # once let a flange pass while the check had measured a single 4 px
        # hole. Measured 2026-07-25: the classical tracer decomposes large
        # circles into segments by design, so on a flange sheet it fits only
        # the tiny bolt holes. Preprocessing is not the cause — channel-min
        # binarisation moved the ink fraction from 0.0164 to 0.0166 and still
        # produced one circle.
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


def measure_dominant_circle_px(ink: Any) -> float | None:
    """Radius of the one circle the sheet is unambiguous about, in pixels.

    Tuned as a MEASURING tool, not a reconstruction one: at this threshold the
    detector returns the dominant outline of a round part and nothing at all on
    a shaft sheet that has no large circle. Measured on the labelled sheets —
    flange: exactly one circle at r=277 px against a true 281 px outline; shaft
    and bearing housing: none. Few measurements that can be trusted beat many
    that cannot.
    """
    import cv2

    try:
        height, width = ink.shape[:2]
        circles = cv2.HoughCircles(
            cv2.bitwise_not(ink), cv2.HOUGH_GRADIENT, dp=1.2,
            minDist=max(width, height) // 20, param1=120, param2=90,
            minRadius=max(6, min(width, height) // 60),
            maxRadius=min(width, height) // 2,
        )
    except Exception:  # noqa: BLE001 — measurement must never break the run
        return None
    if circles is None or len(circles[0]) == 0:
        return None
    return float(max(c[2] for c in circles[0]))


def check_outline_against_image(
    spec: dict, dominant_radius_px: float | None
) -> list[CrossCheckFinding]:
    """Does the outline the reader claims match the one the sheet draws?

    Only meaningful for a round part, and only when the image offers exactly one
    unambiguous circle. It calibrates millimetres per pixel from the claimed
    outer diameter and then asks whether the OTHER stated circles could fit
    inside what was measured — the check that would have caught a Ø80 bore read
    as Ø18, since a bore is not allowed to be larger than the outline it sits in
    once both are in the same units.

    What it deliberately does NOT do is ask "is the stated hole actually drawn?".
    Measured on the flange sheet, where a Ø80 bore is genuinely present and mm/px
    is known from the outline: at param2 60/45/35 the detector does not find the
    real bore at all — a correct reading would be flagged as wrong. At param2 25
    it finds it, but then it also reports 3 circles at Ø200 and 3 at Ø300, where
    the sheet draws none. A check that misses the true case at one threshold and
    hallucinates at the other carries no information, and a false alarm on a
    correct sheet is worse than no check. Left out until there is a detector that
    can answer it; the honest fallback is the impossible-hole test below.
    """
    findings: list[CrossCheckFinding] = []
    if not dominant_radius_px or dominant_radius_px <= 0:
        return findings
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    outer = _num(profile.get("diameter_mm"))
    if not outer or profile.get("shape") != "circle":
        return findings

    mm_per_px = outer / (2.0 * dominant_radius_px)
    for position, hole in enumerate(profile.get("holes") or []):
        if not isinstance(hole, dict):
            continue
        diameter = _num(hole.get("diameter_mm"))
        if not diameter:
            continue
        # A hole bigger than the outline is impossible; a hole below the
        # detector's own resolution cannot be confirmed and is left alone.
        if diameter >= outer:
            findings.append(CrossCheckFinding(
                code="hole_larger_than_measured_outline",
                message=(
                    f"отверстие Ø{diameter:g} не помещается в наружный контур "
                    f"Ø{outer:g}, измеренный на изображении"
                ),
                details={"hole": position, "mm_per_px": round(mm_per_px, 4)},
            ))
    return findings


def detect_axial_hatching(ink: Any) -> dict[str, Any] | None:
    """Does the sheet show a bounded band of ~45° parallel hatching?

    check_outline_against_image above names the exact gap this fills: "is
    the stated hole actually drawn?" was left unanswered there because
    circle detection cannot see it — that discussion is about a flange
    (round in plan, bore visible as a concentric circle). A body of
    revolution drawn in LONGITUDINAL section (a shaft — detal_126.png,
    2026-08-14's live-reproduced case) shows its bore as ANSI31-style
    parallel diagonal hatching inside a channel along the axis, not a
    circle in the main view; nothing in this codebase looked for that
    signal before, so an empty bore[] from the reader was indistinguishable
    from a genuinely solid part unless the reader also happened to notice
    a section VIEW exists (see cad_solid.py's has_section/solid_build_gate).

    Narrow and honest like this module's other measuring tools: this finds
    a bounded channel of near-45° parallel segments (not running along the
    whole outline, which would be a frame/border artifact, not hatching)
    and reports presence/count/extent only — no attempt to derive a bore
    diameter or depth from it. V1: presence/absence evidence, not
    reconstruction.
    """
    import cv2
    import numpy as np

    try:
        height, width = ink.shape[:2]
        lines = cv2.HoughLinesP(
            ink, 1, np.pi / 180, threshold=25,
            minLineLength=max(8, min(height, width) // 40),
            maxLineGap=4,
        )
    except Exception:  # noqa: BLE001 — measurement must never break the run
        return None
    if lines is None or len(lines) == 0:
        return None
    # ANSI31 hatching is conventionally drawn at 45°; keep both diagonals
    # since a draftsman may lean either way.
    hatch_segments = []
    for line in lines[:, 0]:
        x1, y1, x2, y2 = (float(v) for v in line)
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        if 30 <= angle <= 60 or 120 <= angle <= 150:
            hatch_segments.append((x1, y1, x2, y2))
    # A handful of stray diagonal lines (a chamfer edge, a leader line) is
    # noise, not hatching — real section fill draws many closely-spaced
    # parallel strokes. Threshold picked to require a genuine fill pattern,
    # not tuned against a single sheet (see this module's own docstring on
    # why a false alarm on a correct sheet is worse than no check).
    if len(hatch_segments) < 12:
        return None
    xs = [v for seg in hatch_segments for v in (seg[0], seg[2])]
    ys = [v for seg in hatch_segments for v in (seg[1], seg[3])]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    # Bounded, not the whole sheet — a real bore hatch is a band inside the
    # part, not a border/frame artifact spanning the entire drawing.
    if (bbox[2] - bbox[0]) > 0.9 * width and (bbox[3] - bbox[1]) > 0.9 * height:
        return None
    return {
        "segment_count": len(hatch_segments),
        "bbox_px": [round(v, 1) for v in bbox],
    }


def check_axial_hatching_against_bore(
    spec: dict, hatching: dict[str, Any] | None
) -> list[CrossCheckFinding]:
    """Cross-check detected section hatching against the read bore[].

    Only meaningful once the reader has committed to a body of revolution
    with an outer[] profile — a hatching signal means nothing yet for a
    part the reader hasn't even placed a profile on.
    """
    findings: list[CrossCheckFinding] = []
    body = spec.get("main_view") or {}
    if not (body.get("outer") or []):
        return findings
    bore = body.get("bore") or []
    if hatching and not bore:
        findings.append(CrossCheckFinding(
            code="axial_hatching_bore_mismatch",
            message=(
                "на чертеже обнаружена штриховка разреза, похожая на "
                "полость, но bore[] пусто — деталь будет построена "
                "сплошной. Проверьте разрез и, если полость есть, "
                "добавьте секцию bore в редакторе спецификации."
            ),
            severity="error",
            details=hatching,
        ))
    elif not hatching and bore:
        # A weaker signal on purpose: the detector's own false-negative
        # rate is unmeasured against a broad corpus (see its docstring) —
        # a real bore not being found here must not read as proof the bore
        # is wrong.
        findings.append(CrossCheckFinding(
            code="no_axial_hatching_for_stated_bore",
            message=(
                "bore[] заполнено, но штриховка разреза на изображении не "
                "обнаружена детектором — это не доказательство ошибки, "
                "детектор может просто не найти реальную штриховку"
            ),
            severity="warn",
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
    dominant: float | None = None
    if ink is not None:
        measured = measure_circle_radii(ink)
        findings.extend(check_spec_against_raster(spec, measured))
        dominant = measure_dominant_circle_px(ink)
        findings.extend(check_outline_against_image(spec, dominant))
        findings.extend(
            check_axial_hatching_against_bore(spec, detect_axial_hatching(ink))
        )
    stated_circles = len(_spec_circle_diameters(spec))
    raster_state = (
        "not_attempted" if ink is None
        else ("checked" if len(measured) >= 2 and stated_circles >= 2
              else "insufficient_measurements")
    )
    view_correspondence = check_view_correspondence(spec)
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
        "dominant_circle_px": round(dominant, 1) if dominant else None,
        # Whether the raster comparison actually ran. Without this, "errors: 0"
        # reads as "the image agrees" when it can equally mean "there was
        # nothing to compare".
        "raster_check": raster_state,
        # Фаза 1.1: cross-view feature correspondence — the deterministic
        # source for same_object_across_views edges in the native EMG graph
        # (see cad_emg_native.py). Empty until a view's features_shown is
        # populated; never a guessed link.
        "view_correspondence": view_correspondence,
    }
