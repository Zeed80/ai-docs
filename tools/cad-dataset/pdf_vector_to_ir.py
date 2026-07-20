#!/usr/bin/env python3
"""Convert vector PDF pages into raster/CadIR holdout pairs.

Intended for license-approved technical PDFs such as the NIST PMI test-case
definitions. PDF paths are extracted independently by PyMuPDF; the matching
input image is rendered by the PDF engine, not by CadIR, avoiding a
self-rendered ground-truth benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys


def _point(point, zoom: float):
    from app.ai.cad_ir.schema import Point

    return Point(x=float(point.x) * zoom, y=float(point.y) * zoom)


def _cubic_points(p0, p1, p2, p3, zoom: float, steps: int = 12):
    from app.ai.cad_ir.schema import Point

    points = []
    for index in range(steps + 1):
        t = index / steps
        u = 1.0 - t
        x = u**3 * p0.x + 3 * u**2 * t * p1.x + 3 * u * t**2 * p2.x + t**3 * p3.x
        y = u**3 * p0.y + 3 * u**2 * t * p1.y + 3 * u * t**2 * p2.y + t**3 * p3.y
        points.append(Point(x=float(x) * zoom, y=float(y) * zoom))
    return points


def page_to_ir(page, dpi: int, source_group_id: str):
    from app.ai.cad_ir.schema import CadIR, Point, Polyline, Segment, SourceInfo, TextEntity

    zoom = dpi / 72.0
    width = max(1, round(page.rect.width * zoom))
    height = max(1, round(page.rect.height * zoom))
    entities = []
    seen_segments: set[tuple[int, int, int, int]] = set()

    def add_segment(a, b, *, thin: bool = False) -> None:
        p1, p2 = _point(a, zoom), _point(b, zoom)
        key = tuple(round(value * 4) for value in (p1.x, p1.y, p2.x, p2.y))
        reverse = (key[2], key[3], key[0], key[1])
        if key in seen_segments or reverse in seen_segments:
            return
        seen_segments.add(key)
        entities.append(
            Segment(
                p1=p1,
                p2=p2,
                line_class="thin" if thin else "contour",
                width_class="thin" if thin else "main",
                origin="spec",
                assurance="observed",
            )
        )

    for drawing in page.get_drawings():
        width_pt = float(drawing.get("width") or 0.5)
        thin = width_pt <= 0.5
        for item in drawing["items"]:
            kind = item[0]
            if kind == "l":
                add_segment(item[1], item[2], thin=thin)
            elif kind == "re":
                rect = item[1]
                corners = [
                    Point(x=rect.x0, y=rect.y0),
                    Point(x=rect.x1, y=rect.y0),
                    Point(x=rect.x1, y=rect.y1),
                    Point(x=rect.x0, y=rect.y1),
                ]
                for start, end in zip(corners, [*corners[1:], corners[0]], strict=True):
                    add_segment(start, end, thin=thin)
            elif kind == "qu":
                quad = item[1]
                corners = [quad.ul, quad.ur, quad.lr, quad.ll]
                for start, end in zip(corners, [*corners[1:], corners[0]], strict=True):
                    add_segment(start, end, thin=thin)
            elif kind == "c":
                points = _cubic_points(item[1], item[2], item[3], item[4], zoom)
                entities.append(
                    Polyline(
                        points=points,
                        line_class="thin" if thin else "contour",
                        width_class="thin" if thin else "main",
                        origin="spec",
                        assurance="observed",
                    )
                )

    for x0, y0, _x1, _y1, text, _block, _line, _word in page.get_text("words"):
        clean = " ".join(str(text).split())
        if not clean:
            continue
        entities.append(
            TextEntity(
                position=Point(x=float(x0) * zoom, y=float(y0) * zoom),
                text=clean,
                height=10.0,
                origin="spec",
                assurance="observed",
            )
        )
    return CadIR(
        scale=25.4 / dpi,
        scale_source="dpi",
        source=SourceInfo(image_width=width, image_height=height, kind="spec"),
        entities=entities,
        digitization_status="exact_candidate",
        recognizer_used="pdf_vector_ground_truth",
        sheet={"title_block": {"source_group_id": source_group_id}},
    )


def convert(pdf_paths: list[pathlib.Path], out: pathlib.Path, dpi: int, source_id: str) -> dict:
    import fitz

    for folder in ("clean", "ir"):
        (out / folder).mkdir(parents=True, exist_ok=True)
    rows = []
    for pdf_path in pdf_paths:
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        source_group_id = f"{source_id}:{digest}"
        with fitz.open(pdf_path) as document:
            for page_index, page in enumerate(document):
                ir = page_to_ir(page, dpi, source_group_id)
                if len(ir.entities) < 20:
                    continue
                stem = f"{pdf_path.stem}_p{page_index + 1:02d}"
                image_path = out / "clean" / f"{stem}.png"
                ir_path = out / "ir" / f"{stem}.json"
                pixmap = page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csGRAY)
                pixmap.save(image_path)
                ir_path.write_text(ir.model_dump_json())
                rows.append(
                    {
                        "id": stem,
                        "profile": "mechanical",
                        "kind": "real_public_vector_pdf_holdout",
                        "source_id": source_id,
                        "source_group_id": source_group_id,
                        "split": "holdout",
                        "source_pdf": str(pdf_path.resolve()),
                        "source_sha256": digest,
                        "page": page_index,
                        "image": str(image_path.resolve()),
                        "ir": str(ir_path.resolve()),
                        "entities": ir.counts(),
                    }
                )
    with (out / "manifest.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "documents": len(pdf_paths),
        "pages": len(rows),
        "entities": sum(sum(row["entities"].values()) for row in rows),
        "profile": "mechanical",
        "split": "holdout",
        "source_id": source_id,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--source-id", default="nist_mbe_pmi")
    parser.add_argument("--glob", default="nist_?tc_??_asme1_??.pdf")
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / "backend"))
    paths = sorted(args.src.rglob(args.glob))
    if not paths:
        raise SystemExit(f"no PDF files matched {args.glob!r} under {args.src}")
    print(json.dumps(convert(paths, args.out, args.dpi, args.source_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
