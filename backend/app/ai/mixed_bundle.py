"""Deterministic coordinated artifact bundle for a pinned mixed EMG."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import PurePosixPath

from app.domain.engineering_model_graph import EngineeringModelGraph


_EXPECTED_SUFFIXES = {
    "production_step": {".step", ".stp"},
    "production_ifc": {".ifc"},
    "pdf": {".pdf"},
    "dxf": {".dxf"},
}


def mixed_bundle_fingerprint(
    graph: EngineeringModelGraph,
    members: dict[str, EngineeringModelGraph],
    mode: str,
) -> str:
    payload = {
        "mode": mode,
        "mixed_graph_id": graph.graph_id,
        "member_revisions": [
            {
                "alias": alias,
                "graph_id": member.graph_id,
                "revision": member.revision,
                "canonical_sha256": member.canonical_sha256,
            }
            for alias, member in sorted(members.items())
        ],
        "cross_profile_assertions": [
            item.model_dump(mode="json")
            for item in sorted(graph.assertions, key=lambda assertion: assertion.id)
            if item.state == "active" and item.predicate == "cross_profile.link"
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _zip_entry(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def build_mixed_artifact_bundle(
    *,
    graph: EngineeringModelGraph,
    members: dict[str, EngineeringModelGraph],
    mode: str,
    load_artifact: Callable[[str], bytes],
) -> tuple[bytes, bytes, dict]:
    if mode not in {"provisional", "production"}:
        raise ValueError("bundle mode must be provisional or production")
    fingerprint = mixed_bundle_fingerprint(graph, members, mode)
    files: dict[str, bytes] = {}
    artifact_entries = []
    missing = []
    for alias, member in sorted(members.items()):
        files[f"graphs/{alias}.emg.json"] = member.model_dump_json().encode()
        evidence_by_id = {item.id: item for item in member.evidence}
        found_suffixes = set()
        artifact_index = 0
        for assertion in sorted(member.assertions, key=lambda item: item.id):
            if (
                assertion.state != "active"
                or assertion.predicate != "artifact.sha256"
                or assertion.value.kind != "exact"
                or not isinstance(assertion.value.value, str)
            ):
                continue
            evidence = next(
                (
                    evidence_by_id[evidence_id]
                    for evidence_id in assertion.evidence_ids
                    if evidence_id in evidence_by_id
                    and evidence_by_id[evidence_id].payload.get("artifact_path")
                ),
                None,
            )
            if evidence is None:
                continue
            storage_path = str(evidence.payload["artifact_path"])
            content = load_artifact(storage_path)
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != assertion.value.value:
                raise ValueError(f"artifact SHA mismatch for member {alias}: {storage_path}")
            suffix = PurePosixPath(storage_path).suffix.lower() or ".bin"
            found_suffixes.add(suffix)
            archive_path = f"artifacts/{alias}/{artifact_index:03d}{suffix}"
            artifact_index += 1
            files[archive_path] = content
            entry = {
                "member": alias,
                "archive_path": archive_path,
                "source_storage_path": storage_path,
                "sha256": actual_sha,
                "size": len(content),
            }
            report_path = evidence.payload.get("report_path")
            if report_path:
                report_content = load_artifact(str(report_path))
                report_archive = f"reports/{alias}/{artifact_index - 1:03d}.json"
                files[report_archive] = report_content
                entry["report_archive_path"] = report_archive
                entry["report_sha256"] = hashlib.sha256(report_content).hexdigest()
            artifact_entries.append(entry)
        production_targets = [target for target in member.build_targets if target.id == "production"]
        if len(production_targets) != 1:
            missing.append({"member": alias, "reason": "production_target_missing"})
            continue
        expected = _EXPECTED_SUFFIXES.get(production_targets[0].kind, set())
        if expected and not found_suffixes.intersection(expected):
            missing.append({
                "member": alias,
                "reason": "required_artifact_missing",
                "expected_suffixes": sorted(expected),
            })
    manifest = {
        "schema_version": "emg-bundle/1.0",
        "mode": mode,
        "input_fingerprint": fingerprint,
        "mixed_graph": {
            "graph_id": graph.graph_id,
            "revision": graph.revision,
            "canonical_sha256": graph.canonical_sha256,
        },
        "members": [
            {
                "alias": alias,
                "graph_id": member.graph_id,
                "revision": member.revision,
                "canonical_sha256": member.canonical_sha256,
            }
            for alias, member in sorted(members.items())
        ],
        "artifacts": artifact_entries,
        "missing_required_artifacts": missing,
        "complete": not missing,
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    files["manifest.json"] = manifest_bytes
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in sorted(files.items()):
            _zip_entry(archive, name, content)
    bundle_bytes = output.getvalue()
    manifest["bundle_sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
    manifest["bundle_size"] = len(bundle_bytes)
    sidecar_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return bundle_bytes, sidecar_bytes, manifest
