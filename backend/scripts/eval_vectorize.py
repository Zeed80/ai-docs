#!/usr/bin/env python3
"""Golden-set evaluation of the vectorize (scan → CAD IR) pipeline.

Two evaluation sources:
- ``cleanup_test_files/*.dwg`` — ground truth: DWG → dwg2dxf → ezdxf entities
  give true per-type counts; the DXF is rendered to a clean raster and the
  recognizer must reconstruct it (coverage recall/precision + count deltas).
- photo files (``*.jpg``/``*.jpeg``/``*.JPG``) — no GT: the pipeline's own
  coverage score against the binarized photo is the tracked metric.

Usage (host):
    python3 backend/scripts/eval_vectorize.py \
        --dir cleanup_test_files --long-side 1600 \
        --out test-results/eval_vectorize.json

The JSON is the CV baseline the neural vectorizer (Ф2) must beat on the same
files. Deterministic — safe to diff between runs/PRs.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# GT entity types we compare against recognized IR types.
_GT_TYPE_MAP = {
    "LINE": "segment",
    "CIRCLE": "circle",
    "ARC": "arc",
    "LWPOLYLINE": "polyline",
    "POLYLINE": "polyline",
    "TEXT": "text",
    "MTEXT": "text",
}


def _convert_dwg(dwg_path: pathlib.Path, tmp_dir: pathlib.Path) -> pathlib.Path | None:
    if shutil.which("dwg2dxf") is None:
        print("ERROR: dwg2dxf not found", file=sys.stderr)
        return None
    out = tmp_dir / (dwg_path.stem + ".dxf")
    subprocess.run(
        ["dwg2dxf", "-y", "-o", str(out), str(dwg_path)],
        capture_output=True, text=True, timeout=300,
    )
    return out if out.exists() else None


def _gt_counts(doc) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in doc.modelspace():
        mapped = _GT_TYPE_MAP.get(e.dxftype())
        if mapped:
            counts[mapped] = counts.get(mapped, 0) + 1
    return counts


def _render_dxf_png(doc, long_side: int) -> bytes | None:
    """Clean raster of the GT DXF (white bg, black ink) — reuses the proven
    recipe from tools/lora-dataset/render_dwg.py."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    msp = doc.modelspace()
    # prune INSERTs referencing missing blocks (dwg2dxf artifact)
    for ins in list(msp.query("INSERT")):
        if ins.dxf.name not in doc.blocks:
            msp.delete_entity(ins)
    try:
        extents = bbox.extents(msp, fast=True)
        ratio = (extents.size.y / extents.size.x) if extents.has_data and extents.size.x else 0.7
    except Exception:  # noqa: BLE001
        ratio = 0.7
    ratio = min(max(ratio, 0.1), 10.0)
    dpi = 100
    if ratio <= 1.0:
        fig_w, fig_h = long_side / dpi, long_side * ratio / dpi
    else:
        fig_w, fig_h = long_side / ratio / dpi, long_side / dpi
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    try:
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
        ax = fig.add_axes([0, 0, 1, 1])
        Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(msp, finalize=True)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.1)
        plt.close(fig)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"  render failed: {str(exc)[:120]}", file=sys.stderr)
        return None


def _recognize(image_bytes: bytes, enhance: bool, recognizer: str = "cv") -> dict | None:
    """``recognizer``: "cv" (baseline), "neural" (Ф3 model only, no CV
    fallback — a clean read of what the network alone achieves), or
    "arbitrate" (production path: neural vs CV, independently scored)."""
    from app.ai.cad_recognize import CvRecognizer
    from app.ai.cad_recognize.verify import score_coverage
    from app.tasks.cad_trace import _binarize, _ocr_text_entities

    if enhance:
        try:
            from app.ai.drawing_cleanup import enhance_source_for_diffusion

            image_bytes = enhance_source_for_diffusion(image_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"  enhance failed: {str(exc)[:120]}", file=sys.stderr)
    ink, w, h = _binarize(image_bytes)
    try:
        _texts, text_boxes = _ocr_text_entities(image_bytes)
    except Exception:  # noqa: BLE001
        text_boxes = []

    started = time.monotonic()
    used = recognizer
    if recognizer == "cv":
        out = CvRecognizer().recognize(ink, exclusion_boxes=text_boxes)
        score = (
            score_coverage(out.entities, ink, out.keep_raster, thin_px=out.thin_px, thick_px=out.thick_px)
            if out is not None else None
        )
    elif recognizer == "neural":
        from app.ai.cad_recognize.neural import NeuralRecognizer

        out = NeuralRecognizer().recognize(ink, exclusion_boxes=text_boxes)
        score = (
            score_coverage(out.entities, ink, out.keep_raster, thin_px=out.thin_px, thick_px=out.thick_px)
            if out is not None else None
        )
    else:  # arbitrate — the actual production decision path
        from app.ai.cad_recognize.neural import NeuralRecognizer
        from app.ai.cad_recognize.verify import arbitrate_recognition

        result = arbitrate_recognition(ink, text_boxes, NeuralRecognizer(), CvRecognizer())
        out = result if result.entities else None
        score = result.score if result.entities else None
        used = result.recognizer_used
    elapsed = time.monotonic() - started

    if out is None or not out.entities:
        return {"declined": True, "seconds": round(elapsed, 2), "size": [w, h], "recognizer_used": used}
    counts: dict[str, int] = {}
    for e in out.entities:
        counts[e.type] = counts.get(e.type, 0) + 1
    return {
        "declined": False,
        "seconds": round(elapsed, 2),
        "size": [w, h],
        "counts": counts,
        "entities": len(out.entities),
        "coverage_recall": score.recall,
        "coverage_precision": score.precision,
        "coverage_ok": score.ok,
        "recognizer_used": used,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="cleanup_test_files")
    parser.add_argument("--long-side", type=int, default=1600)
    parser.add_argument("--limit-dwg", type=int, default=0, help="0 = all")
    parser.add_argument("--limit-photos", type=int, default=0, help="0 = all")
    parser.add_argument("--out", default="test-results/eval_vectorize.json")
    parser.add_argument(
        "--recognizer", choices=["cv", "neural", "arbitrate"], default="cv",
        help="cv=CV-baseline (default); neural=Ф3 model alone (no CV fallback); "
             "arbitrate=production decision path (neural vs CV, independently scored)",
    )
    args = parser.parse_args()

    import ezdxf

    root = pathlib.Path(args.dir)
    results: dict[str, dict] = {"dwg": {}, "photos": {}}

    dwg_files = sorted(root.glob("*.dwg"))
    if args.limit_dwg:
        dwg_files = dwg_files[: args.limit_dwg]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = pathlib.Path(tmp)
        for dwg in dwg_files:
            print(f"[dwg] {dwg.name}")
            dxf = _convert_dwg(dwg, tmp_dir)
            if dxf is None:
                results["dwg"][dwg.name] = {"error": "convert_failed"}
                continue
            try:
                doc = ezdxf.readfile(dxf)
            except Exception as exc:  # noqa: BLE001
                try:
                    from ezdxf import recover

                    doc, _aud = recover.readfile(dxf)
                except Exception:  # noqa: BLE001
                    results["dwg"][dwg.name] = {"error": f"read_failed: {str(exc)[:80]}"}
                    continue
            gt = _gt_counts(doc)
            png = _render_dxf_png(doc, args.long_side)
            if png is None:
                results["dwg"][dwg.name] = {"error": "render_failed", "gt_counts": gt}
                continue
            rec = _recognize(png, enhance=False, recognizer=args.recognizer)
            rec["gt_counts"] = gt
            results["dwg"][dwg.name] = rec
            print(f"  -> {rec.get('entities', 'declined')} entities, "
                  f"recall={rec.get('coverage_recall')}, precision={rec.get('coverage_precision')}")

    photos = sorted([
        *root.glob("*.jpg"), *root.glob("*.jpeg"), *root.glob("*.JPG"),
        *root.glob("*.png"), *root.glob("*.PNG"),
    ])
    if args.limit_photos:
        photos = photos[: args.limit_photos]
    for photo in photos:
        print(f"[photo] {photo.name}")
        rec = _recognize(photo.read_bytes(), enhance=True, recognizer=args.recognizer)
        results["photos"][photo.name] = rec
        print(f"  -> {rec.get('entities', 'declined')} entities, "
              f"recall={rec.get('coverage_recall')}, precision={rec.get('coverage_precision')}")

    # Aggregates
    def _agg(section: dict) -> dict:
        oks = [r for r in section.values() if r and not r.get("error") and not r.get("declined")]
        declined = sum(1 for r in section.values() if r.get("declined"))
        errors = sum(1 for r in section.values() if r.get("error"))
        if not oks:
            return {"files": len(section), "ok": 0, "declined": declined, "errors": errors}
        return {
            "files": len(section),
            "ok": len(oks),
            "declined": declined,
            "errors": errors,
            "mean_recall": round(sum(r["coverage_recall"] for r in oks) / len(oks), 4),
            "mean_precision": round(sum(r["coverage_precision"] for r in oks) / len(oks), 4),
            "coverage_ok_rate": round(sum(1 for r in oks if r["coverage_ok"]) / len(oks), 3),
        }

    results["summary"] = {"dwg": _agg(results["dwg"]), "photos": _agg(results["photos"])}

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
