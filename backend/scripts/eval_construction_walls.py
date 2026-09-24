#!/usr/bin/env python3
"""Ф7.4: стены плана этажа — замер по растру против эталона из DXF.

Эталон берётся из САМОГО чертежа: в этих DWG слоёв нет (всё на слое 0), но
геометрия точна, и стена в ней — та же пара параллельных линий на расстоянии
толщины. Замер работает по растру того же листа. Сравниваются осевые и
толщины; лишнее считается отдельно — выдуманная стена хуже пропущенной.

Масштаб чертежа (мм на единицу) берётся из размерной цепочки между осями:
подпись «8000» между маркерами осей, расстояние между ними — в единицах.

Запуск в контейнере backend:

    python scripts/eval_construction_walls.py --dwg-dir /tmp/dwg --out /tmp/walls.json
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_NUMBER = re.compile(r"^\d{3,5}$")
# Допуски сравнения: осевая — доля толщины стены, толщина — доля от эталонной.
_POSITION_SHARE = 0.75
_THICKNESS_SHARE = 0.25
_LENGTH_SHARE = 0.5


def axis_markers(msp) -> list[tuple[float, float, float]]:
    """Маркеры координационных осей: (x, y, радиус) — окружности одного размера."""
    from collections import Counter

    circles = [
        (float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius))
        for e in msp.query("CIRCLE")
    ]
    if not circles:
        return []
    common = Counter(round(radius) for _x, _y, radius in circles).most_common(1)[0][0]
    return [item for item in circles if abs(item[2] - common) <= 0.25 * common]


def drawing_scale_mm(msp) -> float | None:
    """Миллиметров чертежа на единицу листа — по цепочке размеров между осями.

    Подпись «8000» стоит посередине между двумя маркерами осей: расстояние
    между ними в единицах и есть эти 8000 мм. Принимается, только когда
    согласны не меньше двух звеньев (одно звено — это догадка).
    """
    markers = axis_markers(msp)
    if len(markers) < 2:
        return None
    texts = []
    for entity in msp:
        if entity.dxftype() not in ("TEXT", "MTEXT"):
            continue
        value = (entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.text).strip()
        if _NUMBER.match(value):
            texts.append((float(value), float(entity.dxf.insert.x), float(entity.dxf.insert.y)))
    ratios: list[float] = []
    for axis in (0, 1):
        # Маркеры одного ряда: они стоят на одной линии поперёк своей оси.
        rows: dict[int, list[tuple[float, float, float]]] = {}
        for marker in markers:
            rows.setdefault(round(marker[1 - axis] / 20.0), []).append(marker)
        for row in rows.values():
            row = sorted(row, key=lambda item: item[axis])
            for first, second in zip(row, row[1:], strict=False):
                span = second[axis] - first[axis]
                if span <= 0:
                    continue
                middle = (first[axis] + second[axis]) / 2.0
                near = [
                    value
                    for value, x, y in texts
                    if abs((x if axis == 0 else y) - middle) <= 0.35 * span
                    and abs((y if axis == 0 else x) - first[1 - axis]) <= 4.0 * span
                ]
                if len(near) == 1:
                    ratios.append(near[0] / span)
    if len(ratios) < 2:
        return None
    ratios.sort()
    middle = ratios[len(ratios) // 2]
    agreeing = [value for value in ratios if abs(value - middle) <= 0.03 * middle]
    return round(middle, 4) if len(agreeing) >= 2 else None


def truth_walls(msp, scale_mm: float):
    """Стены из точной геометрии DXF, в единицах чертежа."""
    from app.ai.construction_walls import (
        MAX_THICKNESS_MM,
        MIN_LENGTH_MM,
        MIN_THICKNESS_MM,
        wall_pairs,
    )

    lines: list[tuple[str, float, float, float]] = []
    for entity in msp.query("LINE"):
        (x1, y1, _z1), (x2, y2, _z2) = entity.dxf.start, entity.dxf.end
        if abs(y1 - y2) < 0.5 and abs(x1 - x2) > 1.0:
            lines.append(("h", float(y1), float(min(x1, x2)), float(max(x1, x2))))
        elif abs(x1 - x2) < 0.5 and abs(y1 - y2) > 1.0:
            lines.append(("v", float(x1), float(min(y1, y2)), float(max(y1, y2))))
    return wall_pairs(
        lines,
        min_thickness=MIN_THICKNESS_MM / scale_mm,
        max_thickness=MAX_THICKNESS_MM / scale_mm,
        min_length=MIN_LENGTH_MM / scale_mm,
    )


def render_with_transform(doc, long_side: int):
    """Растр листа и перевод «единицы чертежа → пиксели» (та же фигура)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    msp = doc.modelspace()
    for insert in list(msp.query("INSERT")):
        if insert.dxf.name not in doc.blocks:
            msp.delete_entity(insert)
    for layer in doc.layers:
        try:
            layer.dxf.color = abs(int(layer.dxf.color)) or 7
            layer.thaw()
        except Exception:  # noqa: BLE001
            continue
    figure = plt.figure(dpi=100)
    axes = figure.add_axes([0, 0, 1, 1])
    backend = MatplotlibBackend(axes)
    config = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    try:
        Frontend(RenderContext(doc), backend, config=config).draw_entities(msp)
        backend.finalize()
        width, height = figure.get_size_inches()
        factor = long_side / 100.0 / max(width, height)
        figure.set_size_inches(width * factor, height * factor)
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=100, facecolor="white")
        x_limits, y_limits = axes.get_xlim(), axes.get_ylim()
    finally:
        plt.close(figure)
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    image = Image.open(io.BytesIO(buffer.getvalue())).convert("L")
    width_px, height_px = image.size

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (
            (x - x_limits[0]) / (x_limits[1] - x_limits[0]) * width_px,
            (y_limits[1] - y) / (y_limits[1] - y_limits[0]) * height_px,
        )

    unit_px = width_px / (x_limits[1] - x_limits[0])
    return image, to_px, unit_px


def truth_in_px(truth, to_px, unit_px: float):
    """Эталонные стены в пикселях растра: та же система, что у замера.

    Перевод — тем же преобразованием, каким рисовался лист: делить пиксели на
    масштаб мало, у растра своё начало координат, а ось y идёт вниз (первая
    версия харнесса сравнивала пиксели с единицами чертежа и получала 4
    совпадения из 168).
    """
    from app.ai.construction_walls import WallSegment

    converted = []
    for wall in truth:
        if wall.axis == "h":
            position = to_px(0.0, wall.position)[1]
            start = to_px(wall.start, 0.0)[0]
            end = to_px(wall.end, 0.0)[0]
        else:
            position = to_px(wall.position, 0.0)[0]
            start = to_px(0.0, wall.end)[1]
            end = to_px(0.0, wall.start)[1]
        converted.append(
            WallSegment(
                axis=wall.axis,
                position=position,
                start=min(start, end),
                end=max(start, end),
                thickness=wall.thickness * unit_px,
            )
        )
    return converted


def truth_openings(msp, walls, scale_mm: float) -> list[dict]:
    """Проёмы эталона: разрывы стен из DXF; дверь — дуга ARC радиусом в
    ширину проёма с центром у одного из его краёв (петля полотна)."""
    import math

    from app.ai.construction_walls import MAX_OPENING_MM, MIN_OPENING_MM, wall_gaps

    arcs = [
        (float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius))
        for e in msp.query("ARC")
    ]
    result = []
    for gap in wall_gaps(
        walls, min_gap=MIN_OPENING_MM / scale_mm, max_gap=MAX_OPENING_MM / scale_mm
    ):
        door = False
        for edge in (gap.start, gap.end):
            hinge = (edge, gap.position) if gap.axis == "h" else (gap.position, edge)
            for x, y, radius in arcs:
                if abs(radius - gap.width) > 0.15 * gap.width:
                    continue
                if math.hypot(x - hinge[0], y - hinge[1]) <= gap.thickness:
                    door = True
        result.append({"gap": gap, "door": door})
    return result


def openings_in_px(openings, to_px, unit_px: float) -> list[dict]:
    """Проёмы эталона — в пикселях растра, тем же преобразованием, что стены."""
    from app.ai.construction_walls import OpeningGap, WallSegment

    converted = []
    for item in openings:
        gap = item["gap"]
        wall = truth_in_px(
            [WallSegment(gap.axis, gap.position, gap.start, gap.end, gap.thickness)],
            to_px,
            unit_px,
        )[0]
        converted.append(
            {
                "gap": OpeningGap(wall.axis, wall.position, wall.start, wall.end, wall.thickness),
                "door": item["door"],
            }
        )
    return converted


def score_openings(truth: list[dict], found: list[dict]) -> dict:
    """Проёмы: найдено по месту и ширине; двери — верно ли узнана дуга."""
    pool = list(found)
    hits = doors_right = doors_truth = doors_false = 0
    for item in truth:
        gap = item["gap"]
        doors_truth += bool(item["door"])
        match = None
        for candidate in pool:
            other = candidate["gap"]
            if other.axis != gap.axis:
                continue
            if abs(other.position - gap.position) > max(_POSITION_SHARE * gap.thickness, 2.0):
                continue
            tolerance = max(0.25 * gap.width, 3.0)
            if abs(other.start - gap.start) > tolerance or abs(other.end - gap.end) > tolerance:
                continue
            match = candidate
            break
        if match is None:
            continue
        pool.remove(match)
        hits += 1
        if item["door"] and match["door"]:
            doors_right += 1
        if match["door"] and not item["door"]:
            doors_false += 1
    return {
        "truth": len(truth),
        "found": len(found),
        "hits": hits,
        "extra": len(pool),
        "doors_truth": doors_truth,
        "doors_right": doors_right,
        "doors_false": doors_false + sum(1 for item in pool if item["door"]),
    }


def score_markers(truth, found) -> dict:
    """Маркеры осей: найден ли кружок на месте эталонного (в радиус)."""
    pool = list(found)
    hits = 0
    for x, y, radius in truth:
        match = next(
            (item for item in pool if abs(item[0] - x) <= radius and abs(item[1] - y) <= radius),
            None,
        )
        if match is not None:
            pool.remove(match)
            hits += 1
    return {"truth": len(truth), "found": len(found), "hits": hits, "extra": len(pool)}


def score(truth, found) -> dict:
    """Сколько эталонных стен найдено и сколько найдено лишнего (всё в px)."""
    pool = list(found)
    hits = 0
    for wall in truth:
        match = None
        for candidate in pool:
            if candidate.axis != wall.axis:
                continue
            if abs(candidate.position - wall.position) > max(_POSITION_SHARE * wall.thickness, 2.0):
                continue
            if abs(candidate.thickness - wall.thickness) > max(
                _THICKNESS_SHARE * wall.thickness, 2.0
            ):
                continue
            overlap = min(candidate.end, wall.end) - max(candidate.start, wall.start)
            if overlap < _LENGTH_SHARE * wall.length:
                continue
            match = candidate
            break
        if match is not None:
            pool.remove(match)
            hits += 1
    return {"truth": len(truth), "found": len(found), "hits": hits, "extra": len(pool)}


def main() -> int:
    import ezdxf
    from ezdxf import recover

    from app.ai.construction_walls import find_walls
    from scripts.eval_vectorize import _convert_dwg

    parser = argparse.ArgumentParser()
    parser.add_argument("--dwg-dir", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--long-side", type=int, default=5000)
    parser.add_argument(
        "--live-scale",
        action="store_true",
        help="масштаб — по растру: маркеры осей + число звена читает модель (как в продукте)",
    )
    parser.add_argument("--openings", action="store_true", help="проёмы и двери (Ф7.3)")
    parser.add_argument("--markers", action="store_true", help="маркеры осей (Ф7.1)")
    args = parser.parse_args()

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for dwg in sorted(args.dwg_dir.glob("*.dwg")):
            dxf = _convert_dwg(dwg, pathlib.Path(tmp))
            if dxf is None:
                continue
            try:
                doc = ezdxf.readfile(dxf)
            except Exception:  # noqa: BLE001
                doc, _audit = recover.readfile(dxf)
            msp = doc.modelspace()
            scale_mm = drawing_scale_mm(msp)
            if scale_mm is None:
                rows.append({"sheet": dwg.stem, "skipped": "масштаб по осям не определён"})
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
                continue
            truth = truth_walls(msp, scale_mm)
            image, to_px, unit_px = render_with_transform(doc, args.long_side)
            markers = None
            if args.markers:
                import numpy as np

                from app.ai.construction_axes import axis_markers as sheet_markers

                markers = score_markers(
                    [(*to_px(x, y), radius * unit_px) for x, y, radius in axis_markers(msp)],
                    sheet_markers(np.asarray(image)),
                )
            mm_per_px = scale_mm / unit_px
            measured_scale = None
            if args.live_scale:
                import asyncio
                import io as _io

                from app.ai.construction_axes import read_sheet_scale

                buffer = _io.BytesIO()
                image.convert("RGB").save(buffer, format="PNG")
                measured = asyncio.run(read_sheet_scale(buffer.getvalue()))
                measured_scale = measured["mm_per_px"]
                if not measured_scale:
                    rows.append({"sheet": dwg.stem, "skipped": measured["reason"]})
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
                    continue
                mm_per_px = measured_scale
            found = find_walls(image, mm_per_px)
            openings = None
            if args.openings:
                from app.ai.construction_walls import find_openings

                openings = score_openings(
                    openings_in_px(truth_openings(msp, truth, scale_mm), to_px, unit_px),
                    find_openings(image, found, mm_per_px),
                )
            row = {
                "sheet": dwg.stem,
                "scale_mm_per_unit": scale_mm,
                "mm_per_px": round(mm_per_px, 4),
                "mm_per_px_truth": round(scale_mm / unit_px, 4),
                "walls": score(truth_in_px(truth, to_px, unit_px), found),
                **({"openings": openings} if openings is not None else {}),
                **({"markers": markers} if markers is not None else {}),
                "thickness_mm": sorted(
                    {round(wall.thickness * scale_mm / 10) * 10 for wall in truth}
                ),
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    total = {
        key: sum(row.get("walls", {}).get(key, 0) for row in rows)
        for key in ("truth", "found", "hits", "extra")
    }
    print(json.dumps({"ИТОГ": total}, ensure_ascii=False))
    if args.markers:
        marker_total = {
            key: sum(row.get("markers", {}).get(key, 0) for row in rows)
            for key in ("truth", "found", "hits", "extra")
        }
        print(json.dumps({"МАРКЕРЫ": marker_total}, ensure_ascii=False))
    if args.openings:
        opening_total = {
            key: sum(row.get("openings", {}).get(key, 0) for row in rows)
            for key in (
                "truth",
                "found",
                "hits",
                "extra",
                "doors_truth",
                "doors_right",
                "doors_false",
            )
        }
        print(json.dumps({"ПРОЁМЫ": opening_total}, ensure_ascii=False))
    if args.out:
        args.out.write_text(
            json.dumps({"rows": rows, "total": total}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
