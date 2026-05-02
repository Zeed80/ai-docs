"""Runtime configuration for the built-in agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.gateway_config import gateway_config

_CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / "agent_config.json"
_REDIS_KEY = "agent_config"


class BuiltinAgentConfig(BaseModel):
    enabled: bool = True
    agent_name: str = "Света"
    model: str = "qwen3.5:9b"
    # LLM provider: "ollama" | "openrouter" | "anthropic" | "deepseek" | "openai_compatible"
    provider: str = "ollama"
    # Ordered fallback chain tried when primary provider fails
    fallback_providers: list[str] = Field(default_factory=list)
    # Inject Anthropic prompt-cache headers (only effective with provider="anthropic")
    prompt_cache_enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    backend_url: str = "http://localhost:8000"
    temperature: float = Field(0.1, ge=0.0, le=2.0)
    max_steps: int = Field(10, ge=1, le=30)
    llm_timeout_seconds: int = Field(180, ge=10, le=1800)
    backend_timeout_seconds: int = Field(30, ge=5, le=300)
    approval_timeout_seconds: int = Field(120, ge=10, le=1800)
    memory_enabled: bool = True
    memory_mode: Literal["sql", "sql_vector", "sql_vector_rerank", "graph", "hybrid"] = "sql"
    memory_top_k: int = Field(8, ge=1, le=30)
    memory_max_chars: int = Field(6000, ge=1000, le=30000)
    max_history_messages: int = Field(40, ge=4, le=200)
    exposed_skills: list[str] = Field(default_factory=list)
    approval_gates: list[str] = Field(default_factory=list)
    system_prompt: str | None = None
    context_compression_enabled: bool = True
    context_compression_threshold: float = Field(0.85, ge=0.5, le=0.98)
    compression_model: str | None = None  # None = use primary model


class BuiltinAgentConfigUpdate(BaseModel):
    enabled: bool | None = None
    agent_name: str | None = None
    model: str | None = None
    provider: str | None = None
    fallback_providers: list[str] | None = None
    prompt_cache_enabled: bool | None = None
    ollama_url: str | None = None
    backend_url: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_steps: int | None = Field(default=None, ge=1, le=30)
    llm_timeout_seconds: int | None = Field(default=None, ge=10, le=1800)
    backend_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    approval_timeout_seconds: int | None = Field(default=None, ge=10, le=1800)
    memory_enabled: bool | None = None
    memory_mode: Literal["sql", "sql_vector", "sql_vector_rerank", "graph", "hybrid"] | None = None
    memory_top_k: int | None = Field(default=None, ge=1, le=30)
    memory_max_chars: int | None = Field(default=None, ge=1000, le=30000)
    max_history_messages: int | None = Field(default=None, ge=4, le=200)
    exposed_skills: list[str] | None = None
    approval_gates: list[str] | None = None
    system_prompt: str | None = None
    context_compression_enabled: bool | None = None
    context_compression_threshold: float | None = Field(default=None, ge=0.5, le=0.98)
    compression_model: str | None = None


def _default_config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        agent_name=gateway_config.agent_name,
        model=gateway_config.reasoning_model,
        ollama_url=gateway_config.reasoning_base_url,
        backend_url=gateway_config.backend_url,
        backend_timeout_seconds=gateway_config.backend_timeout,
        exposed_skills=sorted(gateway_config.exposed_skills),
        approval_gates=sorted(gateway_config.approval_gates),
    )


def _redis_get_agent_config() -> dict | None:
    try:
        import redis as _redis
        from app.config import settings
        r = _redis.from_url(settings.redis_url, decode_responses=True)
        raw = r.get(_REDIS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def _redis_set_agent_config(data: dict) -> None:
    try:
        import redis as _redis
        from app.config import settings
        r = _redis.from_url(settings.redis_url, decode_responses=True)
        r.set(_REDIS_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _env_overrides() -> dict:
    """Values that MUST come from environment, never from saved file."""
    overrides: dict = {}
    ollama_url = os.environ.get("OLLAMA_URL")
    if ollama_url:
        overrides["ollama_url"] = ollama_url.rstrip("/")
    fastapi_url = os.environ.get("FASTAPI_URL")
    if fastapi_url:
        overrides["backend_url"] = fastapi_url.rstrip("/")
    # Load exposed_skills and approval_gates from gateway.yml if not set
    return overrides


def get_builtin_agent_config() -> BuiltinAgentConfig:
    """Load config from Redis → local file → defaults. Env vars always win for URLs."""
    defaults = _default_config().model_dump()

    # Load saved overrides (Redis first, then file)
    saved: dict = {}
    redis_data = _redis_get_agent_config()
    if redis_data:
        saved = redis_data
    elif _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            # Migrate to Redis
            _redis_set_agent_config(saved)
        except Exception:
            pass

    merged = {**defaults, **saved}

    # If exposed_skills is empty, reload from gateway.yml
    if not merged.get("exposed_skills"):
        merged["exposed_skills"] = sorted(gateway_config.exposed_skills)
    if not merged.get("approval_gates"):
        merged["approval_gates"] = sorted(gateway_config.approval_gates)

    # Environment always wins for connection URLs
    merged.update(_env_overrides())

    return BuiltinAgentConfig(**merged)


def save_builtin_agent_config(config: BuiltinAgentConfig) -> BuiltinAgentConfig:
    data = config.model_dump()
    _redis_set_agent_config(data)
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def update_builtin_agent_config(
    patch: BuiltinAgentConfigUpdate,
) -> BuiltinAgentConfig:
    current = get_builtin_agent_config()
    updates = patch.model_dump(exclude_unset=True)
    return save_builtin_agent_config(current.model_copy(update=updates))


def reset_builtin_agent_config() -> BuiltinAgentConfig:
    config = _default_config()
    save_builtin_agent_config(config)
    return config
