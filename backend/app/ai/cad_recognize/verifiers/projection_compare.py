"""Проекция собранного тела вращения против проверенного вида листа (уровень 11).

Допуск графа требует свидетельства `projection_comparison`: геометрия, которую
построило ядро, совпала с листом. Сырая проекция с листом не сравнивается (E11,
проба 2026-09-17: покрытие 0,62–0,73) — лист рисуется по правилам ЕСКД, а
проекция их не знает. Поэтому сравнивается то, что лист рисует так же, как
проекция, по правилам:

* **огибающая** тела: радиус на станции — наибольший |v| горизонтальных видимых
  рёбер вида ``front`` ядра. Паз на одной стороне огибающую не меняет, а на
  главном виде листа паз обычно смотрит на наблюдателя;
* на листе радиус ищется **центром** основной линии хотя бы с одной стороны оси
  (паз может срезать одну кромку); допуск — max(2 px, 2 % радиуса): живой z4-r4
  нарисован шире на 1,5 %, а сравнение по краю толстой линии пропускало Ø +2 мм;
* **уступы** — основная вертикаль листа на станции уступа тела, не дальше
  `_SHOULDER_LINES` толщин линии (огибающая сдвиг уступа не видит: полоса у
  уступа и доля совпавшего на участке его поглощают — корпус v9, сдвиг 3 мм
  не пойман ни разу);
* **не сравниваются**: полосы у уступов и торцов (грань и фаска рисуются
  условно, z4-r4 — до 1,7 мм от надписи), ступени с резьбой (ГОСТ 2.311:
  изображение условное, z4-r4 рисует M18 как Ø16) и фланцы (на разрезе видно
  сечение ушка, а не проекция).

Система координат — вид, проверенный по листу (начало на левом торце у оси,
мм/px по обеим осям). Корпус v9 (dev, сканы 300 dpi): верная деталь прошла
27 из 27 (1 — «не измеримо»), Ø самой длинной ступени +2 мм не прошёл 28 из 28,
уступ, сдвинутый на 3 мм, — 26 из 28; фото и 150–200 dpi — «не измеримо».
Вердикт по участкам: участок с отсчётами прошёл, если
совпало не меньше `_SEGMENT_SHARE`; все прошли и сравнено не меньше
`_SAMPLED_SHARE` длины — «passed».
"""

from __future__ import annotations

from typing import Any

# Полоса у уступа/торца: грань и фаска рисуются условно. В толщинах основной
# линии листа (z4-r4: 4 × 0,52 мм ≈ 2,1 — уступы там до 1,7 мм от надписей),
# но не меньше миллиметра детали (втулка 4:1 — полоса 2 мм съедала половину).
_SHOULDER_MARGIN_LINES = 4.0
_SHOULDER_MARGIN_MIN_MM = 1.0
_STEP_MM = 0.5
_SEGMENT_SHARE = 0.8
# Участок короче стольких отсчётов не судится (узкая ступень между уступами).
_MIN_SAMPLES = 4
# Сравнено не меньше этой доли длины — иначе «не измеримо», а не «совпало».
_SAMPLED_SHARE = 0.5
# Уступ тела и основная вертикаль листа — не дальше стольких толщин линии.
# Корпус v9 (128 уступов, чистый и 150 dpi): смещение 0 у всех найденных;
# живой z4-r4 — до 2,9 (лист не в масштабе): не проходит, и это верно.
_SHOULDER_LINES = 2.0
# Та же мера грубого листа, что у проверки вала (`shaft_profile._MIN_LINE_PX`).
_MIN_LINE_PX = 4.5
_RADIUS_SHARE = 0.02
_RADIUS_MIN_PX = 2.0


def _envelope(front: dict[str, Any]) -> tuple[list[tuple[float, float, float]], float]:
    bounds = front.get("bounds_mm") or {}
    u_min, u_max = float(bounds["u_min"]), float(bounds["u_max"])
    horizontals = []
    for item in front.get("visible") or []:
        if item.get("type") != "line" or len(item.get("points") or []) != 2:
            continue
        (a, b) = item["points"]
        if abs(float(a[1]) - float(b[1])) > 1e-3 or abs(float(a[0]) - float(b[0])) <= 1e-3:
            continue
        horizontals.append(
            (
                min(float(a[0]), float(b[0])) - u_min,
                max(float(a[0]), float(b[0])) - u_min,
                abs(float(a[1])),
            )
        )
    cuts = sorted({round(v, 4) for start, end, _r in horizontals for v in (start, end)})
    segments = []
    for start, end in zip(cuts, cuts[1:], strict=False):
        middle = (start + end) / 2.0
        radii = [r for a, b, r in horizontals if a <= middle <= b]
        if radii and end - start > 1e-3:
            segments.append((start, end, max(radii)))
    # Соседние участки одного радиуса — один участок (паз рвёт рёбра на куски).
    merged: list[tuple[float, float, float]] = []
    for start, end, radius in segments:
        if merged and abs(merged[-1][2] - radius) < 1e-3 and abs(merged[-1][1] - start) < 1e-3:
            merged[-1] = (merged[-1][0], end, radius)
        else:
            merged.append((start, end, radius))
    return merged, u_max - u_min


def _excluded_spans(spec: dict[str, Any]) -> list[tuple[float, float, str]]:
    body = spec.get("main_view") or {}
    spans: list[tuple[float, float, str]] = []
    position = 0.0
    for step in body.get("outer") or []:
        if not isinstance(step, dict):
            continue
        length = float(step.get("length_mm") or 0.0)
        if step.get("thread"):
            spans.append((position, position + length, "резьба (ГОСТ 2.311)"))
        position += length
    for flange in body.get("flanges") or []:
        if isinstance(flange, dict) and isinstance(flange.get("axial_start_mm"), (int, float)):
            start = float(flange["axial_start_mm"])
            spans.append((start, start + float(flange.get("thickness_mm") or 0.0), "фланец"))
    return spans


def _line_centre_near(column: Any, predicted: float, reach: float) -> float | None:
    """Центр прогона чернил, ближайшего к ожидаемой строке, в пределах reach."""
    import numpy as np

    low = max(0, int(predicted - reach))
    high = min(len(column), int(predicted + reach) + 1)
    rows = np.nonzero(column[low:high])[0]
    if rows.size == 0:
        return None
    runs = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
    centres = [low + (float(run[0]) + float(run[-1])) / 2.0 for run in runs]
    return min(centres, key=lambda centre: abs(centre - predicted))


def compare_turned_projection(
    gray: Any, frame: dict[str, Any] | None, front: dict[str, Any] | None, spec: dict[str, Any]
) -> dict[str, Any]:
    """Огибающая собранного тела против основных линий проверенного вида."""
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.flange_outline import _main_lines

    if not frame or not frame.get("origin_px") or not frame.get("mm_per_px"):
        return {"status": "unmeasurable", "reason": "вид не найден проверкой по листу"}
    if not front or not front.get("bounds_mm"):
        return {"status": "unmeasurable", "reason": "ядро не дало проекцию front"}
    gray = np.asarray(gray)
    line = main_line_px(gray)
    if line <= 0:
        return {"status": "unmeasurable", "reason": "основные линии листа не найдены"}
    if line < _MIN_LINE_PX:
        # Корпус v9: на фото (линия 1,8–2,6 px) Ø+2 мм «проходил» в 3 из 7, на
        # 200 dpi сдвиг уступа — в 7 из 28. Грубый лист продукт увеличивает до
        # этой проверки (E17), неувеличенный — не свидетельство.
        return {
            "status": "unmeasurable",
            "reason": f"основная линия {line:.1f} px тоньше {_MIN_LINE_PX:g} px — лист грубый",
        }
    main = _main_lines(gray, line) > 0
    height, width = gray.shape
    ox, oy = (float(v) for v in frame["origin_px"])
    k_u = float(frame["mm_per_px"])
    k_v = float(frame.get("mm_per_px_v") or k_u)
    segments, total = _envelope(front)
    excluded = _excluded_spans(spec)
    margin = max(_SHOULDER_MARGIN_MIN_MM, _SHOULDER_MARGIN_LINES * line * k_u)
    report_segments = []
    sampled = 0
    failed = []
    for start, end, radius in segments:
        stations = []
        s = start + margin
        while s <= end - margin + 1e-9:
            if not any(a - 1e-6 <= s <= b + 1e-6 for a, b, _why in excluded):
                stations.append(s)
            s += _STEP_MM
        if len(stations) < _MIN_SAMPLES:
            continue
        tolerance = max(_RADIUS_MIN_PX, _RADIUS_SHARE * radius / k_v)
        hits = 0
        offsets = []
        for station in stations:
            x = int(round(ox + station / k_u))
            if not 0 <= x < width:
                continue
            column = main[:, max(0, x - 1) : x + 2].any(axis=1)
            best = None
            for sign in (-1.0, 1.0):
                predicted = oy + sign * radius / k_v
                centre = _line_centre_near(column, predicted, tolerance + line)
                if centre is not None:
                    offset = abs(centre - predicted)
                    best = offset if best is None else min(best, offset)
            if best is not None and best <= tolerance:
                hits += 1
                offsets.append(best * k_v)
        share = hits / len(stations)
        sampled += len(stations)
        entry = {
            "from_mm": round(start, 2),
            "to_mm": round(end, 2),
            "diameter_mm": round(2.0 * radius, 3),
            "samples": len(stations),
            "matched_share": round(share, 3),
            "median_offset_mm": round(float(np.median(offsets)), 3) if offsets else None,
        }
        report_segments.append(entry)
        if share < _SEGMENT_SHARE:
            failed.append(entry)
    # Доля — от длины, которую правила вообще разрешают сравнивать.
    comparable = total - sum(max(0.0, min(b, total) - max(a, 0.0)) for a, b, _why in excluded)
    sampled_share = sampled * _STEP_MM / comparable if comparable > 0 else 0.0
    shoulders = shoulder_offsets(gray, frame, front, spec)
    shifted = [
        item
        for item in shoulders
        if item["offset_lines"] is not None and item["offset_lines"] > _SHOULDER_LINES
    ]
    result: dict[str, Any] = {
        "method": "turned_envelope_vs_sheet_main_lines",
        "segments": report_segments,
        "sampled_share": round(sampled_share, 3),
        "shoulders": shoulders,
        "excluded": [
            {"from_mm": round(a, 2), "to_mm": round(b, 2), "why": why} for a, b, why in excluded
        ],
        "tolerance": {
            "radius_share": _RADIUS_SHARE,
            "radius_min_px": _RADIUS_MIN_PX,
            "shoulder_margin_mm": round(margin, 3),
        },
    }
    if shifted:
        worst_shoulder = max(shifted, key=lambda item: item["offset_lines"])
        result["status"] = "failed"
        result["reason"] = (
            f"уступ тела на {worst_shoulder['station_mm']:g} мм стоит на листе в "
            f"{worst_shoulder['offset_mm']:g} мм ({worst_shoulder['offset_lines']:g} толщины "
            f"линии) — лист не в масштабе или деталь собрана не так"
        )
    elif failed:
        worst = min(failed, key=lambda item: item["matched_share"])
        result["status"] = "failed"
        result["reason"] = (
            f"огибающая тела не легла на лист на {len(failed)} участках; хуже всего "
            f"Ø{worst['diameter_mm']:g} на {worst['from_mm']:g}…{worst['to_mm']:g} мм "
            f"({worst['matched_share']:.0%})"
        )
    elif sampled_share < _SAMPLED_SHARE:
        result["status"] = "unmeasurable"
        result["reason"] = f"сравнено {sampled_share:.0%} длины — мало для вывода"
    else:
        result["status"] = "passed"
        result["reason"] = (
            f"огибающая тела совпала с видом листа: {len(report_segments)} участков, "
            f"сравнено {sampled_share:.0%} сравнимой длины"
        )
    return result


def shoulder_offsets(
    gray: Any, frame: dict[str, Any], front: dict[str, Any], spec: dict[str, Any]
) -> list[dict[str, Any]]:
    """Уступы собранного тела на листе: смещение основной вертикали, в толщинах линии.

    Уступ — смена радиуса огибающей между соседними участками. На листе на этой
    станции ищется столбец основных линий, закрывающий полосу между двумя
    радиусами (с одной стороны оси, где полоса чистая).
    """
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.flange_outline import _main_lines

    gray = np.asarray(gray)
    line = main_line_px(gray)
    if line <= 0:
        return []
    main = _main_lines(gray, line) > 0
    height, width = gray.shape
    ox, oy = (float(v) for v in frame["origin_px"])
    k_u = float(frame["mm_per_px"])
    k_v = float(frame.get("mm_per_px_v") or k_u)
    segments, _total = _envelope(front)
    excluded = _excluded_spans(spec)
    result = []
    for (a0, a1, r0), (_b0, _b1, r1) in zip(segments, segments[1:], strict=False):
        if abs(r0 - r1) * 2.0 < 1.0:
            continue
        station = a1
        if any(a - 1e-6 <= station <= b + 1e-6 for a, b, _why in excluded):
            continue
        low, high = sorted((r0, r1))
        x_station = ox + station / k_u
        reach = int(round(8.0 * line))
        best = None
        for sign in (-1.0, 1.0):
            y_a = oy + sign * (low + 0.25 * (high - low)) / k_v
            y_b = oy + sign * (high - 0.25 * (high - low)) / k_v
            top, bottom = int(min(y_a, y_b)), int(max(y_a, y_b))
            if bottom - top < 2 or top < 0 or bottom >= height:
                continue
            band = main[top:bottom, :]
            for dx in range(-reach, reach + 1):
                x = int(round(x_station)) + dx
                if 0 <= x < width and band[:, x].mean() >= 0.9:
                    if best is None or abs(dx) < abs(best):
                        best = dx
        result.append(
            {
                "station_mm": round(station, 2),
                "offset_lines": None if best is None else round(abs(best) / line, 2),
                "offset_mm": None if best is None else round(abs(best) * k_u, 3),
            }
        )
    return result
