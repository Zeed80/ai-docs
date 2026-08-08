#!/usr/bin/env python3
"""Run the production staged CAD reader against official NIST PMI pages.

This is intentionally a strict record-level baseline.  A number mentioned by
the model is not credited for an official compound PMI construct unless the
normalized category and complete specification both match the workbook truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_pmi_manifest import evaluate


PAGE_NAME = re.compile(r"nist_(ctc|ftc)_(\d{2})_asme1_[a-z]+_p(\d{2})\.png$")


def load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def page_truth_index(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for candidate in record.get("evidence", {}).get("drawing_page_candidates", []):
            stem = pathlib.Path(candidate["asset"]).stem
            page_name = f"{stem}_p{int(candidate['page']):02d}.png"
            index.setdefault(page_name, []).append(record)
    return index


def select_pages(
    records: list[dict[str, Any]], images_root: pathlib.Path, limit: int | None
) -> list[tuple[pathlib.Path, list[dict[str, Any]]]]:
    index = page_truth_index(records)
    selected = []
    for page_name, truth in sorted(index.items()):
        image = images_root / page_name
        if image.exists():
            selected.append((image, truth))
        if limit and len(selected) >= limit:
            break
    return selected


def _category_for_annotation(kind: str) -> str:
    return {
        "tolerance": "Geometric Tolerances",
        "datum": "Datum Features, Datum Targets, Datum Reference Frames",
        "roughness": "Dimensioning and Tolerancing Constructs",
        "thread": "Directly Toleranced Dimensions & Dimension Symbols",
    }.get(kind, "Dimensioning and Tolerancing Constructs")


def candidates_from_spec(spec: dict[str, Any], suite: str, case_id: str) -> list[dict[str, Any]]:
    candidates = []
    for dimension in spec.get("dimensions") or []:
        value = str(dimension.get("value") or "").strip()
        if value:
            candidates.append(
                {
                    "suite": suite,
                    "primary_case_id": str(int(case_id)),
                    "category": "Directly Toleranced Dimensions & Dimension Symbols",
                    "specification": value,
                    "assurance": {
                        "semantic_status": "observed",
                        "geometry_linked": False,
                        "drawing_located": bool(dimension.get("evidence")),
                    },
                    "evidence": {"reader_evidence": dimension.get("evidence") or []},
                }
            )
    for annotation in spec.get("annotations") or []:
        value = str(annotation.get("text") or annotation.get("value") or "").strip()
        if value:
            candidates.append(
                {
                    "suite": suite,
                    "primary_case_id": str(int(case_id)),
                    "category": _category_for_annotation(str(annotation.get("kind") or "")),
                    "specification": value,
                    "assurance": {
                        "semantic_status": "observed",
                        "geometry_linked": False,
                        "drawing_located": bool(annotation.get("evidence")),
                    },
                    "evidence": {"reader_evidence": annotation.get("evidence") or []},
                }
            )
    return candidates


async def read_page(image: pathlib.Path, passes: int) -> dict[str, Any]:
    from app.ai.cad_recognize.spec_fragments import read_spec_best_effort
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
    from app.ai.router import ai_router

    started = time.monotonic()
    try:
        raw = await read_spec_best_effort(
            image.read_bytes(), passes=passes, router=ai_router, confidential=True
        )
        if not raw:
            return {"error": "empty_staged_spec", "seconds": time.monotonic() - started}
        spec = EngineeringDrawingSpec.model_validate(raw).model_dump(mode="json")
        return {
            "seconds": time.monotonic() - started,
            "spec": spec,
            "reader_attempts": raw.get("reader_attempts") or [],
            "fragment_reader_attempts": raw.get("fragment_reader_attempts") or [],
        }
    except Exception as error:  # noqa: BLE001 - runtime failures are baseline facts
        return {
            "error": f"{error.__class__.__name__}: {error}"[:500],
            "seconds": time.monotonic() - started,
        }


async def run(args: argparse.Namespace) -> int:
    all_truth = load_jsonl(args.truth)
    pages = select_pages(all_truth, args.images_root, args.limit)
    if not pages:
        raise ValueError("no NIST PMI pages with semantic truth were found")

    reference_by_id = {}
    candidate_records = []
    page_results = []
    for image, page_truth in pages:
        match = PAGE_NAME.fullmatch(image.name)
        if not match:
            raise ValueError(f"unsupported NIST page name: {image.name}")
        suite, case_id, _ = match.groups()
        print(f"→ {image.name} ({len(page_truth)} PMI records)", flush=True)
        result = await read_page(image, args.passes)
        for record in page_truth:
            reference_by_id[record["semantic_id"]] = record
        if "spec" in result:
            candidates = candidates_from_spec(result["spec"], suite, case_id)
            candidate_records.extend(candidates)
            result["candidate_records"] = candidates
        result["image"] = image.name
        result["reference_semantic_ids"] = [record["semantic_id"] for record in page_truth]
        page_results.append(result)

    reference = list(reference_by_id.values())
    metrics = evaluate(reference, candidate_records)
    report = {
        "contract": "nist-pmi-production-reader-baseline-v1",
        "passes": args.passes,
        "pages": len(pages),
        "reader_errors": sum("error" in result for result in page_results),
        "metrics": metrics,
        "page_results": page_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in ("pages", "reader_errors", "metrics")}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", type=pathlib.Path, required=True)
    parser.add_argument("--images-root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--passes", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"NIST PMI reader evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
