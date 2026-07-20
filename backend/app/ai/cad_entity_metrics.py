"""Ground-truth entity metrics for raster-to-CAD evaluation.

Pixel overlap is intentionally absent from this module. A prediction is
correct only when an exportable CAD entity of the same type and semantics can
be matched to a ground-truth entity within a geometric tolerance.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable

from app.ai.cad_ir.schema import CadIR, Entity


def _point(point, width: float, height: float) -> tuple[float, float]:
    return point.x / width, point.y / height


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _normalized_text(value: str) -> str:
    normalized = (value or "").replace("Ø", "⌀").replace("ø", "⌀")
    normalized = normalized.replace("×", "x").replace(",", ".")
    return re.sub(r"\s+", " ", normalized.strip()).casefold()


def _polyline_distance(pred, truth, pw: float, ph: float, tw: float, th: float) -> float:
    left = [_point(point, pw, ph) for point in pred.points]
    right = [_point(point, tw, th) for point in truth.points]
    if pred.closed != truth.closed:
        return 1.0

    def directed(source, target) -> float:
        return max(min(_distance(point, other) for other in target) for point in source)

    return max(directed(left, right), directed(right, left))


def entity_distance(
    predicted: Entity,
    truth: Entity,
    *,
    predicted_size: tuple[int, int],
    truth_size: tuple[int, int],
) -> float:
    """Normalized geometric/semantic distance; 1 means incompatible."""
    if predicted.type != truth.type:
        return 1.0
    pw, ph = map(float, predicted_size)
    tw, th = map(float, truth_size)
    diagonal_p = math.hypot(pw, ph)
    diagonal_t = math.hypot(tw, th)

    if predicted.type == "segment":
        pp1, pp2 = _point(predicted.p1, pw, ph), _point(predicted.p2, pw, ph)
        tp1, tp2 = _point(truth.p1, tw, th), _point(truth.p2, tw, th)
        direct = max(_distance(pp1, tp1), _distance(pp2, tp2))
        reverse = max(_distance(pp1, tp2), _distance(pp2, tp1))
        return min(direct, reverse)
    if predicted.type == "circle":
        center = _distance(
            _point(predicted.center, pw, ph), _point(truth.center, tw, th)
        )
        radius = abs(predicted.radius / diagonal_p - truth.radius / diagonal_t)
        return max(center, radius)
    if predicted.type == "arc":
        center = _distance(
            _point(predicted.center, pw, ph), _point(truth.center, tw, th)
        )
        radius = abs(predicted.radius / diagonal_p - truth.radius / diagonal_t)
        start = abs((predicted.start_angle - truth.start_angle) % 360.0)
        end = abs((predicted.end_angle - truth.end_angle) % 360.0)
        angle = max(min(start, 360.0 - start), min(end, 360.0 - end)) / 360.0
        return max(center, radius, angle)
    if predicted.type == "polyline":
        return _polyline_distance(predicted, truth, pw, ph, tw, th)
    if predicted.type == "text":
        if _normalized_text(predicted.text) != _normalized_text(truth.text):
            return 1.0
        return _distance(
            _point(predicted.position, pw, ph), _point(truth.position, tw, th)
        )
    if predicted.type == "dimension":
        from app.ai.cad_ir.dim_render import dimension_label

        if (
            predicted.kind != truth.kind
            or _normalized_text(dimension_label(predicted))
            != _normalized_text(dimension_label(truth))
            or (
                predicted.value_mm is None
                and truth.value_mm is not None
                or predicted.value_mm is not None
                and truth.value_mm is None
                or predicted.value_mm is not None
                and truth.value_mm is not None
                and abs(predicted.value_mm - truth.value_mm) > 1e-6
            )
            or (predicted.tolerance or "") != (truth.tolerance or "")
        ):
            return 1.0
        return max(
            _distance(_point(predicted.p1, pw, ph), _point(truth.p1, tw, th)),
            _distance(_point(predicted.p2, pw, ph), _point(truth.p2, tw, th)),
        )
    if predicted.type == "annotation":
        from app.ai.cad_ir.annotations import annotation_text

        predicted_text = annotation_text(
            predicted.kind, predicted.value, predicted.symbol, predicted.datum_refs
        )
        truth_text = annotation_text(
            truth.kind, truth.value, truth.symbol, truth.datum_refs
        )
        if predicted.kind != truth.kind or _normalized_text(
            predicted_text
        ) != _normalized_text(truth_text):
            return 1.0
        return _distance(
            _point(predicted.position, pw, ph), _point(truth.position, tw, th)
        )
    if predicted.type == "hatch":
        # Boundary geometry is the CAD meaning of a hatch. Pattern must also
        # agree; otherwise a solid fill can masquerade as section hatching.
        if predicted.pattern != truth.pattern:
            return 1.0
        class _Boundary:
            closed = True

            def __init__(self, points):
                self.points = points

        boundary_distance = _polyline_distance(
            _Boundary(predicted.boundary),
            _Boundary(truth.boundary),
            pw,
            ph,
            tw,
            th,
        )
        if len(predicted.holes) != len(truth.holes):
            return 1.0
        if not predicted.holes:
            return boundary_distance
        import numpy as np
        from scipy.optimize import linear_sum_assignment

        costs = np.array([
            [
                _polyline_distance(
                    _Boundary(predicted_hole),
                    _Boundary(truth_hole),
                    pw,
                    ph,
                    tw,
                    th,
                )
                for truth_hole in truth.holes
            ]
            for predicted_hole in predicted.holes
        ])
        rows, columns = linear_sum_assignment(costs)
        return max(
            boundary_distance,
            max(float(costs[row, column]) for row, column in zip(rows, columns)),
        )
    return 1.0


def compare_entities(
    predicted: Iterable[Entity],
    truth: Iterable[Entity],
    *,
    predicted_size: tuple[int, int],
    truth_size: tuple[int, int],
    tolerance: float = 0.0025,
) -> dict:
    """Hungarian matching by entity type with strict semantic agreement."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    predicted_by_type: dict[str, list[Entity]] = defaultdict(list)
    truth_by_type: dict[str, list[Entity]] = defaultdict(list)
    for entity in predicted:
        if not entity.construction:
            predicted_by_type[entity.type].append(entity)
    for entity in truth:
        if not entity.construction:
            truth_by_type[entity.type].append(entity)

    per_type: dict[str, dict] = {}
    total_tp = total_fp = total_fn = 0
    for entity_type in sorted(set(predicted_by_type) | set(truth_by_type)):
        pred = predicted_by_type[entity_type]
        gt = truth_by_type[entity_type]
        matched = 0
        distances: list[float] = []
        if pred and gt:
            costs = np.array([
                [
                    entity_distance(
                        p,
                        t,
                        predicted_size=predicted_size,
                        truth_size=truth_size,
                    )
                    for t in gt
                ]
                for p in pred
            ])
            rows, columns = linear_sum_assignment(costs)
            for row, column in zip(rows, columns):
                distance = float(costs[row, column])
                if distance <= tolerance:
                    matched += 1
                    distances.append(distance)
        fp, fn = len(pred) - matched, len(gt) - matched
        precision = matched / len(pred) if pred else (1.0 if not gt else 0.0)
        recall = matched / len(gt) if gt else (1.0 if not pred else 0.0)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        per_type[entity_type] = {
            "truth": len(gt),
            "predicted": len(pred),
            "matched": matched,
            "false_positive": fp,
            "false_negative": fn,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "max_distance": round(max(distances), 6) if distances else None,
        }
        total_tp += matched
        total_fp += fp
        total_fn += fn

    micro_precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 1.0
    micro_recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 1.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision + micro_recall
        else 0.0
    )
    exact = total_fp == 0 and total_fn == 0
    return {
        "per_type": per_type,
        "micro": {
            "matched": total_tp,
            "false_positive": total_fp,
            "false_negative": total_fn,
            "precision": round(micro_precision, 6),
            "recall": round(micro_recall, 6),
            "f1": round(micro_f1, 6),
        },
        "exact_sheet": exact,
    }


def compare_ir(predicted: CadIR, truth: CadIR, tolerance: float = 0.0025) -> dict:
    return compare_entities(
        predicted.entities,
        truth.entities,
        predicted_size=(predicted.source.image_width, predicted.source.image_height),
        truth_size=(truth.source.image_width, truth.source.image_height),
        tolerance=tolerance,
    )
