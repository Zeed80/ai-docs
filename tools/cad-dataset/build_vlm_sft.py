#!/usr/bin/env python3
"""Convert (image, CadIR) corpus pairs into Qwen3-VL SFT records.

This is stage 1 of the generative-vectorization plan: instead of tracing a
raster into fragmented segments, a fine-tuned VLM emits the drawing's CAD
primitives directly (the Zero-To-CAD paradigm, applied to our 2D DXF task).
The synthetic corpus already stores each sheet as an exact CadIR next to its
rendered PNG, so those pairs ARE the supervision — this script only reshapes
them into (image -> primitive-DSL) training examples.

DSL: a compact JSON of geometry primitives in an ISOTROPIC 0..1000 space
(every coordinate and radius scaled by 1000/max(width, height), so circles
stay circular and the model learns one consistent grounding convention):

    {"lines": [[x1,y1,x2,y2], ...],
     "circles": [[cx,cy,r], ...],
     "arcs": [[cx,cy,r,start_deg,end_deg], ...],
     "polylines": [{"pts": [[x,y], ...], "closed": 0|1}, ...]}
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

PROMPT = (
    "Ты — векторизатор технических чертежей (ЕСКД). На изображении инженерный "
    "чертёж. Верни геометрические примитивы САМОЙ детали в системе координат "
    "0..1000 (обе оси масштабированы одинаково по большей стороне, 0,0 — левый "
    "верх). Игнорируй размерные/выносные линии, текст, рамку и штамп. Ответ — "
    "строго JSON: {\"lines\":[[x1,y1,x2,y2]],\"circles\":[[cx,cy,r]],"
    "\"arcs\":[[cx,cy,r,start_deg,end_deg]],"
    "\"polylines\":[{\"pts\":[[x,y]],\"closed\":0}]}"
)


def _round(value: float) -> int:
    return int(round(value))


def ir_to_dsl(ir, *, max_points: int = 64) -> dict:
    """Extract geometry primitives from a CadIR as an isotropic 0..1000 DSL."""
    width = float(ir.source.image_width)
    height = float(ir.source.image_height)
    scale = 1000.0 / max(width, height, 1.0)

    lines: list[list[int]] = []
    circles: list[list[int]] = []
    arcs: list[list[int]] = []
    polylines: list[dict] = []
    for entity in ir.entities:
        if getattr(entity, "construction", False):
            continue
        kind = entity.type
        if kind == "segment":
            lines.append([
                _round(entity.p1.x * scale), _round(entity.p1.y * scale),
                _round(entity.p2.x * scale), _round(entity.p2.y * scale),
            ])
        elif kind == "circle":
            circles.append([
                _round(entity.center.x * scale), _round(entity.center.y * scale),
                _round(entity.radius * scale),
            ])
        elif kind == "arc":
            arcs.append([
                _round(entity.center.x * scale), _round(entity.center.y * scale),
                _round(entity.radius * scale),
                _round(entity.start_angle), _round(entity.end_angle),
            ])
        elif kind == "polyline" and len(entity.points) >= 2:
            pts = [[_round(p.x * scale), _round(p.y * scale)] for p in entity.points[:max_points]]
            polylines.append({"pts": pts, "closed": 1 if entity.closed else 0})
    return {"lines": lines, "circles": circles, "arcs": arcs, "polylines": polylines}


def _sft_record(image_path: str, dsl: dict) -> dict:
    return {
        "images": [image_path],
        "messages": [
            {"role": "user", "content": "<image>\n" + PROMPT},
            {"role": "assistant", "content": json.dumps(dsl, ensure_ascii=False, separators=(",", ":"))},
        ],
    }


def build(manifests: list[pathlib.Path], out: pathlib.Path, *, backend: pathlib.Path) -> dict:
    sys.path.insert(0, str(backend))
    from app.ai.cad_ir.schema import CadIR

    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {"train": 0, "val": 0, "holdout": 0}
    empty = skipped = 0
    writers = {
        split: (out / f"{split}.jsonl").open("w", encoding="utf-8")
        for split in counts
    }
    try:
        for manifest in manifests:
            for line in manifest.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                split = row.get("split", "train")
                if split not in writers:
                    split = "train"
                try:
                    ir = CadIR.model_validate_json(pathlib.Path(row["ir"]).read_text())
                except Exception:  # noqa: BLE001 — a broken pair is skipped, recorded
                    skipped += 1
                    continue
                dsl = ir_to_dsl(ir)
                if not any(dsl[k] for k in ("lines", "circles", "arcs", "polylines")):
                    empty += 1
                    continue
                record = _sft_record(str(pathlib.Path(row["image"]).resolve()), dsl)
                writers[split].write(json.dumps(record, ensure_ascii=False) + "\n")
                counts[split] += 1
    finally:
        for writer in writers.values():
            writer.close()
    summary = {"counts": counts, "empty_dropped": empty, "unreadable_dropped": skipped}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", action="append", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument(
        "--backend",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    summary = build(args.manifest, args.out, backend=args.backend)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
