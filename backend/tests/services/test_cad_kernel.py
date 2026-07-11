import io
import json
import zipfile

import pytest

from app.services.cad_kernel import CadKernelError, _decode_artifacts


def _archive(*, report: object | None = None, extra: bool = False) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("model.step", b"ISO-10303-21;\nEND-ISO-10303-21;")
        archive.writestr("model.FCStd", b"PK\x03\x04freecad")
        archive.writestr("model.stl", b"FreeCAD STL".ljust(84, b"\0"))
        archive.writestr(
            "report.json",
            json.dumps(report if report is not None else {"valid": True, "solid_count": 1}),
        )
        if extra:
            archive.writestr("unexpected.txt", "no")
    return payload.getvalue()


def test_decode_artifacts_accepts_complete_valid_kernel_archive():
    artifacts = _decode_artifacts(_archive())

    assert artifacts.step.startswith(b"ISO-10303-21")
    assert artifacts.fcstd.startswith(b"PK")
    assert len(artifacts.stl) == 84
    assert artifacts.report["solid_count"] == 1


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
