"""Offline tests for the drawing pipeline routed entirely through AIRouter.

After the model_resolver → AIRouter.run migration, all VLM/text drawing calls
go through the routing store. These use a real AIRouter with stubbed providers
to assert: images travel in request.images, the vision vs chat dispatch is
correct, confidentiality is honoured, and router=None falls back to the shared
ai_router singleton (not the removed legacy get_vlm_model path).
"""

from __future__ import annotations

import json

import pytest

from app.ai import task_routing as tr
from app.ai.router import AIRouter
from app.ai.schemas import AIRequest, AIResponse, AITask, AIUsage, ProviderKind


class RecordingProvider:
    """Records which method (chat/vision) was called and the request."""

    def __init__(self, kind=ProviderKind.OLLAMA, payload: dict | None = None):
        self.kind = kind
        self.payload = payload or {"title_block": {"name": "Вал"}, "features": [{"name": "Ø20"}]}
        self.vision_calls: list[AIRequest] = []
        self.chat_calls: list[AIRequest] = []

    async def vision(self, request, model):
        self.vision_calls.append(request)
        return AIResponse(
            task=request.task, provider=self.kind, model=model,
            text=json.dumps(self.payload), usage=AIUsage(input_tokens=5, output_tokens=5),
        )

    async def chat(self, request, model):
        self.chat_calls.append(request)
        return AIResponse(
            task=request.task, provider=self.kind, model=model,
            text=json.dumps(self.payload), usage=AIUsage(input_tokens=5, output_tokens=5),
        )

    async def structured_extract(self, request, model):
        return await self.chat(request, model)


@pytest.fixture
def stub_router(monkeypatch):
    r = AIRouter()
    provider = RecordingProvider()
    r.providers = {kind: provider for kind in r.providers}
    r._provider = provider  # type: ignore[attr-defined]

    # Keep telemetry inert.
    from app.ai import telemetry
    monkeypatch.setattr(telemetry, "record_call", lambda **kw: None)
    return r


def _png_bytes() -> bytes:
    # Minimal valid-ish PNG header bytes; providers are stubbed so content is unused.
    return b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.mark.asyncio
async def test_extract_features_routes_vision_with_images(stub_router):
    from app.ai.drawing_extractor import extract_features_from_image

    result = await extract_features_from_image(
        _png_bytes(), router=stub_router, drawing_type="detail"
    )
    assert result["features"], "features parsed from routed VLM response"
    assert len(stub_router._provider.vision_calls) == 1
    req = stub_router._provider.vision_calls[0]
    assert req.task == AITask.DRAWING_ANALYSIS_VLM
    assert req.images, "image must be passed in request.images"
    assert req.confidential is True


@pytest.mark.asyncio
async def test_extract_features_defaults_to_singleton_router(monkeypatch, stub_router):
    # router=None must use the shared ai_router (not the removed legacy path).
    import app.ai.router as router_mod
    monkeypatch.setattr(router_mod, "ai_router", stub_router)

    from app.ai.drawing_extractor import extract_features_from_image

    result = await extract_features_from_image(_png_bytes(), drawing_type="detail")
    assert result["features"]
    assert len(stub_router._provider.vision_calls) == 1


@pytest.mark.asyncio
async def test_text_fallback_uses_chat_not_vision(monkeypatch, stub_router):
    # extract_drawing_features is text-only → must dispatch chat, never vision.
    import app.ai.router as router_mod
    monkeypatch.setattr(router_mod, "ai_router", stub_router)

    from app.ai.drawing_extractor import extract_drawing_features

    result = await extract_drawing_features("Вал Ø20, длина 100, резьба M12")
    assert "features" in result
    assert len(stub_router._provider.chat_calls) == 1
    assert len(stub_router._provider.vision_calls) == 0


@pytest.mark.asyncio
async def test_assembly_bom_routes_with_images(monkeypatch, stub_router):
    monkeypatch.setattr(
        stub_router._provider,
        "payload",
        {"items": [{"position": 1, "name": "Корпус", "qty": 1}]},
        raising=False,
    )

    from app.ai.assembly_extractor import _extract_bom_via_vlm

    # The BOM parser may filter unknown shapes; we only assert routing here.
    await _extract_bom_via_vlm(_png_bytes(), router=stub_router, drawing=None, allow_cloud=False)
    assert len(stub_router._provider.vision_calls) == 1
    assert stub_router._provider.vision_calls[0].images


@pytest.mark.asyncio
async def test_multiimage_sequential_when_no_multi_support(stub_router, monkeypatch):
    # Force routing to a model without multi-image support → sequential per view.
    routing = tr.TaskRouting(
        task="drawing_analysis_vlm",
        models=["gemma4_e4b_ollama"],  # supports_multi_image is False in catalog
        profile="anti_hallucination",
        local_only=True,
    )
    monkeypatch.setattr(tr, "get_routing_for", lambda t, _r=routing: _r)

    from app.ai.drawing_extractor import extract_features_from_image

    await extract_features_from_image(
        [_png_bytes(), _png_bytes(), _png_bytes()],
        router=stub_router,
        view_labels=["front", "side", "top"],
    )
    # one vision call per view
    assert len(stub_router._provider.vision_calls) == 3
    for req in stub_router._provider.vision_calls:
        assert len(req.images) == 1
