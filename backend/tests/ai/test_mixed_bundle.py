import hashlib
import io
import json
import zipfile

import pytest

from app.ai.mixed_bundle import build_mixed_artifact_bundle
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphNode,
)


def _member(graph_id: str, profile: str, kind: str, path: str, content: bytes):
    evidence = Evidence(
        id="evidence:artifact",
        kind="kernel_topology",
        payload={"artifact_path": path, "report_path": path + ".json"},
    )
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile=profile,
        nodes=[GraphNode(id="artifact", type="Artifact")],
        assertions=[
            Assertion(
                id="assertion:sha",
                subject_id="artifact",
                predicate="artifact.sha256",
                value=ExactValue(kind="exact", value=hashlib.sha256(content).hexdigest()),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence.id],
                confidence=1.0,
            )
        ],
        evidence=[evidence],
        build_targets=[BuildTarget(id="production", kind=kind, root_node_ids=["artifact"])],
    ).sealed()


def test_mixed_bundle_is_deterministic_and_verifies_member_artifacts():
    step = b"deterministic STEP"
    ifc = b"deterministic IFC"
    members = {
        "assembly": _member("assembly:1", "assembly", "production_step", "a/model.step", step),
        "building": _member("building:1", "construction", "production_ifc", "b/model.ifc", ifc),
    }
    objects = {
        "a/model.step": step,
        "a/model.step.json": b"{}",
        "b/model.ifc": ifc,
        "b/model.ifc.json": b"{}",
    }
    mixed = EngineeringModelGraph(
        graph_id="mixed:1",
        profile="mixed",
        nodes=[GraphNode(id="root", type="DocumentSet")],
    ).sealed()

    first, first_sidecar, first_manifest = build_mixed_artifact_bundle(
        graph=mixed,
        members=members,
        mode="production",
        load_artifact=objects.__getitem__,
    )
    second, second_sidecar, second_manifest = build_mixed_artifact_bundle(
        graph=mixed,
        members=dict(reversed(list(members.items()))),
        mode="production",
        load_artifact=objects.__getitem__,
    )

    assert first == second
    assert first_sidecar == second_sidecar
    assert first_manifest == second_manifest
    assert first_manifest["complete"] is True
    assert first_manifest["bundle_sha256"] == hashlib.sha256(first).hexdigest()
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert json.loads(archive.read("manifest.json"))["schema_version"] == "emg-bundle/1.0"
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_mixed_bundle_reports_missing_domain_artifact_and_rejects_sha_drift():
    pdf_member = EngineeringModelGraph(
        graph_id="system:1",
        profile="pid",
        nodes=[GraphNode(id="root", type="System")],
        build_targets=[BuildTarget(id="production", kind="pdf", root_node_ids=["root"])],
    ).sealed()
    mixed = EngineeringModelGraph(
        graph_id="mixed:1",
        profile="mixed",
        nodes=[GraphNode(id="root", type="DocumentSet")],
    ).sealed()
    _bundle, _sidecar, manifest = build_mixed_artifact_bundle(
        graph=mixed,
        members={"pid": pdf_member},
        mode="provisional",
        load_artifact=lambda _path: b"",
    )
    assert manifest["complete"] is False
    assert manifest["missing_required_artifacts"][0]["expected_suffixes"] == [".pdf"]

    step = b"expected"
    member = _member("assembly:1", "assembly", "production_step", "model.step", step)
    with pytest.raises(ValueError, match="artifact SHA mismatch"):
        build_mixed_artifact_bundle(
            graph=mixed,
            members={"assembly": member},
            mode="provisional",
            load_artifact=lambda _path: b"tampered",
        )
