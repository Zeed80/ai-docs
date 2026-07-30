"""Spatial evidence for small features on a colour-separated turned drawing.

Small slots must not be inferred from a bag of OCR numbers.  This module finds
their closed blue contours, measures them with the same axial calibration as
the main profile and returns the source rectangles.  Exact stated dimensions
are attached only when a callout agrees with the independent contour measure.
"""

from __future__ import annotations

from typing import Any


def _nearest_stated(
    measured: float,
    values: list[float],
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> float | None:
    candidates = [
        float(value) for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
        and abs(float(value) - measured)
        <= max(absolute_tolerance, measured * relative_tolerance)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs(value - measured))


def localize_turned_features(
    image: Any,
    axial_map: dict[str, Any],
    known_linear_values: list[float],
    *,
    profile_center_y_px: float | None = None,
    known_diameter_values: list[float] | None = None,
    outer_diameter_values: list[float] | None = None,
) -> dict[str, Any]:
    """Locate longitudinal keyway outlines in the source coordinate system.

    The detector intentionally requires saturated-blue vector geometry.  In a
    monochrome scan, annotations and contours cannot be separated reliably and
    the result remains unresolved for the VLM/reviewer instead of fabricating
    a feature.
    """
    import cv2
    import numpy as np

    overall = axial_map.get("overall_mm")
    datum = axial_map.get("datum_line") or []
    if (
        not isinstance(overall, (int, float))
        or len(datum) != 2
        or not all(isinstance(value, (int, float)) for value in datum)
        or float(datum[1]) <= float(datum[0])
    ):
        return {
            "status": "unresolved",
            "keyway_candidates": [],
            "blockers": ["нет проверенной осевой калибровки"],
        }

    rgb = np.asarray(image.convert("RGB"))
    blue = (
        (rgb[:, :, 2] >= 180)
        & (rgb[:, :, 0] <= 60)
        & (rgb[:, :, 1] <= 60)
    ).astype("uint8")
    if int(blue.sum()) < 1000:
        return {
            "status": "unresolved",
            "keyway_candidates": [],
            "blockers": ["геометрия не отделена от аннотаций по цвету"],
        }

    left, right = float(datum[0]), float(datum[1])
    px_per_mm = (right - left) / float(overall)
    if px_per_mm <= 0:
        return {
            "status": "unresolved",
            "keyway_candidates": [],
            "blockers": ["ошибка масштаба главного вида"],
        }

    # A small closing kernel joins anti-aliased fragments of one outline.  It
    # does not bridge distinct features: five pixels are under 2 mm here.
    closed = cv2.morphologyEx(
        blue,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), dtype="uint8"),
    )
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(closed)

    height = rgb.shape[0]
    candidates: list[dict[str, Any]] = []
    for x, y, width, component_height, area in stats[1:]:
        if x < left - 3 or x + width > right + 3:
            continue
        if not (60 <= width <= 420 and 12 <= component_height <= 70):
            continue
        if width / max(component_height, 1) < 3.5 or area < 150:
            continue
        # The longitudinal profile occupies the middle of this sheet.  Closed
        # keyway outlines are shown above or below it; this also excludes the
        # many long cylindrical contour fragments.
        component_center_y = y + component_height / 2.0
        if height * 0.22 <= component_center_y <= height * 0.44:
            continue

        measured_length = (width - 1) / px_per_mm
        measured_width = (component_height - 1) / px_per_mm
        if not (18 <= measured_length <= 140 and 4 <= measured_width <= 22):
            continue
        stated_length = _nearest_stated(
            measured_length,
            known_linear_values,
            absolute_tolerance=2.0,
            relative_tolerance=0.035,
        )
        stated_width = _nearest_stated(
            measured_width,
            [value for value in known_linear_values if 3 <= value <= 20],
            absolute_tolerance=1.5,
            relative_tolerance=0.18,
        )
        depth_observation = None
        # On the longitudinal view, a depth dimension placed between an upper
        # slot and the turned surface is spatially attributable to that slot.
        # A number elsewhere on the sheet (notably roughness 3.2 or radius R4)
        # is not.  The lower slot on the control sheet therefore remains
        # unresolved until its removed section is explicitly linked.
        if (
            isinstance(profile_center_y_px, (int, float))
            and component_center_y < float(profile_center_y_px)
        ):
            import pytesseract

            center_x = x + width / 2.0
            crop_box = (
                max(0, int(center_x - 68)),
                max(0, int(y + component_height - 1)),
                min(rgb.shape[1], int(center_x + 32)),
                min(rgb.shape[0], int(y + component_height + 134)),
            )
            crop = image.crop(crop_box).rotate(90, expand=True)
            raw = pytesseract.image_to_string(
                crop, lang="rus+eng", config="--psm 11"
            )
            tokens: list[float] = []
            import re

            for match in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)", raw):
                value = float(match.group(1).replace(",", "."))
                if 0 < value <= max(measured_width, 1.0):
                    tokens.append(value)
            matched_depths = [
                value for value in tokens
                if _nearest_stated(
                    value,
                    known_linear_values,
                    absolute_tolerance=0.15,
                    relative_tolerance=0.01,
                ) is not None
            ]
            if len(set(matched_depths)) == 1:
                depth = matched_depths[0]
                depth_observation = {
                    "value_mm": depth,
                    "raw_text": raw.strip()[:80],
                    "bbox": list(crop_box),
                    "relation": "between_keyway_and_outer_surface",
                    "confidence": 0.72,
                }
        axial_start = (float(x) - left) / px_per_mm
        confidence = 0.62
        if stated_length is not None:
            confidence += 0.16
        if stated_width is not None:
            confidence += 0.12
        candidates.append({
            "id": "",
            "kind": "keyway_outline",
            "bbox": [int(x), int(y), int(x + width - 1), int(y + component_height - 1)],
            "axial_start_mm": round(axial_start, 3),
            "measured_length_mm": round(measured_length, 3),
            "stated_length_mm": stated_length,
            "measured_width_mm": round(measured_width, 3),
            "stated_width_mm": stated_width,
            "depth_observation": depth_observation,
            "source": "vector_contour_and_axial_scale",
            "confidence": round(confidence, 3),
        })

    candidates.sort(key=lambda item: item["axial_start_mm"])
    for index, item in enumerate(candidates, start=1):
        item["id"] = f"keyway-{index}"

    # A second keyway depth can be stated on a removed/end section rather than
    # beside the longitudinal slot.  Link it only when all three observations
    # agree: the section contains a slot of the same width, its outer circle
    # matches a verified outer diameter, and OCR of the adjacent vertical
    # dimension matches the independently measured notch depth.
    square_sections = [
        tuple(int(value) for value in row)
        for row in stats[1:]
        if row[0] > right + rgb.shape[1] * 0.05
        and row[1] < rgb.shape[0] * 0.3
        and 100 <= row[2] <= 360
        and 0.8 <= row[2] / max(row[3], 1) <= 1.2
        and row[4] >= 500
    ]
    section = min(square_sections, key=lambda row: row[1], default=None)
    if section is not None:
        sx, sy, sw, sh, _area = section
        bottom = sy + sh - 1
        search_y0 = sy + int(sh * 0.72)
        wall_columns = []
        for column in range(sx + int(sw * 0.35), sx + int(sw * 0.65)):
            ys = np.where(blue[search_y0:bottom + 1, column])[0] + search_y0
            runs: list[list[int]] = []
            for row in ys.tolist():
                if not runs or row > runs[-1][-1] + 1:
                    runs.append([row])
                else:
                    runs[-1].append(row)
            bottom_run = next(
                (
                    run for run in reversed(runs)
                    if run[-1] >= bottom - 2 and len(run) >= 7
                ),
                None,
            )
            if bottom_run:
                wall_columns.append((column, bottom_run[0], bottom_run[-1]))
        wall_groups: list[list[tuple[int, int, int]]] = []
        for wall in wall_columns:
            if not wall_groups or wall[0] > wall_groups[-1][-1][0] + 1:
                wall_groups.append([wall])
            else:
                wall_groups[-1].append(wall)
        walls = [
            (
                int(round((group[0][0] + group[-1][0]) / 2)),
                min(item[1] for item in group),
            )
            for group in wall_groups
        ]
        label_box = (
            max(0, sx + sw - 84),
            bottom,
            min(rgb.shape[1], sx + sw + 16),
            min(rgb.shape[0], bottom + 95),
        )
        import pytesseract
        import re

        raw_depth = pytesseract.image_to_string(
            image.crop(label_box).rotate(-90, expand=True),
            lang="rus+eng",
            config="--psm 11",
        ).strip()
        digits = "".join(re.findall(r"\d", raw_depth))
        depth_hypotheses = [float(f"{digits[:-1]}.{digits[-1]}")] if len(digits) == 2 else []
        matches: list[dict[str, Any]] = []
        for slot in candidates:
            if slot.get("depth_observation") is not None:
                continue
            slot_width = slot.get("stated_width_mm")
            if not isinstance(slot_width, (int, float)):
                continue
            for first_index, (first_x, first_top) in enumerate(walls):
                for second_x, second_top in walls[first_index + 1:]:
                    separation = second_x - first_x
                    for outer_diameter in outer_diameter_values or []:
                        if not isinstance(outer_diameter, (int, float)) or outer_diameter <= 0:
                            continue
                        section_scale = (sw - 1) / float(outer_diameter)
                        measured_width = separation / section_scale
                        if abs(measured_width - float(slot_width)) > float(slot_width) * 0.08:
                            continue
                        measured_depth = (bottom - min(first_top, second_top)) / section_scale
                        for depth in depth_hypotheses:
                            if depth >= float(slot_width):
                                continue
                            if abs(measured_depth - depth) > max(0.25, depth * 0.08):
                                continue
                            matches.append({
                                "slot": slot,
                                "depth": depth,
                                "measured_depth": measured_depth,
                                "outer_diameter": float(outer_diameter),
                                "walls": [first_x, second_x],
                            })
        unique = {
            (item["slot"]["id"], item["depth"]): item for item in matches
        }
        if len(unique) == 1:
            match = next(iter(unique.values()))
            match["slot"]["depth_observation"] = {
                "value_mm": match["depth"],
                "raw_text": raw_depth[:80],
                "bbox": list(label_box),
                "relation": "matched_removed_section_keyway",
                "section_bbox": [sx, sy, sx + sw - 1, bottom],
                "section_outer_diameter_mm": match["outer_diameter"],
                "measured_depth_mm": round(match["measured_depth"], 3),
                "confidence": 0.76,
            }
    radial_candidates: list[dict[str, Any]] = []
    radial_blockers: list[str] = []
    small_diameters = sorted({
        float(value) for value in (known_diameter_values or [])
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 3 <= float(value) <= 25
    })
    if isinstance(profile_center_y_px, (int, float)) and small_diameters:
        center_y = int(float(profile_center_y_px))
        y0, y1 = max(0, center_y - 195), min(rgb.shape[0], center_y + 195)
        column_counts = blue[y0:y1, int(left):int(right) + 1].sum(axis=0)
        strong = np.where((column_counts >= 18) & (column_counts <= 90))[0] + int(left)
        line_groups: list[list[int]] = []
        for column in strong.tolist():
            if not line_groups or column > line_groups[-1][-1] + 1:
                line_groups.append([column])
            else:
                line_groups[-1].append(column)
        lines = [int(round((group[0] + group[-1]) / 2)) for group in line_groups]
        for index, first in enumerate(lines):
            first_rows = blue[y0:y1, first].astype(bool)
            for second in lines[index + 1:]:
                measured = (second - first) / px_per_mm
                if measured > 26:
                    break
                supported = [
                    value for value in small_diameters
                    if abs(value - measured) <= max(0.8, value * 0.07)
                ]
                if not supported:
                    continue
                second_rows = blue[y0:y1, second].astype(bool)
                union = int((first_rows | second_rows).sum())
                similarity = (
                    int((first_rows & second_rows).sum()) / union if union else 0.0
                )
                if similarity < 0.78:
                    continue
                station = (((first + second) / 2.0) - left) / px_per_mm
                radial_candidates.append({
                    "id": "",
                    "kind": "radial_opening_walls",
                    "bbox": [first, y0, second, y1 - 1],
                    "axial_position_mm": round(station, 3),
                    "measured_axial_span_mm": round(measured, 3),
                    "supported_diameters_mm": supported,
                    "wall_row_similarity": round(similarity, 3),
                    "source": "paired_vector_walls_and_axial_scale",
                    "confidence": 0.78 if len(supported) == 1 else 0.62,
                })
    radial_candidates.sort(key=lambda item: item["axial_position_mm"])
    for index, item in enumerate(radial_candidates, start=1):
        item["id"] = f"radial-opening-{index}"
        if len(item["supported_diameters_mm"]) != 1:
            radial_blockers.append(
                f"{item['id']}: контур допускает несколько диаметров "
                + "/".join(f"Ø{value:g}" for value in item["supported_diameters_mm"])
            )
    for diameter in small_diameters:
        matches = [
            item for item in radial_candidates
            if diameter in item["supported_diameters_mm"]
        ]
        if len(matches) > 1:
            radial_blockers.append(
                f"Ø{diameter:g}: найдено несколько осевых положений "
                + ", ".join(f"{item['axial_position_mm']:g}" for item in matches)
            )

    diameter_labels: list[dict[str, Any]] = []
    if small_diameters:
        from pytesseract import Output

        label_data = pytesseract.image_to_data(
            image,
            lang="rus+eng",
            config="--psm 11",
            output_type=Output.DICT,
        )
        for index, raw_value in enumerate(label_data.get("text") or []):
            raw_text = str(raw_value or "").strip()
            digits = "".join(character for character in raw_text if character.isdigit())
            if not digits:
                continue
            x = int(label_data["left"][index])
            y = int(label_data["top"][index])
            width = int(label_data["width"][index])
            token_height = int(label_data["height"][index])
            if (
                x < right - 30
                or x > right + 200
                or abs(
                    (y + token_height / 2) - float(profile_center_y_px or 0)
                ) > 320
            ):
                continue
            matches: list[float] = []
            for value in small_diameters:
                nominal = str(int(value)) if value.is_integer() else f"{value:g}".replace(".", "")
                direct = nominal in digits
                digit_iterator = iter(digits)
                one_noise_digit = (
                    len(digits) == len(nominal) + 1
                    and all(character in digit_iterator for character in nominal)
                )
                lost_leading_one = (
                    digits == "0" and nominal == "10" and raw_text[:1] in {"#", "Ø", "Ф"}
                )
                if direct or one_noise_digit or lost_leading_one:
                    matches.append(value)
            if len(matches) != 1:
                continue
            value = matches[0]
            diameter_labels.append({
                "value_mm": value,
                "raw_text": raw_text,
                "bbox": [x, y, x + width, y + token_height],
                "side": (
                    "top"
                    if y + token_height / 2 < float(profile_center_y_px or 0)
                    else "bottom"
                ),
                "confidence": 0.68,
                "source": "spatial_ocr_small_diameter_callout",
            })

    blockers = [] if candidates else ["замкнутые контуры шпоночных пазов не локализованы"]
    blockers.extend(radial_blockers)
    return {
        "status": "ok" if candidates else "unresolved",
        "keyway_candidates": candidates,
        "radial_opening_candidates": radial_candidates,
        "diameter_label_observations": diameter_labels,
        "blockers": blockers,
    }
