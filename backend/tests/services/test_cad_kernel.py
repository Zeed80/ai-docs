import io
import json
import zipfile

import pytest

from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate
from app.services.cad_kernel import (
    CadKernelError,
    _decode_artifacts,
    candidate_compile_payload,
)


def _archive(
    *,
    report: object | None = None,
    extra: bool = False,
    iges: bool = False,
    topology: object | None = None,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("model.step", b"ISO-10303-21;\nEND-ISO-10303-21;")
        archive.writestr("model.FCStd", b"PK\x03\x04freecad")
        archive.writestr("model.stl", b"FreeCAD STL".ljust(84, b"\0"))
        archive.writestr(
            "report.json",
            json.dumps(report if report is not None else {"valid": True, "solid_count": 1}),
        )
        if iges:
            archive.writestr("model.iges", b"IGES CONTENT".ljust(90, b" "))
        if topology is not None:
            archive.writestr("topology.json", json.dumps(topology))
        if extra:
            archive.writestr("unexpected.txt", "no")
    return payload.getvalue()


def test_decode_artifacts_accepts_complete_valid_kernel_archive():
    artifacts = _decode_artifacts(_archive())

    assert artifacts.step.startswith(b"ISO-10303-21")
    assert artifacts.fcstd.startswith(b"PK")
    assert len(artifacts.stl) == 84
    assert artifacts.report["solid_count"] == 1
    assert artifacts.iges is None  # optional, absent here


def test_decode_artifacts_accepts_optional_iges():
    # D4: an IGES member is accepted and returned; its absence is fine too.
    artifacts = _decode_artifacts(_archive(iges=True))
    assert artifacts.iges is not None
    assert artifacts.iges.startswith(b"IGES CONTENT")


def test_decode_artifacts_accepts_optional_topology():
    # Ф3.1 — a zip WITHOUT topology.json (an older kernel, or one that
    # genuinely omitted it) must still decode fine; this is the exact
    # regression a live redeploy hit: the archive allowlist rejected the
    # WHOLE package the moment the kernel started adding this file.
    artifacts = _decode_artifacts(_archive())
    assert artifacts.topology is None

    topology = {
        "schema": "cad-kernel-topology/1.0",
        "faces": [{"key": "face-abc", "vertices": [], "triangles": []}],
    }
    with_topology = _decode_artifacts(_archive(topology=topology))
    assert with_topology.topology == topology


def test_decode_artifacts_rejects_non_dict_topology():
    with pytest.raises(CadKernelError, match="топологию"):
        _decode_artifacts(_archive(topology=["not", "a", "dict"]))


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"not a zip", "повреждённый"),
        (_archive(extra=True), "неполный"),
        (_archive(report={"valid": False, "solid_count": 0}), "валидный solid"),
        (_archive(report=[]), "отчёт валидации"),
    ],
)
def test_decode_artifacts_rejects_untrusted_kernel_payload(payload: bytes, expected: str):
    with pytest.raises(CadKernelError, match=expected):
        _decode_artifacts(payload)


def test_compile_payload_digest_is_stable_and_covers_exact_kernel_json():
    candidate = FeatureTreeCandidate(
        features=[
            Feature3D(
                kind="revolve",
                params={"profile_points": [{"r": 10, "z": 0}, {"r": 10, "z": 20}]},
                confidence=0.9,
            )
        ],
        score=0.9,
        label="Вал",
    )
    first = candidate_compile_payload(
        candidate,
        confirm_assumptions=False,
        metadata={"source": "spec_reader", "generation_id": "test"},
    )
    second = candidate_compile_payload(
        candidate,
        confirm_assumptions=False,
        metadata={"generation_id": "test", "source": "spec_reader"},
    )
    assert first["payload"] == second["payload"]
    assert first["sha256"] == second["sha256"]
    assert len(first["sha256"]) == 64
