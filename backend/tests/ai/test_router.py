from __future__ import annotations

from pydantic import BaseModel

from backend.app.ai.providers.base import AIProvider
from backend.app.ai.router import AIConfidentialityPolicyError, AIRouter
from backend.app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    ChatMessage,
    ProviderConfig,
    ProviderKind,
    ProposedToolCall,
)
from backend.app.ai.model_registry import ModelRegistry


class InvoiceMiniSchema(BaseModel):
    supplier: str
    total: float


class FakeProvider(AIProvider):
    kind = ProviderKind.OLLAMA

    def __init__(self) -> None:
        super().__init__(ProviderConfig(kind=ProviderKind.OLLAMA, base_url="http://fake.local"))

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text='{"supplier": "ACME", "total": 123.45}',
            proposed_tool_calls=[
                ProposedToolCall(name="allowed.skill", arguments={"id": 1}),
                ProposedToolCall(name="blocked.skill", arguments={"id": 2}),
            ],
        )

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            embedding=[0.1, 0.2],
        )


def test_structured_output_is_validated() -> None:
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter(registry, providers={ProviderKind.OLLAMA: FakeProvider()})

    import asyncio

    response = asyncio.run(
        router.run(
            AIRequest(
                task=AITask.STRUCTURED_EXTRACTION,
                messages=[ChatMessage(role="user", content="extract")],
                response_schema=InvoiceMiniSchema,
            )
        )
    )

    assert isinstance(response.data, InvoiceMiniSchema)
    assert response.data.total == 123.45


def test_structured_output_is_validated_for_vision_routes() -> None:
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter(registry, providers={ProviderKind.OLLAMA: FakeProvider()})

    import asyncio

    response = asyncio.run(
        router.run(
            AIRequest(
                task=AITask.INVOICE_OCR,
                images=["data:image/png;base64,ZmFrZQ=="],
                response_schema=InvoiceMiniSchema,
            )
        )
    )

    assert isinstance(response.data, InvoiceMiniSchema)
    assert response.data.supplier == "ACME"


def test_tool_calls_are_only_proposed_and_allowlisted() -> None:
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    registry.routes[AITask.TOOL_CALLING] = registry.routes[AITask.TOOL_CALLING].model_copy(
        update={"fallback_chain": ["gemma4_e4b_ollama"], "required_modalities": set()}
    )
    router = AIRouter(registry, providers={ProviderKind.OLLAMA: FakeProvider()})

    import asyncio

    response = asyncio.run(
        router.run(
            AIRequest(
                task=AITask.TOOL_CALLING,
                messages=[ChatMessage(role="user", content="call tool")],
                tools=[
                    {
                        "name": "allowed.skill",
                        "description": "Allowed skill",
                        "input_schema": {"type": "object"},
                    }
                ],
            )
        )
    )

    assert [call.name for call in response.proposed_tool_calls] == ["allowed.skill"]


def test_cloud_model_requires_explicit_permission() -> None:
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    model = registry.get_model("future_reasoning_cloud")
    router = AIRouter(registry, providers={ProviderKind.OLLAMA: FakeProvider()})

    try:
        router._enforce_policy(
            AIRequest(task=AITask.ENGINEERING_REASONING, confidential=False, allow_cloud=False),
            model,
        )
    except AIConfidentialityPolicyError:
        pass
    else:
        raise AssertionError("cloud model should require explicit permission")
