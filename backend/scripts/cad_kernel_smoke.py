"""Live checks for the CAD kernel — the one part of this system unit tests cannot reach.

``infra/cad-kernel/server.py`` runs inside its own container with FreeCAD and
OpenCascade; nothing in the test suite executes a line of it. Every guarantee it
makes is therefore only as good as the last time somebody ran a real part
through it, and OCC fails in ways no mock reproduces: a fillet it refuses, a
projection strategy that segfaults the process, a scale it silently ignores.

Run it against a running kernel (from inside the compose network):

    docker exec infra-backend-1 python /app/scripts/cad_kernel_smoke.py

Each check prints PASS/FAIL and the numbers it judged on, so a failure says what
the kernel actually did rather than only that it did not agree.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

# An absolute script path makes Python expose ``scripts/`` rather than the
# backend root on sys.path.  Keep the live command documented above runnable in
# both the checkout (``backend/scripts``) and the production image (``/app``).
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

KERNEL = os.environ.get("CAD_KERNEL_URL", "http://cad-kernel:8092")

# A stepped shaft: Ø80x150 then Ø102x200 then Ø60x120 — the shape every check
# below cuts into, so a failure is about the feature and not about the base.
_SHAFT_PROFILE = [
    {"r": 40.0, "z": 0.0},
    {"r": 40.0, "z": 150.0},
    {"r": 51.0, "z": 150.0},
    {"r": 51.0, "z": 350.0},
    {"r": 30.0, "z": 350.0},
    {"r": 30.0, "z": 470.0},
]


def _feature(kind: str, **params: object) -> dict:
    return {"kind": kind, "params": params, "confidence": 0.9}


def _candidate(*features: dict, label: str = "smoke") -> dict:
    return {
        "features": list(features),
        "score": 0.9,
        "label": label,
        "missing_data": [],
        "correspondences": [],
    }


def _base() -> dict:
    return _feature("revolve", profile_points=_SHAFT_PROFILE)


def _post(path: str, payload: dict, timeout: int = 300) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{KERNEL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                return response.status, json.loads(body)
            return response.status, body
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw


def _report_from_zip(payload: bytes) -> dict:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return json.loads(archive.read("report.json"))


def _compile(candidate: dict, *, confirm: bool = True) -> tuple[int, object]:
    return _post(
        "/compile", {"candidate": candidate, "confirm_assumptions": confirm, "metadata": {}}
    )


_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _localized(report: dict, kind: str) -> bool:
    return any(
        item.get("kind") == kind
        and item.get("status") == "built"
        and item.get("localization_ok") is True
        for item in report.get("feature_results", [])
    )


def _check_incremental_body_cache() -> None:
    """Only an unchanged independent body may bypass OCC boolean rebuild."""
    nonce = f"incremental-smoke:{os.getpid()}:{time.time_ns()}"
    body0 = _feature("extrude", width_mm=10.0, height_mm=8.0, depth_mm=6.0)
    body0.update({"body_index": 0, "source_entity_ids": [f"{nonce}:0"]})
    body1 = _feature("extrude", width_mm=7.0, height_mm=5.0, depth_mm=4.0)
    body1.update({"body_index": 1, "source_entity_ids": [f"{nonce}:1"]})
    first_candidate = _candidate(body0, body1, label="incremental cache smoke")
    first_status, first_payload = _compile(first_candidate)
    if first_status != 200 or not isinstance(first_payload, bytes):
        check("independent-body incremental rebuild", False, f"first HTTP {first_status}")
        return
    first = _report_from_zip(first_payload)

    second_candidate = deepcopy(first_candidate)
    second_candidate["features"][1]["params"]["width_mm"] = 9.0
    second_status, second_payload = _compile(second_candidate)
    if second_status != 200 or not isinstance(second_payload, bytes):
        check("independent-body incremental rebuild", False, f"second HTTP {second_status}")
        return
    second = _report_from_zip(second_payload)
    initial = first.get("incremental_build") or {}
    incremental = second.get("incremental_build") or {}
    ok = (
        initial.get("cache_hit_body_indices") == []
        and initial.get("cache_miss_body_indices") == [0, 1]
        and incremental.get("cache_hit_body_indices") == [0]
        and incremental.get("cache_miss_body_indices") == [1]
        and incremental.get("reused_feature_indices") == [0]
        and incremental.get("rebuilt_feature_indices") == [1]
        and incremental.get("full_rebuild") is False
        and second.get("valid") is True
        and (second.get("reopen") or {}).get("valid") is True
    )
    check(
        "independent-body incremental rebuild",
        ok,
        "first "
        f"hit={initial.get('cache_hit_body_indices')} rebuilt={initial.get('rebuilt_feature_indices')}; "
        "second "
        f"hit={incremental.get('cache_hit_body_indices')} reused={incremental.get('reused_feature_indices')} "
        f"rebuilt={incremental.get('rebuilt_feature_indices')}",
    )


def _check_operation_checkpoints() -> None:
    """Reuse only the longest reopened and topology-matched operation prefix."""
    nonce = f"operation-checkpoint-smoke:{os.getpid()}:{time.time_ns()}"
    base = _feature("extrude", width_mm=40.0, height_mm=30.0, depth_mm=10.0)
    base["source_entity_ids"] = [f"{nonce}:base"]
    boss = _feature(
        "boss", profile="circle", center_x_mm=20.0, center_y_mm=15.0,
        diameter_mm=10.0, depth_mm=5.0,
    )
    hole = _feature(
        "hole", center_x_mm=20.0, center_y_mm=15.0,
        diameter_mm=4.0, through=True,
    )
    first_candidate = _candidate(base, boss, hole, label="operation checkpoint smoke")
    first_status, first_payload = _compile(first_candidate)
    if first_status != 200 or not isinstance(first_payload, bytes):
        check("operation-level checkpoint rebuild", False, f"first HTTP {first_status}")
        return
    first = _report_from_zip(first_payload)

    second_candidate = deepcopy(first_candidate)
    second_candidate["features"][2]["params"]["diameter_mm"] = 5.0
    second_status, second_payload = _compile(second_candidate)
    if second_status != 200 or not isinstance(second_payload, bytes):
        check("operation-level checkpoint rebuild", False, f"second HTTP {second_status}")
        return
    second = _report_from_zip(second_payload)
    first_incremental = first.get("incremental_build") or {}
    second_incremental = second.get("incremental_build") or {}
    first_body = (first_incremental.get("operation_checkpoints") or [{}])[0]
    second_body = (second_incremental.get("operation_checkpoints") or [{}])[0]
    first_boss = next(
        (item for item in first_body.get("checkpoints") or []
         if item.get("checkpoint_id") == "profile_operations:0:1"),
        {},
    )
    second_hit = next(
        (item for item in second_body.get("checkpoints") or []
         if item.get("source") == "cache_hit"),
        {},
    )

    reordered_candidate = deepcopy(second_candidate)
    reordered_candidate["features"] = [base, reordered_candidate["features"][2], boss]
    third_status, third_payload = _compile(reordered_candidate)
    if third_status != 200 or not isinstance(third_payload, bytes):
        check("operation-level checkpoint rebuild", False, f"reordered HTTP {third_status}")
        return
    third = _report_from_zip(third_payload)
    third_incremental = third.get("incremental_build") or {}
    third_body = (third_incremental.get("operation_checkpoints") or [{}])[0]
    ok = (
        first_incremental.get("reused_feature_indices") == []
        and first_incremental.get("rebuilt_feature_indices") == [0, 1, 2]
        and second_incremental.get("reused_feature_indices") == [0, 1]
        and second_incremental.get("rebuilt_feature_indices") == [2]
        and second_incremental.get("full_rebuild") is False
        and second_body.get("checkpoint_hit_id") == "profile_operations:0:1"
        and first_boss.get("topology_signature") == second_hit.get("topology_signature")
        and second_hit.get("brep_valid") is True
        and second_hit.get("manifold") is True
        # Global feature indices are audit addresses. Reordering invalidates
        # the old boss/hole prefix even though the stage vocabulary is equal.
        and third_incremental.get("reused_feature_indices") == [0]
        and third_incremental.get("rebuilt_feature_indices") == [1, 2]
        and third_body.get("checkpoint_hit_id") == "base:0:0"
        and second.get("valid") is True
        and third.get("valid") is True
    )
    check(
        "operation-level checkpoint rebuild",
        ok,
        f"first rebuilt={first_incremental.get('rebuilt_feature_indices')}; "
        f"second hit={second_body.get('checkpoint_hit_id')} "
        f"reused={second_incremental.get('reused_feature_indices')} "
        f"rebuilt={second_incremental.get('rebuilt_feature_indices')}; "
        f"reorder hit={third_body.get('checkpoint_hit_id')} "
        f"reused={third_incremental.get('reused_feature_indices')} "
        f"rebuilt={third_incremental.get('rebuilt_feature_indices')}",
    )


def _check_full_application_pipeline() -> None:
    """Exercise the application boundary around the real kernel end to end.

    The lower-level checks above deliberately speak the kernel protocol
    directly.  This one starts at the normalized reader output and finishes by
    independently reopening the DXF, so a green run also proves that the
    application adapters between those stages still agree with each other.
    """
    import asyncio

    from app.ai.cad_ir.dxf_render import render_ir_to_dxf, verify_dxf_roundtrip
    from app.ai.cad_ir.sheet_from_solid import build_sheet_from_solid
    from app.ai.cad_solid import feature_tree_from_spec, solid_build_gate
    from app.services.cad_kernel import compile_candidate

    spec = {
        "part": "Контрольный полый вал",
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 40.0},
                {"diameter_mm": 60.0, "length_mm": 60.0},
            ],
            "bore": [{"diameter_mm": 30.0, "length_mm": 100.0}],
        },
        "views": [{"kind": "section", "label": "А-А"}],
        "dimensions": [
            {"value": "Ø80g6"},
            {"value": "Ø60"},
            {"value": "Ø30"},
            {"value": "40"},
            {"value": "100"},
        ],
        "annotations": [
            {"kind": "roughness", "text": "Ra 1,6", "value": "1,6"},
            {"kind": "datum", "text": "Д", "symbol": "Д"},
            {
                "kind": "tolerance",
                "text": "↗ 0,008 Д",
                "symbol": "runout",
                "value": "0,008",
                "datum_refs": ["Д"],
            },
        ],
    }
    candidate = feature_tree_from_spec(spec)
    if candidate is None:
        check("normalized spec reaches a reopened semantic DXF", False, "no feature tree")
        return
    gate = solid_build_gate(spec, candidate)
    if not gate["allowed"]:
        check(
            "normalized spec reaches a reopened semantic DXF",
            False,
            f"build gate: {gate['blockers']}",
        )
        return

    try:
        artifacts = asyncio.run(
            compile_candidate(
                candidate,
                confirm_assumptions=False,
                metadata={"source": "cad_kernel_smoke", "geometry_only": True},
            )
        )
        sheet = asyncio.run(
            build_sheet_from_solid(
                candidate,
                spec,
                artifacts.report,
                geometry_only=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a live smoke must report the boundary failure
        check(
            "normalized spec reaches a reopened semantic DXF",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return
    if sheet is None:
        check("normalized spec reaches a reopened semantic DXF", False, "no sheet")
        return

    dxf = render_ir_to_dxf(sheet.ir)
    roundtrip = verify_dxf_roundtrip(sheet.ir)
    entity_types = {entity.type for entity in sheet.ir.entities}
    visible_views = sheet.verification.get("view_coverage", {}).get("visible_views", [])
    dimension_texts = [
        entity.text
        for entity in sheet.ir.entities
        if entity.type == "dimension"
    ]
    expected_dimensions = {"40", "100", "Ø80g6", "Ø60", "Ø30"}
    annotation_texts = {
        entity.text
        for entity in sheet.ir.entities
        if entity.type == "annotation"
    }
    expected_annotations = {"Ra 1,6", "Д", "↗ 0,008 Д"}
    ok = (
        bool(artifacts.report.get("brep_valid"))
        and bool(artifacts.report.get("manifold"))
        and sheet.plan.geometry_only
        and sheet.ir.sheet is not None
        and sheet.ir.sheet.frame is False
        and "section" in visible_views
        and "hatch" in entity_types
        and "dimension" in entity_types
        and set(dimension_texts) == expected_dimensions
        and len(dimension_texts) == len(expected_dimensions)
        and annotation_texts == expected_annotations
        and bool(roundtrip.get("ok"))
        and len(dxf) > 0
    )
    check(
        "normalized spec reaches a reopened semantic DXF",
        ok,
        (
            f"views={visible_views}, entities={len(sheet.ir.entities)}, "
            f"dimensions={dimension_texts}, annotations={sorted(annotation_texts)}, "
            f"roundtrip={roundtrip.get('ok')}"
        ),
    )


def main() -> int:
    status, health = _post("/health", {}) if False else (200, None)
    with urllib.request.urlopen(f"{KERNEL}/health", timeout=30) as response:
        health = json.load(response)
    check("kernel is up", bool(health.get("ok")), str(health.get("freecad_version")))

    # Baseline: the plain shaft, so every volume below has something to compare to.
    status, payload = _compile(_candidate(_base()))
    if status != 200:
        check("plain shaft builds", False, f"HTTP {status}: {payload}")
        return 1
    base_report = _report_from_zip(payload)
    base_volume = float(base_report["volume_mm3"])
    check(
        "plain shaft builds",
        base_report["brep_valid"]
        and base_report["manifold"]
        and base_report["solid_count"] == 1
        and all(base_report.get(key, 0) > 0 for key in (
            "shell_count", "face_count", "edge_count", "vertex_count"
        ))
        # The base itself is also localized as the full initial B-Rep.
        and _localized(base_report, "revolve"),
        (
            f"V={base_volume:.0f} mm3, topology="
            f"{base_report.get('solid_count')}/{base_report.get('shell_count')}/"
            f"{base_report.get('face_count')}/{base_report.get('edge_count')}/"
            f"{base_report.get('vertex_count')}"
        ),
    )
    single_incremental = base_report.get("incremental_build") or {}
    single_body = (single_incremental.get("operation_checkpoints") or [{}])[0]
    single_checkpoint = (single_body.get("checkpoints") or [{}])[0]
    check(
        "single-body operation checkpoint contract",
        single_incremental.get("strategy") == "operation_checkpoints"
        and single_incremental.get("cache_enabled") is True
        and single_incremental.get("body_cache_enabled") is False
        and single_checkpoint.get("brep_valid") is True
        and single_checkpoint.get("manifold") is True
        and bool(single_checkpoint.get("topology_signature")),
        f"strategy={single_incremental.get('strategy')}, "
        f"hit={single_body.get('checkpoint_hit_id')}, "
        f"reused={single_incremental.get('reused_feature_indices')}, "
        f"rebuilt={single_incremental.get('rebuilt_feature_indices')}",
    )
    _check_operation_checkpoints()
    _check_incremental_body_cache()

    # A rounded plate is a different base B-Rep, not a square box whose read R
    # disappeared before OpenCascade. Its volume is the rounded-rectangle area
    # times depth.
    status, payload = _compile(_candidate(_feature(
        "extrude",
        width_mm=100.0,
        height_mm=60.0,
        depth_mm=10.0,
        corner_radius_mm=8.0,
    )))
    if status == 200:
        rounded = _report_from_zip(payload)
        expected = (100.0 * 60.0 - (4.0 - math.pi) * 8.0**2) * 10.0
        actual = float(rounded["volume_mm3"])
        check(
            "rounded plate preserves its stated corner radius",
            rounded["brep_valid"]
            and rounded["manifold"]
            and _localized(rounded, "extrude")
            and abs(actual - expected) / expected < 0.001,
            f"V={actual:.1f} mm3, expected={expected:.1f}",
        )
    else:
        check(
            "rounded plate preserves its stated corner radius",
            False,
            f"HTTP {status}: {payload}",
        )

    # 1. An annular groove removes a ring of material and nothing else.
    status, payload = _compile(
        _candidate(
            _base(),
            _feature("groove", axial_position_mm=250.0, width_mm=6.0, depth_mm=3.0),
        )
    )
    if status == 200:
        report = _report_from_zip(payload)
        removed = base_volume - float(report["volume_mm3"])
        # Ring: pi * (R^2 - (R-d)^2) * w, R=51, d=3, w=6
        expected = math.pi * (51.0**2 - 48.0**2) * 6.0
        check(
            "groove cuts the right ring",
            report["brep_valid"] and abs(removed - expected) / expected < 0.02,
            f"removed {removed:.0f} mm3, expected {expected:.0f}",
        )
        localized = next(
            (item for item in report.get("feature_results", []) if item.get("kind") == "groove"),
            {},
        )
        bounds = localized.get("changed_bounds_mm") or {}
        expected_bounds = localized.get("expected_bounds_mm") or {}
        check(
            "groove is localized on the final B-Rep",
            localized.get("status") == "built"
            and localized.get("localization_ok") is True
            and abs(float(localized.get("changed_volume_mm3") or 0.0) - expected) / expected < 0.02
            and 246.9 <= float(bounds.get("z_min", -1.0)) <= 247.1
            and 252.9 <= float(bounds.get("z_max", -1.0)) <= 253.1
            and bounds == expected_bounds,
            f"changed={localized.get('changed_volume_mm3')}, bounds={bounds}",
        )
    else:
        check("groove cuts the right ring", False, f"HTTP {status}: {payload}")

    # 2. A keyway, at three angular positions — the rotation must not break it.
    for angle in (0.0, 90.0, 180.0):
        status, payload = _compile(
            _candidate(
                _base(),
                _feature(
                    "keyway", axial_start_mm=40.0, length_mm=85.0, width_mm=12.0,
                    depth_mm=5.0, angle_deg=angle, end_type="closed",
                ),
            )
        )
        if status == 200:
            report = _report_from_zip(payload)
            removed = base_volume - float(report["volume_mm3"])
            check(
                f"keyway at {angle:g}deg",
                report["brep_valid"]
                and report["solid_count"] == 1
                and removed > 0
                and _localized(report, "keyway"),
                f"removed {removed:.0f} mm3",
            )
        else:
            check(f"keyway at {angle:g}deg", False, f"HTTP {status}: {payload}")

    # 3. A cross-drilling through the shaft.
    status, payload = _compile(
        _candidate(
            _base(),
            _feature(
                "hole", axis="radial", diameter_mm=14.0, axial_position_mm=250.0,
                center_x_mm=0.0, center_y_mm=0.0, through=True,
            ),
        )
    )
    if status == 200:
        report = _report_from_zip(payload)
        removed = base_volume - float(report["volume_mm3"])
        check(
            "cross hole goes through",
            report["brep_valid"] and removed > 0 and _localized(report, "hole"),
            f"removed {removed:.0f} mm3",
        )
    else:
        check("cross hole goes through", False, f"HTTP {status}: {payload}")

    # Cosmetic threads do not alter volume, but both external and internal
    # localization must survive into report.json for TechDraw and audit UI.
    status, payload = _compile(
        _candidate(
            _base(),
            _feature(
                "thread", spec="M75x1,5", diameter_mm=75.0, pitch_mm=1.5,
                axial_start_mm=377.0, length_mm=18.0, internal=False,
            ),
            _feature(
                "thread", spec="M54,5x2", diameter_mm=54.5, pitch_mm=2.0,
                axial_start_mm=445.0, length_mm=25.0, internal=True,
            ),
        )
    )
    if status == 200:
        report = _report_from_zip(payload)
        check(
            "external and internal threads keep exact localization",
            report.get("cosmetic_threads") == [
                {
                    "spec": "M75x1,5", "diameter_mm": 75.0, "pitch_mm": 1.5,
                    "axial_start_mm": 377.0, "length_mm": 18.0, "internal": False,
                },
                {
                    "spec": "M54,5x2", "diameter_mm": 54.5, "pitch_mm": 2.0,
                    "axial_start_mm": 445.0, "length_mm": 25.0, "internal": True,
                },
            ],
            f"threads={report.get('cosmetic_threads')}",
        )
    else:
        check(
            "external and internal threads keep exact localization",
            False,
            f"HTTP {status}: {payload}",
        )

    # End-face threaded patterns carry per-hole XY localization. The pilot is
    # real cut geometry; the thread stays cosmetic but must retain the same
    # centre and source face in report.json.
    axial_features = [_base()]
    for center_y in (20.0, -20.0):
        axial_features.extend([
            _feature(
                "hole", axis="z", diameter_mm=6.8,
                center_x_mm=0.0, center_y_mm=center_y,
                through=False, depth_mm=12.0, from_face="zmax",
            ),
            _feature(
                "thread", spec="M8", diameter_mm=8.0, pitch_mm=1.25,
                center_x_mm=0.0, center_y_mm=center_y,
                from_face="zmax", length_mm=12.0, internal=True,
            ),
        ])
    status, payload = _compile(_candidate(*axial_features))
    if status == 200:
        report = _report_from_zip(payload)
        holes = [
            item for item in report.get("feature_results", [])
            if item.get("kind") == "hole"
        ]
        expected_threads = [
            {
                "spec": "M8", "diameter_mm": 8.0, "pitch_mm": 1.25,
                "length_mm": 12.0, "center_x_mm": 0.0,
                "center_y_mm": center_y, "from_face": "zmax", "internal": True,
            }
            for center_y in (20.0, -20.0)
        ]
        check(
            "axial M8 pattern keeps pilot cuts and per-hole thread localization",
            report["brep_valid"]
            and len(holes) == 2
            and all(
                item.get("status") == "built" and item.get("localization_ok") is True
                for item in holes
            )
            and report.get("cosmetic_threads") == expected_threads,
            f"holes={len(holes)}, threads={report.get('cosmetic_threads')}",
        )
    else:
        check(
            "axial M8 pattern keeps pilot cuts and per-hole thread localization",
            False,
            f"HTTP {status}: {payload}",
        )

    # 4. A chamfer picked by WHAT IT IS, not by a hash nobody can know in advance.
    status, payload = _compile(
        _candidate(
            _base(),
            _feature(
                "chamfer", size_mm=1.0,
                edge_selector={"curve": "Circle", "at_z_mm": 0.0, "diameter_mm": 80.0},
            ),
        )
    )
    if status == 200:
        report = _report_from_zip(payload)
        removed = base_volume - float(report["volume_mm3"])
        check(
            "chamfer resolved by selector",
            report["brep_valid"]
            and 0 < removed < base_volume * 0.01
            and _localized(report, "chamfer"),
            f"removed {removed:.1f} mm3",
        )
    else:
        check("chamfer resolved by selector", False, f"HTTP {status}: {payload}")

    # 5. An ambiguous selector must NAME the candidates instead of guessing.
    status, payload = _compile(
        _candidate(_base(), _feature("chamfer", size_mm=1.0, edge_selector={"curve": "Circle"}))
    )
    if status == 200:
        report = _report_from_zip(payload)
        failed = [
            item for item in report.get("feature_results", [])
            if item.get("kind") == "chamfer" and item.get("status") == "failed"
        ]
        detail = " | ".join(report.get("warnings", []))[:240]
        check(
            "ambiguous selector is refused with candidates",
            abs(float(report["volume_mm3"]) - base_volume) < 1.0
            and bool(failed)
            and "matches" in detail,
            f"HTTP 200, kept body, failed={len(failed)}: {detail}",
        )
    else:
        detail = json.dumps(payload, ensure_ascii=False)[:240] if isinstance(payload, dict) else str(payload)[:240]
        check(
            "ambiguous selector is refused with candidates",
            status == 422 and "matches" in detail,
            f"HTTP {status}: {detail}",
        )

    # 6. A chamfer OpenCascade refuses must not cost the part (warning, not 422).
    # It refuses in two different ways, and both were measured on this build:
    # 60 mm returns a shape that is not valid, 120 mm raises StdFail_NotDone.
    for size, how in ((60.0, "invalid geometry"), (120.0, "raised")):
        status, payload = _compile(
            _candidate(
                _base(),
                _feature(
                    "chamfer", size_mm=size,
                    edge_selector={"curve": "Circle", "at_z_mm": 0.0, "diameter_mm": 80.0},
                ),
            )
        )
        if status == 200:
            report = _report_from_zip(payload)
            kept = abs(float(report["volume_mm3"]) - base_volume) < 1.0
            check(
                f"a refused chamfer keeps the part ({how})",
                report["brep_valid"]
                and kept
                and any("chamfer" in w for w in report.get("warnings", [])),
                f"V={float(report['volume_mm3']):.0f}, warnings={report.get('warnings')}",
            )
        else:
            check(
                f"a refused chamfer keeps the part ({how})", False, f"HTTP {status}: {payload}"
            )

    # 7. The flange path still works — hole after the phase reorder.
    flange = _candidate(
        _feature("revolve", profile_points=[
            {"r": 280.0, "z": 0.0}, {"r": 280.0, "z": 20.0},
        ]),
        _feature("hole", diameter_mm=80.0, center_x_mm=0.0, center_y_mm=0.0, through=True),
        _feature("hole", diameter_mm=14.0, center_x_mm=140.0, center_y_mm=0.0, through=True),
        label="flange",
    )
    status, payload = _compile(flange)
    if status == 200:
        report = _report_from_zip(payload)
        check(
            "flange with bolt holes still builds",
            report["brep_valid"] and report["solid_count"] == 1,
            f"V={float(report['volume_mm3']):.0f} mm3",
        )
    else:
        check("flange with bolt holes still builds", False, f"HTTP {status}: {payload}")

    # 8. A sheet view carries the edge handles a dimension needs.
    status, payload = _post(
        "/drawing",
        {
            "candidate": _candidate(_base()),
            "confirm_assumptions": True,
            "views": [{"kind": "front"}],
            "scale": 0.5,
            "hidden_lines": True,
            "curve_samples": 16,
            "dimensions": [],
        },
    )
    if status == 200 and isinstance(payload, dict):
        visible = (payload.get("views") or [{}])[0].get("visible") or []
        indexed = [item for item in visible if "edge_index" in item]
        check(
            "view edges are addressable",
            bool(indexed) and len(indexed) == len(visible),
            f"{len(indexed)} of {len(visible)} edges numbered",
        )
        # 9. And a dimension can actually be placed on one of them.
        if indexed:
            horizontal = [
                item for item in indexed
                if item.get("type") == "line" and len(item.get("points") or []) == 2
            ]
            target = (horizontal or indexed)[0]
            status, dimensioned = _post(
                "/drawing",
                {
                    "candidate": _candidate(_base()),
                    "confirm_assumptions": True,
                    "views": [{"kind": "front"}],
                    "scale": 0.5,
                    "hidden_lines": True,
                    "curve_samples": 16,
                    "dimensions": [
                        {
                            "view_index": 0,
                            "edge_index": int(target["edge_index"]),
                            "kind": "Distance",
                            "label": "",
                        }
                    ],
                },
            )
            dims = dimensioned.get("dimensions") if isinstance(dimensioned, dict) else None
            check(
                "a dimension lands on the edge it names",
                status == 200 and bool(dims) and bool(dims[0].get("anchors_mm")),
                f"HTTP {status}, dims={dims if dims else dimensioned}",
            )
    else:
        check("view edges are addressable", False, f"HTTP {status}: {payload}")

    # 10. A removed section is constructed as a real B-Rep section but keeps
    # its presentation identity for coverage/layout on the generated sheet.
    status, removed = _post(
        "/drawing",
        {
            "candidate": _candidate(_base()),
            "confirm_assumptions": True,
            "views": [
                {"kind": "front"},
                {
                    "kind": "section",
                    "presentation_kind": "removed_section",
                    "label": "В-В",
                    "section_symbol": "В",
                },
            ],
            "scale": 0.5,
            "hidden_lines": True,
            "dimensions": [],
        },
    )
    removed_views = removed.get("views") if isinstance(removed, dict) else None
    removed_view = removed_views[1] if removed_views and len(removed_views) > 1 else {}
    check(
        "removed section keeps its presentation kind",
        status == 200
        and removed_view.get("kind") == "removed_section"
        and removed_view.get("label") == "В-В"
        and bool(removed_view.get("hatch")),
        f"HTTP {status}, kind={removed_view.get('kind')}, hatch={len(removed_view.get('hatch') or [])}",
    )

    # 11. A local detail is a native crop of the parent projection, with a
    # separate paper scale. It must be smaller in model coverage but larger on
    # paper than the same region in the base view.
    status, detailed = _post(
        "/drawing",
        {
            "candidate": _candidate(_base()),
            "confirm_assumptions": True,
            "views": [
                {"kind": "front"},
                {
                    "kind": "detail",
                    "label": "А",
                    "detail_center_mm": [150.0, 0.0],
                    "detail_radius_mm": 30.0,
                    "detail_scale_factor": 4.0,
                },
            ],
            "scale": 0.5,
            "hidden_lines": True,
            "dimensions": [],
        },
    )
    detail_views = detailed.get("views") if isinstance(detailed, dict) else None
    detail_view = detail_views[1] if detail_views and len(detail_views) > 1 else {}
    detail_bounds = detail_view.get("bounds_mm") or {}
    detail_width = (
        float(detail_bounds.get("u_max", 0.0)) - float(detail_bounds.get("u_min", 0.0))
    )
    check(
        "detail view crops and magnifies its parent projection",
        status == 200
        and detail_view.get("kind") == "detail"
        and detail_view.get("label") == "А"
        and detail_view.get("detail_radius_mm") == 30.0
        and detail_view.get("detail_scale_factor") == 4.0
        and detail_width > 0.0
        and detail_width <= 125.0,
        f"HTTP {status}, width={detail_width:.1f}, edges={len(detail_view.get('visible') or [])}",
    )

    # 12. The application-level chain, not only isolated kernel endpoints:
    # normalized reading -> feature tree -> B-Rep -> sectioned sheet -> CadIR
    # -> geometry-only DXF -> independent semantic reopen.
    _check_full_application_pipeline()

    failed = [name for ok, name, _detail in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
