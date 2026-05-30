"""Verify that the unified task_routing store actually drives AIRouter.run.

These run offline (no live providers): a stub provider replaces the real ones
and the routing store is monkeypatched. They prove the contract that PR1/PR2
rely on — the model AIRouter picks, the fallback order, the confidentiality
gate, and telemetry recording all follow task_routing.

Live, real-invoice coverage lives in test_real_invoice_pipeline.py (marked
@pytest.mark.live) and additionally exercises this same routing path.
"""

from __future__ import annotations

import pytest

from app.ai import task_routing as tr
from app.ai import telemetry
from app.ai.router import AIConfidentialityPolicyError, AIRouter
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    AIUsage,
    ChatMessage,
    ProviderKind,
)


class StubProvider:
    """Records calls; fails for a configurable set of provider_model names."""

    def __init__(self, kind: ProviderKind, fail_models: set[str] | None = None):
        self.kind = kind
        self.fail_models = fail_models or set()
        self.calls: list[str] = []

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        self.calls.append(model)
        if model in self.fail_models:
            raise RuntimeError(f"stub failure for {model}")
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text="ok",
            usage=AIUsage(input_tokens=3, output_tokens=2),
        )

    # other modalities not exercised by CLASSIFICATION
    async def vision(self, request, model):  # pragma: no cover
        return await self.chat(request, model)


@pytest.fixture
def router(monkeypatch):
    """A real AIRouter with stub providers and a captured telemetry sink."""
    r = AIRouter()
    stub = StubProvider(ProviderKind.OLLAMA)
    r.providers = {kind: stub for kind in r.providers}
    r._stub = stub  # type: ignore[attr-defined]

    recorded: list[dict] = []
    monkeypatch.setattr(
        telemetry, "record_call", lambda **kw: recorded.append(kw)
    )
    r._recorded = recorded  # type: ignore[attr-defined]
    return r


def _route(monkeypatch, task: AITask, models, *, local_only=True, allow_cloud=False, profile="balanced"):
    routing = tr.TaskRouting(
        task=task.value, models=models, profile=profile,
        local_only=local_only, allow_cloud=allow_cloud,
    )
    monkeypatch.setattr(tr, "get_routing_for", lambda t, _r=routing: _r)


def _req(task=AITask.CLASSIFICATION):
    return AIRequest(
        task=task,
        messages=[ChatMessage(role="user", content="hi")],
        confidential=True,
    )


@pytest.mark.asyncio
async def test_router_uses_configured_primary(router, monkeypatch):
    _route(monkeypatch, AITask.CLASSIFICATION, ["gemma4_26b_ollama"])
    resp = await router.run(_req())
    assert resp.model == "gemma4:26b"  # provider_model of gemma4_26b_ollama
    assert router._stub.calls == ["gemma4:26b"]


@pytest.mark.asyncio
async def test_router_falls_back_on_failure(router, monkeypatch):
    router._stub.fail_models = {"gemma4:e4b"}  # primary fails
    _route(
        monkeypatch,
        AITask.CLASSIFICATION,
        ["gemma4_e4b_ollama", "gemma4_26b_ollama"],
    )
    resp = await router.run(_req())
    assert resp.model == "gemma4:26b"
    assert router._stub.calls == ["gemma4:e4b", "gemma4:26b"]


@pytest.mark.asyncio
async def test_confidential_task_blocks_cloud_model(router, monkeypatch):
    # Route a confidential task to a cloud model — policy must reject it.
    _route(
        monkeypatch,
        AITask.CLASSIFICATION,
        ["claude_sonnet_anthropic"],
        local_only=True,
        allow_cloud=False,
    )
    with pytest.raises(AIConfidentialityPolicyError):
        await router.run(_req())


@pytest.mark.asyncio
async def test_telemetry_recorded_on_success(router, monkeypatch):
    _route(monkeypatch, AITask.CLASSIFICATION, ["gemma4_26b_ollama"])
    await router.run(_req())
    assert len(router._recorded) == 1
    rec = router._recorded[0]
    assert rec["task"] == "classification"
    assert rec["model"] == "gemma4_26b_ollama"
    assert rec["ok"] is True
    assert rec["output_tokens"] == 2


@pytest.mark.asyncio
async def test_telemetry_records_failure_then_success(router, monkeypatch):
    router._stub.fail_models = {"gemma4:e4b"}
    _route(
        monkeypatch,
        AITask.CLASSIFICATION,
        ["gemma4_e4b_ollama", "gemma4_26b_ollama"],
    )
    await router.run(_req())
    assert [r["ok"] for r in router._recorded] == [False, True]


# ---------------------------------------------------------------------------
# Live: real invoice through the unified ai_router.run() path
# ---------------------------------------------------------------------------

import base64
import os
import re
from pathlib import Path

INVOICES_DIR = Path(
    os.environ.get(
        "INVOICES_DIR", "/home/project/document-invoices-ai_codex/example-invoices"
    )
)
INVOICE_JPG = INVOICES_DIR / "Графит-Гарант № 276 от 23.09.2024.jpg"


def _ollama_up() -> bool:
    import httpx

    url = os.environ.get("OLLAMA_URL", "http://host-gateway:11434")
    try:
        return httpx.get(f"{url.rstrip('/')}/api/tags", timeout=3.0).status_code == 200
    except Exception:
        return False


@pytest.mark.live
@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_invoice_through_router_default_routing():
    """A real invoice JPG, routed by task_routing (default), via ai_router.run.

    Proves the unified path: AIRouter resolves INVOICE_OCR to a local vision
    model from the routing store and extracts correct fields. Skips when Ollama
    is unavailable or the example file is missing.
    """
    if not _ollama_up():
        pytest.skip("Ollama not reachable")
    if not INVOICE_JPG.exists():
        pytest.skip(f"Invoice not found: {INVOICE_JPG}")

    from app.ai.router import ai_router
    from app.ai.task_routing import get_routing_for

    # The OCR route must be local-only (confidential documents).
    routing = get_routing_for(AITask.INVOICE_OCR)
    assert routing.local_only is True

    img_b64 = base64.b64encode(INVOICE_JPG.read_bytes()).decode()
    resp = await ai_router.run(
        AIRequest(
            task=AITask.INVOICE_OCR,
            messages=[
                ChatMessage(
                    role="system",
                    content="Извлеки данные счёта. Ответь строго JSON.",
                ),
                ChatMessage(
                    role="user",
                    content='{"invoice_number":"","date":"","vendor":"","total":0}',
                ),
            ],
            images=[img_b64],
            confidential=True,
        )
    )

    assert resp.provider.value in ("ollama", "llamacpp", "vllm"), "must stay local"
    assert resp.text
    match = re.search(r"\{.*\}", resp.text, re.DOTALL)
    assert match, f"no JSON in response: {resp.text[:200]}"
    import json

    data = json.loads(match.group(0))
    assert data.get("invoice_number") == "276", f"wrong number: {data}"
    assert "Графит" in str(data.get("vendor", "")), f"wrong vendor: {data}"
