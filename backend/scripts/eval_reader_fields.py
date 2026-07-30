"""Score the CAD reader field by field against the ground truth we already have.

``example-drawings/ground_truth.json`` currently carries seven synthetic
sheets. They are useful contract fixtures, but they are not evidence of raster
reader quality. The report therefore carries source provenance and fails its
promotion gate until at least one explicitly labelled real sheet was actually
evaluated; an unrelated photograph elsewhere in the repository does not count.

What this reports is per-field recall — of the diameters the sheet actually
carries, how many did the reader find; same for fits and roughness. A single
aggregate score would hide the thing that matters, which is WHICH field fails.

    python backend/scripts/eval_reader_fields.py \
        --drawings example-drawings --out test-results/reader_fields.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TOLERANCE = 0.005
_FLOOR = 0.05
_REAL_SOURCES = frozenset({"real", "hand_checked_real", "public_real"})


def corpus_provenance(rows: list[dict]) -> dict:
    """Count only explicit provenance; unknown is never silently called real."""
    by_source: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
    real_sheets = sum(
        count for source, count in by_source.items() if source in _REAL_SOURCES
    )
    return {
        "sheets": len(rows),
        "real_sheets": real_sheets,
        "synthetic_sheets": by_source.get("synthetic", 0),
        "unknown_source_sheets": by_source.get("unknown", 0),
        "by_source": dict(sorted(by_source.items())),
    }


def _truth_fields(entry: dict) -> dict:
    """Flatten the ground-truth entry into the values a reader should extract."""
    diameters: list[float] = []
    fits: set[str] = set()
    roughness: set[float] = set()
    for feature in entry.get("features") or []:
        for dimension in feature.get("dimensions") or []:
            nominal = dimension.get("nominal")
            if isinstance(nominal, (int, float)) and dimension.get("dim_type") == "diameter":
                diameters.append(float(nominal))
            if dimension.get("fit_system"):
                fits.add(str(dimension["fit_system"]))
        for surface in feature.get("surfaces") or []:
            value = surface.get("value")
            if isinstance(value, (int, float)):
                roughness.add(float(value))
    return {
        "diameters": sorted(set(diameters)),
        "fits": sorted(fits),
        "roughness": sorted(roughness),
    }


def _read_fields(spec: dict) -> dict:
    """The same quantities, as the reader reported them."""
    import re

    diameters: list[float] = []
    fits: set[str] = set()
    roughness: set[float] = set()

    body = spec.get("main_view") or {}
    for section in (body.get("outer") or []) + (body.get("bore") or []):
        value = section.get("diameter_mm") if isinstance(section, dict) else None
        if isinstance(value, (int, float)):
            diameters.append(float(value))
    profile = body.get("profile") or {}
    for key in ("diameter_mm",):
        if isinstance(profile.get(key), (int, float)):
            diameters.append(float(profile[key]))
    for hole in profile.get("holes") or []:
        if isinstance(hole, dict) and isinstance(hole.get("diameter_mm"), (int, float)):
            diameters.append(float(hole["diameter_mm"]))
    # A pattern states its hole diameter once for the whole set; without this
    # the score punished the reader for expressing four holes compactly.
    for pattern in profile.get("hole_patterns") or []:
        if not isinstance(pattern, dict):
            continue
        value = pattern.get("hole_diameter_mm")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            diameters.append(float(value))

    for dimension in spec.get("dimensions") or []:
        text = str((dimension or {}).get("value") or "")
        number = re.search(r"\d+(?:[.,]\d+)?", text)
        if number and text.lstrip()[:1] in ("Ø", "⌀"):
            diameters.append(float(number.group().replace(",", ".")))
        fit = re.search(r"(?<=\d)([A-Za-z]{1,3}\d{1,2})", text)
        if fit:
            fits.add(fit.group(1))
    for annotation in spec.get("annotations") or []:
        text = str((annotation or {}).get("text") or "")
        match = re.search(r"Ra\s*(\d+(?:[.,]\d+)?)", text, re.IGNORECASE)
        if match:
            roughness.add(float(match.group(1).replace(",", ".")))
    return {
        "diameters": sorted(set(diameters)),
        "fits": sorted(fits),
        "roughness": sorted(roughness),
    }


def _numeric_recall(truth: list[float], found: list[float]) -> tuple[int, int]:
    hits = 0
    for value in truth:
        window = max(_FLOOR, abs(value) * _TOLERANCE)
        if any(abs(value - other) <= window for other in found):
            hits += 1
    return hits, len(truth)


def _set_recall(truth: list[str], found: list[str]) -> tuple[int, int]:
    lowered = {item.lower() for item in found}
    return sum(1 for item in truth if item.lower() in lowered), len(truth)


async def _run(drawings: pathlib.Path, out: pathlib.Path, *, min_real_sheets: int) -> int:
    from app.ai.cad_recognize.spec_fragments import read_spec_by_fragments

    truth_file = drawings / "ground_truth.json"
    entries = json.loads(truth_file.read_text(encoding="utf-8"))
    results = []
    totals = {"diameters": [0, 0], "fits": [0, 0], "roughness": [0, 0]}

    for entry in entries:
        name = entry.get("filename") or ""
        path = drawings / name
        if not path.exists() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        truth = _truth_fields(entry)
        started = time.monotonic()
        spec = await read_spec_by_fragments(path.read_bytes())
        elapsed = round(time.monotonic() - started, 1)
        found = _read_fields(spec or {})

        row = {
            "sheet": name,
            "source": str(entry.get("source") or "unknown"),
            "seconds": elapsed,
            "spec_read": bool(spec),
        }
        for field, scorer in (
            ("diameters", _numeric_recall), ("fits", _set_recall),
            ("roughness", _numeric_recall),
        ):
            hits, total = scorer(truth[field], found[field])
            totals[field][0] += hits
            totals[field][1] += total
            row[field] = {"found": hits, "expected": total,
                          "truth": truth[field], "read": found[field]}
        results.append(row)
        print(
            f"{name:32s} {elapsed:6.1f}s  "
            f"Ø {row['diameters']['found']}/{row['diameters']['expected']}  "
            f"посадки {row['fits']['found']}/{row['fits']['expected']}  "
            f"Ra {row['roughness']['found']}/{row['roughness']['expected']}",
            flush=True,
        )

    summary = {
        field: {"found": hits, "expected": total,
                "recall": round(hits / total, 3) if total else None}
        for field, (hits, total) in totals.items()
    }
    provenance = corpus_provenance(results)
    promotion_eligible = provenance["real_sheets"] >= min_real_sheets
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "contract": "reader-fields-v2",
                "corpus": provenance,
                "promotion_contract": {"min_real_sheets": min_real_sheets},
                "promotion_eligible": promotion_eligible,
                "sheets": results,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\nИТОГО:", json.dumps(summary, ensure_ascii=False))
    print(f"записано в {out}")
    if not promotion_eligible:
        print(
            f"PROMOTION BLOCKED: real sheets {provenance['real_sheets']}"
            f"/{min_real_sheets}",
            flush=True,
        )
    return 0 if promotion_eligible else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drawings", default="example-drawings")
    parser.add_argument("--out", default="test-results/reader_fields.json")
    parser.add_argument(
        "--min-real-sheets",
        type=int,
        default=1,
        help="minimum explicitly labelled real sheets required for promotion",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            pathlib.Path(args.drawings),
            pathlib.Path(args.out),
            min_real_sheets=max(args.min_real_sheets, 0),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
