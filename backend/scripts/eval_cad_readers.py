"""Score CAD spec readers on real drawings, so the slot is picked by numbers.

The reader is the whole ceiling of the redraw path: nothing downstream can
recover a value it never read. This benchmark runs the production staged
reader (bounded fragment questions, dimension/evidence localization,
three-pass consensus and whole-sheet fallback) through the real router for
each candidate model and scores the answer against hand-checked ground truth.

Scoring publishes two denominators separately: nine coarse sheet-level facts
and the full hand-checked manufacturing parameter set (59 for ``spindle_v10``).
It also records the operational questions that decide whether a user gets
anything: did the spec validate, does it compile into a solid, and does a sheet
come off that solid. The coarse score must never be presented as geometric
accuracy.

    python backend/scripts/eval_cad_readers.py \
        --image test_vector_files/detal_126.png --case spindle_v10 \
        --models qwen3_vl_32b_ollama gemma4_e4b_ollama \
        --out test-results/eval_cad_readers.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# Hand-checked from the sheet itself (see the drawing, not a model output).
GROUND_TRUTH: dict[str, dict[str, Any]] = {
    "spindle_v10": {
        "part_contains": "шпиндель",
        "material_contains": "сталь 55",
        "scale": "1:2",
        "rotation_body": True,
        "hollow": True,
        "total_length_mm": 470.0,
        "max_diameter_mm": 102.0,
        "expect_dimension_text": "80js6",
        "expect_annotation_contains": "hrc",
        "reference_spec": "tests/fixtures/detal_126_reference_spec_v3.json",
    },
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


_PARAMETER_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "outer.diameter_mm": ("outer", ("diameter_mm",)),
    "outer.length_mm": ("outer", ("length_mm",)),
    "outer.thread.nominal_diameter_mm": (
        "outer", ("thread", "nominal_diameter_mm")
    ),
    "outer.thread.pitch_mm": ("outer", ("thread", "pitch_mm")),
    "outer.thread.length_mm": ("outer", ("thread", "length_mm")),
    "outer.thread.internal": ("outer", ("thread", "internal")),
    "bore.diameter_mm": ("bore", ("diameter_mm",)),
    "bore.length_mm": ("bore", ("length_mm",)),
    "bore.taper.ratio": ("bore", ("taper", "ratio")),
    "bore.thread.nominal_diameter_mm": (
        "bore", ("thread", "nominal_diameter_mm")
    ),
    "bore.thread.pitch_mm": ("bore", ("thread", "pitch_mm")),
    "bore.thread.length_mm": ("bore", ("thread", "length_mm")),
    "bore.thread.internal": ("bore", ("thread", "internal")),
    "keyways.length_mm": ("keyways", ("length_mm",)),
    "keyways.width_mm": ("keyways", ("width_mm",)),
    "keyways.depth_mm": ("keyways", ("depth_mm",)),
    "cross_holes.diameter_mm": ("cross_holes", ("diameter_mm",)),
    "cross_holes.depth_mm": ("cross_holes", ("depth_mm",)),
    "cross_holes.angle_deg": ("cross_holes", ("angle_deg",)),
    "cross_holes.through": ("cross_holes", ("through",)),
    "cross_holes.counterbore_diameter_mm": (
        "cross_holes", ("counterbore_diameter_mm",)
    ),
    "cross_holes.counterbore_depth_mm": (
        "cross_holes", ("counterbore_depth_mm",)
    ),
    "axial_holes.count": ("axial_holes", ("count",)),
    "axial_holes.bolt_circle_diameter_mm": (
        "axial_holes", ("bolt_circle_diameter_mm",)
    ),
    "axial_holes.thread.nominal_diameter_mm": (
        "axial_holes", ("thread", "nominal_diameter_mm")
    ),
    "axial_holes.thread.internal": (
        "axial_holes", ("thread", "internal")
    ),
    "chamfers.size_mm": ("chamfers", ("size_mm",)),
    "chamfers.angle_deg": ("chamfers", ("angle_deg",)),
}


def _nested_value(item: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if value is None or not isinstance(value, (bool, int, float, str)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def _parameter_groups(spec: dict) -> dict[str, list[Any]]:
    """Read-only manufacturing parameters, excluding constructed positions."""
    body = spec.get("main_view") or {}
    groups: dict[str, list[Any]] = {}
    for name, (collection, path) in _PARAMETER_GROUPS.items():
        groups[name] = []
        for item in body.get(collection) or []:
            if not isinstance(item, dict):
                continue
            value = _nested_value(item, path)
            if value is not None:
                groups[name].append(value)
    return groups


def score_parameters(spec: dict, reference_spec: dict) -> dict[str, Any]:
    """Micro accuracy over hand-read numeric facts, matched one-to-one."""
    predicted = _parameter_groups(spec)
    expected = _parameter_groups(reference_spec)
    details: dict[str, dict[str, Any]] = {}
    matched_total = 0
    expected_total = 0
    for name, wanted_values in expected.items():
        remaining = list(predicted.get(name) or [])
        matched = 0
        for wanted in wanted_values:
            def equivalent(value: Any) -> bool:
                if (
                    isinstance(wanted, (int, float))
                    and not isinstance(wanted, bool)
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    tolerance = max(0.05, abs(float(wanted)) * 0.001)
                    return abs(float(value) - float(wanted)) <= tolerance
                if isinstance(wanted, str) and isinstance(value, str):
                    return _norm(value).replace(",", ".") == _norm(wanted).replace(",", ".")
                return type(value) is type(wanted) and value == wanted

            candidate = next(
                (
                    index
                    for index, value in enumerate(remaining)
                    if equivalent(value)
                ),
                None,
            )
            if candidate is not None:
                matched += 1
                remaining.pop(candidate)
        matched_total += matched
        expected_total += len(wanted_values)
        details[name] = {
            "matched": matched,
            "expected": len(wanted_values),
            "predicted_values": predicted.get(name) or [],
            "expected_values": wanted_values,
        }
    return {
        "parameters_matched": matched_total,
        "parameters_total": expected_total,
        "parameter_accuracy": matched_total / max(expected_total, 1),
        "parameter_details": details,
    }


def summarize_results(results: list[dict]) -> dict[str, Any]:
    matched = sum(int(result.get("parameters_matched") or 0) for result in results)
    total = sum(int(result.get("parameters_total") or 0) for result in results)
    claimed = sum(bool(result.get("success_claimed")) for result in results)
    false_accepts = sum(bool(result.get("false_accept")) for result in results)
    return {
        "runs": len(results),
        "valid_specs": sum(bool(result.get("validates")) for result in results),
        "parameters_matched": matched,
        "parameters_total": total,
        "parameter_accuracy": matched / max(total, 1),
        "success_claims": claimed,
        "false_accepts": false_accepts,
        "false_accept_rate": false_accepts / max(claimed, 1),
    }


def score_spec(spec: dict, truth: dict[str, Any]) -> dict[str, Any]:
    """Per-fact scoring. Every check is either clearly right or clearly wrong."""
    checks: dict[str, bool] = {}
    main = spec.get("main_view") or {}
    bodies = [main, *(spec.get("parts") or [])]

    checks["part_name"] = truth["part_contains"] in _norm(spec.get("part"))
    title = spec.get("title_block") or {}
    checks["material"] = truth["material_contains"] in _norm(title.get("material"))
    checks["scale"] = _norm(title.get("scale")) == truth["scale"]

    rotation_words = ("вращ", "вал", "shaft", "шпинд")
    checks["body_kind"] = any(
        any(word in _norm(body.get("type")) for word in rotation_words)
        for body in bodies
    ) is truth["rotation_body"]

    checks["hollow"] = bool(
        any((body.get("bore") or []) for body in bodies)
    ) is truth["hollow"]

    outer = main.get("outer") or []
    lengths = [
        float(s["length_mm"]) for s in outer
        if isinstance(s, dict) and isinstance(s.get("length_mm"), (int, float))
    ]
    total = sum(lengths)
    checks["total_length"] = (
        abs(total - truth["total_length_mm"]) <= truth["total_length_mm"] * 0.05
    )
    diameters = [
        float(s["diameter_mm"]) for s in outer
        if isinstance(s, dict) and isinstance(s.get("diameter_mm"), (int, float))
    ]
    checks["max_diameter"] = bool(diameters) and (
        abs(max(diameters) - truth["max_diameter_mm"]) <= 1.0
    )

    dim_texts = " ".join(_norm(d.get("value")) for d in (spec.get("dimensions") or []))
    section_notes = " ".join(_norm(s.get("note")) for s in outer)
    checks["fit_read"] = truth["expect_dimension_text"] in (dim_texts + " " + section_notes)

    annotations = " ".join(_norm(a.get("text")) for a in (spec.get("annotations") or []))
    checks["annotation"] = truth["expect_annotation_contains"] in annotations

    result = {
        "checks": checks,
        "facts_correct": sum(1 for ok in checks.values() if ok),
        "facts_total": len(checks),
    }
    reference_spec = truth.get("reference_spec_data")
    if isinstance(reference_spec, dict):
        result.update(score_parameters(spec, reference_spec))
    return result


class _PreferredCadReaderRouter:
    """Pin only vision-reader calls while preserving the routed OCR specialist."""

    def __init__(self, router: Any, model_key: str) -> None:
        self.router = router
        self.model_key = model_key

    async def run(self, request: Any) -> Any:
        task = getattr(getattr(request, "task", None), "value", request.task)
        if task in {"cad_spec_read", "drawing_analysis_vlm"}:
            request = request.model_copy(
                update={"preferred_model": self.model_key}
            )
        return await self.router.run(request)


def reader_trace(raw_spec: dict[str, Any]) -> dict[str, Any]:
    """Keep narrow-question and fallback attempts as separate audit trails."""
    primary_attempts = raw_spec.get("reader_attempts") or []
    embedded_fragment_attempts = raw_spec.get("fragment_reader_attempts") or []
    fragment_attempts = embedded_fragment_attempts or [
        attempt for attempt in primary_attempts
        if isinstance(attempt, dict) and attempt.get("mode") == "fragments"
    ]
    whole_sheet_attempts = [
        attempt for attempt in primary_attempts
        if not isinstance(attempt, dict) or attempt.get("mode") != "fragments"
    ]
    return {
        "fragments": raw_spec.get("fragments"),
        "fragment_attempts": fragment_attempts,
        "whole_sheet_attempts": whole_sheet_attempts,
    }


async def evaluate_model(
    model_key: str, image_bytes: bytes, truth: dict, *, single_image: bool = False
) -> dict:
    from pydantic import ValidationError

    from app.ai.cad_recognize.spec_fragments import read_spec_best_effort
    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
    from app.ai.router import ai_router

    result: dict[str, Any] = {
        "model": model_key,
        "reader_mode": "production_staged_consensus",
        "passes": 3,
    }
    if single_image:
        result["deprecated_option"] = (
            "--single-image ignored: production staged reader chooses its own crops"
        )
    reference_spec = truth.get("reference_spec_data")
    if isinstance(reference_spec, dict):
        expected_total = sum(
            len(values) for values in _parameter_groups(reference_spec).values()
        )
        # A malformed/empty/invalid answer missed every required parameter; it
        # must stay in the micro denominator instead of looking like an empty
        # corpus with no errors.
        result.update({
            "parameters_matched": 0,
            "parameters_total": expected_total,
            "parameter_accuracy": 0.0,
        })
    started = time.monotonic()
    try:
        raw_spec = await read_spec_best_effort(
            image_bytes,
            passes=3,
            router=_PreferredCadReaderRouter(ai_router, model_key),
            confidential=True,
        )
    except Exception as exc:  # noqa: BLE001 — a failing candidate is a result
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        result["seconds"] = round(time.monotonic() - started, 1)
        return result
    result["seconds"] = round(time.monotonic() - started, 1)
    result["model_used"] = model_key
    if not raw_spec:
        result["error"] = "empty_staged_spec"
        return result

    try:
        spec = EngineeringDrawingSpec.model_validate(raw_spec).model_dump(mode="json")
    except ValidationError as exc:
        result["error"] = "schema_invalid"
        result["invalid_fields"] = [
            ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:5]
        ]
        return result

    result["reader_trace"] = reader_trace(raw_spec)
    fragment_attempts = result["reader_trace"]["fragment_attempts"]
    whole_sheet_attempts = result["reader_trace"]["whole_sheet_attempts"]
    result["attempts_completed"] = (
        len(fragment_attempts) + len(whole_sheet_attempts)
    )
    result["fragment_questions"] = sum(
        len((attempt.get("spec") or {}).get("fragment_answers") or [])
        for attempt in fragment_attempts
        if isinstance(attempt, dict)
    )
    result["validates"] = True
    result["blocking_unresolved"] = len(spec.get("unresolved") or [])
    result["views_read"] = [v.get("kind") for v in (spec.get("views") or [])]
    result.update(score_spec(spec, truth))
    # The operational question is no longer "did the 2D drafter emit entities"
    # but "does this reading make a part" — that is what the user receives now,
    # and a reading that scores well on facts and still cannot be built is a
    # reading that failed at the only thing it is for.
    result.update(await _buildability(spec))
    result["spec"] = spec
    return result


async def _buildability(spec: dict) -> dict:
    """Does this reading compile into a solid, and does a sheet come off it?"""
    from app.ai.cad_ir.sheet_from_solid import build_sheet_from_solid
    from app.ai.cad_solid import feature_tree_from_spec, solid_build_gate
    from app.services.cad_kernel import compile_candidate

    candidate = feature_tree_from_spec(spec)
    if candidate is None:
        return {"solid_built": False, "sheet_drawn": False,
                "build_error": "no supported body in the reading"}
    gate = solid_build_gate(spec, candidate, require_source_evidence=True)
    if not gate["allowed"]:
        return {
            "solid_built": False,
            "sheet_drawn": False,
            "build_blocked": True,
            "build_blockers": gate["blockers"],
            "build_warnings": gate["warnings"],
        }
    try:
        artifacts = await compile_candidate(
            candidate, confirm_assumptions=False, metadata={"source": "eval_readers"}
        )
    except Exception as exc:  # noqa: BLE001 — a failed build is a result
        return {"solid_built": False, "sheet_drawn": False,
                "build_error": f"{exc.__class__.__name__}: {exc}"[:200]}
    out: dict[str, Any] = {
        "solid_built": True,
        "solid_valid": bool((artifacts.report or {}).get("brep_valid")),
        "features_built": [feature.kind for feature in candidate.features],
        "features_declared_missing": list(candidate.missing_data),
    }
    try:
        sheet = await build_sheet_from_solid(candidate, spec, artifacts.report or {})
    except Exception as exc:  # noqa: BLE001
        out["sheet_drawn"] = False
        out["sheet_error"] = f"{exc.__class__.__name__}: {exc}"[:200]
        return out
    out["sheet_drawn"] = sheet is not None
    if sheet is not None:
        out["sheet"] = {
            "format": sheet.plan.sheet_format,
            "scale": sheet.plan.scale_label,
            "views": [view["kind"] for view in sheet.plan.views],
            "dimensions": len(sheet.drawing.get("dimensions") or []),
            "views_match_solid": bool((sheet.verification or {}).get("ok")),
        }
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--case", required=True, choices=sorted(GROUND_TRUTH))
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--out", default="test-results/eval_cad_readers.json")
    parser.add_argument(
        "--single-image", action="store_true",
        help="deprecated; staged production reader always chooses its own crops",
    )
    args = parser.parse_args()

    image_bytes = pathlib.Path(args.image).read_bytes()
    truth = dict(GROUND_TRUTH[args.case])
    reference_path = pathlib.Path(__file__).resolve().parents[1] / truth["reference_spec"]
    reference_spec = json.loads(reference_path.read_text(encoding="utf-8"))
    reference_spec.pop("_comment", None)
    truth["reference_spec_data"] = reference_spec
    results = []
    for model_key in args.models:
        print(f"→ {model_key} ...", flush=True)
        result = await evaluate_model(
            model_key, image_bytes, truth, single_image=args.single_image
        )
        result["success_claimed"] = bool(
            result.get("solid_built")
            and result.get("sheet_drawn")
            and (result.get("sheet") or {}).get("views_match_solid")
        )
        truth_complete = bool(
            result.get("parameter_accuracy") == 1.0
            and result.get("facts_correct") == result.get("facts_total")
            and int(result.get("blocking_unresolved") or 0) == 0
        )
        result["false_accept"] = bool(result["success_claimed"] and not truth_complete)
        summary = (
            f"  {result.get('seconds')}s imgs={result.get('images_sent')} "
            f"validates={result.get('validates', False)} "
            f"facts={result.get('facts_correct', 0)}/{result.get('facts_total', 0)} "
            f"parameters={result.get('parameters_matched', 0)}/"
            f"{result.get('parameters_total', 0)} "
            f"entities={result.get('drafted_entities', 0)} "
            f"blocking={result.get('blocking_unresolved', '-')} "
            f"{result.get('error', '')}"
        )
        print(summary, flush=True)
        results.append(result)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize_results(results)
    report = {
        "contract": "real-raster-cad-reader-v3",
        "case": args.case,
        "promotion_contract": {
            "parameter_accuracy": 1.0,
            "false_accept_rate": 0.0,
            "valid_specs": len(results),
        },
        "summary": summary,
        "results": results,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nwrote {out}")
    promotion_passed = bool(
        summary["valid_specs"] == len(results)
        and summary["parameter_accuracy"] == 1.0
        and summary["false_accept_rate"] == 0.0
    )
    return 0 if promotion_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
