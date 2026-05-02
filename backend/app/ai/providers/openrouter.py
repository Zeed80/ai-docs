"""OpenRouter provider — OpenAI-compatible gateway with 200+ models."""

from __future__ import annotations

import os

from app.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.ai.schemas import ProviderConfig, ProviderKind

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAICompatibleProvider):
    """Thin wrapper over OpenAICompatibleProvider for OpenRouter.

    Adds the required HTTP-Referer / X-Title headers and reads the API key
    from OPENROUTER_API_KEY env var.
    """

    kind = ProviderKind.OPENROUTER

    @classmethod
    def from_env(cls) -> "OpenRouterProvider":
        config = ProviderConfig(
            kind=ProviderKind.OPENROUTER,
            base_url=_OPENROUTER_BASE,
            api_key_env="OPENROUTER_API_KEY",
            timeout_seconds=180.0,
            is_local=False,
            extra_headers={
                "HTTP-Referer": "https://ai-workspace.local",
                "X-Title": "AI Manufacturing Workspace",
            },
        )
        return cls(config)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # Override api_key reading since we always use OPENROUTER_API_KEY
        key = os.getenv("OPENROUTER_API_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.setdefault("HTTP-Referer", "https://ai-workspace.local")
        headers.setdefault("X-Title", "AI Manufacturing Workspace")
        return headers
