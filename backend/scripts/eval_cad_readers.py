"""Score CAD spec readers on real drawings, so the slot is picked by numbers.

The reader is the whole ceiling of the redraw path: nothing downstream can
recover a value it never read. This benchmark runs the PRODUCTION prompt and
tiling (``_SPEC_PROMPT`` + ``_spec_images``) through the real router for each
candidate model and scores the answer against hand-checked ground truth for
the sheet — facts a human read off the drawing, not another model's opinion.

Scoring is deliberately coarse and unambiguous (name, material, scale, body
kind, hollowness, overall length, largest diameter, a fit, a hardness note)
plus the two operational questions that decide whether a user gets anything:
did the spec validate, and did the drafter produce geometry.

    python backend/scripts/eval_cad_readers.py \
        --image test_vector_files/detal_126.png --case spindle_v10 \
        --models qwen3_vl_32b_ollama gemma4_e4b_ollama \
        --out test-results/eval_cad_readers.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
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
    },
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


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

    return {
        "checks": checks,
        "facts_correct": sum(1 for ok in checks.values() if ok),
        "facts_total": len(checks),
    }


async def evaluate_model(
    model_key: str, image_bytes: bytes, truth: dict, *, single_image: bool = False
) -> dict:
    from pydantic import ValidationError

    from app.ai.cad_recognize.spec_vectorize import (
        _SPEC_PROMPT,
        _coerce_spec_containers,
        _parse_spec_json,
        _spec_images,
        EngineeringDrawingSpec,
        SpecReadMalformedError,
        SpecReadTruncatedError,
        draft_from_spec,
    )
    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, AITask, ChatMessage
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    images, descriptions, _coverage = _spec_images(image)
    if single_image:
        # Production sends an overview plus source-resolution tiles. Several
        # models answer that with an empty string after minutes of GPU, so this
        # mode isolates "cannot read the drawing" from "cannot take 5 images".
        images, descriptions = images[:1], descriptions[:1]
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(
            role="user",
            content=_SPEC_PROMPT + "\nКАРТА ИЗОБРАЖЕНИЙ:\n" + "\n".join(descriptions),
        )],
        images=[base64.b64encode(value).decode() for value in images],
        confidential=True,
        allow_cloud=False,
        preferred_model=model_key,
        metadata={"num_predict": 24000},
    )
    result: dict[str, Any] = {"model": model_key, "images_sent": len(images)}
    started = time.monotonic()
    try:
        response = await ai_router.run(request)
    except Exception as exc:  # noqa: BLE001 — a failing candidate is a result
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        result["seconds"] = round(time.monotonic() - started, 1)
        return result
    result["seconds"] = round(time.monotonic() - started, 1)
    result["model_used"] = getattr(response, "model", None)
    raw = response.text or ""
    result["raw_chars"] = len(raw)
    # Keep the raw answer: every reader failure so far was diagnosable only
    # from the exact bytes the model produced.
    pathlib.Path(f"/tmp/raw_{model_key}.txt").write_text(raw)
    if not raw.strip():
        result["error"] = "empty_response"
        return result

    try:
        parsed = _coerce_spec_containers(_parse_spec_json(raw, strict=True))
    except SpecReadTruncatedError:
        result["error"] = "truncated_json"
        return result
    except SpecReadMalformedError as exc:
        result["error"] = "malformed_json"
        result["detail"] = str(exc)[:200]
        return result
    if not parsed:
        result["error"] = "unparsable_json"
        return result
    try:
        spec = EngineeringDrawingSpec.model_validate(parsed).model_dump(mode="json")
    except ValidationError as exc:
        result["error"] = "schema_invalid"
        result["invalid_fields"] = [
            ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:5]
        ]
        return result

    result["validates"] = True
    result["blocking_unresolved"] = len(spec.get("unresolved") or [])
    result["views_read"] = [v.get("kind") for v in (spec.get("views") or [])]
    result.update(score_spec(spec, truth))
    ir = draft_from_spec(spec, sheet_format="A3", landscape=True)
    result["drafted_entities"] = 0 if ir is None else len(ir.entities)
    result["spec"] = spec
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--case", required=True, choices=sorted(GROUND_TRUTH))
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--out", default="test-results/eval_cad_readers.json")
    parser.add_argument(
        "--single-image", action="store_true",
        help="send only the overview image (isolates multi-image failures)",
    )
    args = parser.parse_args()

    image_bytes = pathlib.Path(args.image).read_bytes()
    truth = GROUND_TRUTH[args.case]
    results = []
    for model_key in args.models:
        print(f"→ {model_key} ...", flush=True)
        result = await evaluate_model(
            model_key, image_bytes, truth, single_image=args.single_image
        )
        summary = (
            f"  {result.get('seconds')}s imgs={result.get('images_sent')} "
            f"validates={result.get('validates', False)} "
            f"facts={result.get('facts_correct', 0)}/{result.get('facts_total', 0)} "
            f"entities={result.get('drafted_entities', 0)} "
            f"blocking={result.get('blocking_unresolved', '-')} "
            f"{result.get('error', '')}"
        )
        print(summary, flush=True)
        results.append(result)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"case": args.case, "results": results}, ensure_ascii=False, indent=2)
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
