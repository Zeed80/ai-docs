"""Deterministic localisation of horizontal axial dimensions on CAD sheets.

The spec reader already knows the strings printed on the sheet, but a bag of
numbers is not a dimension chain.  This module keeps the missing spatial fact:
the OCR label bbox, the horizontal dimension line and the two extension lines
that terminate it.  Once the overall dimension is found, every observation can
be expressed relative to the left/right datum without asking a VLM to invent
coordinates.

Nothing here creates part geometry.  The observations are evidence supplied to
the reader and persisted in the process journal; incomplete or ambiguous maps
remain blockers.
"""

from __future__ import annotations

import math
import re
from typing import Any

_NUMBER_TOKEN = re.compile(r"[^0-9]*([0-9]+(?:[.,][0-9]+)?)[^0-9]*")


def _matches(value: float, candidates: list[float], relative: float = 0.005) -> bool:
    return any(
        abs(value - candidate) <= max(0.05, abs(candidate) * relative)
        for candidate in candidates
    )


def _nearest(value: float, candidates: list[float]) -> tuple[float | None, float]:
    if not candidates:
        return None, float("inf")
    candidate = min(candidates, key=lambda item: abs(item - value))
    error = abs(candidate - value) / max(abs(candidate), 1e-6)
    return candidate, error


def _plausible_ocr_correction(raw_text: str, candidate: float) -> bool:
    raw_digits = "".join(character for character in raw_text if character.isdigit())
    candidate_text = f"{candidate:g}".replace(".", "")
    if not raw_digits or not candidate_text:
        return False
    if candidate_text.endswith(raw_digits) or raw_digits.endswith(candidate_text):
        return abs(len(candidate_text) - len(raw_digits)) <= 1
    if len(candidate_text) != len(raw_digits):
        return False
    return sum(left != right for left, right in zip(raw_digits, candidate_text, strict=True)) <= 1


def _ocr_numeric_tokens(image: Any) -> list[dict[str, Any]]:
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(
        image,
        lang="rus+eng",
        config="--psm 11",
        output_type=Output.DICT,
    )
    tokens: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("text") or []):
        text = str(raw or "").strip()
        match = _NUMBER_TOKEN.fullmatch(text)
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", "."))
            confidence = float(data["conf"][index])
        except (TypeError, ValueError, KeyError):
            continue
        x = int(data["left"][index])
        y = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])
        if not (0 < value <= 100_000 and 0 < width <= 160 and 0 < height <= 90):
            continue
        tokens.append({
            "raw_text": text,
            "ocr_value_mm": value,
            "ocr_confidence": max(0.0, min(1.0, confidence / 100.0)),
            "label_bbox": [x, y, x + width, y + height],
        })
    return tokens


def _hough_lines(image: Any) -> tuple[list[list[float]], list[list[float]]]:
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        1,
        math.pi / 180.0,
        threshold=25,
        minLineLength=20,
        maxLineGap=10,
    )
    horizontal: list[list[float]] = []
    vertical: list[list[float]] = []
    if lines is None:
        return horizontal, vertical
    for x1, y1, x2, y2 in lines[:, 0]:
        if abs(int(y2) - int(y1)) <= 2 and abs(int(x2) - int(x1)) >= 35:
            horizontal.append([
                float(min(x1, x2)),
                float(y1 + y2) / 2.0,
                float(max(x1, x2)),
            ])
        if abs(int(x2) - int(x1)) <= 2 and abs(int(y2) - int(y1)) >= 20:
            vertical.append([
                float(x1 + x2) / 2.0,
                float(min(y1, y2)),
                float(max(y1, y2)),
            ])
    return horizontal, vertical


def _pair_tokens_with_lines(
    tokens: list[dict[str, Any]],
    horizontal: list[list[float]],
    vertical: list[list[float]],
) -> list[dict[str, Any]]:
    paired: list[dict[str, Any]] = []
    for token in tokens:
        x0, _y0, x1, y1 = token["label_bbox"]
        center_x = (x0 + x1) / 2.0
        candidates = [
            line for line in horizontal
            if y1 + 3 <= line[1] <= y1 + 35
            and line[0] - 15 <= center_x <= line[2] + 15
        ]
        if not candidates:
            continue
        paired_line = None
        for line in sorted(candidates, key=lambda item: (item[1] - y1, -(item[2] - item[0]))):
            line_x0, line_y, line_x1 = line
            left_extensions = [
                item for item in vertical
                if line_x0 - 60 <= item[0] <= line_x0 + 20
                and item[1] - 15 <= line_y <= item[2] + 15
            ]
            right_extensions = [
                item for item in vertical
                if line_x1 - 20 <= item[0] <= line_x1 + 60
                and item[1] - 15 <= line_y <= item[2] + 15
            ]
            if left_extensions and right_extensions:
                paired_line = (line, left_extensions, right_extensions)
                break
        if paired_line is None:
            continue
        (line_x0, line_y, line_x1), left_extensions, right_extensions = paired_line
        left = min(left_extensions, key=lambda item: abs(item[0] - line_x0))
        right = min(right_extensions, key=lambda item: abs(item[0] - line_x1))
        if right[0] - left[0] < 35:
            continue
        paired.append({
            **token,
            "line": [round(left[0], 1), round(line_y, 1), round(right[0], 1), round(line_y, 1)],
            "span_px": round(right[0] - left[0], 1),
        })
    return paired


def _deduplicate(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[int, int, int], dict[str, Any]] = {}
    for item in observations:
        line = item["line"]
        key = (round(line[0] / 3), round(line[2] / 3), round(line[1] / 3))
        current = best.get(key)
        if current is None or item["ocr_confidence"] > current["ocr_confidence"]:
            best[key] = item
    return list(best.values())


def localize_axial_dimensions(
    image: Any,
    known_linear_values: list[float],
) -> dict[str, Any]:
    """Return datum-relative dimension observations with pixel evidence.

    The overall line is the widest OCR-labelled line whose value already
    occurs in the independently read callout list.  Its ratio calibrates only
    the dimension-line spans; it is never used to measure the part silhouette.
    OCR slips such as ``50`` for ``150`` may then be corrected only when one
    unique known callout agrees with that calibrated span within 4%.
    """
    known = sorted({round(float(value), 3) for value in known_linear_values if value > 0})
    try:
        tokens = _ocr_numeric_tokens(image)
        horizontal, vertical = _hough_lines(image)
    except Exception as exc:  # noqa: BLE001 — localisation is an optional reader aid
        return {
            "status": "unavailable",
            "observations": [],
            "blockers": [f"локализатор размерных линий недоступен: {type(exc).__name__}"],
        }
    paired = _deduplicate(_pair_tokens_with_lines(tokens, horizontal, vertical))
    overall_candidates = [
        item for item in paired
        if _matches(item["ocr_value_mm"], known)
    ]
    # The callout VLM may miss a number that Tesseract has already tied to a
    # real dimension line. In that case the widest paired line is the only
    # admissible overall candidate: it spans the two extreme datum extensions,
    # so using its own printed label is observation, not silhouette measuring.
    if not overall_candidates:
        overall_candidates = [
            item for item in paired
            if float(item.get("ocr_confidence") or 0.0) >= 0.45
        ]
    if not overall_candidates:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["не найдена размерная линия общего осевого габарита"],
        }
    overall = max(overall_candidates, key=lambda item: item["span_px"])
    overall_value = float(overall["ocr_value_mm"])
    overall_span = float(overall["span_px"])
    if overall_value <= 0 or overall_span <= 0:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["общий осевой габарит не задаёт масштаб размерных линий"],
        }
    mm_per_px = overall_value / overall_span
    datum_left, _line_y, datum_right, _ = overall["line"]
    datum_tolerance = max(12.0, overall_span * 0.025)

    accepted: list[dict[str, Any]] = []
    for item in paired:
        line_left, line_y, line_right, _ = item["line"]
        if line_left < datum_left - datum_tolerance or line_right > datum_right + datum_tolerance:
            continue
        measured = item["span_px"] * mm_per_px
        snapped, snap_error = _nearest(measured, known)
        ocr_value = float(item["ocr_value_mm"])
        value = ocr_value
        corrected = False
        value_source = "callout"
        ocr_span_error = abs(measured - ocr_value) / max(ocr_value, 1e-6)
        if not _matches(ocr_value, known) and ocr_span_error <= 0.02:
            # The printed token and its independently calibrated dimension
            # line agree. Prefer that direct pair over a nearby VLM candidate:
            # on detal_126 the true ``35`` otherwise snapped to an unrelated
            # ``36`` from the angular note ``36°×2``.
            value_source = "dimension_line_ocr"
        elif (
            snapped is not None
            and snap_error <= 0.04
            and (
                _matches(ocr_value, [snapped])
                or _plausible_ocr_correction(item["raw_text"], snapped)
            )
        ):
            value = snapped
            corrected = not _matches(ocr_value, [snapped])
            value_source = "callout_span_crosscheck"
        elif not _matches(ocr_value, known):
            # A missing leading digit (the real sheet's OCR ``50`` for
            # ``150``) is recoverable from the independently calibrated span,
            # but only when the printed token differs by that narrow OCR edit.
            geometric = float(round(measured))
            geometric_error = abs(measured - geometric) / max(geometric, 1e-6)
            raw_digits = "".join(
                character for character in item["raw_text"] if character.isdigit()
            )
            geometric_digits = f"{geometric:g}".replace(".", "")
            if (
                geometric > 0
                and geometric_error <= 0.04
                and len(geometric_digits) == len(raw_digits) + 1
                and _plausible_ocr_correction(item["raw_text"], geometric)
            ):
                value = geometric
                corrected = True
                value_source = "dimension_span_ocr_correction"
            else:
                # The token itself is still source evidence: OCR bbox + a
                # dimension line with two extension lines. Keep it only if its
                # value agrees geometrically below; the confidence gate rejects
                # labels whose span says something else.
                value_source = "dimension_line_ocr"

        left_aligned = abs(line_left - datum_left) <= datum_tolerance
        right_aligned = abs(line_right - datum_right) <= datum_tolerance
        if left_aligned and right_aligned:
            relation = "overall"
            station = overall_value
        elif left_aligned:
            relation = "from_left_datum"
            station = value
        elif right_aligned:
            relation = "from_right_datum"
            station = overall_value - value
        else:
            relation = "local_interval"
            station = None
        mismatch = abs(measured - value) / max(value, 1e-6)
        confidence = min(float(item["ocr_confidence"]), max(0.0, 1.0 - mismatch))
        if confidence < 0.45:
            continue
        if confidence < 0.6:
            station = None
        accepted.append({
            "id": "",
            "raw_text": item["raw_text"],
            "value_mm": round(value, 3),
            "ocr_value_mm": round(ocr_value, 3),
            "ocr_corrected": corrected,
            "value_source": value_source,
            "relation": relation,
            "station_from_left_mm": round(station, 3) if station is not None else None,
            "label_bbox": item["label_bbox"],
            "dimension_line": item["line"],
            "span_px": item["span_px"],
            "span_check_mm": round(measured, 2),
            "confidence": round(confidence, 3),
        })
    accepted.sort(key=lambda item: (item["dimension_line"][1], item["dimension_line"][0]))
    for index, item in enumerate(accepted, start=1):
        item["id"] = f"axial-dim-{index}"
    return {
        "status": "ok" if accepted else "unresolved",
        "overall_mm": round(overall_value, 3),
        "datum_line": [datum_left, datum_right],
        "mm_per_px": round(mm_per_px, 6),
        "observations": accepted,
        "blockers": [] if accepted else ["осевые размерные линии не связаны с выносками"],
    }
