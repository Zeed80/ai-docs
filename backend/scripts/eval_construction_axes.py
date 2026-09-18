#!/usr/bin/env python3
"""E10: оси и отметки уровня на строительных листах — чтение против эталона DWG.

Эталон берётся из самого DWG (dwg2dxf → ezdxf): обозначения координационных
осей — короткие тексты внутри окружностей-маркеров, отметки уровня — тексты
вида «+3.360», «-6.200», «0.000». Лист для модели — рендер того же DXF.
Модель отвечает на узкий вопрос: какие оси и какие отметки на листе.

Критерий плана: оси ≥ 95 %, отметки ≥ 90 % (полнота), без выдуманных.

Запуск в контейнере backend:

    python scripts/eval_construction_axes.py --dwg-dir /tmp/dwg --out /tmp/e10.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

LEVEL = re.compile(r"^[+\-±]?\d{1,3}[.,]\d{3}$")
AXIS = re.compile(r"^[0-9А-ЯЁA-Z]{1,2}\d?'?$")
# Машиностроительные листы в наборе: их «отметки» — это поля допусков.
_NOT_CONSTRUCTION = {"Дизель", "Деталировка"}

PROMPT = (
    "Это строительный чертёж (план, разрез или фасад). Выпиши:\n"
    "- axes: обозначения координационных осей — цифры или буквы в кружках на "
    "концах штрихпунктирных линий осей;\n"
    "- levels: отметки уровня. Отметка стоит на полке над ЗНАКОМ ОТМЕТКИ "
    "(стрелка или треугольник, упёртый в линию уровня), в метрах с тремя "
    "знаками после точки: +3.360, -6.200, 0.000. Знак «+» или «−» пиши ровно "
    "как на листе (ниже нуля — минус). Размеры в миллиметрах (3000, 150) и "
    "числа размерных цепочек — НЕ отметки. Если знака отметки у числа нет — "
    "не выписывай его.\n"
    "Только то, что видно на листе, без повторов. ОДНОЙ строкой JSON:\n"
    '{"axes":["1","2","А"],"levels":["0.000","+3.360"]}\nТолько JSON.'
)
SCHEMA = {
    "type": "object",
    "properties": {
        "axes": {"type": "array", "maxItems": 60, "items": {"type": "string"}},
        "levels": {"type": "array", "maxItems": 60, "items": {"type": "string"}},
    },
    "required": ["axes", "levels"],
}

# Латиница и кириллица, одинаковые на письме: «A» оси и «А» — одна ось.
_HOMOGLYPHS = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


def normalize_axis(text: str) -> str:
    return text.strip().upper().translate(_HOMOGLYPHS)


def normalize_level(text: str) -> str:
    value = text.strip().replace(",", ".").replace("±", "")
    try:
        return f"{float(value):+.3f}".replace("+0.000", "0.000").replace("-0.000", "0.000")
    except ValueError:
        return value


def truth_from_dxf(doc) -> dict[str, list[str]]:
    texts: list[tuple[str, tuple[float, float]]] = []
    circles: list[tuple[float, float, float]] = []

    def visit(entity, depth: int = 0) -> None:
        kind = entity.dxftype()
        if kind in ("TEXT", "MTEXT", "ATTRIB"):
            text = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
            point = entity.dxf.insert
            texts.append((str(text).strip(), (point.x, point.y)))
        elif kind == "CIRCLE":
            circles.append((entity.dxf.center.x, entity.dxf.center.y, entity.dxf.radius))
        elif kind == "INSERT" and depth < 8:
            try:
                for attrib in entity.attribs:
                    visit(attrib, depth + 1)
                for child in entity.virtual_entities():
                    visit(child, depth + 1)
            except Exception:  # noqa: BLE001 — битый блок пропускается
                pass

    for entity in doc.modelspace():
        visit(entity)
    axes = set()
    for text, (x, y) in texts:
        if not AXIS.fullmatch(text):
            continue
        if any(math.hypot(x - cx, y - cy) <= 1.2 * r for cx, cy, r in circles if r < 2000):
            axes.add(normalize_axis(text))
    levels = {normalize_level(text) for text, _ in texts if LEVEL.match(text)}
    return {"axes": sorted(axes), "levels": sorted(levels)}


def render_sheet(doc, long_side: int = 3000) -> bytes | None:
    """Рендер DXF нужного размера.

    `eval_vectorize._render_dxf_png` отдаёт ~500 px по короткой стороне при
    любом заданном размере: `finalize()` бэкенда ezdxf сам ужимает фигуру до
    ~6 × 5 дюймов. Первый прогон E10 шёл по таким листам (отметки 36 %).
    Здесь размер фигуры восстанавливается после finalize.
    """
    import io

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
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return None
    finally:
        plt.close(figure)


LEVELS_PROMPT = (
    "Это фрагмент строительного чертежа. Выпиши отметки уровня: число на "
    "полке над ЗНАКОМ ОТМЕТКИ (стрелка или треугольник, упёртый в линию "
    "уровня), в метрах с тремя знаками после точки: +3.360, -6.200, 0.000. "
    "Знак пиши как на листе. Размеры в миллиметрах и числа размерных цепочек — "
    "НЕ отметки. Нет отметок — пустой список. ОДНОЙ строкой JSON:\n"
    '{"levels":["0.000"]}\nТолько JSON.'
)
LEVELS_SCHEMA = {
    "type": "object",
    "properties": {"levels": {"type": "array", "maxItems": 30, "items": {"type": "string"}}},
    "required": ["levels"],
}


def content_regions(image, *, max_regions: int = 12) -> list[tuple[int, int, int, int]]:
    """Области листа с рисунком: кластеры чернил, слитые с запасом.

    Лист с несколькими чертежами (фасады, план) целиком модели не прочесть:
    текст отметок в нём 3–4 px. Каждый чертёж — отдельный вырез.
    """
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"))
    scale = 800.0 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ink = (small < 200).astype(np.uint8)
    ink = cv2.dilate(ink, np.ones((15, 15), np.uint8))
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    boxes = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if w * h < 400:
            continue
        pad = 8
        boxes.append(
            (
                int(max(0, x - pad) / scale),
                int(max(0, y - pad) / scale),
                int(min(small.shape[1], x + w + pad) / scale),
                int(min(small.shape[0], y + h + pad) / scale),
            )
        )
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    return boxes[:max_regions]


def score(truth: list[str], read: list[str]) -> dict[str, int]:
    truth_set, read_set = set(truth), set(read)
    return {
        "found": len(truth_set & read_set),
        "expected": len(truth_set),
        "extra": len(read_set - truth_set),
    }


async def main() -> int:
    import ezdxf
    from ezdxf import recover
    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router
    from scripts.eval_vectorize import _convert_dwg

    parser = argparse.ArgumentParser()
    parser.add_argument("--dwg-dir", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument(
        "--tiles", action="store_true", help="отметки — по вырезам областей с рисунком"
    )
    parser.add_argument(
        "--marks",
        action="store_true",
        help="отметки — только у найденных знаков отметки (рендер 12 000 px)",
    )
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for dwg in sorted(args.dwg_dir.glob("*.dwg")):
            if dwg.stem in _NOT_CONSTRUCTION:
                continue
            dxf = _convert_dwg(dwg, pathlib.Path(tmp))
            if dxf is None:
                continue
            try:
                doc = ezdxf.readfile(dxf)
            except Exception:  # noqa: BLE001
                doc, _audit = recover.readfile(dxf)
            truth = truth_from_dxf(doc)
            png = render_sheet(doc, 3000)
            big = render_sheet(doc, 7000) if args.tiles else None
            if not png:
                continue
            import io

            image = Image.open(io.BytesIO(png)).convert("RGB")
            answer = await _ask(
                PROMPT,
                _overview(image, side=2400),
                router=ai_router,
                confidential=True,
                num_predict=800,
                schema=SCHEMA,
                timeout_seconds=180.0,
            )
            read_axes = sorted({normalize_axis(a) for a in (answer or {}).get("axes") or []})
            read_levels = sorted({normalize_level(v) for v in (answer or {}).get("levels") or []})
            if args.marks:
                import numpy as np

                from app.ai.construction_levels import (
                    LEVEL_AT_MARK_PROMPT,
                    LEVEL_AT_MARK_SCHEMA,
                    level_marks,
                    mark_crop_box,
                )

                Image.MAX_IMAGE_PIXELS = None
                at_marks: set[str] = set()
                # Два масштаба: на обычном листе знак при 5000 px в самый раз,
                # на сборном листе фасадов он там мельче 12 px и находится
                # только при 12 000 px — где у обычного листа вырез уже не
                # вмещает полку с числом (единый 12 000 — 13 из 28).
                marks_at: list = []
                for long_side in (5000, 12000):
                    sheet = Image.open(io.BytesIO(render_sheet(doc, long_side))).convert("RGB")
                    marks_at += [
                        (sheet, mark) for mark in level_marks(np.asarray(sheet.convert("L")))
                    ]
                for sheet, mark in marks_at:
                    part = await _ask(
                        LEVEL_AT_MARK_PROMPT,
                        sheet.crop(mark_crop_box(mark, sheet.size)),
                        router=ai_router,
                        confidential=True,
                        num_predict=120,
                        schema=LEVEL_AT_MARK_SCHEMA,
                        timeout_seconds=60.0,
                    )
                    value = (part or {}).get("level")
                    if value:
                        at_marks.add(normalize_level(str(value)))
                read_levels = sorted(at_marks)
            if big:
                full = Image.open(io.BytesIO(big)).convert("RGB")
                tiled: set[str] = set()
                for box in content_regions(full):
                    part = await _ask(
                        LEVELS_PROMPT,
                        _overview(full.crop(box), side=1800),
                        router=ai_router,
                        confidential=True,
                        num_predict=400,
                        schema=LEVELS_SCHEMA,
                        timeout_seconds=120.0,
                    )
                    tiled |= {normalize_level(v) for v in (part or {}).get("levels") or []}
                read_levels = sorted(tiled)
            row = {
                "sheet": dwg.stem,
                "truth": truth,
                "read": {"axes": read_axes, "levels": read_levels},
                "axes": score(truth["axes"], read_axes),
                "levels": score(truth["levels"], read_levels),
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    total = {
        key: {part: sum(row[key][part] for row in rows) for part in ("found", "expected", "extra")}
        for key in ("axes", "levels")
    }
    args.out.write_text(
        json.dumps({"rows": rows, "total": total}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("ИТОГ", json.dumps(total, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
