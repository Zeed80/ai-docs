"""Deterministic engineering validation of a CAD IR (Ф7: formalized 7-level
assurance pipeline, per the external critique's ordering).

Same "rule table + typed codes" pattern as ``techdraw_validate`` and
``normcontrol_agent``, applied to recognized/edited IR geometry. ``validate_ir``
runs the DETERMINISTIC levels (1-5) after every recognition pass and every
review/editor patch — cheap, synchronous, no LLM. Levels 6-7 (LLM
normcontrol + VLM visual critique) are a SEPARATE, explicitly opt-in async
step (``run_llm_review_levels`` in this module) — never invoked
automatically on a routine edit, only on demand (e.g. before acceptance),
matching the critique's "verification stronger than generation, LLM/VLM
strictly at the end" principle: the deterministic levels have already run
and caught what they can before any model is asked for an opinion.

Levels:
  1 схема/валидность IR       — SCALE_UNKNOWN (IR completeness)
  2 геометрия                 — GEOM_*
  3 точные размеры/цепи       — DIM_CHAIN_MISMATCH, RA_INVALID
  4 ЕСКД-оформление           — ESKD_*
  5 технологичность           — TECH_* (reserved, no rules registered yet)
  6 normcontrol (LLM)         — NORMCONTROL_LLM (opt-in, run_llm_review_levels)
  7 VLM-критик                — VLM_CRITIC (opt-in, run_llm_review_levels)
Level 0 is not part of this ladder — recognition-quality signals
(COVERAGE_LOW, NEURAL_UNAVAILABLE, RECOGNIZER_DISCREPANCY, LOW_CONFIDENCE,
DIFFUSION_*) measure how much to trust the INPUT, not the drawing's
engineering correctness.
"""

from __future__ import annotations

import re
from enum import Enum

import structlog

from app.ai.cad_ir.schema import (
    CadIR,
    Circle,
    Polyline,
    ReviewItem,
    Segment,
    ValidationIssueIR,
    ValidationReportIR,
)

logger = structlog.get_logger()

# Entities below this confidence are queued for human review.
REVIEW_CONFIDENCE_THRESHOLD = 0.7

_DUPLICATE_TOL_PX = 2.0
_DANGLING_TOL_PX = 3.0
_MIN_SEGMENT_LEN_PX = 3.0


class CadCheckCode(str, Enum):
    GEOM_SELF_INTERSECTION = "GEOM_SELF_INTERSECTION"
    GEOM_DUPLICATE = "GEOM_DUPLICATE"
    GEOM_DEGENERATE = "GEOM_DEGENERATE"
    GEOM_OPEN_CONTOUR = "GEOM_OPEN_CONTOUR"
    ESKD_LINE_WEIGHT = "ESKD_LINE_WEIGHT"
    ESKD_SCALE_NONSTANDARD = "ESKD_SCALE_NONSTANDARD"
    SCALE_UNKNOWN = "SCALE_UNKNOWN"
    SCALE_UNVERIFIED = "SCALE_UNVERIFIED"
    COVERAGE_LOW = "COVERAGE_LOW"
    NEURAL_UNAVAILABLE = "NEURAL_UNAVAILABLE"
    RECOGNIZER_DISCREPANCY = "RECOGNIZER_DISCREPANCY"
    # B1: recognition returned no vector geometry; the sheet shipped as a
    # raster-passthrough draft for manual review/tracing instead of failing.
    RECOGNITION_EMPTY = "RECOGNITION_EMPTY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    # pixel provenance of diffusion-prepared sources (sticky across revalidation)
    DIFFUSION_ADDED_INK = "DIFFUSION_ADDED_INK"
    DIFFUSION_REMOVED_INK = "DIFFUSION_REMOVED_INK"
    DIFFUSION_SOURCE_UNVERIFIED = "DIFFUSION_SOURCE_UNVERIFIED"
    # reserved for later phases (VLM dimensions / manufacturability)
    DIM_CHAIN_MISMATCH = "DIM_CHAIN_MISMATCH"
    RA_INVALID = "RA_INVALID"
    TECH_RULE = "TECH_RULE"
    CONSTRAINT_UNSATISFIED = "CONSTRAINT_UNSATISFIED"
    CONSTRAINT_REFERENCE_INVALID = "CONSTRAINT_REFERENCE_INVALID"
    # level 6/7 additions (Ф7)
    NORMCONTROL_LLM = "NORMCONTROL_LLM"
    VLM_CRITIC = "VLM_CRITIC"
    # Ф7.3: full ЕСКД checker (formats/title block/line types)
    ESKD_SHEET_FORMAT_UNKNOWN = "ESKD_SHEET_FORMAT_UNKNOWN"
    ESKD_TITLE_BLOCK_INCOMPLETE = "ESKD_TITLE_BLOCK_INCOMPLETE"
    ESKD_NO_CONTOUR_GEOMETRY = "ESKD_NO_CONTOUR_GEOMETRY"
    # C2: extended ЕСКД profile coverage
    ESKD_TEXT_HEIGHT = "ESKD_TEXT_HEIGHT"  # ГОСТ 2.304 font height series
    ESKD_DIMENSION_INCOMPLETE = "ESKD_DIMENSION_INCOMPLETE"  # ГОСТ 2.307 value present
    # C4: structured annotation validity (roughness/thread/tolerance/datum)
    ESKD_ANNOTATION_INVALID = "ESKD_ANNOTATION_INVALID"


# Assurance-pipeline level per code (Ф7.1) — see module docstring for the
# 7-level ordering. Codes not listed here default to level 0 (recognition
# quality, not an engineering-correctness level).
_CHECK_LEVEL: dict[str, int] = {
    "SCALE_UNKNOWN": 1,
    "SCALE_UNVERIFIED": 1,
    "GEOM_SELF_INTERSECTION": 2,
    "GEOM_DUPLICATE": 2,
    "GEOM_DEGENERATE": 2,
    "GEOM_OPEN_CONTOUR": 2,
    "DIM_CHAIN_MISMATCH": 3,
    "RA_INVALID": 3,
    "ESKD_LINE_WEIGHT": 4,
    "ESKD_SCALE_NONSTANDARD": 4,
    "ESKD_SHEET_FORMAT_UNKNOWN": 4,
    "ESKD_TITLE_BLOCK_INCOMPLETE": 4,
    "ESKD_NO_CONTOUR_GEOMETRY": 4,
    "ESKD_TEXT_HEIGHT": 4,
    "ESKD_DIMENSION_INCOMPLETE": 3,
    "ESKD_ANNOTATION_INVALID": 3,
    "TECH_RULE": 5,
    "CONSTRAINT_UNSATISFIED": 3,
    "CONSTRAINT_REFERENCE_INVALID": 3,
    "NORMCONTROL_LLM": 6,
    "VLM_CRITIC": 7,
}


# ГОСТ 2.302 standard scales (drawing:real), as scale factors
_GOST_SCALES = (
    0.01, 0.02, 0.025, 0.04, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 1.0,
    2.0, 2.5, 4.0, 5.0, 10.0, 20.0, 25.0, 40.0, 50.0, 100.0,
)


# Ф9/C2: which ГОСТ each check enforces — resolved from the versioned ЕСКД
# rule profile (app.ai.eskd_profile). The plain citation is always attached;
# app.ai.norm_citation additionally resolves it against ingested
# NormativeDocument/NormativeClause rows when the corpus has that standard.
# Codes not in the profile (geometry/scale-completeness) fall back to these
# bare citations.
_NORM_REF: dict[str, str] = {
    "RA_INVALID": "ГОСТ 2789-73",
}


def _issue(code: CadCheckCode, severity: str, message: str, entity_ids: list[str] | None = None) -> ValidationIssueIR:
    """A non-profile issue (geometry, scale completeness, recognition
    signals). ЕСКД-profile checks use ``eskd_issue`` so their rule_id/
    fix_hint/citation travel from the single registry."""
    return ValidationIssueIR(
        code=code.value, severity=severity, message_ru=message, entity_ids=entity_ids or [],
        level=_CHECK_LEVEL.get(code.value, 0),
        norm_ref=_NORM_REF.get(code.value),
    )


def eskd_issue(
    code: CadCheckCode,
    message: str,
    entity_ids: list[str] | None = None,
    severity: str | None = None,
) -> ValidationIssueIR:
    """An issue backed by the versioned ЕСКД rule profile: rule_id, fix path,
    citation, clause and level all come from the registry entry for ``code``.
    ``severity`` overrides the rule default only when a check needs to."""
    from app.ai.eskd_profile import rule_for

    rule = rule_for(code.value)
    if rule is None:  # defensive: a profile code with no registry entry
        return _issue(code, severity or "warn", message, entity_ids)
    return ValidationIssueIR(
        code=code.value,
        severity=severity or rule.default_severity,
        message_ru=message,
        entity_ids=entity_ids or [],
        level=rule.level,
        # Bare GOST citation so norm_citation.resolve_norm_citations (which
        # strips a trailing year and prefix-matches the ingested corpus) still
        # finds the document. The specific clause lives in the rule registry
        # and travels via rule_id; the fix_hint gives the actionable path.
        norm_ref=rule.gost,
        rule_id=rule.rule_id,
        fix_hint=rule.fix_hint,
    )


def _check_scale(ir: CadIR) -> list[ValidationIssueIR]:
    if ir.scale is None:
        return [_issue(
            CadCheckCode.SCALE_UNKNOWN, "error",
            "Масштаб не определён — размеры в DXF будут в условных единицах (пикселях). "
            "Укажите масштаб вручную или добавьте рамку формата.",
        )]
    if ir.scale_source is None:
        return [_issue(
            CadCheckCode.SCALE_UNVERIFIED, "error",
            "Метрический масштаб не имеет подтверждённого источника. "
            "Укажите мм/px вручную или подтвердите формат листа.",
        )]
    return []


def _check_degenerate(ir: CadIR) -> list[ValidationIssueIR]:
    issues = []
    for e in ir.entities:
        if isinstance(e, Segment):
            length = ((e.p1.x - e.p2.x) ** 2 + (e.p1.y - e.p2.y) ** 2) ** 0.5
            if length < _MIN_SEGMENT_LEN_PX:
                issues.append(_issue(
                    CadCheckCode.GEOM_DEGENERATE, "error",
                    f"Вырожденный отрезок длиной {length:.1f}px", [e.id],
                ))
    return issues


def _check_duplicates(ir: CadIR) -> list[ValidationIssueIR]:
    issues = []
    segments = [e for e in ir.entities if isinstance(e, Segment)]
    for i, a in enumerate(segments):
        for b in segments[i + 1:]:
            same = (
                abs(a.p1.x - b.p1.x) <= _DUPLICATE_TOL_PX and abs(a.p1.y - b.p1.y) <= _DUPLICATE_TOL_PX
                and abs(a.p2.x - b.p2.x) <= _DUPLICATE_TOL_PX and abs(a.p2.y - b.p2.y) <= _DUPLICATE_TOL_PX
            ) or (
                abs(a.p1.x - b.p2.x) <= _DUPLICATE_TOL_PX and abs(a.p1.y - b.p2.y) <= _DUPLICATE_TOL_PX
                and abs(a.p2.x - b.p1.x) <= _DUPLICATE_TOL_PX and abs(a.p2.y - b.p1.y) <= _DUPLICATE_TOL_PX
            )
            if same:
                issues.append(_issue(
                    CadCheckCode.GEOM_DUPLICATE, "error",
                    "Дублирующиеся отрезки — в CAD останутся наложенные линии",
                    [a.id, b.id],
                ))
    return issues


def _check_self_intersections(ir: CadIR) -> list[ValidationIssueIR]:
    try:
        from shapely.geometry import LineString
    except ImportError:
        return []
    issues = []
    for e in ir.entities:
        if isinstance(e, Polyline) and len(e.points) >= 3:
            coords = [(p.x, p.y) for p in e.points]
            if e.closed:
                coords.append(coords[0])
            line = LineString(coords)
            if not line.is_simple:
                issues.append(_issue(
                    CadCheckCode.GEOM_SELF_INTERSECTION, "error",
                    "Полилиния самопересекается — контур некорректен", [e.id],
                ))
    return issues


def _check_closed_contours(ir: CadIR) -> list[ValidationIssueIR]:
    """Hatch boundaries must be closed regions; a hatch over an open contour
    is a broken drawing. (Full open-contour analysis of the whole sheet needs
    dimension semantics — later phase.)"""
    issues = []
    for e in ir.entities:
        if e.type == "hatch" and len(e.boundary) < 3:
            issues.append(_issue(
                CadCheckCode.GEOM_OPEN_CONTOUR, "error",
                "Штриховка без замкнутой границы", [e.id],
            ))
    return issues


def _check_line_weights(ir: CadIR) -> list[ValidationIssueIR]:
    """ГОСТ 2.303: contours are drawn with the main weight, auxiliary lines
    (axis/dim/hatch) with the thin one."""
    issues = []
    wrong = [
        e.id for e in ir.entities
        if e.line_class in ("axis", "dim", "hatch") and e.width_class == "main"
    ]
    if wrong:
        issues.append(eskd_issue(
            CadCheckCode.ESKD_LINE_WEIGHT,
            "Осевые/размерные/штриховые линии должны быть тонкими (ГОСТ 2.303)",
            wrong,
        ))
    return issues


def _check_coverage(ir: CadIR) -> list[ValidationIssueIR]:
    rec = ir.validation.coverage_recall
    prec = ir.validation.coverage_precision
    if rec is None or prec is None:
        return []
    if rec < 0.85 or prec < 0.85:
        return [_issue(
            CadCheckCode.COVERAGE_LOW, "error",
            f"Распознанная геометрия покрывает исходник недостаточно "
            f"(recall {rec:.0%}, precision {prec:.0%}) — результат требует ручной проверки",
        )]
    return []


def _check_dimension_chains(ir: CadIR) -> list[ValidationIssueIR]:
    from app.ai.cad_hypothesis import check_dimension_chains

    return [_issue(CadCheckCode.DIM_CHAIN_MISMATCH, "warn", msg) for msg in check_dimension_chains(ir)]


def _check_constraints(ir: CadIR) -> list[ValidationIssueIR]:
    from app.ai.cad_ir.constraints import evaluate_constraints

    issues: list[ValidationIssueIR] = []
    for result in evaluate_constraints(ir):
        if result.ok:
            continue
        code = (
            CadCheckCode.CONSTRAINT_REFERENCE_INVALID
            if "ссыл" in result.message or "не найден" in result.message or "применим" in result.message
            else CadCheckCode.CONSTRAINT_UNSATISFIED
        )
        issues.append(_issue(code, "error", f"Ограничение {result.constraint_id}: {result.message}", list(result.entity_ids)))
    return issues


_RA_PATTERN = re.compile(r"\bRa\s*([\d.,]+)", re.IGNORECASE)


def _check_roughness_values(ir: CadIR) -> list[ValidationIssueIR]:
    """Level 3: any 'Ra <value>' callout (dimension or free text) should be
    a value from the standard series (ГОСТ 2789) — a non-standard number is
    almost always a misread digit or a typo, not a deliberate choice."""
    from app.ai import techdraw_reference as tdref

    issues = []
    for e in ir.entities:
        text = getattr(e, "text", None)
        if not text:
            continue
        m = _RA_PATTERN.search(text)
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        nearest = tdref.nearest_ra(value)
        if abs(nearest - value) > 1e-6:
            issues.append(eskd_issue(
                CadCheckCode.RA_INVALID,
                f"Ra {value:g} не входит в стандартный ряд ГОСТ 2789 (ближайшее — Ra {nearest:g})",
                [e.id],
            ))
    return issues


_SCALE_RATIO_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")
_SCALE_TOLERANCE = 1e-3


def _check_scale_standard(ir: CadIR) -> list[ValidationIssueIR]:
    """Level 4: a stated drawing scale (e.g. the title block's "М 1:2"
    field, ГОСТ 2.109) must be one of the ГОСТ 2.302 standard ratios — this
    is metadata a human enters (or the title-block OCR reads), distinct from
    ``ir.scale`` (the px→mm measurement factor used for rendering/export)."""
    stated = (ir.sheet.title_block or {}).get("scale")
    if not isinstance(stated, str):
        return []
    m = _SCALE_RATIO_PATTERN.match(stated)
    if not m:
        return []
    num, den = float(m.group(1)), float(m.group(2))
    if den == 0:
        return []
    ratio = num / den
    if any(abs(ratio - g) <= _SCALE_TOLERANCE * max(g, 1e-9) for g in _GOST_SCALES):
        return []
    return [eskd_issue(
        CadCheckCode.ESKD_SCALE_NONSTANDARD,
        f"Масштаб {stated} не входит в стандартный ряд ГОСТ 2.302",
    )]


_GOST_SHEET_FORMATS = frozenset({"A0", "A1", "A2", "A3", "A4"})


def _check_sheet_format(ir: CadIR) -> list[ValidationIssueIR]:
    """Level 4: a framed (ЕСКД-formatted) sheet should state a recognized
    ГОСТ 2.301 format — informational only (unlike a wrong scale ratio, an
    unusual/unset format doesn't misrepresent anything, it's just unstated
    metadata a consumer downstream might need)."""
    if not ir.sheet.frame:
        return []
    if ir.sheet.format in _GOST_SHEET_FORMATS:
        return []
    return [eskd_issue(
        CadCheckCode.ESKD_SHEET_FORMAT_UNKNOWN,
        f"Формат листа {ir.sheet.format or 'не указан'} не входит в стандартный ряд ГОСТ 2.301 (A0-A4)",
    )]


def _check_title_block_complete(ir: CadIR) -> list[ValidationIssueIR]:
    """Level 4: when the sheet is framed (a ГОСТ 2.104 stamp area exists),
    that area should actually contain identifying text — an empty detected
    stamp region means the основная надпись was never filled in, not just
    that the frame is missing.

    Severity is deliberately "info", not "warn": "draw first, fill the
    stamp in later" is a completely normal, common editing order (frame
    added at blank-sheet creation, name/designation typed in only once the
    drawing itself is done) — this would otherwise be a PERSISTENT warning
    on every single revision for as long as that's true, well before the
    user did anything wrong. It still shows up (info doesn't disappear),
    it just doesn't compete for attention with a real ЕСКД violation."""
    if not ir.sheet.frame:
        return []
    tb = ir.sheet.title_block or {}
    # C3: when structured fields exist, judge completeness by the two
    # mandatory ГОСТ 2.104 identifiers (designation + name) rather than by
    # "any text somewhere in the region".
    fields = tb.get("fields")
    if isinstance(fields, dict):
        if str(fields.get("designation") or "").strip() and str(fields.get("name") or "").strip():
            return []
        return [eskd_issue(
            CadCheckCode.ESKD_TITLE_BLOCK_INCOMPLETE,
            "Основная надпись: не заполнены обозначение и/или наименование",
        )]
    region = tb.get("region")
    if not isinstance(region, dict):
        return []
    x0, y0, x1, y1 = region.get("x0"), region.get("y0"), region.get("x1"), region.get("y1")
    if None in (x0, y0, x1, y1):
        return []
    has_text = any(
        e.type == "text" and x0 <= e.position.x <= x1 and y0 <= e.position.y <= y1
        for e in ir.entities
    )
    if has_text:
        return []
    return [eskd_issue(
        CadCheckCode.ESKD_TITLE_BLOCK_INCOMPLETE,
        "Основная надпись (штамп) пуста — не заполнены наименование/обозначение",
    )]


def _check_contour_geometry(ir: CadIR) -> list[ValidationIssueIR]:
    """Level 4: a drawing with zero main-weight contour geometry (only
    auxiliary/dimension/text/hatch entities) is almost certainly incomplete
    — nothing representing the object itself was actually drawn."""
    if not ir.entities:
        return []  # an untouched blank sheet isn't "malformed", just not started
    has_contour = any(
        e.line_class == "contour" and e.width_class == "main"
        for e in ir.entities
        if e.type in ("segment", "arc", "circle", "polyline")
    )
    if has_contour:
        return []
    return [eskd_issue(
        CadCheckCode.ESKD_NO_CONTOUR_GEOMETRY,
        "На листе нет основной контурной геометрии — только вспомогательные элементы",
    )]


_TEXT_HEIGHT_TOL_MM = 0.35  # OCR/rounding slack around the nominal series


def _check_text_heights(ir: CadIR) -> list[ValidationIssueIR]:
    """C2 / ГОСТ 2.304: text/dimension label height should be a nominal font
    size from the standard series. Only meaningful with a known scale (px→mm),
    so it's silent on an unscaled draft. Info severity — a slightly-off height
    is a formatting nit, not a geometry error."""
    from app.ai.eskd_profile import GOST_2304_TEXT_HEIGHTS_MM

    if ir.scale is None:
        return []
    issues = []
    for e in ir.entities:
        height_px = getattr(e, "height", None)
        if not height_px or e.type not in ("text", "dimension"):
            continue
        height_mm = height_px * ir.scale
        if height_mm < 1.0:  # too small to have been read reliably
            continue
        nearest = min(GOST_2304_TEXT_HEIGHTS_MM, key=lambda h: abs(h - height_mm))
        if abs(nearest - height_mm) > _TEXT_HEIGHT_TOL_MM:
            issues.append(eskd_issue(
                CadCheckCode.ESKD_TEXT_HEIGHT,
                f"Высота шрифта {height_mm:.1f} мм не из ряда ГОСТ 2.304 "
                f"(ближайшая — {nearest:g} мм)",
                [e.id],
            ))
    return issues


def _check_annotations(ir: CadIR) -> list[ValidationIssueIR]:
    """C4 / ГОСТ 2.308: each structured annotation (roughness/thread/geometric
    tolerance/datum) must be valid per its standard."""
    from app.ai.cad_ir.annotations import validate_annotation

    issues = []
    for e in ir.entities:
        if e.type != "annotation":
            continue
        ok, message = validate_annotation(e)
        if not ok:
            issues.append(eskd_issue(
                CadCheckCode.ESKD_ANNOTATION_INVALID, message or "Некорректная аннотация", [e.id],
            ))
    return issues


def _check_dimension_complete(ir: CadIR) -> list[ValidationIssueIR]:
    """C2 / ГОСТ 2.307: a dimension entity must carry a numeric value — a
    dimension line with neither a parsed value nor any label text is an
    incomplete размер that cannot cross the release boundary."""
    issues = []
    for e in ir.entities:
        if e.type != "dimension":
            continue
        has_value = getattr(e, "value_mm", None) is not None
        has_text = bool((getattr(e, "text", "") or "").strip())
        if not has_value and not has_text:
            issues.append(eskd_issue(
                CadCheckCode.ESKD_DIMENSION_INCOMPLETE,
                "Размер без числового значения (ГОСТ 2.307)",
                [e.id],
            ))
    return issues


_CHECKS = (
    _check_scale,
    _check_degenerate,
    _check_duplicates,
    _check_self_intersections,
    _check_closed_contours,
    _check_line_weights,
    _check_scale_standard,
    _check_sheet_format,
    _check_title_block_complete,
    _check_contour_geometry,
    _check_text_heights,
    _check_dimension_complete,
    _check_annotations,
    _check_roughness_values,
    _check_coverage,
    _check_dimension_chains,
    _check_constraints,
)


def validate_ir(ir: CadIR) -> ValidationReportIR:
    """Run all checks; store and return the report. Also (re)builds the
    review queue: entities under the confidence threshold plus entities named
    by error-severity issues."""
    # Provenance findings are produced once by the pipeline, not re-derivable
    # from the IR alone — carry them through every revalidation until the
    # flagged entities are gone.
    sticky_issues = [
        i
        for i in ir.validation.issues
        if i.code.startswith("DIFFUSION_")
        and (not i.entity_ids or any(ir.entity_by_id(eid) for eid in i.entity_ids))
    ]
    issues: list[ValidationIssueIR] = list(sticky_issues)
    for check in _CHECKS:
        issues.extend(check(ir))

    from app.ai.eskd_profile import ESKD_PROFILE_VERSION

    report = ValidationReportIR(
        issues=issues,
        coverage_recall=ir.validation.coverage_recall,
        coverage_precision=ir.validation.coverage_precision,
        eskd_profile_version=ESKD_PROFILE_VERSION,
    )
    ir.validation = report

    resolved = {r.entity_id for r in ir.review if r.resolved}
    # Sticky non-auto reasons (e.g. diffusion_modified from the provenance
    # mask) survive revalidation — only their producer or a human resolves them.
    sticky = [r for r in ir.review if not r.resolved and r.reason not in ("low_confidence", "validation_error")]
    review: list[ReviewItem] = [r for r in ir.review if r.resolved] + sticky
    sticky_ids = {r.entity_id for r in sticky}
    for e in ir.entities:
        if e.id in resolved or e.id in sticky_ids or e.assurance == "human_approved":
            continue
        if e.confidence < REVIEW_CONFIDENCE_THRESHOLD:
            review.append(ReviewItem(entity_id=e.id, reason="low_confidence"))
    flagged = {
        eid
        for issue in issues
        if issue.severity == "error"
        for eid in issue.entity_ids
    }
    queued = {r.entity_id for r in review}
    for eid in flagged - queued - resolved:
        review.append(ReviewItem(entity_id=eid, reason="validation_error"))
    ir.review = review

    logger.info(
        "cad_validate",
        issues=len(issues),
        errors=len(report.blocking),
        review_pending=sum(1 for r in review if not r.resolved),
    )
    return report


_LLM_REVIEW_PROMPT = """Ты — нормоконтролёр технических чертежей, последний шаг конвейера
проверки (уровни 6-7). ВСЕ детерминированные проверки уже пройдены до тебя: геометрия,
размерные цепи, типы линий по ГОСТ 2.303, масштаб, шероховатость. Ищи только то, что они
принципиально не могут поймать:
- уровень 6 (формальный нормоконтроль ЕСКД): нелогичные/противоречивые обозначения,
  отсутствующие обязательные элементы там, где они явно нужны, несоответствие оформления
  ЕСКД, которое не сводится к геометрии
- уровень 7 (визуальная критика): деталь физически не может выглядеть так, как нарисована;
  явные визуальные аномалии, нестыковки между элементами

Верни СТРОГО JSON без markdown-блоков:
{"issues": [{"level": 6, "severity": "warn", "message": "конкретная формулировка проблемы"}]}

level: 6 или 7. severity: "error"|"warn"|"info".
Если чертёж выглядит нормально — верни {"issues": []}. НЕ придумывай проблем, которых нет —
ложное срабатывание отвлекает инженера от реальных ошибок не меньше, чем пропуск настоящей."""


def _parse_llm_review(raw_text: str) -> list[ValidationIssueIR]:
    from app.ai.drawing_extractor import _parse_json_response

    parsed = _parse_json_response(raw_text)
    if not isinstance(parsed, dict):
        return []
    raw_issues = parsed.get("issues")
    if not isinstance(raw_issues, list):
        return []
    out: list[ValidationIssueIR] = []
    for item in raw_issues:
        if not isinstance(item, dict) or not item.get("message"):
            continue
        level = 7 if item.get("level") == 7 else 6
        code = CadCheckCode.VLM_CRITIC if level == 7 else CadCheckCode.NORMCONTROL_LLM
        severity = item.get("severity") if item.get("severity") in ("error", "warn", "info") else "warn"
        out.append(_issue(code, severity, str(item["message"])[:500]))
    return out


async def run_llm_review_levels(
    png_bytes: bytes,
    *,
    router: "object | None" = None,
    confidential: bool = True,
) -> list[ValidationIssueIR]:
    """Levels 6-7 of the assurance pipeline (Ф7.1): LLM-based ЕСКД
    normcontrol + VLM visual critique over the rendered drawing.

    Explicitly OPT-IN and separate from ``validate_ir`` — never called on a
    routine PATCH (that stays deterministic and instant, per the module's
    design). Callers run this on demand (e.g. before acceptance) and splice
    the result into ``ir.validation.issues`` themselves; a subsequent
    ``validate_ir`` call drops these again since they're a point-in-time
    judgement about a specific render, not a fact re-derivable from the IR —
    correct behavior, not a bug: if the drawing changed, the old critique
    may no longer apply and must be re-run.

    Never raises — degrades to ``[]`` on any failure, same policy as
    ``vlm_dimensions``: a flaky/unavailable model must not block anything a
    human hasn't asked it to gate.
    """
    import base64

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[
            ChatMessage(role="system", content=_LLM_REVIEW_PROMPT),
            ChatMessage(role="user", content="Проверь этот чертёж."),
        ],
        images=[base64.b64encode(png_bytes).decode()],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
        issues = _parse_llm_review(response.text or "")
        logger.info("cad_llm_review", issues=len(issues))
        return issues
    except Exception as exc:  # noqa: BLE001 — a bad LLM call must not fail the caller
        logger.warning("cad_llm_review_failed", error=str(exc)[:200])
        return []
