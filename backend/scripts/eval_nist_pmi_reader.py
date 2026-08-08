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
SEMANTIC_ADAPTER = "nist-pmi-semantic-adapter-v2"

_REFERENCE_DIMENSION = re.compile(r"^(?:REF\b|\([^()]+\))", re.IGNORECASE)
_BASIC_DIMENSION = re.compile(r"^(?:BASIC\b|\[[^\]]+\]|\u25a1\s*\d)", re.IGNORECASE)
_PLUS_MINUS_TOLERANCE = re.compile(r"\d[^\n]{0,80}(?:\u00b1|\+/-)\s*\.?\d")
_LIMIT_DIMENSION = re.compile(r"\d(?:[.,]\d+)?\s*[-\u2013]\s*\d(?:[.,]\d+)?")
_UNILATERAL_TOLERANCE = re.compile(r"\d(?:[.,]\d+)?[^\n]{0,30}[+-]\s*\.?\d")
_FIT_CLASS = re.compile(r"(?:\d|\u2300)\s*(?:JS|js|[A-HJ-NP-Za-hj-np-z])\d{1,2}\b")
_THREAD_TOLERANCE = re.compile(r"\bM\s*\d[^\n]{0,50}\b\d+[A-Za-z]\b")


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


def _category_for_annotation(kind: str) -> str | None:
    return {
        "tolerance": "Geometric Tolerances",
        "datum": "Datum Features, Datum Targets, Datum Reference Frames",
        "roughness": "Dimensioning and Tolerancing Constructs",
        "thread": "Directly Toleranced Dimensions & Dimension Symbols",
    }.get(kind)


def _category_for_dimension(value: str) -> str | None:
    """Return a PMI class only for an explicit semantic construct.

    Ordinary drawing dimensions are geometry inputs, not NIST PMI candidate
    records.  Treating every number as PMI inflated the old baseline with 228
    dimension candidates and made the score describe OCR volume rather than
    semantic recognition quality.
    """

    if _REFERENCE_DIMENSION.search(value) or _BASIC_DIMENSION.search(value):
        return "Basic and Reference Dimensions:"
    if any(pattern.search(value) for pattern in (
        _PLUS_MINUS_TOLERANCE,
        _LIMIT_DIMENSION,
        _UNILATERAL_TOLERANCE,
        _FIT_CLASS,
        _THREAD_TOLERANCE,
    )):
        return "Directly Toleranced Dimensions & Dimension Symbols"
    return None


def candidates_from_spec(spec: dict[str, Any], suite: str, case_id: str) -> list[dict[str, Any]]:
    candidates = []
    for dimension in spec.get("dimensions") or []:
        value = str(dimension.get("value") or "").strip()
        category = _category_for_dimension(value)
        if value and category:
            candidates.append(
                {
                    "suite": suite,
                    "primary_case_id": str(int(case_id)),
                    "category": category,
                    "specification": value,
                    "reader_source": "dimension",
                    "reader_adapter": SEMANTIC_ADAPTER,
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
        kind = str(annotation.get("kind") or "")
        category = _category_for_annotation(kind)
        if value and category:
            candidates.append(
                {
                    "suite": suite,
                    "primary_case_id": str(int(case_id)),
                    "category": category,
                    "specification": value,
                    "reader_source": "annotation",
                    "reader_annotation_kind": kind,
                    "reader_adapter": SEMANTIC_ADAPTER,
                    "assurance": {
                        "semantic_status": "observed",
                        "geometry_linked": False,
                        "drawing_located": bool(annotation.get("evidence")),
                    },
                    "evidence": {"reader_evidence": annotation.get("evidence") or []},
                }
            )
    return candidates


def restore_candidate_sources(page_result: dict[str, Any]) -> None:
    """Upgrade checkpoints written before reader_source was recorded.

    candidates_from_spec always appends dimensions first and annotations
    second.  The parsed spec stored beside them makes that boundary explicit,
    so resuming an older checkpoint does not require another model call.
    """

    candidates = page_result.get("candidate_records") or []
    dimensions = (page_result.get("spec") or {}).get("dimensions") or []
    dimension_count = len(dimensions)
    annotations = (page_result.get("spec") or {}).get("annotations") or []
    for index, record in enumerate(candidates):
        if not isinstance(record, dict) or record.get("reader_source"):
            continue
        if index < dimension_count:
            record["reader_source"] = "dimension"
            continue
        record["reader_source"] = "annotation"
        annotation_index = index - dimension_count
        if annotation_index < len(annotations) and isinstance(
            annotations[annotation_index], dict
        ):
            record["reader_annotation_kind"] = str(
                annotations[annotation_index].get("kind") or "other"
            )


async def read_page(
    image: pathlib.Path, passes: int, model_key: str | None = None
) -> dict[str, Any]:
    from app.ai.cad_recognize.spec_fragments import read_spec_best_effort
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
    from app.ai.router import ai_router

    started = time.monotonic()
    try:
        router = ai_router
        if model_key:
            from eval_cad_readers import _PreferredCadReaderRouter

            router = _PreferredCadReaderRouter(ai_router, model_key)
        raw = await read_spec_best_effort(
            image.read_bytes(), passes=passes, router=router, confidential=True
        )
        if not raw:
            return {"error": "empty_staged_spec", "seconds": time.monotonic() - started}
        spec = EngineeringDrawingSpec.model_validate(raw).model_dump(mode="json")
        return {
            "seconds": time.monotonic() - started,
            "requested_model": model_key,
            "spec": spec,
            "reader_attempts": raw.get("reader_attempts") or [],
            "fragment_reader_attempts": raw.get("fragment_reader_attempts") or [],
        }
    except Exception as error:  # noqa: BLE001 - runtime failures are baseline facts
        return {
            "error": f"{error.__class__.__name__}: {error}"[:500],
            "seconds": time.monotonic() - started,
            "requested_model": model_key,
        }


async def run(args: argparse.Namespace) -> int:
    all_truth = load_jsonl(args.truth)
    pages = select_pages(all_truth, args.images_root, args.limit)
    if not pages:
        raise ValueError("no NIST PMI pages with semantic truth were found")

    truth_by_id = {record["semantic_id"]: record for record in all_truth}
    page_results = []
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        page_results = list(previous.get("page_results") or [])
    completed_images = {result.get("image") for result in page_results}

    def write_checkpoint() -> dict[str, Any]:
        reference_by_id = {}
        candidate_records = []
        for result in page_results:
            if result.get("spec"):
                match = PAGE_NAME.fullmatch(str(result.get("image") or ""))
                if match:
                    suite, case_id, _ = match.groups()
                    result["candidate_records"] = candidates_from_spec(
                        result["spec"], suite, case_id
                    )
            restore_candidate_sources(result)
            for semantic_id in result.get("reference_semantic_ids") or []:
                if semantic_id in truth_by_id:
                    reference_by_id[semantic_id] = truth_by_id[semantic_id]
            candidate_records.extend(result.get("candidate_records") or [])
        metrics = evaluate(list(reference_by_id.values()), candidate_records)
        metrics_by_source = {
            source: evaluate(
                list(reference_by_id.values()),
                [
                    record for record in candidate_records
                    if record.get("reader_source") == source
                ],
            )
            for source in ("dimension", "annotation")
        }
        report = {
            "contract": "nist-pmi-production-reader-baseline-v1",
            "candidate_adapter": SEMANTIC_ADAPTER,
            "passes": args.passes,
            "requested_model": args.model_key,
            "requested_pages": len(pages),
            "pages": len(page_results),
            "reader_errors": sum("error" in result for result in page_results),
            "metrics": metrics,
            "metrics_by_source": metrics_by_source,
            "page_results": page_results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return report

    for image, page_truth in pages:
        if image.name in completed_images:
            continue
        match = PAGE_NAME.fullmatch(image.name)
        if not match:
            raise ValueError(f"unsupported NIST page name: {image.name}")
        suite, case_id, _ = match.groups()
        print(f"→ {image.name} ({len(page_truth)} PMI records)", flush=True)
        result = await read_page(image, args.passes, args.model_key)
        if "spec" in result:
            candidates = candidates_from_spec(result["spec"], suite, case_id)
            result["candidate_records"] = candidates
        result["image"] = image.name
        result["reference_semantic_ids"] = [record["semantic_id"] for record in page_truth]
        page_results.append(result)
        write_checkpoint()

    report = write_checkpoint()
    print(json.dumps({key: report[key] for key in ("pages", "reader_errors", "metrics")}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", type=pathlib.Path, required=True)
    parser.add_argument("--images-root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--passes", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model-key", help="pin only CAD vision-reader calls to this catalog key")
    parser.add_argument("--resume", action="store_true", help="continue from output checkpoints")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"NIST PMI reader evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
