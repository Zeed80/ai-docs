"""Runtime configuration for the built-in agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.gateway_config import gateway_config

_CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / "agent_config.json"


class BuiltinAgentConfig(BaseModel):
    enabled: bool = True
    agent_name: str = "Света"
    model: str = "qwen3.5:9b"
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


class BuiltinAgentConfigUpdate(BaseModel):
    enabled: bool | None = None
    agent_name: str | None = None
    model: str | None = None
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


def get_builtin_agent_config() -> BuiltinAgentConfig:
    """Load config from disk and merge it with gateway-compatible defaults."""
    defaults = _default_config().model_dump()
    if not _CONFIG_FILE.exists():
        return BuiltinAgentConfig(**defaults)
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return BuiltinAgentConfig(**defaults)
    return BuiltinAgentConfig(**{**defaults, **data})


def save_builtin_agent_config(config: BuiltinAgentConfig) -> BuiltinAgentConfig:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
