"""Versioned, fail-closed catalog of models supported by Strata.

The catalog contains data only: runtime behavior is selected through local
enums, never through shell arguments or paths supplied by an upstream source.
The helpers at the bottom deliberately retain the shape of the legacy
``FAMILIES``/``QUANTS`` mappings so lifecycle code can migrate incrementally.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai.schemas import Modality, ModelCapability, ModelStatus, ProviderKind

BUNDLED_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "strata_official_catalog.v1.json"
)

MAX_MODEL_SIZE_GB = 1_000_000.0
MAX_CONTEXT_TOKENS = 10_000_000

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_SERVED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SAFE_TAG = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z.-]+)?$")
_CAPABILITY_FIELDS = set(ModelCapability.model_fields) - {"name", "provider", "provider_model"}

FamilyKind = Literal["pack", "budget", "own", "unknown"]
RuntimeProfile = Literal["flash_next_pack", "unsloth_budget", "qwen36_own", "unknown"]
SetupPolicy = Literal["default", "disable_experimental_speed_projection", "unknown"]
DiskLayout = Literal["strata_standard", "unknown"]
CatalogOrigin = Literal["official", "retired", "bundled"]


class CatalogValidationError(ValueError):
    """An update would change stable identity or violate catalog invariants."""


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_safe_id(value: str, *, served_name: bool = False, tag: bool = False) -> str:
    pattern = _SAFE_TAG if tag else _SAFE_SERVED_NAME if served_name else _SAFE_ID
    if not pattern.fullmatch(value) or ".." in value:
        raise ValueError(f"unsafe catalog identifier: {value!r}")
    return value


def _validate_version(value: str | None) -> str | None:
    if value is not None and not _VERSION.fullmatch(value):
        raise ValueError(f"invalid Strata version requirement: {value!r}")
    return value


class StrataFamily(_CatalogModel):
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    about: str = Field(max_length=4000)
    tag: str = Field(max_length=160)
    kind: FamilyKind
    catalog_key: str = Field(min_length=1, max_length=200)
    served_name: str = Field(min_length=1, max_length=240)
    runtime_profile: RuntimeProfile
    setup_policy: SetupPolicy
    disk_layout: DiskLayout
    min_version: str | None = Field(default=None, max_length=80)
    upstream_present: bool
    origin: CatalogOrigin
    capability: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "catalog_key")
    @classmethod
    def _safe_ids(cls, value: str) -> str:
        return _validate_safe_id(value)

    @field_validator("served_name")
    @classmethod
    def _safe_served_name(cls, value: str) -> str:
        return _validate_safe_id(value, served_name=True)

    @field_validator("tag")
    @classmethod
    def _safe_tag(cls, value: str) -> str:
        return _validate_safe_id(value, tag=True)

    @field_validator("min_version")
    @classmethod
    def _valid_version(cls, value: str | None) -> str | None:
        return _validate_version(value)

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - _CAPABILITY_FIELDS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown ModelCapability fields: {names}")
        # Validate curated data at the catalog boundary. ModelCapability itself
        # ignores extras for historical reasons, hence the explicit set check.
        ModelCapability(
            name="catalog-validation",
            provider=ProviderKind.STRATA,
            provider_model="catalog-validation",
            **value,
        )
        return value


class StrataVariant(_CatalogModel):
    id: str = Field(min_length=1, max_length=160)
    setup_id: str = Field(min_length=1, max_length=160)
    family: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=500)
    download_gb: float = Field(gt=0, le=MAX_MODEL_SIZE_GB, allow_inf_nan=False)
    experts_gb: float = Field(gt=0, le=MAX_MODEL_SIZE_GB, allow_inf_nan=False)
    ram_gb: float = Field(gt=0, le=MAX_MODEL_SIZE_GB, allow_inf_nan=False)
    default_context: int = Field(default=65536, gt=0, le=MAX_CONTEXT_TOKENS)
    recommended_context: int | None = Field(default=None, gt=0, le=MAX_CONTEXT_TOKENS)
    min_version: str | None = Field(default=None, max_length=80)
    upstream_present: bool
    origin: CatalogOrigin

    @field_validator("id", "setup_id", "family", "model")
    @classmethod
    def _safe_ids(cls, value: str) -> str:
        return _validate_safe_id(value)

    @field_validator("min_version")
    @classmethod
    def _valid_version(cls, value: str | None) -> str | None:
        return _validate_version(value)


class StrataCatalogV1(_CatalogModel):
    schema_version: Literal[1]
    source: str = Field(min_length=1, max_length=200)
    source_revision: str = Field(min_length=1, max_length=500)
    fetched_at: datetime
    feed_revision: str | None = Field(default=None, max_length=500)
    families: list[StrataFamily]
    variants: list[StrataVariant]

    @field_validator("fetched_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _unique_and_referential_identities(self) -> StrataCatalogV1:
        family_ids = [family.id for family in self.families]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("duplicate Strata family id")

        served_by_key: dict[str, str] = {}
        for family in self.families:
            previous = served_by_key.setdefault(family.catalog_key, family.served_name)
            if previous != family.served_name:
                raise ValueError(
                    f"catalog_key {family.catalog_key!r} is shared by different served names"
                )

        variant_ids = [variant.id for variant in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("duplicate Strata variant id")

        family_model_pairs = [(variant.family, variant.model) for variant in self.variants]
        if len(family_model_pairs) != len(set(family_model_pairs)):
            raise ValueError("duplicate Strata family/model pair")

        known_families = set(family_ids)
        missing = sorted({variant.family for variant in self.variants} - known_families)
        if missing:
            raise ValueError(f"variants reference unknown families: {', '.join(missing)}")
        return self


CatalogInput = StrataCatalogV1 | Mapping[str, Any]


def _as_catalog(value: CatalogInput) -> StrataCatalogV1:
    if isinstance(value, StrataCatalogV1):
        return value
    return StrataCatalogV1.model_validate(dict(value))


def load_bundled_catalog() -> StrataCatalogV1:
    """Load and fully validate the offline snapshot shipped with the backend."""
    with BUNDLED_CATALOG_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return StrataCatalogV1.model_validate(payload)


def family_map(catalog: CatalogInput) -> dict[str, dict[str, Any]]:
    """Return the legacy ``FAMILIES`` mapping plus non-breaking catalog metadata."""
    return {
        family.id: family.model_dump(exclude={"capability"}, exclude_none=True)
        for family in _as_catalog(catalog).families
    }


def variant_map(catalog: CatalogInput) -> dict[str, dict[str, Any]]:
    """Return variants keyed by stable id, including legacy ``family``/``model``."""
    return {
        variant.id: variant.model_dump(exclude_none=True)
        for variant in _as_catalog(catalog).variants
    }


def catalog_capability(family: StrataFamily) -> ModelCapability:
    """Translate curated facts, or conservatively mark a new family as unknown."""
    common = {
        "name": family.catalog_key,
        "provider": ProviderKind.STRATA,
        "provider_model": family.served_name,
    }
    if not family.capability:
        return ModelCapability(
            **common,
            status=ModelStatus.CANDIDATE,
            modalities={Modality.TEXT},
            capability_source="discovered",
            capabilities_unknown=True,
            local_only=True,
        )
    return ModelCapability(**common, **family.capability)


_FAMILY_IDENTITY = (
    "id",
    "catalog_key",
    "served_name",
    "runtime_profile",
    "setup_policy",
    "disk_layout",
)
_VARIANT_IDENTITY = ("id", "setup_id", "family", "model")


def _check_identity(previous: Any, incoming: Any, fields: tuple[str, ...], kind: str) -> None:
    for field in fields:
        old_value = getattr(previous, field)
        new_value = getattr(incoming, field)
        if old_value != new_value:
            raise CatalogValidationError(
                f"immutable {kind} identity changed for {previous.id!r}: "
                f"{field} {old_value!r} -> {new_value!r}"
            )


def _active(entry: Any) -> Any:
    data = entry.model_dump()
    data["upstream_present"] = True
    if data["origin"] == "retired":
        data["origin"] = "official"
    return type(entry).model_validate(data)


def _retired(entry: Any) -> Any:
    return type(entry).model_validate(
        {**entry.model_dump(), "upstream_present": False, "origin": "retired"}
    )


def _merge_entries(
    previous: list[Any], incoming: list[Any], identity: tuple[str, ...], kind: str
) -> tuple[list[Any], int, int, int]:
    old_by_id = {entry.id: entry for entry in previous}
    new_by_id = {entry.id: entry for entry in incoming}
    merged: list[Any] = []
    added = updated = retired = 0

    for old in previous:
        new = new_by_id.get(old.id)
        if new is None:
            replacement = _retired(old) if old.upstream_present else old.model_copy(deep=True)
            if replacement.model_dump(mode="json") != old.model_dump(mode="json"):
                retired += 1
        else:
            _check_identity(old, new, identity, kind)
            replacement = _active(new)
            if replacement.model_dump(mode="json") != old.model_dump(mode="json"):
                updated += 1
        merged.append(replacement)

    for new in incoming:
        if new.id not in old_by_id:
            merged.append(_active(new))
            added += 1

    return merged, added, updated, retired


def merge_catalogs(
    previous: CatalogInput, incoming: CatalogInput
) -> tuple[StrataCatalogV1, dict[str, int | bool]]:
    """Merge an upstream snapshot without deleting history or changing identity.

    ``added``/``updated``/``retired`` count families and variants together.
    ``changed`` also covers catalog-level provenance, so a source revision
    change is observable even when no model entry changed.
    """
    old = _as_catalog(previous)
    new = _as_catalog(incoming)
    families, families_added, families_updated, families_retired = _merge_entries(
        old.families, new.families, _FAMILY_IDENTITY, "family"
    )
    variants, variants_added, variants_updated, variants_retired = _merge_entries(
        old.variants, new.variants, _VARIANT_IDENTITY, "variant"
    )
    payload = new.model_dump()
    payload["families"] = [entry.model_dump() for entry in families]
    payload["variants"] = [entry.model_dump() for entry in variants]
    merged = StrataCatalogV1.model_validate(payload)
    counts: dict[str, int | bool] = {
        "added": families_added + variants_added,
        "updated": families_updated + variants_updated,
        "retired": families_retired + variants_retired,
        "changed": merged.model_dump(mode="json") != old.model_dump(mode="json"),
    }
    return merged, counts


__all__ = [
    "BUNDLED_CATALOG_PATH",
    "CatalogValidationError",
    "StrataCatalogV1",
    "StrataFamily",
    "StrataVariant",
    "catalog_capability",
    "family_map",
    "load_bundled_catalog",
    "merge_catalogs",
    "variant_map",
]
