"""Единая модель элементов детали (дорожка У, шаг У1).

Спек разложен по типам деталей: у тела вращения свои списки (поперечные
отверстия, пазы, канавки, осевые массивы, фланцы), у пластины — отверстия и
прорези контура, у корпуса — элементы на шести гранях габарита. Проверка и
ядро повторяют это деление, и каждый новый тип рождал ещё одну ветку, а
многоосевое тело вращения (радиальные отверстия под углом, отверстия во
фланце не по оси) не выражалось вовсе.

Здесь любая деталь — основа плюс список элементов. У элемента — вид, место в
3D (точка на поверхности, ось инструмента, опорное направление в плоскости
элемента), параметры и массив. Проверка выбирает вид листа, в котором ось
элемента смотрит на наблюдателя, — от типа детали она не зависит.

Система координат детали — та, в которой её строит ядро:

* тело вращения — ось +Z от левого торца (z = 0), угол 0° — направление +X;
* призматическая деталь (пластина, фланец, корпус) — X по ширине, Y по высоте
  от центра контура, Z по толщине от грани плана (z = 0: план смотрит на неё,
  глухие отверстия сверлятся с неё, см. `cad_solid`).

``axis`` у выреза — направление от поверхности в материал (куда идёт
инструмент), у прибавления (прилив, фланец) — наружу, куда растёт материал.

Шаг У1 только переводит спек в элементы, ничего не меняя в продукте:
``source_path`` у каждого элемента ведёт обратно в спек.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Vector = tuple[float, float, float]

# Группы спека, которые строят геометрию на основе, — каждая переводится.
# Тест-контракт сверяет: ни одна из них не теряется.
ROTATION_GROUPS = (
    "cross_holes",
    "keyways",
    "grooves",
    "axial_holes",
    "circular_hole_patterns",
    "chamfers",
    "fillets",
    "flanges",
    "face_grooves",
)
PROFILE_GROUPS = ("holes", "hole_patterns", "slots", "wall_features")


@dataclass(frozen=True)
class Placement:
    """Место элемента: точка на поверхности, ось инструмента, опорное направление."""

    origin: Vector
    axis: Vector
    ref: Vector = (1.0, 0.0, 0.0)


@dataclass
class Feature:
    kind: str
    placement: Placement
    params: dict[str, Any]
    source_path: str
    body_index: int = 0
    pattern: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)


def features_of(spec: dict[str, Any]) -> list[Feature]:
    """Все элементы всех тел спека в единой модели."""
    bodies = [body for body in spec.get("parts") or [] if isinstance(body, dict)]
    if not bodies:
        bodies = [spec.get("main_view") or {}]
        prefixes = ["main_view"]
    else:
        prefixes = [f"parts[{index}]" for index in range(len(bodies))]
    found: list[Feature] = []
    for index, (body, prefix) in enumerate(zip(bodies, prefixes, strict=True)):
        profile = body.get("profile") if isinstance(body.get("profile"), dict) else None
        if body.get("outer"):
            found += _rotation_features(body, prefix, index)
        if profile is not None:
            found += _profile_features(profile, f"{prefix}.profile", index, z0=0.0)
    return found


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------- тело вращения


def _stations(outer: list[dict]) -> list[tuple[float, float, float]]:
    """(от, до, радиус) ступеней по оси."""
    result, z = [], 0.0
    for section in outer:
        length, diameter = _num(section.get("length_mm")), _num(section.get("diameter_mm"))
        if not length or not diameter:
            continue
        result.append((z, z + length, diameter / 2.0))
        z += length
    return result


def _radius_at(stations: list[tuple[float, float, float]], z: float) -> float | None:
    for low, high, radius in stations:
        if low - 1e-6 <= z <= high + 1e-6:
            return radius
    return None


def _radial(angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return math.cos(angle), math.sin(angle)


def _rotation_features(body: dict, prefix: str, index: int) -> list[Feature]:
    stations = _stations(body.get("outer") or [])
    length = stations[-1][1] if stations else 0.0
    found: list[Feature] = []

    for i, hole in enumerate(body.get("cross_holes") or []):
        z, diameter = _num(hole.get("axial_position_mm")), _num(hole.get("diameter_mm"))
        if z is None or not diameter:
            continue
        angle = _num(hole.get("angle_deg")) or 0.0
        cos, sin = _radial(angle)
        radius = _radius_at(stations, z) or 0.0
        count = int(hole.get("count") or 1)
        found.append(
            Feature(
                kind="hole",
                placement=Placement(
                    (radius * cos, radius * sin, z), (-cos, -sin, 0.0), (0.0, 0.0, 1.0)
                ),
                params=_hole_params(hole, diameter),
                source_path=f"{prefix}.cross_holes[{i}]",
                body_index=index,
                pattern=(
                    {
                        "kind": "circular",
                        "count": count,
                        "axis": (0.0, 0.0, 1.0),
                        "centre": (0.0, 0.0, z),
                        "start_angle_deg": angle,
                        "spacing_deg": _num(hole.get("spacing_deg")) or 360.0 / count,
                    }
                    if count > 1
                    else None
                ),
                tags=["radial"],
            )
        )

    for i, keyway in enumerate(body.get("keyways") or []):
        start, run = _num(keyway.get("axial_start_mm")), _num(keyway.get("length_mm"))
        if start is None or not run:
            continue
        z = start + run / 2.0
        cos, sin = _radial(_num(keyway.get("angle_deg")) or 0.0)
        radius = _radius_at(stations, z) or 0.0
        found.append(
            Feature(
                kind="keyway",
                placement=Placement(
                    (radius * cos, radius * sin, z), (-cos, -sin, 0.0), (0.0, 0.0, 1.0)
                ),
                params={
                    "length_mm": run,
                    "width_mm": _num(keyway.get("width_mm")),
                    "depth_mm": _num(keyway.get("depth_mm")),
                    "end_type": keyway.get("end_type"),
                },
                source_path=f"{prefix}.keyways[{i}]",
                body_index=index,
            )
        )

    for i, groove in enumerate(body.get("grooves") or []):
        z = _num(groove.get("axial_position_mm"))
        if z is None:
            continue
        found.append(
            Feature(
                kind="groove",
                placement=Placement((0.0, 0.0, z), (0.0, 0.0, 1.0)),
                params={
                    "width_mm": _num(groove.get("width_mm")),
                    "depth_mm": _num(groove.get("depth_mm")),
                    "root_diameter_mm": _num(groove.get("root_diameter_mm")),
                    "internal": bool(groove.get("internal")),
                },
                source_path=f"{prefix}.grooves[{i}]",
                body_index=index,
                tags=["around_axis"],
            )
        )

    for group in ("axial_holes", "circular_hole_patterns"):
        for i, pattern in enumerate(body.get(group) or []):
            pcd = _num(pattern.get("bolt_circle_diameter_mm"))
            count = int(pattern.get("count") or 0)
            if not pcd or count < 1:
                continue
            start = _num(pattern.get("start_angle_deg")) or 0.0
            from_max = pattern.get("from_face") == "zmax"
            z = length if from_max else 0.0
            axis: Vector = (0.0, 0.0, -1.0 if from_max else 1.0)
            cos, sin = _radial(start)
            diameter = _num(pattern.get("hole_diameter_mm"))
            thread = pattern.get("thread") if isinstance(pattern.get("thread"), dict) else None
            if diameter is None and thread:
                diameter = _num(thread.get("nominal_diameter_mm"))
            params = _hole_params(pattern, diameter)
            if pattern.get("axis_mode") == "inclined":
                params["inclination_deg"] = _num(pattern.get("inclination_deg"))
                params["radial_direction"] = pattern.get("radial_direction")
            found.append(
                Feature(
                    kind="hole",
                    placement=Placement(
                        (pcd / 2.0 * cos, pcd / 2.0 * sin, z), axis, (1.0, 0.0, 0.0)
                    ),
                    params=params,
                    source_path=f"{prefix}.{group}[{i}]",
                    body_index=index,
                    pattern={
                        "kind": "circular",
                        "count": count,
                        "axis": (0.0, 0.0, 1.0),
                        "centre": (0.0, 0.0, z),
                        "start_angle_deg": start,
                        "spacing_deg": _num(pattern.get("spacing_deg")) or 360.0 / count,
                    },
                    tags=["axial"],
                )
            )

    for group, kind, size_key in (
        ("chamfers", "chamfer", "size_mm"),
        ("fillets", "fillet", "radius_mm"),
    ):
        for i, edge in enumerate(body.get(group) or []):
            location = edge.get("location")
            z = {"left_end": 0.0, "right_end": length}.get(location, _num(edge.get("at_z_mm")))
            if z is None:
                z = 0.0
            found.append(
                Feature(
                    kind=kind,
                    placement=Placement((0.0, 0.0, z), (0.0, 0.0, 1.0)),
                    params={
                        size_key: _num(edge.get(size_key)),
                        "angle_deg": _num(edge.get("angle_deg")),
                        "location": location,
                        "at_diameter_mm": _num(edge.get("at_diameter_mm")),
                    },
                    source_path=f"{prefix}.{group}[{i}]",
                    body_index=index,
                    tags=["around_axis", "edge"],
                )
            )

    for i, flange in enumerate(body.get("flanges") or []):
        start, thickness = _num(flange.get("axial_start_mm")), _num(flange.get("thickness_mm"))
        profile = flange.get("profile") if isinstance(flange.get("profile"), dict) else None
        if start is None or not thickness or profile is None:
            continue
        found.append(
            Feature(
                kind="flange",
                placement=Placement((0.0, 0.0, start), (0.0, 0.0, 1.0)),
                params={"thickness_mm": thickness, "shape": profile.get("shape")},
                source_path=f"{prefix}.flanges[{i}]",
                body_index=index,
            )
        )
        # Отверстия во фланце — параллельно оси, с грани фланца.
        found += _profile_features(
            profile, f"{prefix}.flanges[{i}].profile", index, z0=start, plate_axis_offset=True
        )

    for i, groove in enumerate(body.get("face_grooves") or []):
        at_right = groove.get("end") == "right"
        found.append(
            Feature(
                kind="face_groove",
                placement=Placement(
                    (0.0, 0.0, length if at_right else 0.0), (0.0, 0.0, -1.0 if at_right else 1.0)
                ),
                params={
                    "depth_mm": _num(groove.get("depth_mm")),
                    "outer_diameter_mm": _num(groove.get("outer_diameter_mm")),
                    "inner_diameter_mm": _num(groove.get("inner_diameter_mm")),
                },
                source_path=f"{prefix}.face_grooves[{i}]",
                body_index=index,
                tags=["around_axis"],
            )
        )
    return found


# ------------------------------------------------------------ призматическая


# Грани габарита в системе ядра (`server._work_plane_frame`): оси u, v, нормаль
# наружу и угол грани в коробке 0…W × 0…H × 0…T.
def _plane_frame(name: str, width: float, height: float, depth: float):
    frames = {
        "top": ((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.0)),
        "bottom": ((1, 0, 0), (0, -1, 0), (0, 0, -1), (0.0, height, depth)),
        "front": ((1, 0, 0), (0, 0, 1), (0, -1, 0), (0.0, height, 0.0)),
        "back": ((0, 0, 1), (1, 0, 0), (0, 1, 0), (0.0, 0.0, 0.0)),
        "left": ((0, 0, 1), (0, 1, 0), (-1, 0, 0), (width, 0.0, 0.0)),
        "right": ((0, 1, 0), (0, 0, 1), (1, 0, 0), (0.0, 0.0, 0.0)),
    }
    return frames[name]


def _profile_features(
    profile: dict,
    prefix: str,
    index: int,
    *,
    z0: float,
    plate_axis_offset: bool = False,
) -> list[Feature]:
    """Элементы контура: отверстия, массивы, прорези (с грани плана) и элементы граней."""
    found: list[Feature] = []
    thickness = _num(profile.get("thickness_mm")) or 0.0
    axis: Vector = (0.0, 0.0, 1.0)
    tag = "axial" if plate_axis_offset else "face"

    for i, hole in enumerate(profile.get("holes") or []):
        x, y = _num(hole.get("center_x_mm")), _num(hole.get("center_y_mm"))
        diameter = _num(hole.get("diameter_mm"))
        if x is None or y is None or not diameter:
            continue
        found.append(
            Feature(
                kind="hole",
                placement=Placement((x, y, z0), axis),
                params=_hole_params(hole, diameter),
                source_path=f"{prefix}.holes[{i}]",
                body_index=index,
                tags=[tag],
            )
        )

    for i, pattern in enumerate(profile.get("hole_patterns") or []):
        diameter = _num(pattern.get("hole_diameter_mm"))
        kind = pattern.get("kind") or "bolt_circle"
        if kind == "bolt_circle":
            pcd = _num(pattern.get("bolt_circle_diameter_mm")) or 0.0
            start = _num(pattern.get("start_angle_deg")) or 0.0
            cos, sin = _radial(start)
            origin: Vector = (pcd / 2.0 * cos, pcd / 2.0 * sin, z0)
            count = int(pattern.get("count") or 1)
            spec_pattern = {
                "kind": "circular",
                "count": count,
                "axis": (0.0, 0.0, 1.0),
                "centre": (0.0, 0.0, z0),
                "start_angle_deg": start,
                "spacing_deg": 360.0 / count,
            }
        elif kind == "linear":
            origin = (
                _num(pattern.get("start_x_mm")) or 0.0,
                _num(pattern.get("start_y_mm")) or 0.0,
                z0,
            )
            spec_pattern = {
                "kind": "linear",
                "count": int(pattern.get("count") or 1),
                "spacing_mm": _num(pattern.get("spacing_mm")),
                "direction": (*_radial(_num(pattern.get("direction_deg")) or 0.0), 0.0),
            }
        else:
            origin = (
                _num(pattern.get("start_x_mm")) or 0.0,
                _num(pattern.get("start_y_mm")) or 0.0,
                z0,
            )
            spec_pattern = {
                "kind": "rectangular",
                "rows": int(pattern.get("rows") or 1),
                "columns": int(pattern.get("columns") or 1),
                "spacing_x_mm": _num(pattern.get("spacing_x_mm")),
                "spacing_y_mm": _num(pattern.get("spacing_y_mm")),
            }
        found.append(
            Feature(
                kind="hole",
                placement=Placement(origin, axis),
                params=_hole_params(pattern, diameter),
                source_path=f"{prefix}.hole_patterns[{i}]",
                body_index=index,
                pattern=spec_pattern,
                tags=[tag],
            )
        )

    for i, slot in enumerate(profile.get("slots") or []):
        x, y = _num(slot.get("center_x_mm")), _num(slot.get("center_y_mm"))
        if x is None or y is None:
            continue
        rotation = _num(slot.get("rotation_deg")) or 0.0
        found.append(
            Feature(
                kind="slot",
                placement=Placement((x, y, z0), axis, (*_radial(rotation), 0.0)),
                params={
                    "length_mm": _num(slot.get("length_mm")),
                    "width_mm": _num(slot.get("width_mm")),
                    "through": True,
                },
                source_path=f"{prefix}.slots[{i}]",
                body_index=index,
                tags=[tag],
            )
        )

    width, height = _num(profile.get("width_mm")), _num(profile.get("height_mm"))
    for i, item in enumerate(profile.get("wall_features") or []):
        if not width or not height or not thickness:
            continue
        from app.ai.cad_solid import _wall_feature_params

        params = _wall_feature_params(item, width=width, height=height, thickness=thickness)
        if params is None:
            continue
        u, v, n, corner = _plane_frame(str(item.get("on_plane")), width, height, thickness)
        lx, ly = params["center_x_mm"], params["center_y_mm"]
        # Грань лежит на «толщине под гранью» вдоль нормали от угла рамки —
        # как у ядра (top/bottom — толщина, front/back — высота, left/right —
        # ширина).
        under = {"top": thickness, "bottom": thickness, "front": height, "back": height}.get(
            str(item.get("on_plane")), width
        )
        point = [corner[k] + lx * u[k] + ly * v[k] + under * n[k] for k in range(3)]
        origin = (point[0] - width / 2.0, point[1] - height / 2.0, point[2])
        outward = (float(n[0]), float(n[1]), float(n[2]))
        cut = item.get("kind") == "pocket"
        found.append(
            Feature(
                kind=str(item.get("kind")),
                placement=Placement(
                    origin,
                    (-outward[0], -outward[1], -outward[2]) if cut else outward,
                    (float(u[0]), float(u[1]), float(u[2])),
                ),
                params={
                    "profile": item.get("profile"),
                    "diameter_mm": _num(item.get("diameter_mm")),
                    "width_mm": _num(item.get("width_mm")),
                    "height_mm": _num(item.get("height_mm")),
                    "depth_mm": _num(item.get("depth_mm")),
                    "on_plane": item.get("on_plane"),
                },
                source_path=f"{prefix}.wall_features[{i}]",
                body_index=index,
                tags=["face"],
            )
        )
    return found


def _hole_params(item: dict, diameter: float | None) -> dict[str, Any]:
    depth = _num(item.get("depth_mm"))
    through = item.get("through")
    thread = item.get("thread") if isinstance(item.get("thread"), dict) else None
    return {
        "diameter_mm": diameter,
        "depth_mm": depth,
        "through": bool(through) if through is not None else depth is None,
        **({"thread": thread.get("designation")} if thread else {}),
        **(
            {"counterbore_diameter_mm": _num(item.get("counterbore_diameter_mm"))}
            if item.get("counterbore_diameter_mm")
            else {}
        ),
    }
