"""Typed client for the isolated FreeCAD/OpenCascade compilation service."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx

from app.ai.cad_ir.feature_tree import FeatureTreeCandidate
from app.config import settings


class CadKernelError(RuntimeError):
    pass


class CadKernelUnavailable(CadKernelError):
    pass


class CadKernelRejected(CadKernelError):
    pass


@dataclass(frozen=True)
class CadKernelArtifacts:
    step: bytes
    fcstd: bytes
    stl: bytes
    report: dict[str, Any]
    iges: bytes | None = None  # D4: optional exact-geometry IGES export


_EXPECTED_FILES = {"model.step", "model.FCStd", "model.stl", "report.json"}
_OPTIONAL_FILES = {"model.iges"}
_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
_MAX_MEMBER_BYTES = 80 * 1024 * 1024


def _decode_artifacts(content: bytes) -> CadKernelArtifacts:
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise CadKernelError("cad-kernel вернул слишком большой пакет")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            names = {info.filename for info in members}
            if not _EXPECTED_FILES <= names or not names <= (_EXPECTED_FILES | _OPTIONAL_FILES):
                raise CadKernelError("cad-kernel вернул неполный пакет артефактов")
            for info in members:
                if info.file_size <= 0 or info.file_size > _MAX_MEMBER_BYTES:
                    raise CadKernelError(f"Некорректный размер {info.filename}")
            step = archive.read("model.step")
            fcstd = archive.read("model.FCStd")
            stl = archive.read("model.stl")
            report = json.loads(archive.read("report.json"))
            iges = archive.read("model.iges") if "model.iges" in names else None
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CadKernelError("cad-kernel вернул повреждённый пакет") from exc

    if b"ISO-10303-21" not in step[:256] or not fcstd.startswith(b"PK") or len(stl) < 84:
        raise CadKernelError("cad-kernel вернул артефакт с неверной сигнатурой")
    if not isinstance(report, dict):
        raise CadKernelError("cad-kernel вернул некорректный отчёт валидации")
    try:
        valid_solid = bool(report.get("valid")) and int(report.get("solid_count") or 0) >= 1
    except (TypeError, ValueError) as exc:
        raise CadKernelError("cad-kernel вернул некорректный отчёт валидации") from exc
    if not valid_solid:
        raise CadKernelError("OpenCascade не подтвердил валидный solid")
    return CadKernelArtifacts(step=step, fcstd=fcstd, stl=stl, report=report, iges=iges)


async def compile_candidate(
    candidate: FeatureTreeCandidate,
    *,
    confirm_assumptions: bool,
    metadata: dict[str, str | int | float | bool | None],
) -> CadKernelArtifacts:
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "confirm_assumptions": confirm_assumptions,
        "metadata": metadata,
    }
    from app.core import metrics

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.post(f"{settings.cad_kernel_url.rstrip('/')}/compile", json=payload)
    except httpx.HTTPError as exc:
        metrics.cad_kernel_compile_total.labels(status="error").inc()
        raise CadKernelUnavailable(f"cad-kernel недоступен: {exc}") from exc
    if response.status_code != 200:
        metrics.cad_kernel_compile_total.labels(status="error").inc()
    if response.status_code == 409:
        raise CadKernelRejected("Нужно явно подтвердить допущения выбранной 3D-гипотезы")
    if response.status_code == 422:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise CadKernelRejected(str(detail or "CAD-ядро отклонило некорректную геометрию"))
    if response.status_code != 200:
        raise CadKernelUnavailable(f"cad-kernel вернул HTTP {response.status_code}")
    metrics.cad_kernel_compile_total.labels(status="ok").inc()
    return _decode_artifacts(response.content)
