"""DXF → DWG conversion via the LibreDWG ``dxf2dwg`` binary.

DXF is the master export format (written natively with ezdxf); DWG is a
closed format, so it is produced downstream by converting the DXF artifact.
LibreDWG's DWG *write* support is experimental — the conversion is
best-effort: callers get a typed error and should fall back to offering
the DXF artifact instead of failing the whole request.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()

_DXF2DWG_BINARY = "dxf2dwg"
_CONVERT_TIMEOUT_S = 60
# A DWG file smaller than this is a header stub, not a real drawing.
_MIN_DWG_SIZE_BYTES = 512


class DwgConversionError(Exception):
    """DXF→DWG conversion failed or produced an implausible file."""


def dxf2dwg_available() -> bool:
    return shutil.which(_DXF2DWG_BINARY) is not None


def convert_dxf_to_dwg(dxf_content: bytes) -> bytes:
    """Convert DXF bytes to DWG bytes. Raises DwgConversionError on failure."""
    if not dxf2dwg_available():
        raise DwgConversionError("Конвертер dxf2dwg недоступен в этой сборке")

    with tempfile.TemporaryDirectory(prefix="dxf2dwg-") as tmp:
        src = Path(tmp) / "in.dxf"
        dst = Path(tmp) / "out.dwg"
        src.write_bytes(dxf_content)
        try:
            proc = subprocess.run(
                [_DXF2DWG_BINARY, "-y", "-o", str(dst), str(src)],
                capture_output=True,
                timeout=_CONVERT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DwgConversionError("Конвертация DXF→DWG превысила лимит времени") from exc

        if proc.returncode != 0 or not dst.exists():
            stderr = proc.stderr.decode(errors="replace")[-500:]
            logger.warning("dxf2dwg_failed", returncode=proc.returncode, stderr=stderr)
            raise DwgConversionError("Не удалось сконвертировать DXF в DWG")

        data = dst.read_bytes()

    # LibreDWG DWG write is experimental: validate the result looks like a DWG
    # (magic "AC10xx" version string) and is not a truncated stub.
    if len(data) < _MIN_DWG_SIZE_BYTES or not data.startswith(b"AC10"):
        logger.warning("dxf2dwg_implausible_output", size=len(data), head=data[:6].hex())
        raise DwgConversionError("Конвертер вернул некорректный DWG-файл")

    return data
