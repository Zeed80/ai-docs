"""Hold the BUILDER to a hand-read sheet: spec -> solid -> drawing.

The reader has its own benchmark (eval_cad_readers.py). This one removes the
reader from the picture entirely: it starts from a spec a person read off the
drawing by hand, so every failure here belongs to the builder — the feature
translation, the kernel, the projection, the dimensioning.

What it measures, in the order the questions matter:

1. Does the part build at all, and is the solid valid and closed?
2. Does the solid measure what the spec says (length, largest diameter)?
3. Do the requested features actually appear — a groove removes material, a
   keyway removes material, a chamfer removes a little?
4. Does a sheet come back, with the views the part's class calls for?
5. Do the views measure the solid, at the scale the sheet claims?
6. How many of the read callouts carry onto the drawing as dimensions?

Needs a running kernel:

    docker exec infra-backend-1 python /app/scripts/eval_sheet_from_solid.py \
        --spec tests/fixtures/detal_126_reference_spec_v2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


async def evaluate(spec: dict) -> dict:
    from app.ai.cad_ir.sheet_from_solid import build_sheet_from_solid
    from app.ai.cad_solid import feature_tree_from_spec, verify_solid_against_spec
    from app.services.cad_kernel import compile_candidate

    result: dict = {"part": spec.get("part") or ""}

    candidate = feature_tree_from_spec(spec)
    if candidate is None:
        result["built"] = False
        result["error"] = "no supported body in the spec"
        return result
    result["features"] = [feature.kind for feature in candidate.features]
    result["not_built"] = list(candidate.missing_data)

    try:
        artifacts = await compile_candidate(
            candidate, confirm_assumptions=True, metadata={"source": "eval"}
        )
    except Exception as exc:  # noqa: BLE001 — a failed build is a result
        result["built"] = False
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        return result

    report = artifacts.report or {}
    result["built"] = True
    result["brep_valid"] = bool(report.get("brep_valid"))
    result["manifold"] = bool(report.get("manifold"))
    result["solid_count"] = report.get("solid_count")
    result["volume_mm3"] = round(float(report.get("volume_mm3") or 0.0), 1)
    result["solid_matches_spec"] = verify_solid_against_spec(report, spec).as_dict()

    sheet = await build_sheet_from_solid(candidate, spec, report)
    if sheet is None:
        result["sheet"] = None
        return result
    dimensions = sheet.drawing.get("dimensions") or []
    stated = [
        str((item or {}).get("value") or "").strip()
        for item in (spec.get("dimensions") or [])
    ]
    result["sheet"] = {
        "part_class": sheet.plan.part_class,
        "format": sheet.plan.sheet_format,
        "scale": sheet.plan.scale_label,
        "views": [view["kind"] for view in sheet.plan.views],
        "scaffold_views": sorted(sheet.plan.scaffold_views),
        "entities": len(sheet.ir.entities),
        "hatch_regions": sum(1 for e in sheet.ir.entities if e.type == "hatch"),
        "dimensions_placed": len(dimensions),
        "callouts_read": len([value for value in stated if value]),
        "views_match_solid": sheet.verification,
        "warnings": sheet.warnings,
    }
    result["dimension_labels"] = [
        f"{item.get('label') or '?'}={item.get('value_mm')}" for item in dimensions
    ]
    return result


def score(result: dict) -> tuple[int, int, list[str]]:
    """Pass/total plus what failed — every check is unambiguous."""
    checks: list[tuple[str, bool]] = [
        ("part builds", bool(result.get("built"))),
        ("solid is valid", bool(result.get("brep_valid"))),
        ("solid is closed", bool(result.get("manifold"))),
        ("one solid", result.get("solid_count") == 1),
        ("solid matches the spec", bool((result.get("solid_matches_spec") or {}).get("ok"))),
        ("sheet is drawn", bool(result.get("sheet"))),
    ]
    sheet = result.get("sheet") or {}
    if sheet:
        checks.append(("views measure the solid", bool((sheet.get("views_match_solid") or {}).get("ok"))))
        checks.append(("sheet carries dimensions", int(sheet.get("dimensions_placed") or 0) > 0))
        # A section without hatching is a section nobody can read.
        if "section" in (sheet.get("views") or []):
            checks.append(("section is hatched", int(sheet.get("hatch_regions") or 0) > 0))
    failed = [name for name, ok in checks if not ok]
    return len(checks) - len(failed), len(checks), failed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec",
        default="tests/fixtures/detal_126_reference_spec_v2.json",
        help="hand-read reference spec to build from",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    path = pathlib.Path(args.spec)
    if not path.is_absolute():
        path = pathlib.Path(__file__).resolve().parents[1] / path
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec.pop("_comment", None)

    result = await evaluate(spec)
    passed, total, failed = score(result)
    result["score"] = {"passed": passed, "total": total, "failed": failed}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n{passed}/{total} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
