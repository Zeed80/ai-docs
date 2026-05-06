"""Runtime configuration for the built-in agent."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.ai.gateway_config import gateway_config

_CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / "agent_config.json"
_REDIS_KEY = "agent_config"


class BuiltinAgentConfig(BaseModel):
    enabled: bool = True
    agent_name: str = "Света"
    model: str = "qwen3.5:9b"
    department_enabled: bool = True
    orchestrator_model: str | None = None
    orchestrator_provider: str | None = None
    orchestrator_disable_thinking: bool = False
    worker_model: str | None = None
    worker_provider: str | None = None
    worker_disable_thinking: bool = False
    auditor_model: str | None = None
    auditor_provider: str | None = None
    auditor_disable_thinking: bool = False
    builder_model: str | None = None
    builder_provider: str | None = None
    builder_disable_thinking: bool = False
    fast_model: str | None = None
    fast_provider: str | None = None
    fast_disable_thinking: bool = False
    # LLM provider: ollama, vllm, lmstudio, openai-compatible or supported cloud provider.
    provider: str = "ollama"
    # Ordered fallback chain tried when primary provider fails
    fallback_providers: list[str] = Field(default_factory=list)
    # Inject Anthropic prompt-cache headers (only effective with provider="anthropic")
    prompt_cache_enabled: bool = False
    disable_thinking: bool = False
    ollama_url: str = "http://localhost:11434"
    vllm_url: str = "http://localhost:8001/v1"
    lmstudio_url: str = "http://localhost:1234/v1"
    openai_compatible_url: str = "http://localhost:8001/v1"
    backend_url: str = "http://localhost:8000"
    temperature: float = Field(0.1, ge=0.0, le=2.0)
    max_steps: int = Field(10, ge=1, le=30)
    llm_timeout_seconds: int = Field(180, ge=10, le=1800)
    backend_timeout_seconds: int = Field(30, ge=5, le=300)
    approval_timeout_seconds: int = Field(120, ge=10, le=1800)
    max_worker_steps: int = Field(12, ge=1, le=60)
    max_audit_retries: int = Field(1, ge=0, le=5)
    memory_enabled: bool = True
    audit_enabled: bool = True
    allow_capability_builder: bool = True
    capability_builder_requires_approval: bool = True
    max_history_messages: int = Field(40, ge=4, le=200)
    exposed_skills: list[str] = Field(default_factory=list)
    approval_gates: list[str] = Field(default_factory=list)
    system_prompt: str | None = None
    context_compression_enabled: bool = True
    context_compression_threshold: float = Field(0.85, ge=0.5, le=0.98)
    compression_model: str | None = None  # None = use primary model
    mcp_servers: list[dict] = Field(default_factory=list)  # [{name, transport, ...}]


class BuiltinAgentConfigUpdate(BaseModel):
    enabled: bool | None = None
    agent_name: str | None = None
    model: str | None = None
    department_enabled: bool | None = None
    orchestrator_model: str | None = None
    orchestrator_provider: str | None = None
    orchestrator_disable_thinking: bool | None = None
    worker_model: str | None = None
    worker_provider: str | None = None
    worker_disable_thinking: bool | None = None
    auditor_model: str | None = None
    auditor_provider: str | None = None
    auditor_disable_thinking: bool | None = None
    builder_model: str | None = None
    builder_provider: str | None = None
    builder_disable_thinking: bool | None = None
    fast_model: str | None = None
    fast_provider: str | None = None
    fast_disable_thinking: bool | None = None
    provider: str | None = None
    fallback_providers: list[str] | None = None
    prompt_cache_enabled: bool | None = None
    disable_thinking: bool | None = None
    ollama_url: str | None = None
    vllm_url: str | None = None
    lmstudio_url: str | None = None
    openai_compatible_url: str | None = None
    backend_url: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_steps: int | None = Field(default=None, ge=1, le=30)
    llm_timeout_seconds: int | None = Field(default=None, ge=10, le=1800)
    backend_timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    approval_timeout_seconds: int | None = Field(default=None, ge=10, le=1800)
    max_worker_steps: int | None = Field(default=None, ge=1, le=60)
    max_audit_retries: int | None = Field(default=None, ge=0, le=5)
    memory_enabled: bool | None = None
    audit_enabled: bool | None = None
    allow_capability_builder: bool | None = None
    capability_builder_requires_approval: bool | None = None
    max_history_messages: int | None = Field(default=None, ge=4, le=200)
    exposed_skills: list[str] | None = None
    approval_gates: list[str] | None = None
    system_prompt: str | None = None
    context_compression_enabled: bool | None = None
    context_compression_threshold: float | None = Field(default=None, ge=0.5, le=0.98)
    compression_model: str | None = None
    mcp_servers: list[dict] | None = None


def _all_registry_skill_names() -> list[str]:
    """Return all skill names from YAML registry."""
    try:
        registry_path = gateway_config.registry_path
        if not registry_path.exists():
            return []
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        skills = data.get("skills") or data.get("tools") or []
        names = sorted({
            str(skill.get("name", "")).strip()
            for skill in skills
            if str(skill.get("name", "")).strip()
        })
        return names
    except Exception:
        return []


def _default_config() -> BuiltinAgentConfig:
    registry_skills = _all_registry_skill_names()
    default_skills = registry_skills or sorted(gateway_config.exposed_skills)
    return BuiltinAgentConfig(
        agent_name=gateway_config.agent_name,
        model=gateway_config.reasoning_model,
        ollama_url=gateway_config.reasoning_base_url,
        backend_url=gateway_config.backend_url,
        backend_timeout_seconds=gateway_config.backend_timeout,
        exposed_skills=default_skills,
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
    registry_skills = _all_registry_skill_names()

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

    # If exposed_skills is empty, fallback to full registry.
    if not merged.get("exposed_skills"):
        merged["exposed_skills"] = registry_skills or sorted(gateway_config.exposed_skills)
    elif registry_skills:
        # Keep runtime configs forward-compatible with newly generated skills.
        merged["exposed_skills"] = sorted(set(merged["exposed_skills"]) | set(registry_skills))
    if not merged.get("approval_gates"):
        merged["approval_gates"] = sorted(gateway_config.approval_gates)
    else:
        # Approval gates are safety invariants; never let an older saved config
        # silently drop gates required by the current gateway/registry contract.
        merged["approval_gates"] = sorted(
            set(merged["approval_gates"]) | set(gateway_config.approval_gates)
        )

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
    gateway_config.reload()
    config = _default_config()
    save_builtin_agent_config(config)
    return config
