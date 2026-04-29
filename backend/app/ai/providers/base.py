from __future__ import annotations

from abc import ABC, abstractmethod

from app.ai.schemas import AIRequest, AIResponse, ProviderConfig, ProviderKind


class AIProvider(ABC):
    kind: ProviderKind

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError

    @abstractmethod
    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError

    @abstractmethod
    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError

    async def structured_extract(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)

    async def rerank(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError(f"{self.kind} does not implement rerank")

    async def speech(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError(f"{self.kind} does not implement speech")

    async def tool_calling(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)
