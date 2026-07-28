"""Typed client for the isolated FreeCAD/OpenCascade compilation service."""

from __future__ import annotations

import io
import hashlib
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
    payload = candidate_compile_payload(
        candidate,
        confirm_assumptions=confirm_assumptions,
        metadata=metadata,
    )["payload"]
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


def candidate_compile_payload(
    candidate: FeatureTreeCandidate,
    *,
    confirm_assumptions: bool,
    metadata: dict[str, str | int | float | bool | None],
) -> dict[str, Any]:
    """Return the exact JSON boundary and its stable digest for audit/UI.

    ``httpx(..., json=payload)`` serializes this same object.  The digest uses a
    canonical representation so key ordering in a browser cannot change it.
    """
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "confirm_assumptions": confirm_assumptions,
        "metadata": metadata,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {"payload": payload, "sha256": hashlib.sha256(canonical).hexdigest()}


async def project_candidate(
    candidate: FeatureTreeCandidate, *, views: tuple[str, ...] = ("front", "side")
) -> dict[str, Any] | None:
    """Orthographic views of a compiled candidate, as exact 2D primitives.

    Returns None when the kernel does not expose ``/project`` (older image), so
    a deployment mid-upgrade degrades to "no derived views" instead of failing
    the whole digitization.
    """
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "views": list(views),
        "confirm_assumptions": True,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
            response = await client.post(
                f"{settings.cad_kernel_url.rstrip('/')}/project", json=payload
            )
    except httpx.HTTPError as exc:
        raise CadKernelUnavailable(f"cad-kernel недоступен: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code == 422:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise CadKernelRejected(str(detail or "CAD-ядро отклонило проекцию"))
    if response.status_code != 200:
        raise CadKernelUnavailable(f"cad-kernel вернул HTTP {response.status_code}")
    return response.json().get("views")


async def draw_candidate_sheet(
    candidate: FeatureTreeCandidate,
    *,
    views: list[dict[str, Any]],
    scale: float = 1.0,
    hidden_lines: bool = True,
    dimensions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Sheet views built by TechDraw, sections included.

    ``project_candidate`` returns raw orthographic projections; this returns
    what a drawing needs and those cannot express — above all a section view,
    which for a hollow turned part IS the main view. Returns None on a kernel
    without ``/drawing`` so a deployment mid-upgrade degrades to the older
    projections instead of failing the digitization.
    """
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "views": views,
        "scale": scale,
        "hidden_lines": hidden_lines,
        "dimensions": dimensions or [],
        "confirm_assumptions": True,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
            response = await client.post(
                f"{settings.cad_kernel_url.rstrip('/')}/drawing", json=payload
            )
    except httpx.HTTPError as exc:
        raise CadKernelUnavailable(f"cad-kernel недоступен: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code == 422:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise CadKernelRejected(str(detail or "CAD-ядро отклонило построение листа"))
    if response.status_code != 200:
        raise CadKernelUnavailable(f"cad-kernel вернул HTTP {response.status_code}")
    return response.json()
