#!/usr/bin/env python3
"""Acquire only license-approved CAD vectorization sources.

The source registry is the policy boundary. Quarantined/non-commercial
sources are never downloaded, even when they are much larger than the
approved corpus. QCAD assets additionally require an allow-listed license in
their per-file RDF metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

ALLOWED_RDF_LICENSES = {
    "http://creativecommons.org/publicdomain/mark/1.0/",
    "http://creativecommons.org/licenses/by/3.0/",
}
DRAWABLE_TYPES = {
    "LINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "LWPOLYLINE",
    "POLYLINE",
    "SPLINE",
    "TEXT",
    "MTEXT",
    "DIMENSION",
    "HATCH",
    "INSERT",
}


@dataclass(frozen=True)
class Asset:
    source_id: str
    source_group_id: str
    profile: str
    relative_path: str
    output_path: str
    license: str
    sha256: str
    entity_count: int
    split: str
    asset_format: str = "dxf"
    attribution: str | None = None


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_split(source_group_id: str) -> str:
    bucket = int(hashlib.sha256(source_group_id.encode()).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "val"
    return "holdout"


def _read_registry(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported source registry schema")
    sources = {item["id"]: item for item in payload["sources"]}
    for item in sources.values():
        if item["status"].startswith("approved") and not item["commercial_training"]:
            raise ValueError(f"approved source {item['id']} forbids commercial training")
    return sources


def _rdf_license(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(errors="replace")
    match = re.search(r"<dcterms:license(?: rdf:resource=\"([^\"]+)\"|>([^<]+))", text)
    if not match:
        return None
    return (match.group(1) or match.group(2)).strip()


def _profile_for(relative: pathlib.Path) -> str:
    lowered = "/".join(part.lower() for part in relative.parts)
    if "architecture" in lowered or "aec" in lowered or "bath" in lowered or "window" in lowered:
        return "construction"
    if "mechanic" in lowered or "gear" in lowered or "screw" in lowered or "flange" in lowered:
        return "mechanical"
    return "mixed"


def _entity_count(path: pathlib.Path) -> int:
    import ezdxf

    document = ezdxf.readfile(path)
    auditor = document.audit()
    if auditor.has_errors:
        raise ValueError(f"DXF audit errors: {len(auditor.errors)}")
    return sum(1 for entity in document.modelspace() if entity.dxftype() in DRAWABLE_TYPES)


def _clone_qcad(target: pathlib.Path, revision: str) -> None:
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        subprocess.run(["git", "init"], cwd=target, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/qcad/qcad.git"],
            cwd=target,
            check=True,
        )
        subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=target, check=True)
    subprocess.run(["git", "sparse-checkout", "set", "libraries", "examples"], cwd=target, check=True)
    # Fetch the reviewed commit itself. A shallow clone of the moving default
    # branch made acquisition fail whenever upstream advanced, despite the
    # registry having a deliberate immutable pin.
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", revision],
        cwd=target,
        check=True,
    )
    subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=target, check=True)
    actual_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
    if actual_revision != revision:
        raise RuntimeError(
            f"QCAD revision drift: registry pins {revision}, downloaded {actual_revision}; "
            "review licenses/content and update the pin explicitly"
        )


def acquire_qcad(
    source: dict[str, Any],
    checkout: pathlib.Path,
    output: pathlib.Path,
) -> tuple[list[Asset], list[dict[str, str]]]:
    _clone_qcad(checkout, source["revision"])
    assets: list[Asset] = []
    rejected: list[dict[str, str]] = []
    source_root = checkout / "libraries"
    for dxf_path in sorted(source_root.rglob("*.dxf")):
        relative = dxf_path.relative_to(checkout)
        rdf_path = dxf_path.with_suffix(".rdf")
        license_uri = _rdf_license(rdf_path)
        if license_uri not in ALLOWED_RDF_LICENSES:
            rejected.append({"path": str(relative), "reason": "missing_or_disallowed_sidecar_license"})
            continue
        try:
            entity_count = _entity_count(dxf_path)
        except Exception as exc:  # noqa: BLE001
            rejected.append({"path": str(relative), "reason": f"invalid_dxf:{type(exc).__name__}"})
            continue
        if entity_count < 3:
            rejected.append({"path": str(relative), "reason": "too_few_drawable_entities"})
            continue

        profile = _profile_for(relative)
        digest = _sha256(dxf_path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(relative.with_suffix("")))[:120]
        destination = output / "vector" / source["id"] / f"{safe_name}_{digest[:12]}.dxf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dxf_path, destination)
        group = f"{source['id']}:{relative}"
        assets.append(
            Asset(
                source_id=source["id"],
                source_group_id=group,
                profile=profile,
                relative_path=str(relative),
                output_path=str(destination.resolve()),
                license=license_uri,
                sha256=digest,
                entity_count=entity_count,
                split=_stable_split(group),
            )
        )
    return assets, rejected


def _github_tree(repo: str, revision: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/git/trees/{revision}?recursive=1"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "cad-corpus-builder/1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        payload = json.load(response)
    if payload.get("truncated"):
        raise RuntimeError(f"GitHub tree for {repo}@{revision} was truncated")
    return payload["tree"]


def _step_geometry_count(content: bytes) -> int:
    text = content.decode("latin-1", errors="ignore").upper()
    if "ISO-10303-21" not in text:
        return 0
    return sum(
        text.count(token)
        for token in ("ADVANCED_FACE(", "EDGE_CURVE(", "ORIENTED_EDGE(", "CIRCLE(")
    )


def _family_for_step(path: str, roots: list[str]) -> str:
    root = max((root for root in roots if path.startswith(f"{root}/")), key=len)
    remainder = path[len(root) + 1 :]
    first = remainder.split("/", 1)[0]
    return f"{root}/{first}" if "/" in remainder else root


def acquire_freecad_library(
    source: dict[str, Any],
    output: pathlib.Path,
) -> tuple[list[Asset], list[dict[str, str]]]:
    """Download a pinned, family-balanced subset instead of cloning ~5 GB."""

    repo = "FreeCAD/FreeCAD-library"
    roots = list(source["selection_roots"])
    candidates = [
        item
        for item in _github_tree(repo, source["revision"])
        if item.get("type") == "blob"
        and item["path"].lower().endswith((".step", ".stp"))
        and any(item["path"].startswith(f"{root}/") for root in roots)
    ]
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        by_family[_family_for_step(item["path"], roots)].append(item)

    selected: list[dict[str, Any]] = []
    per_family = int(source["max_per_family"])
    for family in sorted(by_family):
        # Hash ordering prevents repository naming conventions from selecting
        # only the smallest size series (M1, M1.2, M1.4, ...).
        ordered = sorted(
            by_family[family],
            key=lambda item: hashlib.sha256(item["path"].encode()).hexdigest(),
        )
        selected.extend(ordered[:per_family])
    selected = sorted(
        selected,
        key=lambda item: hashlib.sha256(item["path"].encode()).hexdigest(),
    )[: int(source["max_assets"])]

    assets: list[Asset] = []
    rejected: list[dict[str, str]] = []
    for item in selected:
        relative = item["path"]
        quoted = urllib.parse.quote(relative, safe="/")
        url = (
            f"https://raw.githubusercontent.com/{repo}/{source['revision']}/{quoted}"
        )
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "cad-corpus-builder/1"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                content = response.read(25 * 1024 * 1024 + 1)
        except Exception as exc:  # noqa: BLE001
            rejected.append(
                {"path": relative, "reason": f"download_failed:{type(exc).__name__}"}
            )
            continue
        if len(content) > 25 * 1024 * 1024:
            rejected.append({"path": relative, "reason": "file_too_large"})
            continue
        geometry_count = _step_geometry_count(content)
        if geometry_count < 10:
            rejected.append({"path": relative, "reason": "too_little_step_geometry"})
            continue

        digest = hashlib.sha256(content).hexdigest()
        safe_name = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            str(pathlib.PurePosixPath(relative).with_suffix("")),
        )[:150]
        destination = (
            output
            / "step"
            / source["id"]
            / f"{safe_name}_{digest[:12]}.step"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        group = f"{source['id']}:{relative}"
        assets.append(
            Asset(
                source_id=source["id"],
                source_group_id=group,
                profile=(
                    "construction"
                    if relative.startswith("Architectural Parts/")
                    else "mechanical"
                ),
                relative_path=relative,
                output_path=str(destination.resolve()),
                license=source["license"],
                sha256=digest,
                entity_count=geometry_count,
                split=_stable_split(group),
                asset_format="step",
                attribution=f"FreeCAD Parts Library contributor; source path: {relative}",
            )
        )
    return assets, rejected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("cad-dataset-out/open-sources"))
    parser.add_argument("--qcad-checkout", type=pathlib.Path)
    parser.add_argument(
        "--registry",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("source_registry.json"),
    )
    args = parser.parse_args()

    sources = _read_registry(args.registry)
    qcad = sources["qcad_open_library"]
    checkout = args.qcad_checkout or args.out / "_checkouts" / "qcad"
    assets, rejected = acquire_qcad(qcad, checkout, args.out)
    freecad_assets, freecad_rejected = acquire_freecad_library(
        sources["freecad_parts_library"],
        args.out,
    )
    assets.extend(freecad_assets)
    rejected.extend(freecad_rejected)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "assets.jsonl"
    with manifest.open("w") as stream:
        for asset in assets:
            stream.write(json.dumps(asset.__dict__, ensure_ascii=False) + "\n")
    (args.out / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2))
    snapshot = {
        "registry_sha256": _sha256(args.registry),
        "approved_sources": [
            source_id for source_id, item in sources.items() if item["status"].startswith("approved")
        ],
        "quarantined_sources": [
            source_id for source_id, item in sources.items() if item["status"] == "quarantined"
        ],
        "accepted_assets": len(assets),
        "rejected_assets": len(rejected),
        "profiles": {
            profile: sum(asset.profile == profile for asset in assets)
            for profile in ("mechanical", "construction", "mixed")
        },
        "formats": {
            asset_format: sum(asset.asset_format == asset_format for asset in assets)
            for asset_format in ("dxf", "step", "ifc")
        },
        "splits": {
            split: sum(asset.split == split for asset in assets)
            for split in ("train", "val", "holdout")
        },
    }
    (args.out / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0 if assets else 1


if __name__ == "__main__":
    sys.exit(main())
