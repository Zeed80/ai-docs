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
    "DIMENSION": "dimension",
    "HATCH": "hatch",
}


def _strip_sortentstable(dxf_path: pathlib.Path) -> None:
    """dwg2dxf emits SORTENTSTABLE objects with group code 331 that ezdxf
    1.4+ rejects outright (DXFStructureError, even in recover mode). The
    object only affects draw order — irrelevant for counting/rendering —
    so drop it from the tag stream."""
    lines = dxf_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i + 1 < len(lines):
        code, value = lines[i], lines[i + 1]
        if code.strip() == "0" and value.strip() == "SORTENTSTABLE":
            i += 2
            while i + 1 < len(lines) and lines[i].strip() != "0":
                i += 2
            continue
        out.append(code)
        out.append(value)
        i += 2
    out.extend(lines[i:])
    dxf_path.write_text("".join(out), encoding="utf-8")


def _convert_dwg(dwg_path: pathlib.Path, tmp_dir: pathlib.Path) -> pathlib.Path | None:
    if shutil.which("dwg2dxf") is None:
        print("ERROR: dwg2dxf not found", file=sys.stderr)
        return None
    out = tmp_dir / (dwg_path.stem + ".dxf")
    subprocess.run(
        ["dwg2dxf", "-y", "-o", str(out), str(dwg_path)],
        capture_output=True, text=True, timeout=300,
    )
    if not out.exists():
        return None
    try:
        _strip_sortentstable(out)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, reader decides
        print(f"  sortentstable strip failed: {str(exc)[:120]}", file=sys.stderr)
    return out


def _gt_counts(doc) -> dict[str, int]:
    counts: dict[str, int] = {}
    def visit(entity, depth: int = 0) -> None:
        mapped = _GT_TYPE_MAP.get(entity.dxftype())
        if mapped:
            counts[mapped] = counts.get(mapped, 0) + 1
        elif entity.dxftype() == "INSERT" and depth < 16:
            try:
                for child in entity.virtual_entities():
                    visit(child, depth + 1)
            except Exception:  # noqa: BLE001 — integrity report handles this
                pass

    for entity in doc.modelspace():
        visit(entity)
    return counts


def _ground_truth_integrity(doc) -> tuple[bool, list[str]]:
    """Reject sheets whose CAD source cannot produce complete semantic GT."""
    supported = set(_GT_TYPE_MAP) | {"INSERT"}
    issues: list[str] = []

    def visit(entity, depth: int = 0) -> None:
        kind = entity.dxftype()
        if kind not in supported:
            issues.append(f"unsupported:{kind}")
            return
        if kind != "INSERT":
            return
        if depth >= 16:
            issues.append("insert_depth")
            return
        try:
            children = list(entity.virtual_entities())
        except Exception:  # noqa: BLE001
            issues.append(f"broken_insert:{getattr(entity.dxf, 'name', '?')}")
            return
        for child in children:
            visit(child, depth + 1)

    for entity in doc.modelspace():
        visit(entity)
    unique = sorted(set(issues))
    return not unique, unique


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
    # dwg2dxf writes layer "0" with a negative color (= layer off); the
    # Frontend then silently draws nothing and the "clean raster" is a blank
    # white sheet. Ground truth must always render fully: thaw + switch on.
    for layer in doc.layers:
        try:
            layer.dxf.color = abs(int(layer.dxf.color)) or 7
            layer.thaw()
        except Exception:  # noqa: BLE001
            continue
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
        backend = MatplotlibBackend(ax)
        # draw_entities, not draw_layout: dwg2dxf's SORTENTSTABLE leftovers
        # crash ezdxf's redraw-order resolution, and draw order is irrelevant
        # for a black-on-white ground-truth raster anyway.
        Frontend(RenderContext(doc), backend, config=cfg).draw_entities(msp)
        backend.finalize()
        ax.set_aspect("equal")
        ax.margins(0)
        ax.autoscale_view()
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            dpi=dpi,
            facecolor="white",
            bbox_inches="tight",
            pad_inches=0.1,
        )
        plt.close(fig)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"  render failed: {str(exc)[:120]}", file=sys.stderr)
        return None


def _safe_stem(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def _write_report(
    report_dir: pathlib.Path,
    stem: str,
    ink,
    entities,
    keep_raster,
    thin_px: int,
    thick_px: int,
) -> dict:
    """Side-by-side visual evidence for one file: binarized source, colored
    vector render (by entity type), and a coverage diff — missed ink red,
    hallucinated geometry orange. Returns fragmentation stats for the JSON."""
    import cv2
    import numpy as np

    from app.ai.cad_ir.png_render import rasterize_entities
    from app.ai.drawing_vectorize import _coverage_dilate_px

    report_dir.mkdir(parents=True, exist_ok=True)
    ink_bool = np.asarray(ink) > 0
    h, w = ink_bool.shape[:2]
    src = np.full((h, w), 255, np.uint8)
    src[ink_bool] = 0
    cv2.imwrite(str(report_dir / f"{stem}__src.png"), src)
    if not entities:
        return {}

    palette = {  # BGR
        "segment": (0, 0, 0),
        "polyline": (180, 0, 180),
        "circle": (255, 0, 0),
        "arc": (0, 160, 0),
        "hatch": (160, 160, 160),
        "text": (0, 0, 255),
        "dimension": (0, 128, 255),
    }
    vec = np.full((h, w, 3), 255, np.uint8)
    for e in entities:
        color = palette.get(e.type, (0, 0, 0))
        width = max(1, thin_px)
        if e.type == "segment":
            cv2.line(vec, (int(e.p1.x), int(e.p1.y)), (int(e.p2.x), int(e.p2.y)), color, width)
        elif e.type == "circle":
            cv2.circle(vec, (int(e.center.x), int(e.center.y)), max(1, int(e.radius)), color, width)
        elif e.type == "arc":
            cv2.ellipse(
                vec, (int(e.center.x), int(e.center.y)),
                (max(1, int(e.radius)), max(1, int(e.radius))),
                0, e.start_angle, e.end_angle, color, width,
            )
        elif e.type == "polyline":
            pts = np.array([[int(p.x), int(p.y)] for p in e.points], np.int32)
            cv2.polylines(vec, [pts], e.closed, color, width)
        elif e.type == "hatch":
            pts = np.array([[int(p.x), int(p.y)] for p in e.boundary], np.int32)
            cv2.polylines(vec, [pts], True, color, 1)
        elif e.type in ("text", "dimension") and e.source_region is not None:
            r = e.source_region
            cv2.rectangle(vec, (int(r.x0), int(r.y0)), (int(r.x1), int(r.y1)), color, 1)
    cv2.imwrite(str(report_dir / f"{stem}__vec.png"), vec)

    drawn = rasterize_entities(entities, w, h, thin_px, thick_px) < 128
    covered = drawn if keep_raster is None else (drawn | np.asarray(keep_raster).astype(bool))
    k = 2 * _coverage_dilate_px(h, w) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    covered_grown = cv2.dilate(covered.astype(np.uint8), kernel) > 0
    ink_grown = cv2.dilate(ink_bool.astype(np.uint8), kernel) > 0
    missed = ink_bool & ~covered_grown
    hallucinated = drawn & ~ink_grown
    diff = np.full((h, w, 3), 255, np.uint8)
    diff[ink_bool] = (200, 200, 200)
    diff[missed] = (0, 0, 255)
    diff[hallucinated] = (0, 128, 255)
    cv2.imwrite(str(report_dir / f"{stem}__diff.png"), diff)

    seg_lens = [
        float(np.hypot(e.p2.x - e.p1.x, e.p2.y - e.p1.y))
        for e in entities if e.type == "segment"
    ]
    short_cap = max(8.0, 0.01 * max(w, h))
    return {
        "missed_ink_fraction": round(float(missed.sum()) / max(int(ink_bool.sum()), 1), 4),
        "hallucinated_fraction": round(float(hallucinated.sum()) / max(int(drawn.sum()), 1), 4),
        "segments": len(seg_lens),
        "short_segments": sum(1 for length in seg_lens if length < short_cap),
        "median_segment_px": round(float(np.median(seg_lens)), 1) if seg_lens else None,
    }


def _write_html_index(report_dir: pathlib.Path, results: dict) -> None:
    import html
    from urllib.parse import quote

    rows: list[str] = []
    for section in ("dwg", "photos"):
        for name, rec in sorted(results.get(section, {}).items()):
            if not rec or rec.get("error"):
                continue
            stem = _safe_stem(name)
            imgs = "".join(
                f'<td><a href="{quote(stem)}__{kind}.png">'
                f'<img src="{quote(stem)}__{kind}.png" loading="lazy"></a></td>'
                for kind in ("src", "vec", "diff")
            )
            metric = (
                "<b>DECLINED</b>" if rec.get("declined")
                else f"entities={rec.get('entities')} recall={rec.get('coverage_recall')} "
                     f"precision={rec.get('coverage_precision')} "
                     f"short_seg={rec.get('short_segments')}/{rec.get('segments')} "
                     f"missed={rec.get('missed_ink_fraction')}"
            )
            rows.append(
                f"<tr><td>{html.escape(name)}<br><small>{rec.get('recognizer_used', '')}"
                f" · {metric}</small></td>{imgs}</tr>"
            )
    (report_dir / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>vectorize report</title>"
        "<style>img{max-width:420px;display:block}td{vertical-align:top;"
        "border-bottom:1px solid #ccc;padding:6px;font-family:sans-serif}</style>"
        "<table><tr><th>file</th><th>source ink</th><th>vector (by type)</th>"
        "<th>diff (red=missed, orange=hallucinated)</th></tr>"
        + "".join(rows) + "</table>",
        encoding="utf-8",
    )


def _geometry_quality(entities) -> dict:
    """B4: self-referential geometry-quality metrics — no GT alignment needed,
    so they work on photos too. They measure exactly the "рваная геометрия /
    мусор" pain: how fragmented the lines are, how much is degenerate/duplicate
    noise, and how many endpoints float free instead of meeting other geometry.
    Lower is better for every rate; fragmentation 1.0 = already consolidated."""
    import math
    segs = [e for e in entities if e.type == "segment"]
    n = len(segs)
    if n == 0:
        return {"n_segments": 0}

    def _len(s) -> float:
        return math.hypot(s.p2.x - s.p1.x, s.p2.y - s.p1.y)

    degen = sum(1 for s in segs if _len(s) < 3.0)

    tol = 2.0
    seen: set = set()
    dup = 0
    for s in segs:
        a = (round(s.p1.x / tol), round(s.p1.y / tol))
        b = (round(s.p2.x / tol), round(s.p2.y / tol))
        key = (a, b) if a <= b else (b, a)
        if key in seen:
            dup += 1
        else:
            seen.add(key)

    from app.ai.cad_recognize.verify import _open_endpoint_rate

    frag = 1.0
    try:
        from app.ai.cad_recognize.topology import consolidate_entities

        merged, _stats = consolidate_entities(list(segs))
        merged_segs = sum(1 for e in merged if e.type == "segment")
        frag = round(n / max(merged_segs, 1), 2)
    except Exception:  # noqa: BLE001
        pass

    return {
        "n_segments": n,
        "fragmentation": frag,
        "degenerate_rate": round(degen / n, 3),
        "duplicate_rate": round(dup / n, 3),
        "open_endpoint_rate": round(
            _open_endpoint_rate(segs, min_segments=2) or 0.0,
            3,
        ),
    }


def _dxf_roundtrip(entities, w: int, h: int) -> dict:
    """B4/H1: the full downstream chain — IR → ЕСКД validation → DXF →
    independent re-parse with ezdxf. A drawing that can't be re-opened is
    useless regardless of its coverage score; the validator's blocking-error
    count is the regression signal for the ЕСКД layer."""
    import io

    import ezdxf

    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    try:
        ir = CadIR(
            source=SourceInfo(image_width=w, image_height=h),
            scale=1.0,
            scale_source="manual",
            entities=list(entities),
        )
        eskd_errors = -1
        try:
            from app.ai.cad_validate import validate_ir

            report = validate_ir(ir)
            eskd_errors = len(report.blocking)
        except Exception as exc:  # noqa: BLE001
            print(f"  validate_ir failed: {str(exc)[:100]}", file=sys.stderr)
        data = render_ir_to_dxf(ir)
        doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
        return {
            "dxf_reopens": True,
            "dxf_entities": sum(1 for _ in doc.modelspace()),
            "eskd_errors": eskd_errors,
        }
    except Exception as exc:  # noqa: BLE001
        return {"dxf_reopens": False, "dxf_error": str(exc)[:100]}


def _recognize(
    image_bytes: bytes,
    enhance: bool,
    recognizer: str = "cv",
    report_dir: pathlib.Path | None = None,
    stem: str = "",
    truth_ir=None,
) -> dict | None:
    """``recognizer``: "cv" (baseline), "neural" (the seq2seq
    ``cad-vectorizer`` model only, no CV fallback — a clean read of what the
    candidate network alone achieves), or "arbitrate" (the production
    technical-vectorizer vs CV path, independently scored).

    Keep the two remote recognizers explicit here: evaluating a candidate
    checkpoint through ``TechnicalVectorizerRecognizer`` silently measures a
    different service and makes the candidate report meaningless.
    """
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
        texts, text_boxes = _ocr_text_entities(image_bytes)
    except Exception:  # noqa: BLE001
        texts = []
        text_boxes = []

    started = time.monotonic()
    used = recognizer
    if recognizer == "cv":
        out = CvRecognizer().recognize(ink, exclusion_boxes=text_boxes)
        score = (
            score_coverage(
                out.entities,
                ink,
                out.keep_raster,
                thin_px=out.thin_px,
                thick_px=out.thick_px,
            )
            if out is not None else None
        )
    elif recognizer in (
        "neural",
        "neural-tiled",
        "primitive-set",
        "directional-fields",
        "edge-graph",
        "edge-graph-snapped",
        "evidence-heatmap",
        "hierarchical-sheet",
        "hybrid-engineering",
        "hybrid-hierarchical",
    ):
        if recognizer in ("hybrid-engineering", "hybrid-hierarchical"):
            from app.ai.cad_recognize.hybrid_engineering import HybridEngineeringRecognizer

            if recognizer == "hybrid-hierarchical":
                from app.ai.cad_recognize.hierarchical_sheet import (
                    HierarchicalSheetRecognizer,
                )

                candidate = HybridEngineeringRecognizer(
                    primitive=HierarchicalSheetRecognizer()
                )
            else:
                candidate = HybridEngineeringRecognizer()
        elif recognizer == "hierarchical-sheet":
            from app.ai.cad_recognize.hierarchical_sheet import HierarchicalSheetRecognizer

            candidate = HierarchicalSheetRecognizer()
        elif recognizer == "evidence-heatmap":
            from app.ai.cad_recognize.evidence_heatmap import EvidenceHeatmapRecognizer

            candidate = EvidenceHeatmapRecognizer()
        elif recognizer == "directional-fields":
            from app.ai.cad_recognize.directional_fields import (
                DirectionalFieldRecognizer,
            )

            candidate = DirectionalFieldRecognizer()
        elif recognizer == "edge-graph":
            from app.ai.cad_recognize.edge_graph import EdgeGraphRecognizer

            candidate = EdgeGraphRecognizer()
        elif recognizer == "edge-graph-snapped":
            from app.ai.cad_recognize.edge_graph import SourceSnappedEdgeGraphRecognizer

            candidate = SourceSnappedEdgeGraphRecognizer()
        elif recognizer == "primitive-set":
            from app.ai.cad_recognize.primitive_set import PrimitiveSetRecognizer

            candidate = PrimitiveSetRecognizer()
        else:
            from app.ai.cad_recognize.neural import NeuralRecognizer

            candidate = NeuralRecognizer(
                tile_size=640 if recognizer == "neural-tiled" else None,
                tile_overlap=160,
            )
        out = candidate.recognize(ink, exclusion_boxes=text_boxes)
        score = (
            score_coverage(
                out.entities,
                ink,
                out.keep_raster,
                thin_px=out.thin_px,
                thick_px=out.thick_px,
            )
            if out is not None else None
        )
    else:  # arbitrate — the actual production decision path
        from app.ai.cad_recognize.technical_vectorizer import TechnicalVectorizerRecognizer
        from app.ai.cad_recognize.verify import arbitrate_recognition

        result = arbitrate_recognition(
            ink,
            text_boxes,
            TechnicalVectorizerRecognizer(),
            CvRecognizer(),
        )
        out = result if result.entities else None
        score = result.score if result.entities else None
        used = result.recognizer_used
    elapsed = time.monotonic() - started

    if out is None or not out.entities:
        if report_dir is not None:
            _write_report(report_dir, stem, ink, [], None, 1, 2)
        return {
            "declined": True,
            "seconds": round(elapsed, 2),
            "size": [w, h],
            "recognizer_used": used,
        }
    recognized_entities = [*out.entities, *texts]
    counts: dict[str, int] = {}
    for e in recognized_entities:
        counts[e.type] = counts.get(e.type, 0) + 1
    rec = {
        "declined": False,
        "seconds": round(elapsed, 2),
        "size": [w, h],
        "counts": counts,
        "entities": len(recognized_entities),
        "coverage_recall": score.recall,
        "coverage_precision": score.precision,
        "coverage_ok": score.ok,
        "recognizer_used": used,
        "quality": _geometry_quality(recognized_entities),
        **_dxf_roundtrip(recognized_entities, w, h),
    }
    if truth_ir is not None:
        from app.ai.cad_entity_metrics import compare_entities

        entity_metrics = compare_entities(
            recognized_entities,
            truth_ir.entities,
            predicted_size=(w, h),
            truth_size=(
                truth_ir.source.image_width,
                truth_ir.source.image_height,
            ),
            include_details=True,
        )
        rec["entity_metrics"] = entity_metrics
        rec["exact_sheet"] = entity_metrics["exact_sheet"]
        # This exposes the old lie directly: a green pixel score claiming a
        # usable result while entity-level ground truth says it is not exact.
        rec["legacy_claimed_exact"] = bool(score.ok)
        rec["false_exact"] = bool(score.ok and not entity_metrics["exact_sheet"])
    if report_dir is not None:
        rec.update(_write_report(
            report_dir, stem, ink, recognized_entities, out.keep_raster, out.thin_px, out.thick_px,
        ))
    return rec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="cleanup_test_files")
    parser.add_argument("--long-side", type=int, default=1600)
    parser.add_argument("--limit-dwg", type=int, default=0, help="0 = all")
    parser.add_argument("--limit-photos", type=int, default=0, help="0 = all")
    parser.add_argument("--skip-dwg", action="store_true")
    parser.add_argument("--skip-photos", action="store_true")
    parser.add_argument("--out", default="test-results/eval_vectorize.json")
    parser.add_argument(
        "--recognizer",
        choices=[
            "cv",
            "neural",
            "neural-tiled",
            "primitive-set",
            "directional-fields",
            "edge-graph",
            "edge-graph-snapped",
            "evidence-heatmap",
            "hierarchical-sheet",
            "hybrid-engineering",
            "hybrid-hierarchical",
            "arbitrate",
        ],
        default="cv",
        help="cv=CV-baseline (default); neural=cad-vectorizer seq2seq candidate "
             "alone (no CV fallback); neural-tiled=overlapping 640px candidate "
             "tiles; primitive-set=unordered multi-type detector candidate; "
             "directional-fields=direct endpoint/direction line proposals; "
             "edge-graph=learned line-of-interest adjacency over dense nodes; "
             "edge-graph-snapped=the same graph snapped to source skeleton nodes; "
             "evidence-heatmap=learned geometry evidence plus deterministic fitter; "
             "hierarchical-sheet=global view regions then local primitives; "
             "hybrid-engineering=CV global geometry plus independently "
             "ink-verified learned circles/arcs; "
             "hybrid-hierarchical=the same conservative fusion using the "
             "sheet-level candidate; "
             "arbitrate=production technical-vectorizer "
             "vs CV path, independently scored",
    )
    parser.add_argument(
        "--report-dir", default="",
        help="write per-file visual evidence (src/vec/diff PNG + index.html) here",
    )
    parser.add_argument(
        "--check-baseline", default="",
        help="compare the run summary against this baseline JSON and exit 1 on "
             "a regression (recall/coverage-ok/dxf-reopen down, or fragmentation/"
             "noise up beyond tolerance)",
    )
    args = parser.parse_args()
    report_dir = pathlib.Path(args.report_dir) if args.report_dir else None

    import ezdxf

    root = pathlib.Path(args.dir)
    results: dict[str, dict] = {"dwg": {}, "photos": {}}

    dwg_files = [] if args.skip_dwg else sorted(root.glob("*.dwg"))
    if args.limit_dwg:
        dwg_files = dwg_files[: args.limit_dwg]
    photos = [] if args.skip_photos else sorted([
        *root.glob("*.jpg"), *root.glob("*.jpeg"), *root.glob("*.JPG"),
        *root.glob("*.png"), *root.glob("*.PNG"),
    ])
    if args.limit_photos:
        photos = photos[: args.limit_photos]
    if not dwg_files and not photos:
        print(
            f"ERROR: no DWG or raster drawings found in {root}; regression was not executed.",
            file=sys.stderr,
        )
        return 2
    import hashlib

    from app.ai.cad_pipeline_manifest import build_cad_pipeline_manifest

    inputs = [
        {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in [*dwg_files, *photos]
    ]
    results["run_manifest"] = {
        **build_cad_pipeline_manifest(profile="auto", method="trace"),
        "evaluator": {
            "recognizer": args.recognizer,
            "long_side": args.long_side,
            "entity_tolerance": 0.0025,
        },
        "inputs": inputs,
        "input_set_sha256": hashlib.sha256(
            json.dumps(inputs, sort_keys=True).encode()
        ).hexdigest(),
    }
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
            gt_complete, gt_issues = _ground_truth_integrity(doc)
            try:
                from app.ai.cad_ir.adapters.from_dxf import dxf_to_ir
                from app.ai.cad_ir.png_render import render_ir_to_png
                from app.ai.cad_ir.resize import fit_ir_to_long_side

                truth_ir = fit_ir_to_long_side(
                    dxf_to_ir(dxf.read_bytes()),
                    args.long_side,
                )
                png = render_ir_to_png(truth_ir, thin_px=2, thick_px=3)
            except Exception as exc:  # noqa: BLE001
                results["dwg"][dwg.name] = {"error": "render_failed", "gt_counts": gt}
                print(f"  canonical GT failed: {str(exc)[:120]}", file=sys.stderr)
                continue
            rec = _recognize(
                png, enhance=False, recognizer=args.recognizer,
                report_dir=report_dir, stem=_safe_stem(dwg.name),
                truth_ir=truth_ir,
            )
            from app.ai.cad_profile import choose_profile

            profile = choose_profile(
                "auto",
                [
                    entity.text
                    for entity in truth_ir.entities
                    if entity.type in ("text", "dimension", "annotation")
                ],
                dwg.name,
            )
            rec["profile"] = profile.profile
            rec["profile_confidence"] = profile.confidence
            rec["gt_counts"] = gt
            rec["ground_truth_complete"] = gt_complete
            rec["ground_truth_issues"] = gt_issues
            results["dwg"][dwg.name] = rec
            print(f"  -> {rec.get('entities', 'declined')} entities, "
                  f"recall={rec.get('coverage_recall')}, precision={rec.get('coverage_precision')}")

    for photo in photos:
        print(f"[photo] {photo.name}")
        rec = _recognize(
            photo.read_bytes(), enhance=True, recognizer=args.recognizer,
            report_dir=report_dir, stem=_safe_stem(photo.name),
        )
        results["photos"][photo.name] = rec
        print(f"  -> {rec.get('entities', 'declined')} entities, "
              f"recall={rec.get('coverage_recall')}, precision={rec.get('coverage_precision')}")

    # Aggregates
    def _entity_aggregates(records: list[dict]) -> dict:
        evaluated = [
            r
            for r in records
            if r.get("entity_metrics") and r.get("ground_truth_complete", True)
        ]
        if not evaluated:
            return {}
        matched = sum(r["entity_metrics"]["micro"]["matched"] for r in evaluated)
        false_positive = sum(
            r["entity_metrics"]["micro"]["false_positive"] for r in evaluated
        )
        false_negative = sum(
            r["entity_metrics"]["micro"]["false_negative"] for r in evaluated
        )
        precision = matched / max(matched + false_positive, 1)
        recall = matched / max(matched + false_negative, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        claims = [r for r in evaluated if r.get("legacy_claimed_exact")]
        per_type: dict[str, dict[str, int | float]] = {}
        entity_types = sorted({
            kind
            for record in evaluated
            for kind in record["entity_metrics"]["per_type"]
        })
        for kind in entity_types:
            rows = [
                record["entity_metrics"]["per_type"].get(kind, {})
                for record in evaluated
            ]
            tp = sum(int(row.get("matched", 0)) for row in rows)
            fp = sum(int(row.get("false_positive", 0)) for row in rows)
            fn = sum(int(row.get("false_negative", 0)) for row in rows)
            kind_precision = tp / max(tp + fp, 1)
            kind_recall = tp / max(tp + fn, 1)
            kind_f1 = 2 * kind_precision * kind_recall / max(kind_precision + kind_recall, 1e-12)
            per_type[kind] = {
                "matched": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": round(kind_precision, 6),
                "recall": round(kind_recall, 6),
                "f1": round(kind_f1, 6),
            }
        return {
            "entity_precision": round(precision, 6),
            "entity_recall": round(recall, 6),
            "entity_f1": round(f1, 6),
            "exact_sheet_rate": round(
                sum(1 for r in evaluated if r.get("exact_sheet")) / len(evaluated),
                6,
            ),
            "false_exact_rate": round(
                sum(1 for r in claims if r.get("false_exact")) / max(len(claims), 1),
                6,
            ),
            "entity_evaluated_files": len(evaluated),
            "ground_truth_excluded_files": sum(
                1
                for record in records
                if record.get("entity_metrics")
                and not record.get("ground_truth_complete", True)
            ),
            "entity_errors_by_type": per_type,
        }

    def _agg(section: dict) -> dict:
        oks = [r for r in section.values() if r and not r.get("error") and not r.get("declined")]
        declined = sum(1 for r in section.values() if r.get("declined"))
        errors = sum(1 for r in section.values() if r.get("error"))
        if not oks:
            return {"files": len(section), "ok": 0, "declined": declined, "errors": errors}
        def _qmean(key: str) -> float:
            vals = [r["quality"][key] for r in oks if r.get("quality", {}).get(key) is not None]
            return round(sum(vals) / len(vals), 3) if vals else 0.0

        return {
            "files": len(section),
            "ok": len(oks),
            "declined": declined,
            "errors": errors,
            "mean_recall": round(sum(r["coverage_recall"] for r in oks) / len(oks), 4),
            "mean_precision": round(sum(r["coverage_precision"] for r in oks) / len(oks), 4),
            "coverage_ok_rate": round(sum(1 for r in oks if r["coverage_ok"]) / len(oks), 3),
            # B4: geometry-quality + round-trip aggregates (lower rates better;
            # fragmentation → 1.0 is best; dxf_reopen_rate → 1.0 is best).
            "mean_fragmentation": _qmean("fragmentation"),
            "mean_degenerate_rate": _qmean("degenerate_rate"),
            "mean_duplicate_rate": _qmean("duplicate_rate"),
            "mean_open_endpoint_rate": _qmean("open_endpoint_rate"),
            "dxf_reopen_rate": round(sum(1 for r in oks if r.get("dxf_reopens")) / len(oks), 3),
            "mean_eskd_errors": round(
                sum(r.get("eskd_errors", 0) for r in oks if r.get("eskd_errors", -1) >= 0)
                / max(sum(1 for r in oks if r.get("eskd_errors", -1) >= 0), 1),
                2,
            ),
            **_entity_aggregates(oks),
        }

    profile_names = sorted({
        record.get("profile", "auto")
        for record in results["dwg"].values()
        if record and not record.get("error")
    })
    results["summary"] = {
        "dwg": _agg(results["dwg"]),
        "photos": _agg(results["photos"]),
        "profiles": {
            profile: _agg({
                name: record
                for name, record in results["dwg"].items()
                if record.get("profile", "auto") == profile
            })
            for profile in profile_names
        },
    }

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    if report_dir is not None:
        _write_html_index(report_dir, results)
        print(f"report: {report_dir / 'index.html'}")
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))

    if args.check_baseline:
        return _check_regression(results["summary"], pathlib.Path(args.check_baseline))
    return 0


# Metrics where higher is better vs. lower is better, and how much drift is
# tolerated before it counts as a regression.
_HIGHER_BETTER = {
    "mean_recall": 0.03,
    "coverage_ok_rate": 0.05,
    "dxf_reopen_rate": 0.01,
    "entity_precision": 0.005,
    "entity_recall": 0.005,
    "entity_f1": 0.005,
    "exact_sheet_rate": 0.005,
}
_LOWER_BETTER = {
    "mean_fragmentation": 0.3,
    "mean_degenerate_rate": 0.02,
    "mean_duplicate_rate": 0.02,
    "mean_open_endpoint_rate": 0.05,
    "mean_eskd_errors": 3.0,
    "false_exact_rate": 0.0,
}


def _check_regression(summary: dict, baseline_path: pathlib.Path) -> int:
    """Fail (exit 1) if any section regressed beyond tolerance vs the baseline.
    This is what turns eval_vectorize into an automated quality gate (B4/H1)."""
    if not baseline_path.exists():
        print(f"ERROR: baseline {baseline_path} not found", file=sys.stderr)
        return 2
    base = json.loads(baseline_path.read_text()).get("summary", {})
    regressions: list[str] = []
    for section in ("dwg", "photos"):
        now, was = summary.get(section, {}), base.get(section, {})
        for metric, tol in _HIGHER_BETTER.items():
            if metric in now and metric in was and now[metric] < was[metric] - tol:
                regressions.append(f"{section}.{metric}: {was[metric]} → {now[metric]} (↓)")
        for metric, tol in _LOWER_BETTER.items():
            if metric in now and metric in was and now[metric] > was[metric] + tol:
                regressions.append(f"{section}.{metric}: {was[metric]} → {now[metric]} (↑)")
    if regressions:
        print("\nREGRESSION vs baseline:", file=sys.stderr)
        for r in regressions:
            print(f"  ✗ {r}", file=sys.stderr)
        return 1
    print("\n✓ no regression vs baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
