"""DXF→DWG conversion service (LibreDWG dxf2dwg)."""

from __future__ import annotations

import pytest

from app.services.dwg_convert import (
    DwgConversionError,
    convert_dxf_to_dwg,
    dxf2dwg_available,
)


def _sample_dxf_bytes() -> bytes:
    import io

    import ezdxf

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "OBJECT"})
    msp.add_circle(center=(40, 20), radius=10, dxfattribs={"layer": "OBJECT"})
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


@pytest.mark.skipif(not dxf2dwg_available(), reason="dxf2dwg binary not installed")
def test_convert_valid_dxf_produces_dwg() -> None:
    dwg = convert_dxf_to_dwg(_sample_dxf_bytes())
    assert dwg.startswith(b"AC10")
    assert len(dwg) > 512


@pytest.mark.skipif(not dxf2dwg_available(), reason="dxf2dwg binary not installed")
def test_convert_garbage_raises() -> None:
    with pytest.raises(DwgConversionError):
        convert_dxf_to_dwg(b"this is not a dxf file")


def test_unavailable_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.dwg_convert.shutil.which", lambda _: None)
    with pytest.raises(DwgConversionError, match="недоступен"):
        convert_dxf_to_dwg(b"")
