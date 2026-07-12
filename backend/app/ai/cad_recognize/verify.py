"""Independent deterministic verifier for recognition proposals.

Rasterizes proposed IR entities and scores them against the source ink in
both directions (recall: no real ink lost; precision: nothing hallucinated),
within the same size-scaled dilation the legacy redraw verifier used. The
creator never grades itself: whichever backend proposed the entities, this
module decides whether the proposal is trustworthy — and, later, arbitrates
between neural and CV proposals per region.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.ai.cad_ir.png_render import rasterize_entities
from app.ai.cad_ir.schema import CadIR, Entity
from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.topology import consolidate_entities
from app.ai.drawing_vectorize import (
    _MIN_COVERAGE_PRECISION,
    _MIN_COVERAGE_RECALL,
    _coverage_dilate_px,
)

logger = structlog.get_logger()


@dataclass
class CoverageScore:
    recall: float
    precision: float

    @property
    def ok(self) -> bool:
        return self.recall >= _MIN_COVERAGE_RECALL and self.precision >= _MIN_COVERAGE_PRECISION


def score_coverage(
    entities: list[Entity],
    ink: Any,
    keep_raster: Any | None = None,
    thin_px: int = 1,
    thick_px: int = 2,
) -> CoverageScore:
    """Coverage of the source ink (uint8/bool mask) by the proposed geometry.

    ``keep_raster`` regions are treated as covered (they ship as raster) and
    excluded from precision — the proposal is judged only on what it claims
    to have vectorized.
    """
    import cv2
    import numpy as np

    ink_bool = np.asarray(ink) > 0
    h, w = ink_bool.shape[:2]
    drawn = rasterize_entities(entities, w, h, thin_px, thick_px) < 128
    covered = drawn if keep_raster is None else (drawn | np.asarray(keep_raster).astype(bool))

    k = 2 * _coverage_dilate_px(h, w) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    covered_grown = cv2.dilate(covered.astype(np.uint8), kernel) > 0
    ink_grown = cv2.dilate(ink_bool.astype(np.uint8), kernel) > 0

    ink_total = int(ink_bool.sum())
    drawn_total = int(drawn.sum())
    if ink_total == 0 or drawn_total == 0:
        return CoverageScore(recall=0.0, precision=0.0)
    recall = float((covered_grown & ink_bool).sum()) / ink_total
    precision = float((ink_grown & drawn).sum()) / drawn_total
    score = CoverageScore(recall=round(recall, 4), precision=round(precision, 4))
    logger.info("cad_verify_coverage", recall=score.recall, precision=score.precision, ok=score.ok)
    return score


def apply_to_ir(ir: CadIR, score: CoverageScore) -> None:
    ir.validation.coverage_recall = score.recall
    ir.validation.coverage_precision = score.precision


@dataclass
class ArbitrationResult:
    entities: list[Entity]
    keep_raster: Any | None
    thin_px: int
    thick_px: int
    recognizer_used: str  # "neural" | "cv" | "neural+cv" (disagreement, both kept)
    score: CoverageScore
    neural_available: bool
    discrepancy: bool
    notes: dict[str, Any]


# Entity-count disagreement beyond this relative gap between two backends
# that BOTH individually pass the coverage bar is flagged rather than
# silently resolved — a whole-sheet miscount is exactly the kind of
# disagreement a human should see, not have arbitrated away quietly.
_DISCREPANCY_RELATIVE_GAP = 0.30
# A line model can cover every source pixel with thousands of tiny patch
# fragments and still be unusable as CAD. Prefer the established CV topology
# when neural expands the entity count this aggressively — but ONLY when CV
# is itself a COMPLETE read (full coverage bar). Comparing entity counts when
# CV misses geometry confounds "fragmentation" with "more coverage": found
# live (B0, 2026-07-12) — CV at recall 0.62 was being preferred over a
# passing neural read at recall 0.98 purely because the fuller result had
# proportionally more entities. Counts are compared on the neural proposal's
# OWN entities (pre-CV-supplement) and after topology consolidation, so the
# ratio reflects genuine leftover fragmentation, not supplemented families.
_NEURAL_FRAGMENTATION_RATIO = 3.0

# The lone-survivor gate exists to reject a genuinely fabricated result
# (recall AND precision both near zero — nothing recognizable overlaps real
# ink, e.g. runaway neural generation) before it ships as if it were a real
# recognition. It must NOT reject an honest partial recognition (e.g. CV
# finding 82% of the geometry with zero hallucination, precision=1.0) —
# that is exactly what CadCheckCode.COVERAGE_LOW + the review queue exist
# for downstream in cad_validate._check_coverage, using the SAME full
# production bar (_MIN_COVERAGE_RECALL/_MIN_COVERAGE_PRECISION, 0.85) this
# module imports. Confirmed live (2026-07-11): a real photo scored CV
# recall=0.82/precision=1.0 and got discarded to zero entities here before
# cad_validate ever ran — turning "flag for review" into "found nothing at
# all". This floor is deliberately far below the production bar; only true
# noise gets rejected at this stage. Two-recognizer arbitration below
# already never force-empties on a failing score (it always ships
# `chosen.entities` and lets cad_validate flag COVERAGE_LOW) — this floor
# brings the lone-survivor path in line with that existing behavior.
_LONE_SURVIVOR_MIN_RECALL = 0.3
_LONE_SURVIVOR_MIN_PRECISION = 0.3


def _passes_lone_survivor_floor(score: CoverageScore) -> bool:
    return score.recall >= _LONE_SURVIVOR_MIN_RECALL and score.precision >= _LONE_SURVIVOR_MIN_PRECISION


def _consolidated(out: RecognizeOutput | None) -> RecognizeOutput | None:
    """Topology repair on a raw proposal BEFORE scoring/arbitration: both
    backends over-fragment (patch borders, junction splits), and entity-count
    heuristics below must compare real strokes, not fragments."""
    if out is None or len(out.entities) < 2:
        return out
    entities, stats = consolidate_entities(out.entities)
    return RecognizeOutput(
        entities=entities,
        keep_raster=out.keep_raster,
        thin_px=out.thin_px,
        thick_px=out.thick_px,
        notes={**out.notes, "topology": stats},
    )


def _supplement_neural_with_cv(
    neural_out: RecognizeOutput,
    cv_out: RecognizeOutput,
) -> tuple[RecognizeOutput, set[str]]:
    """Preserve primitive families the active neural backend cannot emit.

    The production technical-vectorizer is intentionally line-only. Whole-
    sheet winner-takes-all arbitration therefore used to erase every CV
    circle, arc, polyline and hatch whenever neural won on line coverage.
    Supplement only entity families absent from neural so a future multi-type
    model remains authoritative for the families it actually predicts.
    """
    neural_types = {entity.type for entity in neural_out.entities}
    missing_types = {entity.type for entity in cv_out.entities} - neural_types
    supplements = [entity for entity in cv_out.entities if entity.type in missing_types]
    if not supplements:
        return neural_out, set()
    return RecognizeOutput(
        entities=[*neural_out.entities, *supplements],
        keep_raster=cv_out.keep_raster,
        thin_px=cv_out.thin_px,
        thick_px=max(neural_out.thick_px, cv_out.thick_px),
        notes={
            **neural_out.notes,
            "cv_supplement_types": sorted(missing_types),
            "cv_supplement_entities": len(supplements),
        },
    ), missing_types


def arbitrate_recognition(
    ink: Any,
    exclusion_boxes: list[tuple[int, int, int, int]] | None,
    neural_recognizer,
    cv_recognizer,
) -> ArbitrationResult:
    """Run neural (if available) and CV, score both independently against
    the source ink, and pick the winner — never on the model's own say-so.

    Whole-sheet arbitration (not per-region): the CV backend is the
    established fallback, so it always runs; neural only needs to beat it.
    When both pass the coverage bar but disagree substantially on how much
    geometry there is, BOTH proposals are kept as candidates and the whole
    thing is flagged as a discrepancy for the review queue — the plan is
    explicit that disagreement must surface, not be resolved silently.
    """
    cv_out = _consolidated(cv_recognizer.recognize(ink, exclusion_boxes))
    cv_score = (
        score_coverage(cv_out.entities, ink, cv_out.keep_raster, cv_out.thin_px, cv_out.thick_px)
        if cv_out is not None
        else CoverageScore(0.0, 0.0)
    )

    neural_out = None
    neural_available = True
    try:
        neural_out = _consolidated(neural_recognizer.recognize(ink, exclusion_boxes))
    except Exception as exc:  # noqa: BLE001 — arbitration must survive a broken client
        logger.warning("neural_recognize_error", error=str(exc)[:200])
        neural_out = None
    if neural_out is None:
        neural_available = False
    neural_own_count = len(neural_out.entities) if neural_out is not None else 0
    supplemented_types: set[str] = set()
    if neural_out is not None and cv_out is not None:
        neural_out, supplemented_types = _supplement_neural_with_cv(neural_out, cv_out)
    neural_score = (
        score_coverage(neural_out.entities, ink, neural_out.keep_raster, neural_out.thin_px, neural_out.thick_px)
        if neural_out is not None
        else CoverageScore(0.0, 0.0)
    )

    if neural_out is None or cv_out is None:
        # The lone survivor still has to earn its keep: a low-coverage
        # result is exactly as untrustworthy as a decline (confirmed live —
        # a neural miss on an out-of-domain sheet slipped through here as
        # "found something" before this check existed). Only ship it if it
        # actually passes the same bar arbitration would have required.
        survivor = cv_out if cv_out is not None else neural_out
        survivor_score = cv_score if cv_out is not None else neural_score
        used = "cv" if cv_out is not None else "neural"
        if survivor is None or not _passes_lone_survivor_floor(survivor_score):
            return ArbitrationResult(
                entities=[], keep_raster=None, thin_px=2, thick_px=3,
                recognizer_used=used, score=survivor_score or CoverageScore(0.0, 0.0),
                neural_available=neural_available, discrepancy=False, notes={},
            )
        return ArbitrationResult(
            entities=survivor.entities,
            keep_raster=survivor.keep_raster,
            thin_px=survivor.thin_px,
            thick_px=survivor.thick_px,
            recognizer_used=used,
            score=survivor_score,
            neural_available=neural_available,
            discrepancy=False,
            notes={},
        )

    neural_avg = (neural_score.recall + neural_score.precision) / 2
    cv_avg = (cv_score.recall + cv_score.precision) / 2
    n_neural, n_cv = len(neural_out.entities), len(cv_out.entities)
    both_pass = neural_score.ok and cv_score.ok
    rel_gap = abs(n_neural - n_cv) / max(n_neural, n_cv, 1)
    discrepancy = both_pass and rel_gap >= _DISCREPANCY_RELATIVE_GAP
    neural_fragmented = (
        n_cv > 0
        and neural_own_count / n_cv >= _NEURAL_FRAGMENTATION_RATIO
        and cv_score.ok
    )

    if neural_fragmented:
        chosen, used, chosen_score = cv_out, "cv", cv_score
        discrepancy = True
    elif discrepancy:
        # Keep neural's geometry (target primary path) but flag loudly;
        # cad_trace surfaces the counts so a human can compare, not guess.
        chosen, used, chosen_score = neural_out, "neural+cv", neural_score
    elif neural_score.ok and neural_avg >= cv_avg:
        chosen, used, chosen_score = (
            neural_out,
            "neural+cv" if supplemented_types else "neural",
            neural_score,
        )
    else:
        chosen, used, chosen_score = cv_out, "cv", cv_score

    return ArbitrationResult(
        entities=chosen.entities,
        keep_raster=chosen.keep_raster,
        thin_px=chosen.thin_px,
        thick_px=chosen.thick_px,
        recognizer_used=used,
        score=chosen_score,
        neural_available=neural_available,
        discrepancy=discrepancy,
        notes={"neural_entities": n_neural, "cv_entities": n_cv,
               "neural_own_entities": neural_own_count,
               "cv_supplement_types": sorted(supplemented_types),
               "neural_fragmented": neural_fragmented,
               "neural_score": (neural_score.recall, neural_score.precision),
               "cv_score": (cv_score.recall, cv_score.precision)},
    )
