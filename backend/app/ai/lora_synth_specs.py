"""Random ЕСКД part/assembly specs for synthetic LoRA-dataset targets —
fed into techdraw.render_spec_to_svg. Port of tools/lora-dataset/
synth_techdraw.py's generators (that CLI stays the research tool)."""

from __future__ import annotations

import random

_NAMES_SHAFT = ["Вал", "Вал-шестерня", "Ось", "Валик", "Шток", "Палец"]
_NAMES_PLATE_RECT = ["Планка", "Пластина", "Крышка", "Плита опорная"]
_NAMES_PLATE_CIRCLE = ["Фланец", "Диск", "Кольцо опорное", "Крышка торцевая"]
_NAMES_ASM = ["Узел натяжения", "Опора", "Кронштейн в сборе", "Ролик в сборе"]
_MATERIALS = [
    "Сталь 45 ГОСТ 1050-88", "Сталь 40Х ГОСТ 4543-71", "Сталь 20 ГОСТ 1050-88",
    "Бр.АМц 9-2 ГОСТ 18175-78", "Д16Т ГОСТ 4784-97", "СЧ20 ГОСТ 1412-85",
]
_TOLS_SHAFT = ["", "h6", "h7", "k6", "js6", "f7"]
_TOLS_HOLE = ["", "H7", "H8"]
_THREADS = ["M12", "M16", "M20×1.5", "M24×2", "M30×2"]
_RA = [None, 0.8, 1.6, 3.2, 6.3]


def random_spec(kind: str, rng: random.Random) -> dict:
    return {"shaft": _shaft, "plate": _plate, "assembly": _assembly}[kind](rng)


def spec_caption(spec: dict) -> str:
    """Deterministic RU dataset caption from a synthetic spec — the spec IS
    the ground truth, so captioning synthetics through a VLM only burned GPU
    hours and added its recognition errors. Mirrors CAPTION_PROMPT's format:
    type, views, key features; no dimensions, no document numbers."""
    kind = spec.get("type")
    name = (spec.get("title") or {}).get("name", "деталь")
    if kind == "shaft":
        segs = spec.get("segments", [])
        feats = []
        if any(s.get("thread") for s in segs):
            feats.append("резьбой на конце")
        if any(s.get("bore_diameter") for s in segs):
            feats.append("осевой расточкой с местным разрезом")
        if any(s.get("chamfer") for s in segs):
            feats.append("фасками")
        tail = (" с " + ", ".join(feats)) if feats else ""
        views = "главный вид с размерными линиями"
        if any(s.get("thread_end_view") for s in segs):
            views += " и вид с торца"
        return (f"Чертёж детали «{name}»: ступенчатый вал из {len(segs)} ступеней{tail}. "
                f"На листе {views}, рамка и основная надпись.")
    if kind == "plate":
        holes = spec.get("holes", [])
        if spec.get("shape") == "circle":
            body = f"круглая деталь «{name}»"
            if spec.get("bolt_circle_n"):
                body += f" с {spec['bolt_circle_n']} отверстиями по окружности"
            if holes:
                body += " и центральным отверстием"
        else:
            body = f"прямоугольная пластина «{name}»"
            if holes:
                body += f" с {len(holes)} отверстиями"
        return (f"Чертёж детали: {body}. Два вида (главный и сбоку) с размерными "
                "линиями, рамка и основная надпись.")
    if kind == "assembly":
        n = len(spec.get("components", []))
        return (f"Сборочный чертёж «{name}» из {n} позиций с номерами позиций "
                "и спецификацией. Рамка и основная надпись.")
    return f"Чертёж детали «{name}» с рамкой и основной надписью."


def _title(rng: random.Random, name_pool: list[str]) -> dict:
    return {
        "name": rng.choice(name_pool),
        "designation": f"ТМ.{rng.randint(100000, 999999)}.{rng.randint(1, 999):03d}",
        "material": rng.choice(_MATERIALS),
        "developer": rng.choice(["Иванов", "Петров", "Сидорова", "Кузнецов"]),
        "checked_by": rng.choice(["Смирнов", "Волкова", ""]),
        "litera": rng.choice(["", "У", "О1"]),
        "show_frame": True,
    }


def _shaft(rng: random.Random) -> dict:
    n = rng.randint(2, 6)
    segments = []
    for i in range(n):
        d = rng.choice([12, 16, 20, 25, 30, 35, 40, 45, 50, 60])
        seg = {
            "diameter": float(d),
            "length": float(rng.choice([15, 20, 25, 30, 40, 50, 60, 80])),
            "tolerance": rng.choice(_TOLS_SHAFT),
            "roughness": rng.choice(_RA),
            "chamfer": rng.choice([0.0, 0.0, 1.0, 1.6, 2.0]),
        }
        if i in (0, n - 1) and rng.random() < 0.35:
            seg["thread"] = rng.choice(_THREADS)
            seg["thread_end_view"] = rng.random() < 0.5
        if rng.random() < 0.2:
            seg["bore_diameter"] = float(max(4, d - rng.choice([6, 8, 10])))
            seg["section_hatch"] = True
        segments.append(seg)
    return {"type": "shaft", "segments": segments, "title": _title(rng, _NAMES_SHAFT)}


def _plate(rng: random.Random) -> dict:
    shape = rng.choice(["rect", "circle"])
    spec: dict = {
        "type": "plate",
        "shape": shape,
        "thickness": float(rng.choice([6, 8, 10, 12, 16, 20])),
        "thickness_tol": rng.choice(["", "h14", "js14"]),
        "roughness": rng.choice(_RA),
        "title": _title(rng, _NAMES_PLATE_RECT if shape == "rect" else _NAMES_PLATE_CIRCLE),
        "holes": [],
    }
    if shape == "rect":
        spec["width"] = float(rng.choice([60, 80, 100, 120, 160]))
        spec["height"] = float(rng.choice([40, 60, 80, 100]))
        for _ in range(rng.randint(0, 4)):
            spec["holes"].append({
                "x": rng.uniform(-0.35, 0.35) * spec["width"],
                "y": rng.uniform(-0.35, 0.35) * spec["height"],
                "diameter": float(rng.choice([6, 8, 10, 12])),
                "tolerance": rng.choice(_TOLS_HOLE),
            })
    else:
        spec["diameter"] = float(rng.choice([80, 100, 120, 160, 200]))
        if rng.random() < 0.7:
            spec["bolt_circle_d"] = spec["diameter"] * rng.uniform(0.6, 0.8)
            spec["bolt_circle_n"] = rng.choice([4, 6, 8])
            spec["bolt_hole_d"] = float(rng.choice([6, 9, 11, 13]))
            spec["bolt_hole_tol"] = rng.choice(_TOLS_HOLE)
        if rng.random() < 0.5:
            spec["holes"].append({
                "x": 0.0, "y": 0.0,
                "diameter": spec["diameter"] * rng.uniform(0.2, 0.4),
                "tolerance": "H7",
            })
    return spec


def mutate_spec(spec: dict, rng: random.Random) -> tuple[dict, str] | None:
    """One realistic engineering edit of a part spec → (edited spec, RU
    instruction describing exactly that edit). Powers the "drawing_edit"
    dataset preset: control = render(A), target = render(A'), prompt = the
    instruction — unlimited perfectly-labeled edit pairs. Only mutations
    that keep the overall extents (so both renders share scale/layout):
    chamfers, threads, axial bores, plate holes."""
    import copy

    s = copy.deepcopy(spec)
    if s.get("type") == "shaft":
        segs = s["segments"]
        candidates: list[tuple[str, int]] = []
        for i, seg in enumerate(segs):
            if seg.get("chamfer"):
                candidates.append(("remove_chamfer", i))
            else:
                candidates.append(("add_chamfer", i))
            if i in (0, len(segs) - 1):
                candidates.append(("remove_thread", i) if seg.get("thread") else ("add_thread", i))
            if seg.get("bore_diameter"):
                candidates.append(("remove_bore", i))
        op, i = rng.choice(candidates)
        seg = segs[i]
        d = seg["diameter"]
        if op == "remove_chamfer":
            seg["chamfer"] = 0.0
            return s, f"убери фаску на ступени Ø{d:g}"
        if op == "add_chamfer":
            seg["chamfer"] = rng.choice([1.0, 1.6, 2.0])
            return s, f"добавь фаску {seg['chamfer']:g}×45° на ступени Ø{d:g}"
        if op == "remove_thread":
            seg["thread"] = ""
            seg["thread_end_view"] = False
            return s, f"убери резьбу на ступени Ø{d:g}"
        if op == "add_thread":
            seg["thread"] = rng.choice(_THREADS)
            return s, f"добавь резьбу {seg['thread']} на ступени Ø{d:g}"
        if op == "remove_bore":
            seg["bore_diameter"] = 0.0
            seg["section_hatch"] = False
            return s, f"убери осевую расточку в ступени Ø{d:g}"
    if s.get("type") == "plate":
        holes = s.get("holes", [])
        if holes and rng.random() < 0.5:
            removed = holes.pop(rng.randrange(len(holes)))
            return s, f"убери отверстие Ø{removed['diameter']:g}"
        max_x = (s.get("width", s.get("diameter", 100)) * 0.3)
        max_y = (s.get("height", s.get("diameter", 100)) * 0.3)
        hole = {
            "x": rng.uniform(-max_x, max_x), "y": rng.uniform(-max_y, max_y),
            "diameter": float(rng.choice([6, 8, 10, 12])),
            "tolerance": rng.choice(_TOLS_HOLE),
        }
        holes.append(hole)
        s["holes"] = holes
        return s, f"добавь отверстие Ø{hole['diameter']:g}"
    return None


def _assembly(rng: random.Random) -> dict:
    parts = []
    bom = []
    for pos in range(1, rng.randint(2, 4) + 1):
        child = _shaft(rng) if rng.random() < 0.5 else _plate(rng)
        child["title"]["show_frame"] = False
        parts.append({"ref": str(pos), "spec": child, "qty": rng.randint(1, 4)})
        bom.append({
            "pos": pos,
            "designation": child["title"]["designation"],
            "name": child["title"]["name"],
            "qty": parts[-1]["qty"],
            "material": child["title"]["material"],
        })
    title = _title(rng, _NAMES_ASM)
    title["material"] = ""
    return {"type": "assembly", "components": parts, "bom": bom, "title": title}
