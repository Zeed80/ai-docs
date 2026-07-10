"""Typed runtime access to the broad capability contract."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class CapabilityDefinition(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    path: str = ""
    method: str = "POST"
    gate_actions: list[str] = Field(default_factory=list)
    # Actions excluded from self-learning recipes — a DIFFERENT axis from
    # gate_actions: not about needing human approval, but about reproducibility.
    # An action whose result is inherently non-deterministic (e.g. a diffusion
    # generation with a fresh random seed each run) must never be replayed by a
    # learned recipe as if it were the same result every time.
    non_recipeable_actions: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # Ф8.2: declared domain of applicability (materials/ranges/exclusions a
    # skill actually has data/rules for) — descriptive metadata surfaced to
    # the model in its tool description, not a mechanically enforced filter
    # (same status as the rest of `description`). Real enforcement of a
    # competence boundary happens in the skill's own code (see
    # tp_generator.material_group_with_confidence for a worked example) and
    # is what actually produces an honest refusal — this field documents
    # that boundary so it's visible without reading the implementation.
    domain: dict[str, Any] | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()

    @field_validator("gate_actions", "non_recipeable_actions")
    @classmethod
    def unique_gate_actions(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(action).strip() for action in value if str(action).strip()))


class CapabilityManifest(BaseModel):
    version: int = 1
    mode: str = "capabilities"
    capabilities: list[CapabilityDefinition] = Field(default_factory=list)

    @field_validator("capabilities")
    @classmethod
    def unique_capability_names(
        cls, value: list[CapabilityDefinition]
    ) -> list[CapabilityDefinition]:
        names = [capability.name for capability in value]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate capability names: {duplicates}")
        return value

    @property
    def by_name(self) -> dict[str, CapabilityDefinition]:
        return {capability.name: capability for capability in self.capabilities}

    @property
    def gate_actions(self) -> dict[str, set[str]]:
        return {
            capability.name: set(capability.gate_actions)
            for capability in self.capabilities
        }

    @property
    def non_recipeable_actions(self) -> dict[str, set[str]]:
        return {
            capability.name: set(capability.non_recipeable_actions)
            for capability in self.capabilities
        }

    def is_gated(self, capability: str, action: str | None) -> bool:
        return bool(action and action in self.gate_actions.get(capability, set()))


_cache: dict[Path, tuple[float, CapabilityManifest]] = {}


def load_capability_manifest(path: Path | None = None) -> CapabilityManifest:
    """Load and validate capabilities.yml with mtime-based caching."""
    if path is None:
        from app.ai.gateway_config import gateway_config

        path = gateway_config.capabilities_path
    resolved = path.resolve()
    mtime = resolved.stat().st_mtime
    cached = _cache.get(resolved)
    if cached and cached[0] == mtime:
        return cached[1]

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    manifest = CapabilityManifest.model_validate(raw)
    _cache[resolved] = (mtime, manifest)
    return manifest


def capability_schema_hash(path: Path | None = None) -> str:
    """Stable hash used to invalidate learned recipes after contract changes."""
    if path is None:
        from app.ai.gateway_config import gateway_config

        path = gateway_config.capabilities_path
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_capability_manifest_cache() -> None:
    _cache.clear()
