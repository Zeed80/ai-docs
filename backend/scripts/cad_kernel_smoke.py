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
import os
import sys
import urllib.error
import urllib.request

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
        base_report["brep_valid"] and base_report["solid_count"] == 1,
        f"V={base_volume:.0f} mm3",
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
        import math

        expected = math.pi * (51.0**2 - 48.0**2) * 6.0
        check(
            "groove cuts the right ring",
            report["brep_valid"] and abs(removed - expected) / expected < 0.02,
            f"removed {removed:.0f} mm3, expected {expected:.0f}",
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
                report["brep_valid"] and report["solid_count"] == 1 and removed > 0,
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
            report["brep_valid"] and removed > 0,
            f"removed {removed:.0f} mm3",
        )
    else:
        check("cross hole goes through", False, f"HTTP {status}: {payload}")

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
            report["brep_valid"] and 0 < removed < base_volume * 0.01,
            f"removed {removed:.1f} mm3",
        )
    else:
        check("chamfer resolved by selector", False, f"HTTP {status}: {payload}")

    # 5. An ambiguous selector must NAME the candidates instead of guessing.
    status, payload = _compile(
        _candidate(_base(), _feature("chamfer", size_mm=1.0, edge_selector={"curve": "Circle"}))
    )
    detail = json.dumps(payload, ensure_ascii=False)[:160] if isinstance(payload, dict) else str(payload)[:160]
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

    failed = [name for ok, name, _detail in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
