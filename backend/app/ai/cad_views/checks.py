"""Этап E1: построенное тело против надписей листа.

Инженер проверяет перечерченную деталь так: каждая размерная надпись
чертежа должна находиться на детали. Для тела вращения Ø — площадка
профиля (наружная или расточка), длина — расстояние между двумя торцами
или уступами; для выдавливания — расстояние между вершинами контура или
центрами отверстий по одной оси, Ø — отверстие. Надпись, которой на теле
нет, — пропущенный элемент или ошибка чтения: она уходит оператору списком,
а доля объяснённых — в отчёт прогона. Это не доказательство верности тела
(совпасть может и случайно), а сигнал, чего в нём заведомо не хватает.
"""

from __future__ import annotations

from typing import Any


def _near(value: float, pool: list[float], share: float = 0.02, floor: float = 0.3) -> bool:
    return any(abs(value - v) <= max(share * max(v, 1e-6), floor) for v in pool)


def _differences(values: list[float]) -> list[float]:
    ordered = sorted(set(round(v, 3) for v in values))
    return [b - a for i, a in enumerate(ordered) for b in ordered[i + 1 :] if b - a > 0]


def body_measures(result: Any) -> tuple[list[float], list[float]]:
    """(диаметры тела, длины тела) в мм по результату метода views."""
    profile = result.profile or {}
    diameters: list[float] = []
    lengths: list[float] = []
    if profile.get("kind") == "extrude":
        xs: list[float] = [0.0]
        ys: list[float] = [0.0]
        for segment in profile.get("sketch") or []:
            x, y = segment["to"]
            xs.append(float(x))
            ys.append(float(y))
            if segment.get("kind") == "arc" and segment.get("center"):
                cx, cy = segment["center"]
                diameters.append(2.0 * ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5)
        for feature in result.features or []:
            params = feature.get("params") or {}
            if "diameter_mm" in params:
                diameters.append(float(params["diameter_mm"]))
            if "center_x_mm" in params:
                xs.append(float(params["center_x_mm"]))
                ys.append(float(params["center_y_mm"]))
        lengths = _differences(xs) + _differences(ys)
        thickness = profile.get("thickness_mm")
        if thickness:
            lengths.append(float(thickness))
        return diameters, lengths
    stations: list[float] = []
    for key in ("outer", "bore"):
        points = profile.get(key) or []
        for a, b in zip(points, points[1:]):
            if abs(a["r"] - b["r"]) <= 0.02 * max(a["r"], b["r"], 1.0) and b["z"] - a["z"] > 0.3:
                diameters.append(a["r"] + b["r"])
        stations.extend(p["z"] for p in points if p["z"] >= 0)
    for feature in result.features or []:
        if "diameter_mm" in feature:
            diameters.append(float(feature["diameter_mm"]))
        origin = feature.get("origin_mm")
        if origin:
            stations.append(float(origin[2]))
    lengths = _differences(stations)
    return diameters, lengths


def label_coverage(result: Any, label_texts: list[str]) -> dict[str, Any]:
    """Сколько размерных надписей листа есть на теле и какие — нет."""
    from app.ai.cad_views.labels import parse_label

    diameters, lengths = body_measures(result)
    explained: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for text in label_texts:
        label = parse_label(text)
        if label.value is None or label.kind not in ("diameter", "thread", "linear", "thickness"):
            continue
        key = f"{label.kind}:{label.value:g}"
        if key in seen:
            continue
        seen.add(key)
        if label.kind in ("diameter", "thread"):
            # Резьба рисуется по впадинам или по вершинам — допуск шире.
            share = 0.12 if label.kind == "thread" else 0.03
            ok = _near(label.value, diameters, share)
        else:
            ok = _near(label.value, lengths)
        (explained if ok else missing).append(text)
    total = len(explained) + len(missing)
    return {
        "explained": explained,
        "missing": missing,
        "share": round(len(explained) / total, 3) if total else None,
    }


__all__ = ["body_measures", "label_coverage"]
