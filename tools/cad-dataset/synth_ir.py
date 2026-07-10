#!/usr/bin/env python3
"""Generate synthetic (degraded-photo, ground-truth CadIR) training pairs for
the neural vectorizer — no photographed source needed, no confidentiality
concerns (Dual-AI rule: real DWGs stay local-only; synthetics don't).

Ground truth is built DIRECTLY in IR-space (random mechanical-part entity
graphs: rectangular/rounded-rect plates with holes, circular flanges with
bolt circles, stepped shaft profiles) — not reverse-engineered from a
renderer. This guarantees the target sequence and the rendered pixels agree
exactly, with zero conversion loss.

The clean IR is rendered once via the project's own ``cad_ir.png_render``
(the SAME rasterizer production coverage-scores against — using anything
else would teach the model a rendering convention the pipeline doesn't
share). tools/lora-dataset/degrade.py's proven photo simulation then
produces N pixel-ALIGNED "control" variants per clean target (paper
tint/binding/perspective/lighting/blur/JPEG, then unwarp+CLAHE exactly like
production's enhance_source_for_diffusion) — because ``unwarp_exact``
returns an image of the SAME size and (up to a small random jitter) the SAME
frame as the clean input, the clean IR's pixel coordinates are valid ground
truth for every degraded variant without any re-registration step.

Usage:
    python3 synth_ir.py --count 400 --variants 3 --out <dir> [--seed 0]
                        [--repo <repo root>]

Output layout under --out:
    clean/synth_0000.png            # rendered from the GT IR (not shipped to training)
    control/synth_0000__v0.png      # degraded, pixel-aligned to clean
    ir/synth_0000.json              # ground-truth CadIR (schema_version=2)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys


def _rect_plate(rng: random.Random, w: int, h: int) -> list[dict]:
    """Rectangle outline (plain or rounded-corner) + 0-4 holes + optional axes."""
    margin = int(min(w, h) * rng.uniform(0.08, 0.18))
    x0, y0, x1, y1 = margin, margin, w - margin, h - margin
    entities: list[dict] = []
    rounded = rng.random() < 0.4
    corner_r = min(x1 - x0, y1 - y0) * rng.uniform(0.05, 0.12) if rounded else 0.0

    if rounded and corner_r > 4:
        entities += [
            {"type": "segment", "p1": {"x": x0 + corner_r, "y": y0}, "p2": {"x": x1 - corner_r, "y": y0}},
            {"type": "segment", "p1": {"x": x1, "y": y0 + corner_r}, "p2": {"x": x1, "y": y1 - corner_r}},
            {"type": "segment", "p1": {"x": x1 - corner_r, "y": y1}, "p2": {"x": x0 + corner_r, "y": y1}},
            {"type": "segment", "p1": {"x": x0, "y": y1 - corner_r}, "p2": {"x": x0, "y": y0 + corner_r}},
        ]
        corners = [
            ((x0 + corner_r, y0 + corner_r), 180, 270),
            ((x1 - corner_r, y0 + corner_r), 270, 360),
            ((x1 - corner_r, y1 - corner_r), 0, 90),
            ((x0 + corner_r, y1 - corner_r), 90, 180),
        ]
        for (cx, cy), a0, a1 in corners:
            entities.append({
                "type": "arc", "center": {"x": cx, "y": cy}, "radius": corner_r,
                "start_angle": a0, "end_angle": a1,
            })
    else:
        entities += [
            {"type": "segment", "p1": {"x": x0, "y": y0}, "p2": {"x": x1, "y": y0}},
            {"type": "segment", "p1": {"x": x1, "y": y0}, "p2": {"x": x1, "y": y1}},
            {"type": "segment", "p1": {"x": x1, "y": y1}, "p2": {"x": x0, "y": y1}},
            {"type": "segment", "p1": {"x": x0, "y": y1}, "p2": {"x": x0, "y": y0}},
        ]

    n_holes = rng.randint(0, 4)
    placed: list[tuple[float, float, float]] = []
    pad = corner_r + 6
    for _ in range(n_holes):
        for _try in range(20):
            r = rng.uniform(0.03, 0.09) * min(w, h)
            cx = rng.uniform(x0 + pad + r, x1 - pad - r)
            cy = rng.uniform(y0 + pad + r, y1 - pad - r)
            if all((cx - px) ** 2 + (cy - py) ** 2 > (r + pr + 10) ** 2 for px, py, pr in placed):
                placed.append((cx, cy, r))
                entities.append({"type": "circle", "center": {"x": cx, "y": cy}, "radius": r})
                if rng.random() < 0.5:
                    axis_len = r * rng.uniform(2.2, 3.2)
                    entities.append({
                        "type": "segment",
                        "p1": {"x": cx - axis_len, "y": cy}, "p2": {"x": cx + axis_len, "y": cy},
                        "line_class": "axis", "width_class": "thin",
                    })
                break
    return entities


def _circular_flange(rng: random.Random, w: int, h: int) -> list[dict]:
    """Outer circle + optional bolt-circle holes + optional center bore."""
    cx, cy = w / 2, h / 2
    r_out = min(w, h) * rng.uniform(0.32, 0.45)
    entities: list[dict] = [{"type": "circle", "center": {"x": cx, "y": cy}, "radius": r_out}]
    entities.append({
        "type": "segment", "p1": {"x": cx - r_out * 1.15, "y": cy}, "p2": {"x": cx + r_out * 1.15, "y": cy},
        "line_class": "axis", "width_class": "thin",
    })
    entities.append({
        "type": "segment", "p1": {"x": cx, "y": cy - r_out * 1.15}, "p2": {"x": cx, "y": cy + r_out * 1.15},
        "line_class": "axis", "width_class": "thin",
    })
    if rng.random() < 0.5:
        r_bore = r_out * rng.uniform(0.2, 0.4)
        entities.append({"type": "circle", "center": {"x": cx, "y": cy}, "radius": r_bore})
    if rng.random() < 0.75:
        n = rng.choice([3, 4, 6, 8])
        r_bolt = r_out * rng.uniform(0.6, 0.8)
        r_hole = r_out * rng.uniform(0.05, 0.09)
        start = rng.uniform(0, 360 / n)
        for i in range(n):
            ang = __import__("math").radians(start + i * 360 / n)
            hx = cx + r_bolt * __import__("math").cos(ang)
            hy = cy + r_bolt * __import__("math").sin(ang)
            entities.append({"type": "circle", "center": {"x": hx, "y": hy}, "radius": r_hole})
    return entities


def _shaft_profile(rng: random.Random, w: int, h: int) -> list[dict]:
    """Stepped-diameter shaft silhouette (front view) + centerline."""
    import math

    n = rng.randint(2, 5)
    total_len = w * rng.uniform(0.7, 0.9)
    x = (w - total_len) / 2
    cy = h / 2
    max_d = h * rng.uniform(0.35, 0.55)
    entities: list[dict] = [{
        "type": "segment", "p1": {"x": x - total_len * 0.05, "y": cy},
        "p2": {"x": x + total_len * 1.05, "y": cy},
        "line_class": "axis", "width_class": "thin",
    }]
    seg_len = total_len / n
    prev_d = None
    for i in range(n):
        d = max_d * rng.uniform(0.35, 1.0)
        x0, x1 = x + i * seg_len, x + (i + 1) * seg_len
        entities.append({"type": "segment", "p1": {"x": x0, "y": cy - d / 2}, "p2": {"x": x1, "y": cy - d / 2}})
        entities.append({"type": "segment", "p1": {"x": x0, "y": cy + d / 2}, "p2": {"x": x1, "y": cy + d / 2}})
        if prev_d is not None and abs(d - prev_d) > 1:
            entities.append({"type": "segment", "p1": {"x": x0, "y": cy - prev_d / 2}, "p2": {"x": x0, "y": cy - d / 2}})
            entities.append({"type": "segment", "p1": {"x": x0, "y": cy + prev_d / 2}, "p2": {"x": x0, "y": cy + d / 2}})
        prev_d = d
    entities.append({"type": "segment", "p1": {"x": x, "y": cy - prev_d / 2}, "p2": {"x": x, "y": cy + prev_d / 2}})
    entities.append({
        "type": "segment", "p1": {"x": x + total_len, "y": cy - prev_d / 2},
        "p2": {"x": x + total_len, "y": cy + prev_d / 2},
    })
    if rng.random() < 0.3:
        # a keyway/hole cutting through one segment — a small circle nearby
        cx = x + rng.uniform(0.2, 0.8) * total_len
        entities.append({"type": "circle", "center": {"x": cx, "y": cy}, "radius": max_d * 0.08})
    _ = math
    return entities


_ARCHETYPES = [_rect_plate, _rect_plate, _circular_flange, _shaft_profile]


def _finalize(entities: list[dict]) -> list[dict]:
    for e in entities:
        e.setdefault("line_class", "contour")
        e.setdefault("width_class", "main")
        e["confidence"] = 1.0
        e["origin"] = "spec"
        e["assurance"] = "constraint_validated"  # by-construction geometry, not observed from a photo
    return entities


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=400)
    ap.add_argument("--variants", type=int, default=3, help="degraded copies per clean target")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--width", type=int, default=900)
    ap.add_argument("--height", type=int, default=700)
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    args = ap.parse_args()

    for sub in ("clean", "control", "ir"):
        (args.out / sub).mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(args.repo / "backend"))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lora-dataset"))
    import numpy as np
    from PIL import Image

    from app.ai.cad_ir.png_render import rasterize_entities  # noqa: E402
    from degrade import clahe_like_prod, simulate_photo, unwarp_exact  # noqa: E402

    rng = random.Random(args.seed)
    ok = fail = 0
    for i in range(args.count):
        try:
            gen = rng.choice(_ARCHETYPES)
            entities = _finalize(gen(rng, args.width, args.height))
            ir = {
                "schema_version": 2,
                "units": "mm",
                "scale": None,
                "source": {"image_width": args.width, "image_height": args.height, "kind": "spec"},
                "entities": entities,
            }
            canvas = rasterize_entities(
                [_to_entity_obj(e) for e in entities], args.width, args.height, thin_px=2, thick_px=3
            )
            clean_rgb = np.stack([canvas] * 3, axis=-1)
            stem = f"synth_{i:05d}"
            Image.fromarray(clean_rgb).save(args.out / "clean" / f"{stem}.png")
            (args.out / "ir" / f"{stem}.json").write_text(json.dumps(ir, ensure_ascii=False))

            for v in range(args.variants):
                deg_rng = np.random.default_rng(args.seed + i * 1000 + v)
                photo, quad = simulate_photo(clean_rgb, deg_rng)
                control = clahe_like_prod(unwarp_exact(photo, quad, args.width, args.height, deg_rng))
                Image.fromarray(control).save(args.out / "control" / f"{stem}__v{v}.png")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL #{i}: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
            fail += 1
    print(f"done: {ok} synthetic IR targets x{args.variants} variants, {fail} failed")
    return 0 if fail == 0 else 1


def _to_entity_obj(e: dict):
    """Adapt a plain dict to the Pydantic Entity union rasterize_entities expects."""
    from app.ai.cad_ir.schema import Arc, Circle, Polyline, Segment

    kind = e["type"]
    cls = {"segment": Segment, "arc": Arc, "circle": Circle, "polyline": Polyline}[kind]
    return cls(**e)


if __name__ == "__main__":
    sys.exit(main())
