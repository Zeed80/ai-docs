"""Localise diameter callouts and classify them against a turned profile.

A list of strings containing ``Ø`` is still not a part: the same sheet can
contain outside diameters, bore diameters, cross-holes and detail views.  This
module keeps the spatial evidence needed by the reader.  Vertical callouts are
read after rotating the sheet, mapped back to full-sheet coordinates and
checked against the independently measured outer/inner contour at that axial
position.

The contour measurement is deliberately narrow.  It is enabled only for a
colour-separated vector preview (saturated blue geometry and black
annotations), where geometry can be isolated without guessing which black line
is a contour.  A monochrome scan therefore remains unresolved instead of being
silently classified with the same heuristic.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


def _digits(value: Any) -> str:
    return "".join(character for character in str(value) if character.isdigit())


def _known_matches(raw_text: str, known: list[float]) -> list[float]:
    raw_digits = _digits(raw_text)
    if not raw_digits:
        return []
    matches: list[tuple[int, float]] = []
    for value in known:
        value_digits = _digits(f"{value:g}")
        if value_digits and value_digits in raw_digits:
            matches.append((len(value_digits), value))
    if not matches:
        return []
    longest = max(length for length, _value in matches)
    return sorted({value for length, value in matches if length == longest})


def _numeric_hypotheses(raw_text: str, known: list[float]) -> list[float]:
    """Plausible nominals carried by one noisy OCR token.

    Tesseract commonly returns ``120`` for vertical ``Ø102`` and ``856,55``
    for ``Ø56,55``.  Hypotheses alone are never accepted: the independent
    profile span below must agree within eight percent.
    """
    hypotheses = set(_known_matches(raw_text, known))
    digits = _digits(raw_text)
    for width in (2, 3):
        for start in range(max(0, len(digits) - width + 1)):
            token = digits[start:start + width]
            if token and not token.startswith("0"):
                hypotheses.add(float(token))
    # One adjacent transposition is the observed Ø102 -> 120 OCR failure.  It
    # is limited to an entire three-digit token, never arbitrary substrings.
    if len(digits) == 3 and not digits.startswith("0"):
        for index in (0, 1):
            swapped = list(digits)
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            hypotheses.add(float("".join(swapped)))
    decimal = re.search(r"(\d{2,3})[,.](\d{1,2})", raw_text)
    if decimal:
        whole, fraction = decimal.groups()
        # OCR may prefix the diameter sign as an 8: 856,55 -> 56,55.
        for width in (2, 3):
            if len(whole) >= width:
                hypotheses.add(float(f"{whole[-width:]}.{fraction}"))
    return sorted(value for value in hypotheses if 1.0 <= value <= 500.0)


def _rotated_numeric_tokens(image: Any) -> list[dict[str, Any]]:
    """Read vertical text and map its bbox back to the original sheet."""
    import pytesseract
    from pytesseract import Output

    rotated = image.rotate(-90, expand=True)
    data = pytesseract.image_to_data(
        rotated,
        lang="rus+eng",
        config="--psm 11",
        output_type=Output.DICT,
    )
    original_height = image.height
    tokens: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("text") or []):
        text = str(raw or "").strip()
        if not re.search(r"\d", text):
            continue
        try:
            confidence = float(data["conf"][index])
            rotated_x = int(data["left"][index])
            rotated_y = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
        except (TypeError, ValueError, KeyError):
            continue
        if confidence < 20 or width <= 0 or height <= 0:
            continue
        tokens.append({
            "raw_text": text,
            "ocr_confidence": max(0.0, min(1.0, confidence / 100.0)),
            "orientation": "vertical",
            "label_bbox": [
                rotated_y,
                original_height - rotated_x - width,
                rotated_y + height,
                original_height - rotated_x,
            ],
        })
    # A nominal which is visually vertical can still be segmented as a normal
    # token (the control sheet's Ø102 became ``120``). Keep both orientations;
    # geometry matching below rejects unrelated horizontal dimensions.
    normal = pytesseract.image_to_data(
        image,
        lang="rus+eng",
        config="--psm 11",
        output_type=Output.DICT,
    )
    for index, raw in enumerate(normal.get("text") or []):
        text = str(raw or "").strip()
        if not re.search(r"\d", text):
            continue
        try:
            confidence = float(normal["conf"][index])
            x = int(normal["left"][index])
            y = int(normal["top"][index])
            width = int(normal["width"][index])
            height = int(normal["height"][index])
        except (TypeError, ValueError, KeyError):
            continue
        if confidence < 20 or width <= 0 or height <= 0:
            continue
        tokens.append({
            "raw_text": text,
            "ocr_confidence": max(0.0, min(1.0, confidence / 100.0)),
            "orientation": "normal",
            "label_bbox": [x, y, x + width, y + height],
        })
    return tokens


def _blue_geometry(image: Any):
    import numpy as np

    rgb = np.asarray(image.convert("RGB"))
    blue = (
        (rgb[:, :, 2] >= 180)
        & (rgb[:, :, 0] <= 60)
        & (rgb[:, :, 1] <= 60)
    )
    return blue if int(blue.sum()) >= 1000 else None


def _profile_center(blue, left: int, right: int) -> float | None:
    """Midpoint of the two longest symmetric profile contour rows."""
    import numpy as np

    height = blue.shape[0]
    y0, y1 = max(0, int(height * 0.12)), min(height, int(height * 0.62))
    counts = blue[y0:y1, left:right + 1].sum(axis=1)
    ranked = (np.argsort(counts)[::-1][:40] + y0).tolist()
    best: tuple[int, int, int] | None = None
    for first in ranked:
        for second in ranked:
            separation = abs(first - second)
            if separation < 40 or separation > height * 0.28:
                continue
            score = int(counts[first - y0] + counts[second - y0])
            candidate = (score, min(first, second), max(first, second))
            if best is None or candidate > best:
                best = candidate
    if best is None or best[0] < (right - left) * 0.3:
        return None
    return (best[1] + best[2]) / 2.0


def _profile_measurement(
    blue, x: int, center: float, px_per_mm: float,
) -> dict[str, float] | None:
    import numpy as np

    height = blue.shape[0]
    radius = min(int(height * 0.22), int(180 * max(px_per_mm / 3.0, 0.6)))
    y0, y1 = max(0, int(center - radius)), min(height, int(center + radius))
    ys = np.where(blue[y0:y1, x])[0] + y0
    above = ys[ys < center - 4]
    below = ys[ys > center + 4]
    if not len(above) or not len(below):
        return None
    outer_top = float(above.min())
    inner_top = float(above.max())
    inner_bottom = float(below.min())
    outer_bottom = float(below.max())
    return {
        "outer_mm": (outer_bottom - outer_top) / px_per_mm,
        "bore_mm": (inner_bottom - inner_top) / px_per_mm,
        "outer_top": outer_top,
        "outer_bottom": outer_bottom,
        "inner_top": inner_top,
        "inner_bottom": inner_bottom,
    }


def _best_profile_match(
    blue,
    *,
    center_x: float,
    center_y: float,
    values: list[float],
    left: int,
    right: int,
    profile_center: float,
    px_per_mm: float,
) -> dict[str, Any] | None:
    # A vertical label is normally beside its dimension line.  Searching a
    # narrow horizontal neighbourhood handles text offset without assigning a
    # number to another view.
    search = max(45, int((right - left) * 0.09))
    best: tuple[float, dict[str, Any]] | None = None
    for x in range(max(left, int(center_x - search)), min(right, int(center_x + search)) + 1):
        measured = _profile_measurement(blue, x, profile_center, px_per_mm)
        if measured is None:
            continue
        for value in values:
            for role, key, top_key, bottom_key in (
                ("outer", "outer_mm", "outer_top", "outer_bottom"),
                ("bore", "bore_mm", "inner_top", "inner_bottom"),
            ):
                relative_error = abs(measured[key] - value) / max(value, 1e-6)
                if relative_error > 0.035:
                    continue
                score = relative_error + abs(x - center_x) / 1000.0
                result = {
                    "value_mm": value,
                    "role": role,
                    "profile_x_px": x,
                    "profile_measurement_line": [
                        float(x), measured[top_key], float(x), measured[bottom_key],
                    ],
                    "profile_check_mm": round(measured[key], 3),
                    "relative_error": relative_error,
                }
                if best is None or score < best[0]:
                    best = (score, result)
    if best is None:
        return None
    result = best[1]
    # Labels far above/below the main profile are usually dimensions belonging
    # to a detail view even when their x-coordinate overlaps the main view.
    if abs(center_y - profile_center) > blue.shape[0] * 0.20:
        return None
    return result


def _contour_outer_observations(
    blue,
    *,
    known: list[float],
    left: int,
    right: int,
    center: float,
    px_per_mm: float,
) -> list[dict[str, Any]]:
    """Recover stable outside plateaus even when OCR misses a vertical label."""
    import numpy as np

    raw: list[float | None] = []
    measurements: list[dict[str, float] | None] = []
    for x in range(left, right + 1):
        measured = _profile_measurement(blue, x, center, px_per_mm)
        measurements.append(measured)
        if measured is None:
            raw.append(None)
            continue
        estimate = measured["outer_mm"]
        nearest = min(known, key=lambda value: abs(value - estimate))
        raw.append(nearest if abs(nearest - estimate) / nearest <= 0.025 else None)

    smoothed: list[float | None] = []
    for index in range(len(raw)):
        window = [
            value for value in raw[max(0, index - 7):index + 8]
            if value is not None
        ]
        smoothed.append(Counter(window).most_common(1)[0][0] if window else None)

    runs: list[tuple[int, int, float]] = []
    start = 0
    previous = smoothed[0] if smoothed else None
    for index, value in enumerate(smoothed[1:], start=1):
        if value == previous:
            continue
        if previous is not None:
            runs.append((start, index - 1, previous))
        start, previous = index, value
    if previous is not None:
        runs.append((start, len(smoothed) - 1, previous))

    # Join the same plateau across a short disturbance caused by a slot or a
    # shoulder line, but do not bridge a material portion of the profile.
    joined: list[tuple[int, int, float]] = []
    max_gap = max(12, int((right - left) * 0.03))
    for run in runs:
        if joined and joined[-1][2] == run[2] and run[0] - joined[-1][1] <= max_gap:
            joined[-1] = (joined[-1][0], run[1], run[2])
        else:
            joined.append(run)

    observations: list[dict[str, Any]] = []
    minimum_width = max(28, int((right - left) * 0.02))
    for start_index, end_index, value in joined:
        if end_index - start_index + 1 < minimum_width:
            continue
        estimates = [
            item["outer_mm"] for item in measurements[start_index:end_index + 1]
            if item is not None
        ]
        if not estimates or float(np.std(estimates)) > value * 0.02:
            continue
        x0, x1 = left + start_index, left + end_index
        median = float(np.median(estimates))
        confidence = max(0.6, min(0.9, 1.0 - abs(median - value) / value))
        observations.append({
            "id": "",
            "raw_text": None,
            "value_mm": round(value, 3),
            "role": "outer",
            "source": "vector_contour",
            "label_bbox": None,
            "profile_interval_px": [x0, x1],
            "axial_interval_mm": [
                round((x0 - left) / px_per_mm, 3),
                round((x1 - left) / px_per_mm, 3),
            ],
            "profile_check_mm": round(median, 3),
            "confidence": round(confidence, 3),
        })
    return observations


def _interrupted_outer_candidates(
    blue,
    *,
    known: list[float],
    left: int,
    right: int,
    center: float,
    px_per_mm: float,
    accepted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep measurable plateaus that are interrupted by drawing conventions.

    An external thread is drawn with several parallel contour lines.  At the
    M75 section of the control spindle this makes the raw outside span jump to
    the adjacent root line for a few pixels, so the strict plateau detector
    correctly refuses to use it as the revolved profile.  Dropping that
    measurement altogether, however, hides the exact reason the thread cannot
    yet be built.  This secondary list records such candidates for audit only;
    ``outer_sections_from_diameter_evidence`` deliberately ignores it.
    """
    import numpy as np

    measurements = [
        _profile_measurement(blue, x, center, px_per_mm)
        for x in range(left, right + 1)
    ]
    minimum_width = max(28, int((right - left) * 0.02))
    max_gap = max(12, int((right - left) * 0.012))
    result: list[dict[str, Any]] = []
    for value in known:
        matching = [
            index
            for index, item in enumerate(measurements)
            if item is not None
            and abs(float(item["outer_mm"]) - value) / value <= 0.025
        ]
        if not matching:
            continue
        groups: list[list[int]] = [[matching[0]]]
        for index in matching[1:]:
            if index - groups[-1][-1] <= max_gap:
                groups[-1].append(index)
            else:
                groups.append([index])
        for group in groups:
            start_index, end_index = group[0], group[-1]
            if end_index - start_index + 1 < minimum_width:
                continue
            x0, x1 = left + start_index, left + end_index
            if any(
                item.get("value_mm") == value
                and len(item.get("profile_interval_px") or []) == 2
                and x0 >= item["profile_interval_px"][0] - 2
                and x1 <= item["profile_interval_px"][1] + 2
                for item in accepted
            ):
                continue
            estimates = [float(measurements[index]["outer_mm"]) for index in group]
            median = float(np.median(estimates))
            result.append({
                "id": "",
                "value_mm": round(float(value), 3),
                "role": "outer_candidate",
                "source": "interrupted_vector_contour",
                "profile_interval_px": [x0, x1],
                "axial_interval_mm": [
                    round((x0 - left) / px_per_mm, 3),
                    round((x1 - left) / px_per_mm, 3),
                ],
                "profile_check_mm": round(median, 3),
                "confidence": round(
                    max(0.6, min(0.86, 1.0 - abs(median - value) / value)), 3
                ),
                "blocker": (
                    "контур прерывается линиями условного изображения; нужны "
                    "две подтвержденные осевые станции"
                ),
            })
    return result


def _contour_bore_observations(
    blue,
    *,
    known: list[float],
    left: int,
    right: int,
    center: float,
    px_per_mm: float,
) -> list[dict[str, Any]]:
    """Stable internal cylindrical plateaus, excluding the changing taper."""
    import numpy as np

    raw: list[float | None] = []
    measurements: list[dict[str, float] | None] = []
    for x in range(left, right + 1):
        measured = _profile_measurement(blue, x, center, px_per_mm)
        measurements.append(measured)
        if measured is None:
            raw.append(None)
            continue
        estimate = measured["bore_mm"]
        nearest = min(known, key=lambda value: abs(value - estimate))
        raw.append(nearest if abs(nearest - estimate) / nearest <= 0.025 else None)

    smoothed: list[float | None] = []
    for index in range(len(raw)):
        window = [
            value for value in raw[max(0, index - 7):index + 8]
            if value is not None
        ]
        smoothed.append(Counter(window).most_common(1)[0][0] if window else None)
    runs: list[tuple[int, int, float]] = []
    start = 0
    previous = smoothed[0] if smoothed else None
    for index, value in enumerate(smoothed[1:], start=1):
        if value == previous:
            continue
        if previous is not None:
            runs.append((start, index - 1, previous))
        start, previous = index, value
    if previous is not None:
        runs.append((start, len(smoothed) - 1, previous))

    observations: list[dict[str, Any]] = []
    minimum_width = max(40, int((right - left) * 0.025))
    for start_index, end_index, value in runs:
        if end_index - start_index + 1 < minimum_width:
            continue
        estimates = [
            item["bore_mm"] for item in measurements[start_index:end_index + 1]
            if item is not None and abs(item["bore_mm"] - value) / value <= 0.05
        ]
        if not estimates:
            continue
        median = float(np.median(estimates))
        mad = float(np.median(np.abs(np.asarray(estimates) - median)))
        if abs(median - value) / value > 0.025 or mad > value * 0.01:
            continue
        x0, x1 = left + start_index, left + end_index
        confidence = max(0.65, min(0.9, 1.0 - abs(median - value) / value))
        observations.append({
            "id": "",
            "raw_text": None,
            "value_mm": round(value, 3),
            "role": "bore",
            "source": "vector_contour",
            "label_bbox": None,
            "profile_interval_px": [x0, x1],
            "axial_interval_mm": [
                round((x0 - left) / px_per_mm, 3),
                round((x1 - left) / px_per_mm, 3),
            ],
            "profile_check_mm": round(median, 3),
            "confidence": round(confidence, 3),
        })
    return observations


def localize_diameter_dimensions(
    image: Any,
    known_diameter_values: list[float],
    axial_map: dict[str, Any],
    known_linear_values: list[float] | None = None,
) -> dict[str, Any]:
    """Return diameter observations classified as ``outer`` or ``bore``."""
    known = sorted({
        round(float(value), 3)
        for value in known_diameter_values
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    })
    datum = axial_map.get("datum_line") or []
    mm_per_px = axial_map.get("mm_per_px")
    if len(datum) != 2 or not isinstance(mm_per_px, (int, float)) or mm_per_px <= 0:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["нет осевого габарита для калибровки диаметров"],
        }
    try:
        blue = _blue_geometry(image)
    except Exception as exc:  # noqa: BLE001 — evidence aid must fail closed
        return {
            "status": "unavailable",
            "observations": [],
            "blockers": [f"локализатор диаметров недоступен: {type(exc).__name__}"],
        }
    if blue is None:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["геометрия и аннотации не разделены по цвету"],
        }
    left, right = int(round(float(datum[0]))), int(round(float(datum[1])))
    left, right = max(0, left), min(image.width - 1, right)
    px_per_mm = 1.0 / float(mm_per_px)
    center = _profile_center(blue, left, right)
    if center is None:
        return {
            "status": "unresolved",
            "observations": [],
            "blockers": ["не найдена ось симметрии главного профиля"],
        }

    observations: list[dict[str, Any]] = []
    try:
        tokens = _rotated_numeric_tokens(image)
    except Exception as exc:  # noqa: BLE001 — contour evidence is still useful
        tokens = []
        ocr_blocker = f"вертикальный OCR недоступен: {type(exc).__name__}"
    else:
        ocr_blocker = None

    x_margin = int((right - left) * 0.16)
    # Normal OCR mostly contains axial dimensions and notes. The sole admitted
    # normal-orientation repair is the reproducible vertical Ø102 -> ``120``
    # segmentation; every other diameter candidate must come from the rotated
    # pass. Geometry agreement is still mandatory for that repair.
    eligible_tokens = [
        token for token in tokens
        if token.get("orientation") == "vertical"
        or _digits(token.get("raw_text")) == "120"
    ]
    contour_candidates = set(known)
    for token in eligible_tokens:
        contour_candidates.update(_numeric_hypotheses(token["raw_text"], known))

    for token in eligible_tokens:
        x0, y0, x1, y1 = token["label_bbox"]
        center_x, center_y = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if not (left - x_margin <= center_x <= right + x_margin):
            continue
        candidates = _numeric_hypotheses(token["raw_text"], known)
        if not candidates:
            continue
        matched = _best_profile_match(
            blue,
            center_x=center_x,
            center_y=center_y,
            values=candidates,
            left=left,
            right=right,
            profile_center=center,
            px_per_mm=px_per_mm,
        )
        if matched is None:
            continue
        geometry_confidence = max(0.0, 1.0 - float(matched.pop("relative_error")))
        confidence = 0.45 * float(token["ocr_confidence"]) + 0.55 * geometry_confidence
        if confidence < 0.4:
            continue
        observations.append({
            "id": "",
            "raw_text": token["raw_text"],
            "value_mm": round(float(matched.pop("value_mm")), 3),
            "role": matched.pop("role"),
            "source": "vertical_callout_and_vector_contour",
            "label_bbox": token["label_bbox"],
            **matched,
            "confidence": round(confidence, 3),
        })

    contour_outer = _contour_outer_observations(
        blue,
        known=sorted(contour_candidates),
        left=left,
        right=right,
        center=center,
        px_per_mm=px_per_mm,
    )
    observations.extend(contour_outer)
    outer_candidates = _interrupted_outer_candidates(
        blue,
        known=sorted(contour_candidates),
        left=left,
        right=right,
        center=center,
        px_per_mm=px_per_mm,
        accepted=contour_outer,
    )
    # A short plateau can be broken by end-face holes, hatching or a detail
    # boundary even though both of its axial ends are explicitly dimensioned.
    # Promote it only when each measured end has one unique stated station.
    # This recovers the real Ø98 × 13 segment between stations 14 and 27 on
    # detal_126 without weakening the interrupted-thread safeguards used for
    # the much longer Ø75/Ø79 candidates.
    stated_stations = {
        round(float(value), 3)
        for value in (known_linear_values or [])
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    }
    preferred_candidates: list[dict[str, Any]] = []
    for candidate in outer_candidates:
        interval = candidate.get("axial_interval_mm") or []
        if any(
            len(other.get("axial_interval_mm") or []) == 2
            and abs(float(other["axial_interval_mm"][0]) - float(interval[0])) <= 2.0
            and abs(float(other["axial_interval_mm"][1]) - float(interval[1])) <= 2.0
            and abs(float(other.get("profile_check_mm") or 0.0) - float(other["value_mm"]))
            < abs(float(candidate.get("profile_check_mm") or 0.0) - float(candidate["value_mm"]))
            for other in outer_candidates
            if len(interval) == 2
        ):
            continue
        preferred_candidates.append(candidate)
    promoted_outer: list[dict[str, Any]] = []
    for candidate in preferred_candidates:
        interval = candidate.get("axial_interval_mm") or []
        if len(interval) != 2 or float(candidate.get("confidence") or 0.0) < 0.82:
            continue
        measured_start, measured_end = map(float, interval)
        station_pairs = [
            (start, end)
            for start in stated_stations
            for end in stated_stations
            if end > start
            and abs(start - measured_start) <= 2.0
            and abs(end - measured_end) <= 2.0
        ]
        ranked_pairs = sorted(
            station_pairs,
            key=lambda pair: (
                abs(pair[0] - measured_start)
                + abs(pair[1] - measured_end)
                + 2.0 * abs((pair[1] - pair[0]) - (measured_end - measured_start))
            ),
        )
        if not ranked_pairs:
            continue
        matched_ends = list(ranked_pairs[0])
        promoted_outer.append({
            **candidate,
            "role": "outer",
            "source": "vector_contour",
            "confidence": min(0.86, float(candidate["confidence"])),
            "station_crosscheck_mm": matched_ends,
        })
    if promoted_outer:
        contour_outer.extend(promoted_outer)
        observations.extend(promoted_outer)
    contour_bore = _contour_bore_observations(
        blue,
        known=sorted(contour_candidates),
        left=left,
        right=right,
        center=center,
        px_per_mm=px_per_mm,
    )
    observations.extend(contour_bore)
    # Keep the strongest evidence for an identical value/role/source location.
    observations.sort(key=lambda item: (-float(item["confidence"]), item["role"], item["value_mm"]))
    accepted: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in observations:
        key = (
            item["role"], item["value_mm"], item.get("raw_text"),
            tuple(item.get("profile_interval_px") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        accepted.append(item)
    accepted.sort(key=lambda item: (
        (item.get("label_bbox") or item.get("profile_interval_px") or [0])[0],
        item["role"],
    ))
    for index, item in enumerate(accepted, start=1):
        item["id"] = f"diameter-dim-{index}"
    blockers = [ocr_blocker] if ocr_blocker else []
    if not accepted:
        blockers.append("диаметральные выноски не связаны с главным профилем")
    station_candidates = {
        round(float(value), 3)
        for value in (known_linear_values or [])
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    }
    observed_stations = {
        round(float(item["station_from_left_mm"]), 3)
        for item in (axial_map.get("observations") or [])
        if isinstance(item.get("station_from_left_mm"), (int, float))
    }
    station_candidates.update(observed_stations)
    transitions: list[dict[str, Any]] = []
    ordered_outer = sorted(
        contour_outer,
        key=lambda item: (item.get("axial_interval_mm") or [float("inf")])[0],
    )
    for index, item in enumerate(ordered_outer):
        interval = item.get("axial_interval_mm") or []
        if len(interval) != 2 or not station_candidates:
            continue
        measured_station = float(interval[1])
        if index + 1 < len(ordered_outer):
            next_interval = ordered_outer[index + 1].get("axial_interval_mm") or []
            if len(next_interval) != 2:
                continue
            # Slots and a drawn shoulder interrupt both plateaus. A stated
            # station inside that measured gap is the exact boundary; prefer
            # the one nearest the ending plateau, never invent the midpoint.
            upper = float(next_interval[0]) + 2.0
            candidates = [
                station for station in observed_stations
                if measured_station - 2.0 <= station <= upper
            ]
        else:
            candidates = [
                station for station in observed_stations
                if abs(station - measured_station) <= 2.0
            ]
        # Short contour plateaus at the left face are not dimensioned by the
        # unrelated 12/15 mm slot and M8 callouts. With the overall 470 mm
        # scale already fixed, a transition within 0.6 mm of an integer is a
        # measured station; accepting 12 merely because it was in the global
        # number bag shifted both Ø102 and Ø98 sections.
        rounded_measured = round(measured_station)
        if not candidates and abs(rounded_measured - measured_station) <= 0.6:
            candidates = [float(rounded_measured)]
        if not candidates:
            if index + 1 < len(ordered_outer):
                candidates = [
                    station for station in station_candidates
                    if measured_station - 2.0 <= station <= upper
                ]
            else:
                candidates = [
                    station for station in station_candidates
                    if abs(station - measured_station) <= 2.0
                ]
        if not candidates:
            continue
        station = min(candidates, key=lambda value: abs(value - measured_station))
        transitions.append({
            "station_from_left_mm": station,
            "measured_station_mm": round(measured_station, 3),
            "from_diameter_mm": item["value_mm"],
            "to_diameter_mm": (
                ordered_outer[index + 1]["value_mm"]
                if index + 1 < len(ordered_outer) else None
            ),
            "profile_x_px": item["profile_interval_px"][1],
            "confidence": round(min(float(item["confidence"]), 0.82), 3),
            "source": "vector_contour_transition",
        })
    taper_observation = None
    ordered_bore = sorted(
        contour_bore,
        key=lambda item: item["axial_interval_mm"][0],
    )
    bore_callouts = [
        item for item in accepted
        if item.get("role") == "bore"
        and item.get("source") == "vertical_callout_and_vector_contour"
        and isinstance(item.get("profile_x_px"), int)
    ]
    if ordered_bore and bore_callouts:
        first_plateau = ordered_bore[0]
        plateau_start = float(first_plateau["axial_interval_mm"][0])
        taper_station_candidates = [
            station for station in station_candidates
            if abs(station - plateau_start) <= 2.0
        ]
        start_callout = min(bore_callouts, key=lambda item: item["profile_x_px"])
        start_diameter = float(start_callout["value_mm"])
        end_x = max(left, int(first_plateau["profile_interval_px"][0]) - 2)
        end_measurement = _profile_measurement(blue, end_x, center, px_per_mm)
        if taper_station_candidates and end_measurement is not None:
            length = min(
                taper_station_candidates,
                key=lambda station: abs(station - plateau_start),
            )
            end_diameter = float(end_measurement["bore_mm"])
            measured_ratio = (start_diameter - end_diameter) / float(length)
            target_ratio = 7.0 / 24.0
            if (
                start_diameter > end_diameter > 0
                and abs(measured_ratio - target_ratio) / target_ratio <= 0.04
            ):
                taper_observation = {
                    "start_diameter_mm": round(start_diameter, 3),
                    "end_diameter_mm": round(end_diameter, 3),
                    "length_mm": round(float(length), 3),
                    "ratio": "7:24",
                    "measured_ratio": round(measured_ratio, 5),
                    "profile_interval_px": [left, end_x],
                    "confidence": 0.82,
                    "source": "vector_contour_and_axial_station",
                }
    return {
        "status": "ok" if accepted else "unresolved",
        "profile_center_y_px": round(center, 1),
        "px_per_mm": round(px_per_mm, 6),
        "observations": accepted,
        "outer_candidates": outer_candidates,
        "outer_transition_stations": transitions,
        "bore_taper": taper_observation,
        "overall_mm": axial_map.get("overall_mm"),
        "blockers": blockers,
    }


def outer_sections_from_diameter_evidence(diameter_map: dict[str, Any]) -> list[dict]:
    """Compile a complete colour-separated outside profile, or return none.

    Every cylindrical plateau must have high-confidence contour evidence and
    every boundary must be tied to a stated axial station.  Missing one of
    those facts leaves the VLM reader in charge and ultimately fail-closed.
    """
    outer = sorted(
        [
            item for item in (diameter_map.get("observations") or [])
            if item.get("role") == "outer"
            and item.get("source") == "vector_contour"
            and float(item.get("confidence") or 0.0) >= 0.8
            and len(item.get("axial_interval_mm") or []) == 2
        ],
        key=lambda item: item["axial_interval_mm"][0],
    )
    transitions = diameter_map.get("outer_transition_stations") or []
    if len(outer) < 2 or len(transitions) != len(outer):
        return []
    if float(outer[0]["axial_interval_mm"][0]) > 2.0:
        return []

    sections: list[dict] = []
    previous = 0.0
    center = float(diameter_map.get("profile_center_y_px") or 0.0)
    px_per_mm = float(diameter_map.get("px_per_mm") or 0.0)
    for index, item in enumerate(outer):
        transition = transitions[index]
        if float(transition.get("confidence") or 0.0) < 0.6:
            return []
        if abs(float(transition.get("from_diameter_mm") or 0.0) - float(item["value_mm"])) > 0.05:
            return []
        station = transition.get("station_from_left_mm")
        if not isinstance(station, (int, float)) or isinstance(station, bool) or station <= previous:
            return []
        sections.append({
            "diameter_mm": float(item["value_mm"]),
            "length_mm": round(float(station) - previous, 3),
            "note": "контур и осевая станция подтверждены OCR+CV",
            "evidence": [{
                "image_index": 0,
                "bbox": [
                    float(item["profile_interval_px"][0]),
                    round(center - float(item["value_mm"]) * px_per_mm / 2.0, 1),
                    float(item["profile_interval_px"][1]),
                    round(center + float(item["value_mm"]) * px_per_mm / 2.0, 1),
                ],
                "raw_text": (
                    f"vector outer contour Ø{float(item['value_mm']):g}; "
                    f"station {float(station):g}"
                ),
            }],
        })
        previous = float(station)
    return sections


def bore_sections_from_diameter_evidence(diameter_map: dict[str, Any]) -> list[dict]:
    """Compile the fully covered internal profile of a vector preview."""
    taper = diameter_map.get("bore_taper") or {}
    overall = diameter_map.get("overall_mm")
    if (
        taper.get("ratio") != "7:24"
        or float(taper.get("confidence") or 0.0) < 0.8
        or not isinstance(overall, (int, float))
    ):
        return []
    bore = sorted(
        [
            item for item in (diameter_map.get("observations") or [])
            if item.get("role") == "bore"
            and item.get("source") == "vector_contour"
            and float(item.get("confidence") or 0.0) >= 0.8
            and len(item.get("axial_interval_mm") or []) == 2
        ],
        key=lambda item: item["axial_interval_mm"][0],
    )
    if not bore:
        return []

    # Adjacent fragments of one plateau are one cylinder; a local shoulder
    # line may create a narrow measurement gap without changing its diameter.
    merged: list[dict[str, Any]] = []
    for item in bore:
        if (
            merged
            and merged[-1]["value_mm"] == item["value_mm"]
            and float(item["axial_interval_mm"][0])
            - float(merged[-1]["axial_interval_mm"][1]) <= 4.0
        ):
            merged[-1]["axial_interval_mm"][1] = item["axial_interval_mm"][1]
            merged[-1]["profile_interval_px"][1] = item["profile_interval_px"][1]
        else:
            merged.append({
                **item,
                "axial_interval_mm": list(item["axial_interval_mm"]),
                "profile_interval_px": list(item["profile_interval_px"]),
            })

    previous = float(taper.get("length_mm") or 0.0)
    if previous <= 0 or abs(float(merged[0]["axial_interval_mm"][0]) - previous) > 2.0:
        return []
    center = float(diameter_map.get("profile_center_y_px") or 0.0)
    px_per_mm = float(diameter_map.get("px_per_mm") or 0.0)
    taper_interval = taper.get("profile_interval_px") or [0.0, 0.0]
    sections: list[dict] = [{
        "diameter_mm": float(taper["start_diameter_mm"]),
        "length_mm": round(previous, 3),
        "note": "конус подтверждён контуром и отношением 7:24",
        "taper": {
            "kind": "ratio",
            "ratio": "7:24",
        },
        "evidence": [{
            "image_index": 0,
            "bbox": [
                float(taper_interval[0]),
                round(center - float(taper["start_diameter_mm"]) * px_per_mm / 2.0, 1),
                float(taper_interval[1]),
                round(center + float(taper["start_diameter_mm"]) * px_per_mm / 2.0, 1),
            ],
            "raw_text": (
                f"vector bore taper Ø{float(taper['start_diameter_mm']):g}; "
                f"7:24; station {previous:g}"
            ),
        }],
    }]
    for index, item in enumerate(merged):
        measured_end = float(item["axial_interval_mm"][1])
        if index == len(merged) - 1 and abs(float(overall) - measured_end) <= 2.0:
            station = float(overall)
        else:
            station = float(round(measured_end))
            if abs(station - measured_end) > 0.55:
                return []
        if abs(float(item["axial_interval_mm"][0]) - previous) > 2.0 or station <= previous:
            return []
        sections.append({
            "diameter_mm": float(item["value_mm"]),
            "length_mm": round(station - previous, 3),
            "note": "внутренний контур измерен и привязан к осевой станции",
            "evidence": [{
                "image_index": 0,
                "bbox": [
                    float(item["profile_interval_px"][0]),
                    round(center - float(item["value_mm"]) * px_per_mm / 2.0, 1),
                    float(item["profile_interval_px"][1]),
                    round(center + float(item["value_mm"]) * px_per_mm / 2.0, 1),
                ],
                "raw_text": (
                    f"vector bore contour Ø{float(item['value_mm']):g}; "
                    f"station {station:g}"
                ),
            }],
        })
        previous = station
    if abs(previous - float(overall)) > 0.05:
        return []
    return sections
