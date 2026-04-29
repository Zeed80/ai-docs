from __future__ import annotations

from app.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.ai.schemas import ProviderKind


class VLLMProvider(OpenAICompatibleProvider):
    kind = ProviderKind.VLLM
