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
    "- levels: отметки уровня — числа вида +3.360, -6.200, 0.000 (у знака "
    "отметки или на выносной полке).\n"
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
