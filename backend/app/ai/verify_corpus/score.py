"""Спек, прочитанный конвейером, против эталона генератора — поле за полем.

Базовая линия плана (задача M5): что текущий конвейер «по описанию» читает с
листа, для которого известна истина. Одно итоговое число здесь вредно — оно
прячет, КАКОЕ поле подводит, — поэтому оценка отдаётся по полям.

Отверстия сравниваются после раскрытия массивов в отдельные центры: четыре
отверстия по углам и «прямоугольный массив 2×2» — одна и та же деталь, и
ридер вправе назвать её любым из двух способов. Через центры же проверяется
фаза окружности болтов — её продукт сейчас не читает (всегда 0°).
"""

from __future__ import annotations

import math
from typing import Any

# Тот же допуск, что у консенсуса: 0,5 % или 0,05 мм.
_RELATIVE = 0.005
_FLOOR = 0.05
# Положение отверстия: читается из размеров, а не мерится, — допуск в мм.
_POSITION_MM = 1.0


def _agree(left: Any, right: Any) -> bool:
    if not _number(left) or not _number(right):
        return False
    return abs(float(left) - float(right)) <= max(_FLOOR, abs(float(right)) * _RELATIVE)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def expand_holes(profile: dict[str, Any]) -> list[tuple[float, float, float]]:
    """Все отверстия профиля как ``(x, y, Ø)`` — с раскрытыми массивами."""
    holes: list[tuple[float, float, float]] = []
    for hole in profile.get("holes") or []:
        if isinstance(hole, dict) and all(
            _number(hole.get(key)) for key in ("center_x_mm", "center_y_mm", "diameter_mm")
        ):
            holes.append(
                (float(hole["center_x_mm"]), float(hole["center_y_mm"]), float(hole["diameter_mm"]))
            )
    for pattern in profile.get("hole_patterns") or []:
        if not isinstance(pattern, dict) or not _number(pattern.get("hole_diameter_mm")):
            continue
        diameter = float(pattern["hole_diameter_mm"])
        kind = pattern.get("kind") or "bolt_circle"
        if kind == "bolt_circle" and _number(pattern.get("count")):
            radius = float(pattern.get("bolt_circle_diameter_mm") or 0.0) / 2.0
            start = float(pattern.get("start_angle_deg") or 0.0)
            count = int(pattern["count"])
            for index in range(count):
                angle = math.radians(start + 360.0 * index / count)
                holes.append((radius * math.cos(angle), radius * math.sin(angle), diameter))
        elif kind == "rectangular" and all(
            _number(pattern.get(key))
            for key in (
                "rows",
                "columns",
                "spacing_x_mm",
                "spacing_y_mm",
                "start_x_mm",
                "start_y_mm",
            )
        ):
            for row in range(int(pattern["rows"])):
                for column in range(int(pattern["columns"])):
                    holes.append(
                        (
                            float(pattern["start_x_mm"]) + column * float(pattern["spacing_x_mm"]),
                            float(pattern["start_y_mm"]) + row * float(pattern["spacing_y_mm"]),
                            diameter,
                        )
                    )
        elif kind == "linear" and all(
            _number(pattern.get(key))
            for key in ("count", "spacing_mm", "direction_deg", "start_x_mm", "start_y_mm")
        ):
            angle = math.radians(float(pattern["direction_deg"]))
            for index in range(int(pattern["count"])):
                step = index * float(pattern["spacing_mm"])
                holes.append(
                    (
                        float(pattern["start_x_mm"]) + step * math.cos(angle),
                        float(pattern["start_y_mm"]) + step * math.sin(angle),
                        diameter,
                    )
                )
    return holes


def _match(truth: list, read: list, same) -> tuple[int, int, int]:
    """Сколько эталонных найдено, сколько всего эталонных, сколько лишних прочитано."""
    pool = list(read)
    hits = 0
    for item in truth:
        found = next((other for other in pool if same(item, other)), None)
        if found is not None:
            pool.remove(found)
            hits += 1
    return hits, len(truth), len(pool)


def _field(hits: int, expected: int, extra: int = 0) -> dict[str, int]:
    return {"found": hits, "expected": expected, "extra": extra}


def score_spec(truth_spec: dict[str, Any], read_spec: dict[str, Any] | None) -> dict[str, Any]:
    """Оценка прочитанного спека против эталона по полям."""
    read_spec = read_spec or {}
    truth = truth_spec.get("main_view") or {}
    read = read_spec.get("main_view") or {}
    if truth.get("outer"):
        return {"kind": "rotation", **_score_rotation(truth, read)}
    if truth_spec.get("welds"):
        return {"kind": "weldment", **_score_weldment(truth_spec, read_spec)}
    if isinstance(truth.get("sheet_metal"), dict):
        return {"kind": "sheet_metal", **_score_sheet_metal(truth, read)}
    return {"kind": "profile", **_score_profile(truth, read)}


def _score_weldment(truth: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    """Сварной узел (X3): пластины по размерам, рёбра по месту, швы по типу и катету."""
    from app.ai.cad_solid import _body_box

    def sizes(spec: dict) -> list[tuple]:
        return [
            tuple(
                (part.get("profile") or {}).get(key)
                for key in ("width_mm", "height_mm", "thickness_mm")
            )
            for part in spec.get("parts") or []
            if isinstance(part, dict)
        ]

    def boxes(spec: dict) -> list:
        found = []
        for part in spec.get("parts") or []:
            try:
                box = _body_box(part) if isinstance(part, dict) else None
            except (TypeError, ValueError):
                box = None
            if box is not None:
                found.append(box)
        return found

    same_size = lambda a, b: all(_agree(x, y) for x, y in zip(a, b, strict=True))  # noqa: E731
    same_box = lambda a, b: all(  # noqa: E731
        _agree(x, y) for x, y in zip(a[0] + a[1], b[0] + b[1], strict=True)
    )
    same_weld = lambda a, b: (  # noqa: E731
        str(a.get("designation")) == str(b.get("designation"))
        and _agree(a.get("leg_mm"), b.get("leg_mm"))
    )
    return {
        "class_ok": len(read.get("parts") or []) >= 2,
        "plates": _field(*_match(sizes(truth), sizes(read), same_size)),
        "placed": _field(*_match(boxes(truth), boxes(read), same_box)),
        "welds": _field(*_match(truth.get("welds") or [], read.get("welds") or [], same_weld)),
    }


def _score_sheet_metal(truth: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    """Гнутая деталь (X4): класс, полки по порядку (или в обратном — это та же
    деталь, прочитанная с другого края), гибы, R, s, ширина."""
    t_sheet = truth["sheet_metal"]
    r_sheet = read.get("sheet_metal") if isinstance(read.get("sheet_metal"), dict) else {}
    t_flanges = list(t_sheet.get("flanges_mm") or [])
    r_flanges = list(r_sheet.get("flanges_mm") or [])

    def same_run(left: list, right: list) -> bool:
        return len(left) == len(right) and all(
            _agree(a, b) for a, b in zip(left, right, strict=True)
        )

    return {
        "class_ok": bool(r_sheet),
        "flanges_exact": same_run(t_flanges, r_flanges) or same_run(t_flanges, r_flanges[::-1]),
        "bends_ok": len(r_sheet.get("turns") or []) == len(t_sheet.get("turns") or []),
        "radius_ok": _agree(r_sheet.get("radius_mm"), t_sheet.get("radius_mm")),
        "thickness_ok": _agree(r_sheet.get("thickness_mm"), t_sheet.get("thickness_mm")),
        "width_ok": _agree(r_sheet.get("width_mm"), t_sheet.get("width_mm")),
    }


def _score_rotation(truth: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    t_outer = [s for s in truth.get("outer") or [] if isinstance(s, dict)]
    r_outer = [s for s in read.get("outer") or [] if isinstance(s, dict)]
    same_count = len(t_outer) == len(r_outer)
    steps_exact = same_count and all(
        _agree(r.get("diameter_mm"), t.get("diameter_mm"))
        and _agree(r.get("length_mm"), t.get("length_mm"))
        for t, r in zip(t_outer, r_outer, strict=False)
    )
    diameters = _match(
        [s["diameter_mm"] for s in t_outer],
        [s.get("diameter_mm") for s in r_outer],
        _agree,
    )
    lengths_ok = (
        sum(
            1
            for t, r in zip(t_outer, r_outer, strict=False)
            if _agree(r.get("length_mm"), t.get("length_mm"))
        )
        if same_count
        else 0
    )
    t_total = sum(float(s["length_mm"]) for s in t_outer)
    r_total = sum(float(s.get("length_mm") or 0.0) for s in r_outer)

    def near(tolerance: float, *keys: str):
        return lambda t, r: all(
            _number(r.get(key)) and abs(float(r[key]) - float(t[key])) <= tolerance for key in keys
        )

    features = {
        "keyways": _match(
            truth.get("keyways") or [],
            read.get("keyways") or [],
            near(_POSITION_MM, "axial_start_mm", "length_mm"),
        ),
        "grooves": _match(
            truth.get("grooves") or [],
            read.get("grooves") or [],
            near(_POSITION_MM, "axial_position_mm"),
        ),
        "cross_holes": _match(
            truth.get("cross_holes") or [],
            read.get("cross_holes") or [],
            near(_POSITION_MM, "axial_position_mm", "diameter_mm"),
        ),
        "chamfers": _match(
            truth.get("chamfers") or [],
            read.get("chamfers") or [],
            lambda _t, _r: True,
        ),
    }
    t_bore = [s for s in truth.get("bore") or [] if isinstance(s, dict)]
    r_bore = [s for s in read.get("bore") or [] if isinstance(s, dict)]
    return {
        "geometry": bool(r_outer),
        "class_ok": bool(r_outer) and not read.get("profile"),
        "profile_exact": steps_exact,
        "steps": {"truth": len(t_outer), "read": len(r_outer)},
        "diameters": _field(*diameters),
        "lengths": _field(lengths_ok, len(t_outer)),
        "overall_ok": _agree(r_total, t_total),
        "bore": _field(
            *_match(
                [s["diameter_mm"] for s in t_bore], [s.get("diameter_mm") for s in r_bore], _agree
            )
        ),
        **{name: _field(*result) for name, result in features.items()},
    }


def _score_profile(truth: dict[str, Any], read: dict[str, Any]) -> dict[str, Any]:
    t_profile = truth.get("profile") or {}
    r_profile = read.get("profile") or {}
    shape = t_profile.get("shape")
    sizes = ("diameter_mm",) if shape == "circle" else ("width_mm", "height_mm")
    t_holes = expand_holes(t_profile)
    r_holes = expand_holes(r_profile)
    by_diameter = _match([h[2] for h in t_holes], [h[2] for h in r_holes], _agree)
    by_position = _match(
        t_holes,
        r_holes,
        lambda t, r: _agree(r[2], t[2]) and math.hypot(r[0] - t[0], r[1] - t[1]) <= _POSITION_MM,
    )
    slots = _match(
        t_profile.get("slots") or [],
        r_profile.get("slots") or [],
        lambda t, r: all(
            _number(r.get(key)) and abs(float(r[key]) - float(t[key])) <= _POSITION_MM
            for key in ("center_x_mm", "center_y_mm", "length_mm", "width_mm")
        ),
    )

    # Карманы и приливы на гранях (X2, корпуса): элемент считается прочитанным,
    # если совпали грань, вид элемента, размер, глубина и положение на грани.
    def wall_agrees(t: dict, r: dict) -> bool:
        if r.get("kind") != t.get("kind") or r.get("on_plane") != t.get("on_plane"):
            return False
        if not _agree(r.get("depth_mm"), t.get("depth_mm")):
            return False
        keys = (
            ("diameter_mm",)
            if t.get("profile", "circle") == "circle"
            else ("width_mm", "height_mm")
        )
        if not all(_agree(r.get(key), t.get(key)) for key in keys):
            return False
        return all(
            abs(float(r.get(key) or 0.0) - float(t.get(key) or 0.0)) <= _POSITION_MM
            for key in ("center_u_mm", "center_v_mm")
        )

    walls = _match(
        [item for item in t_profile.get("wall_features") or [] if isinstance(item, dict)],
        [item for item in r_profile.get("wall_features") or [] if isinstance(item, dict)],
        wall_agrees,
    )
    radius = t_profile.get("corner_radius_mm")
    return {
        "geometry": bool(r_profile.get("shape")),
        "class_ok": r_profile.get("shape") == shape and not read.get("outer"),
        "sizes_ok": all(_agree(r_profile.get(key), t_profile.get(key)) for key in sizes),
        "thickness_ok": _agree(r_profile.get("thickness_mm"), t_profile.get("thickness_mm")),
        "corner_radius_ok": None
        if radius is None
        else _agree(r_profile.get("corner_radius_mm"), radius),
        "hole_diameters": _field(*by_diameter),
        "hole_positions": _field(*by_position),
        "slots": _field(*slots),
        "wall_features": _field(*walls),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Сводка по видам деталей: доли для флагов, found/expected для списков."""
    summary: dict[str, Any] = {}
    for kind in sorted({row["sheet_kind"] for row in rows}):
        scores = [row["score"] for row in rows if row["sheet_kind"] == kind]
        item: dict[str, Any] = {"sheets": len(scores)}
        for key in scores[0]:
            values = [score.get(key) for score in scores]
            if all(isinstance(value, bool) for value in values):
                item[key] = round(sum(values) / len(values), 3)
            elif all(isinstance(value, dict) and "expected" in value for value in values):
                found = sum(value["found"] for value in values)
                expected = sum(value["expected"] for value in values)
                item[key] = {
                    "found": found,
                    "expected": expected,
                    "extra": sum(value.get("extra", 0) for value in values),
                    "recall": round(found / expected, 3) if expected else None,
                }
            elif key == "corner_radius_ok":
                known = [value for value in values if value is not None]
                item[key] = round(sum(known) / len(known), 3) if known else None
        summary[kind] = item
    return summary
