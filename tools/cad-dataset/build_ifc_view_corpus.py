#!/usr/bin/env python3
"""Build raster/CadIR sheets from semantic IFC container projections."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import sys
from collections import Counter, defaultdict


def _line_style(ifc_class: str, edge_kind: str, visibility: str) -> tuple[str, str]:
    if visibility == "hidden":
        return "hidden", "thin"
    if edge_kind == "section":
        return "contour", "main"
    if ifc_class in {"IfcGrid", "IfcVirtualElement"}:
        return "axis", "thin"
    if ifc_class in {"IfcSpace", "IfcOpeningElement", "IfcAnnotation"}:
        return "thin", "thin"
    service_tokens = (
        "Distribution", "Flow", "Pipe", "Duct", "Cable", "Pump", "Fan", "Boiler",
        "Chiller", "Valve", "Sanitary", "Outlet", "Controller", "Sensor", "Actuator",
    )
    if any(token in ifc_class for token in service_tokens):
        return "thin", "thin"
    return "contour", "main"


def _canonical_assets_and_splits(source_assets: list[dict]) -> tuple[list[dict], dict[str, str]]:
    by_group: dict[str, list[dict]] = defaultdict(list)
    for row in source_assets:
        by_group[row["source_group_id"]].append(row)
    canonical_assets = [
        sorted(
            group_rows,
            key=lambda row: ("IFC4X3" in row["relative_path"], row["relative_path"]),
            reverse=True,
        )[0]
        for group_rows in by_group.values()
    ]
    ordered_groups = sorted(by_group, key=lambda group: hashlib.sha256(group.encode()).hexdigest())
    if len(ordered_groups) >= 3:
        train_end = min(len(ordered_groups) - 2, max(1, round(len(ordered_groups) * 0.70)))
        val_end = min(len(ordered_groups) - 1, max(train_end + 1, round(len(ordered_groups) * 0.85)))
    else:
        train_end, val_end = len(ordered_groups), len(ordered_groups)
    benchmark_split = {
        group: "train" if index < train_end else "val" if index < val_end else "holdout"
        for index, group in enumerate(ordered_groups)
    }
    return canonical_assets, benchmark_split


def _degrade(png: bytes, seed: int) -> bytes:
    import cv2
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    image = cv2.GaussianBlur(image, (3, 3), float(rng.uniform(0.2, 0.8)))
    noise = rng.normal(0, rng.uniform(1.0, 4.0), image.shape)
    image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    stream = io.BytesIO()
    Image.fromarray(image).save(stream, format="JPEG", quality=int(rng.integers(78, 95)))
    output = io.BytesIO()
    Image.open(io.BytesIO(stream.getvalue())).convert("L").save(output, format="PNG")
    return output.getvalue()


def build(
    assets_path: pathlib.Path,
    projections: pathlib.Path,
    out: pathlib.Path,
    *,
    repo: pathlib.Path | None = None,
    max_entities: int = 2000,
    include_hidden: bool = False,
) -> dict:
    repo = repo or pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "backend"))
    from app.ai.cad_ir.png_render import render_ir_to_png
    from app.ai.cad_ir.schema import CadIR, Point, Segment, SourceInfo

    source_assets = [
        row
        for row in (json.loads(line) for line in assets_path.read_text().splitlines() if line.strip())
        if row.get("asset_format") == "ifc"
    ]
    canonical_assets, benchmark_split = _canonical_assets_and_splits(source_assets)
    assets = {pathlib.Path(row["output_path"]).stem: row for row in canonical_assets}
    for folder in ("ir", "clean", "control"):
        (out / folder).mkdir(parents=True, exist_ok=True)
    rows = []
    rejected = []
    for projection_path in sorted(projections.glob("*.json")):
        if projection_path.name == "summary.json" or projection_path.stem not in assets:
            continue
        asset = assets[projection_path.stem]
        payload = json.loads(projection_path.read_text())
        element_map = {item["guid"]: item for item in payload["elements"]}
        for view_name, primitives in payload["views"].items():
            # A top projection of the complete building usually shows its roof,
            # not a floor plan. Once a cut-plane view exists it is the only
            # truthful plan representation admitted into the corpus.
            if view_name == "plan" and payload["views"].get("plan_section"):
                continue
            if not include_hidden:
                primitives = [
                    item
                    for item in primitives
                    if item.get("visibility", "visible") == "visible"
                ]
            groups: dict[str, list[dict]] = defaultdict(list)
            for primitive in primitives:
                element = element_map.get(primitive["element_guid"], {})
                if view_name == "plan_section":
                    group_guid = primitive.get("storey_guid") or element.get("storey_guid")
                else:
                    group_guid = element.get("container_guid")
                group_guid = group_guid or "root"
                groups[group_guid].append(primitive)
            for container_guid, selected in sorted(groups.items()):
                coordinates = [point for item in selected for point in (item["p1"], item["p2"])]
                min_x, max_x = min(p[0] for p in coordinates), max(p[0] for p in coordinates)
                min_y, max_y = min(p[1] for p in coordinates), max(p[1] for p in coordinates)
                span_x, span_y = max_x - min_x, max_y - min_y
                identifier = f"{projection_path.stem}__{container_guid[:12]}__{view_name}"
                if min(span_x, span_y) <= 1e-6:
                    rejected.append({"id": identifier, "reason": "degenerate"})
                    continue
                if not 3 <= len(selected) <= max_entities:
                    rejected.append({"id": identifier, "reason": f"entity_count:{len(selected)}"})
                    continue
                width = height = 1024
                scale = min(900 / span_x, 900 / span_y)
                offset_x = (width - span_x * scale) / 2
                offset_y = (height - span_y * scale) / 2

                def point(raw):
                    return Point(
                        x=offset_x + (raw[0] - min_x) * scale,
                        y=height - (offset_y + (raw[1] - min_y) * scale),
                    )

                entities = []
                included_guids = set()
                class_counts = Counter()
                for primitive in selected:
                    p1, p2 = point(primitive["p1"]), point(primitive["p2"])
                    if ((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2) ** 0.5 < 1.0:
                        continue
                    guid = primitive["element_guid"]
                    ifc_class = primitive["ifc_class"]
                    line_class, width_class = _line_style(
                        ifc_class,
                        primitive.get("edge_kind", "feature"),
                        primitive.get("visibility", "visible"),
                    )
                    included_guids.add(guid)
                    class_counts[ifc_class] += 1
                    entities.append(Segment(
                        p1=p1,
                        p2=p2,
                        confidence=1.0,
                        origin="spec",
                        assurance="constraint_validated",
                        line_class=line_class,
                        width_class=width_class,
                        evidence=[
                            f"ifc-guid:{guid}",
                            f"ifc-class:{ifc_class}",
                            f"edge-kind:{primitive.get('edge_kind', 'feature')}",
                            f"visibility:{primitive.get('visibility', 'visible')}",
                        ],
                    ))
                if len(entities) < 3:
                    rejected.append({"id": identifier, "reason": "too_few_visible_entities"})
                    continue
                ir = CadIR(
                    source=SourceInfo(kind="spec", image_width=width, image_height=height),
                    entities=entities,
                    scale=1 / scale,
                    scale_source="calibration",
                    recognizer_used="ifc-semantic-projection",
                    digitization_status="exact_candidate",
                )
                exact_png = render_ir_to_png(ir)
                source_png = _degrade(exact_png, int(asset["sha256"][:8], 16) ^ len(rows))
                ir_path = out / "ir" / f"{identifier}.json"
                image_path = out / "clean" / f"{identifier}.png"
                control_path = out / "control" / f"{identifier}.png"
                ir_path.write_text(ir.model_dump_json())
                image_path.write_bytes(source_png)
                control_path.write_bytes(exact_png)
                rows.append({
                    "schema_version": 2,
                    "id": identifier,
                    "profile": "construction",
                    "domain": "construction",
                    "drawing_class": asset["drawing_class"],
                    "kind": "ifc_container_exact_projection",
                    "truth_kind": (
                        "ifc_semantic_storey_section"
                        if view_name == "plan_section"
                        else "ifc_semantic_hidden_line_projection"
                    ),
                    "truth_layers": ["vector_drawing_geometry", "ifc_semantics", "ifc_geometry"],
                    "source_group_id": asset["source_group_id"],
                    "split": benchmark_split[asset["source_group_id"]],
                    "source_split": asset["split"],
                    "image": str(image_path.resolve()),
                    "control_images": [str(control_path.resolve())],
                    "ir": str(ir_path.resolve()),
                    "model": asset["output_path"],
                    "view": view_name,
                    "projection_contract": payload.get("projection", {}),
                    "container_guid": container_guid,
                    "element_guids": sorted(included_guids),
                    "ifc_class_counts": dict(sorted(class_counts.items())),
                    "entity_count": len(entities),
                    "license": asset["license"],
                    "attribution": asset.get("attribution"),
                })
    with (out / "manifest.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "sheets": len(rows),
        "source_assets": len(source_assets),
        "canonical_assets": len(canonical_assets),
        "source_groups": len({row["source_group_id"] for row in rows}),
        "elements": len({(row["source_group_id"], guid) for row in rows for guid in row["element_guids"]}),
        "entities": sum(row["entity_count"] for row in rows),
        "rejected": len(rejected),
        "splits": {split: sum(row["split"] == split for row in rows) for split in ("train", "val", "holdout")},
    }
    (out / "summary.json").write_text(json.dumps({**summary, "rejections": rejected}, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", required=True, type=pathlib.Path)
    parser.add_argument("--projections", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--max-entities", type=int, default=2000)
    parser.add_argument("--include-hidden", action="store_true")
    args = parser.parse_args()
    summary = build(
        args.assets,
        args.projections,
        args.out,
        max_entities=args.max_entities,
        include_hidden=args.include_hidden,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["sheets"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
