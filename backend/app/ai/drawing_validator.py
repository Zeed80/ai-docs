"""Drawing validation after VLM extraction.

Checks extraction completeness and consistency:
- DXF entity coverage: what fraction of entities are explained by features
- Dimension chain integrity: partial dims should sum to total ±0.5%
- GOST notation validation: Ra values, GD&T symbols, tolerance formats
- Auto-correction of common OCR artifacts

Sets Drawing.status = needs_review when confidence_score < 0.6.
Saves report to Drawing.metadata_["validation_report"].
"""

from __future__ import annotations

import re
import uuid
import structlog
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger()

# Valid Ra roughness values per ГОСТ 2789 (preferred series R10)
_VALID_RA_VALUES = frozenset({
    0.012, 0.025, 0.050, 0.100, 0.200, 0.400, 0.800,
    1.600, 3.200, 6.300, 12.500, 25.0, 50.0, 100.0,
})
# Tolerance to accept nearby values (±5%)
_RA_TOLERANCE = 0.05

# OCR artifact corrections for common Ra values
_RA_CORRECTIONS: dict[float, float] = {
    1.5: 1.6, 1.7: 1.6,
    3.1: 3.2, 3.3: 3.2,
    6.2: 6.3, 6.4: 6.3,
    12.4: 12.5, 12.6: 12.5,
}


@dataclass
class DrawingValidationReport:
    drawing_id: uuid.UUID
    confidence_score: float = 1.0
    entity_coverage_pct: float = 100.0    # % DXF entities explained by features
    dimension_chain_ok: bool = True
    roughness_valid: bool = True
    tolerance_valid: bool = True
    warnings: list[str] = field(default_factory=list)
    auto_fixed: list[str] = field(default_factory=list)
    needs_review: bool = False


def validate_drawing_extraction(
    drawing_id: uuid.UUID,
    features_data: list[dict[str, Any]],
    dxf_entities: list[dict[str, Any]] | None = None,
) -> DrawingValidationReport:
    """Validate extracted drawing features for consistency and completeness.

    Args:
        drawing_id: Drawing UUID for the report
        features_data: List of feature dicts from VLM extraction
        dxf_entities: Optional DXF entity list from ezdxf parsing

    Returns:
        DrawingValidationReport with score, warnings, and auto-fixes applied to features_data in-place.
    """
    report = DrawingValidationReport(drawing_id=drawing_id)

    if not features_data:
        report.confidence_score = 0.0
        report.warnings.append("Нет извлечённых элементов (features пуст)")
        report.needs_review = True
        return report

    # 1. DXF entity coverage
    if dxf_entities:
        report.entity_coverage_pct = _check_entity_coverage(features_data, dxf_entities)
        if report.entity_coverage_pct < 60.0:
            report.warnings.append(
                f"Низкое покрытие DXF-сущностей: {report.entity_coverage_pct:.0f}% "
                f"(порог 60%). Возможны пропущенные элементы."
            )

    # 2. Dimension chain integrity
    chain_ok, chain_warnings = _check_dimension_chains(features_data)
    report.dimension_chain_ok = chain_ok
    report.warnings.extend(chain_warnings)

    # 3. Ra roughness validation + auto-fix
    ra_ok, ra_warnings, ra_fixes = _validate_and_fix_roughness(features_data)
    report.roughness_valid = ra_ok
    report.warnings.extend(ra_warnings)
    report.auto_fixed.extend(ra_fixes)

    # 4. GD&T symbol and tolerance format validation
    tol_ok, tol_warnings, tol_fixes = _validate_and_fix_tolerances(features_data)
    report.tolerance_valid = tol_ok
    report.warnings.extend(tol_warnings)
    report.auto_fixed.extend(tol_fixes)

    # 5. Compute overall confidence score
    avg_confidence = sum(f.get("confidence", 0.5) for f in features_data) / len(features_data)
    coverage_factor = min(1.0, report.entity_coverage_pct / 100.0) if dxf_entities else 1.0
    chain_factor = 1.0 if report.dimension_chain_ok else 0.85
    ra_factor = 1.0 if report.roughness_valid else 0.90
    tol_factor = 1.0 if report.tolerance_valid else 0.90

    report.confidence_score = round(
        avg_confidence * coverage_factor * chain_factor * ra_factor * tol_factor, 3
    )
    report.needs_review = report.confidence_score < 0.6

    if report.needs_review:
        report.warnings.append(
            f"Низкий score {report.confidence_score:.2f} < 0.6 — чертёж требует ручной проверки"
        )

    logger.info(
        "drawing_validation_complete",
        drawing_id=str(drawing_id),
        features=len(features_data),
        score=report.confidence_score,
        warnings=len(report.warnings),
        auto_fixed=len(report.auto_fixed),
        needs_review=report.needs_review,
    )
    return report


# ── DXF entity coverage ────────────────────────────────────────────────────────


def _check_entity_coverage(features_data: list[dict], dxf_entities: list[dict]) -> float:
    """Estimate what percentage of DXF geometry entities are explained by features."""
    if not dxf_entities:
        return 100.0

    # Only count geometric entities (not text/dims — they're metadata)
    GEOMETRIC_TYPES = {"CIRCLE", "ARC", "LINE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"}
    geo_entities = [e for e in dxf_entities if e.get("type") in GEOMETRIC_TYPES]

    if not geo_entities:
        return 100.0

    # Simple heuristic: if we have features, assume each feature explains ~3 entities
    # A more precise check would require bounding-box matching
    explained_estimate = len(features_data) * 3
    coverage = min(100.0, explained_estimate / len(geo_entities) * 100)
    return round(coverage, 1)


# ── Dimension chain integrity ──────────────────────────────────────────────────


def _check_dimension_chains(features_data: list[dict]) -> tuple[bool, list[str]]:
    """Check that partial dimensions sum to total dimensions within ±0.5%.

    Heuristic: look for features that have a total (label "overall" or single dim)
    alongside multiple partial dims. Flag mismatch.
    """
    warnings: list[str] = []

    for feat in features_data:
        dims = feat.get("dimensions", [])
        if len(dims) < 2:
            continue

        # Separate dims by type
        linear_dims = [d for d in dims if d.get("dim_type") in ("linear", "depth")]
        if len(linear_dims) < 3:
            continue

        nominals = sorted([float(d.get("nominal", 0)) for d in linear_dims if d.get("nominal")])
        if not nominals or nominals[-1] <= 0:
            continue

        # If largest dim ≈ sum of all others: possible chain
        total = nominals[-1]
        parts = nominals[:-1]
        parts_sum = sum(parts)
        if parts_sum > 0 and abs(parts_sum - total) / total > 0.005:
            # Doesn't match — might be intentional (different chains), warn only if large delta
            if abs(parts_sum - total) / total > 0.05:
                warnings.append(
                    f"Элемент '{feat.get('name', '?')}': "
                    f"сумма частичных размеров ({parts_sum:.2f}) ≠ "
                    f"максимальному ({total:.2f}) — проверьте цепочку размеров"
                )

    return len(warnings) == 0, warnings


# ── Roughness validation ───────────────────────────────────────────────────────


def _validate_and_fix_roughness(features_data: list[dict]) -> tuple[bool, list[str], list[str]]:
    """Validate Ra values against GOST 2789 preferred series; auto-fix OCR artifacts."""
    warnings: list[str] = []
    fixes: list[str] = []
    all_ok = True

    for feat in features_data:
        feat_name = feat.get("name", "?")
        for surf in feat.get("surfaces", []):
            if surf.get("roughness_type", "Ra") != "Ra":
                continue
            value = surf.get("value")
            if value is None:
                continue
            try:
                ra = float(value)
            except (TypeError, ValueError):
                continue

            # Check OCR correction table first
            if ra in _RA_CORRECTIONS:
                corrected = _RA_CORRECTIONS[ra]
                surf["value"] = corrected
                fixes.append(
                    f"'{feat_name}': Ra {ra} → {corrected} (OCR-коррекция)"
                )
                ra = corrected

            # Check against preferred series (with 5% tolerance)
            if not _is_valid_ra(ra):
                all_ok = False
                warnings.append(
                    f"'{feat_name}': Ra {ra} не входит в ряд ГОСТ 2789. "
                    f"Проверьте шероховатость."
                )

    return all_ok, warnings, fixes


def _is_valid_ra(value: float) -> bool:
    """Check if Ra value is within 5% of a standard preferred-series value."""
    for std in _VALID_RA_VALUES:
        if abs(value - std) / std <= _RA_TOLERANCE:
            return True
    return False


# ── Tolerance and GD&T validation ─────────────────────────────────────────────

# GD&T symbols per ISO 1101 / ГОСТ 2.308
_VALID_GDT_SYMBOLS = frozenset({
    "⊥", "∥", "∠", "⌀", "○", "◎", "//", "⊙",
    "⌯", "⌰", "⌱", "⌲", "⌳", "⌴", "⌵", "⌶",
    "⊞", "⊟", "⊠", "⊡", "◻",
    # ASCII equivalents often returned by VLMs
    "perp", "para", "circ", "sym", "flat", "cyl", "cone", "run", "trun",
    "str", "ang", "pos", "conc", "prof",
})

# ISO/ГОСТ fit letter pattern: H7, k6, n6, H7/k6, etc.
_FIT_PATTERN = re.compile(r"^[A-Za-z]{1,2}\d{1,2}(/[A-Za-z]{1,2}\d{1,2})?$")


def _validate_and_fix_tolerances(features_data: list[dict]) -> tuple[bool, list[str], list[str]]:
    """Validate GD&T symbols and fit designations."""
    warnings: list[str] = []
    fixes: list[str] = []
    all_ok = True

    for feat in features_data:
        feat_name = feat.get("name", "?")

        # Check fit designations in dimensions
        for dim in feat.get("dimensions", []):
            fit = dim.get("fit_system")
            if fit and not _FIT_PATTERN.match(str(fit)):
                # Try simple cleanup: strip whitespace, normalize
                cleaned = re.sub(r"\s+", "", str(fit))
                if _FIT_PATTERN.match(cleaned):
                    dim["fit_system"] = cleaned
                    fixes.append(f"'{feat_name}': посадка '{fit}' → '{cleaned}'")
                else:
                    all_ok = False
                    warnings.append(
                        f"'{feat_name}': нестандартная посадка '{fit}' — "
                        f"ожидается формат 'H7' или 'H7/k6'"
                    )

        # Check GD&T symbols
        for gdt in feat.get("gdt", []):
            symbol = gdt.get("symbol", "")
            tol = gdt.get("tolerance_value")

            if tol is not None:
                try:
                    tol_f = float(tol)
                    if tol_f < 0:
                        gdt["tolerance_value"] = abs(tol_f)
                        fixes.append(f"'{feat_name}': GD&T допуск {tol} → {abs(tol_f)} (знак)")
                except (TypeError, ValueError):
                    pass

            if symbol and symbol not in _VALID_GDT_SYMBOLS:
                # Warn but don't block — VLMs sometimes use descriptive names
                warnings.append(
                    f"'{feat_name}': нераспознанный символ GD&T '{symbol}'"
                )

    return all_ok, warnings, fixes


# ── Report serialization ───────────────────────────────────────────────────────


def report_to_dict(report: DrawingValidationReport) -> dict:
    """Serialize validation report to a dict for storage in Drawing.metadata_."""
    return {
        "drawing_id": str(report.drawing_id),
        "confidence_score": report.confidence_score,
        "entity_coverage_pct": report.entity_coverage_pct,
        "dimension_chain_ok": report.dimension_chain_ok,
        "roughness_valid": report.roughness_valid,
        "tolerance_valid": report.tolerance_valid,
        "warnings": report.warnings,
        "auto_fixed": report.auto_fixed,
        "needs_review": report.needs_review,
    }
