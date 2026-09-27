"""Ветка метода `views` в cad_trace: результат пайплайна → ядро → параметры прогона."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from PIL import Image


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (200, 100), 255).save(buffer, format="PNG")
    return buffer.getvalue()


def test_views_method_builds_and_stores_the_body(monkeypatch):
    import app.ai.cad_views.pipeline as pipeline
    import app.services.cad_kernel as kernel
    import app.storage as storage
    from app.ai.cad_views.pipeline import ViewsResult
    from app.tasks import cad_trace

    candidate = {
        "candidate": {
            "features": [
                {
                    "kind": "revolve",
                    "params": {"profile_points": [{"r": 5, "z": 0}, {"r": 5, "z": 10}]},
                    "confidence": 0.7,
                }
            ],
            "score": 0.7,
            "label": "втулка",
        }
    }
    reading = SimpleNamespace(sheet_kind="detail", main=1, regions=[], views=lambda: [])

    async def fake_digitize(gray, router=None):
        return (
            ViewsResult(True, candidate=candidate, profile={"role": "section", "main_view": "А-А"}),
            reading,
            ["Ø10"],
        )

    async def fake_compile(cand, confirm_assumptions, metadata):
        return SimpleNamespace(
            step=b"STEP",
            iges=None,
            stl=b"STL",
            report={"volume_mm3": 785.4, "bounds_mm": {"z": 10}},
        )

    uploaded = []
    monkeypatch.setattr(pipeline, "digitize_revolve", fake_digitize)
    monkeypatch.setattr(kernel, "compile_candidate", fake_compile)
    monkeypatch.setattr(storage, "upload_file", lambda data, path, ctype: uploaded.append(path))
    events = []

    async def record(stage, status, message, details=None):
        events.append((stage, status))

    out = asyncio.run(cad_trace._run_views_method(_png(), "g1", "owner", record))

    solid = out["solid_3d"]
    assert solid["built"] and solid["method"] == "views"
    assert set(solid["paths"]) == {"step", "stl"} and len(uploaded) == 2
    assert out["views_reading"]["labels"] == ["Ø10"]
    assert ("kernel.compile", "completed") in events


def test_views_method_reports_why_no_body():
    from app.ai.cad_views.pipeline import ViewsResult

    result = ViewsResult(False, "главное изображение не симметрично оси — не тело вращения")
    assert not result.ok and "не тело вращения" in result.reason
