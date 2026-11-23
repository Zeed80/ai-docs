"""Изолированные контракты versioned-каталога Strata: без сети, Redis и БД."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.ai.providers.strata_catalog import (
    MAX_CONTEXT_TOKENS,
    MAX_MODEL_SIZE_GB,
    CatalogValidationError,
    StrataCatalogV1,
    StrataFamily,
    StrataVariant,
    catalog_capability,
    family_map,
    load_bundled_catalog,
    merge_catalogs,
    merge_curated_bundled,
    variant_map,
)
from app.ai.schemas import Modality, ModelStatus, ProviderKind

VARIANT_IDS = {
    "Q2_0",
    "IQ2_XS",
    "IQ3_XXS",
    "IQ3_S",
    "swift-IQ2_XS",
    "swift-IQ3_XXS",
    "swift-IQ3_S",
    "coder-IQ1_M",
    "unsloth-UD-IQ4_XS",
    "unsloth-UD-Q4_K_XL",
    "unsloth-UD-IQ1_M",
    "unsloth-UD-Q2_0",
    "unsloth-UD-IQ2_XS",
    "unsloth-UD-Q2_K_XL",
    "unsloth-UD-IQ3_XXS",
    "unsloth-UD-Q3_K_XL",
    "unsloth-UD-IQ4_XS-v31",
    "unsloth-UD-Q5_K_XL",
    "unsloth-UD-Q6_K_XL",
    "unsloth-Q8_0",
    "qwen36-UD-Q4_K_XL",
    "qwen36-UD-Q6_K_XL",
    "qwen36-Q8_0",
    "gyro-Gyro-S",
    "gyro-Gyro-M",
    "nvfp4-NVFP4",
    "nvfp4-NVFP4-NVIDIA",
    "nvfp4-NVFP4-QAD",
    "hy4-flash-SQ-64",
    "hy4-flash-SQ-96",
    "hy4-flash-SQ-128",
    *{
        f"inkling-2-mini-{model}"
        for model in (
            "SQ-20",
            "SQ-15",
            "SQ-10",
            "UD-Q8_K_XL",
            "UD-Q4_K_XL",
            "UD-Q4_K_M",
            "UD-IQ4_XS",
            "UD-Q3_K_XL",
            "UD-IQ3_XXS",
            "UD-Q2_K_XL",
            "UD-IQ2_XXS",
            "UD-IQ1_M",
            "UD-IQ1_S",
        )
    },
    *{
        f"deepseek-v4.1-flash-{model}"
        for model in (
            "IST-DAS-1.75bpw",
            "IST-DAS-2.00bpw",
            "IST-DAS-2.25bpw",
            "UD-IQ1_S",
            "UD-IQ1_M",
            "UD-Q8_K_XL",
        )
    },
}

FAMILY_IDS = {
    "qwen",
    "swift",
    "coder",
    "unsloth",
    "qwen36",
    "gyro",
    "nvfp4",
    "hy4-flash",
    "inkling-2-mini",
    "deepseek41",
}

EXPECTED_SIZES = {
    "Q2_0": ("qwen", "Q2_0", "Q2_0 — самая быстрая", 66.4, 34.0, 48),
    "IQ2_XS": ("qwen", "IQ2_XS", "IQ2_XS — рекомендуемая", 68.0, 35.5, 48),
    "IQ3_XXS": ("qwen", "IQ3_XXS", "IQ3_XXS — точнее, медленнее", 75.8, 42.9, 60),
    "IQ3_S": ("qwen", "IQ3_S", "IQ3_S — лучшее качество", 83.6, 50.3, 62),
    "swift-IQ2_XS": ("swift", "IQ2_XS", "IQ2_XS — быстрая", 68.0, 35.5, 48),
    "swift-IQ3_XXS": ("swift", "IQ3_XXS", "IQ3_XXS — точнее", 75.8, 42.9, 60),
    "swift-IQ3_S": ("swift", "IQ3_S", "IQ3_S — лучшее качество", 83.6, 50.3, 62),
    "coder-IQ1_M": ("coder", "IQ1_M", "IQ1_M — единственный размер", 58.4, 23.4, 32),
    "unsloth-UD-IQ4_XS": (
        "unsloth",
        "UD-IQ4_XS",
        "UD-IQ4_XS — ~4 бита",
        93.7,
        59.5,
        48,
    ),
    "unsloth-UD-Q4_K_XL": (
        "unsloth",
        "UD-Q4_K_XL",
        "UD-Q4_K_XL — 4 бита, K-кванты",
        111.3,
        77.0,
        48,
    ),
    "qwen36-UD-Q4_K_XL": (
        "qwen36",
        "UD-Q4_K_XL",
        "UD-Q4_K_XL — 4 бита, самая лёгкая",
        22.9,
        21.0,
        32,
    ),
    "qwen36-UD-Q6_K_XL": (
        "qwen36",
        "UD-Q6_K_XL",
        "UD-Q6_K_XL — 6 бит, ближе к полной",
        32.6,
        30.0,
        44,
    ),
    "qwen36-Q8_0": (
        "qwen36",
        "Q8_0",
        "Q8_0 — 8 бит, ближе всего к полной",
        37.8,
        35.0,
        52,
    ),
}


def _capability(quality: float, speed: float, notes: str) -> dict:
    return {
        "status": "production",
        "modalities": ["text", "vision", "tool_calling"],
        "max_context_tokens": 65536,
        "supports_structured_output": True,
        "supports_tool_calling": True,
        "thinking_supported": True,
        "thinking_enabled": True,
        "thinking_level_default": "low",
        "local_only": True,
        "vram_gb_estimate": 22.0,
        "quality_score": quality,
        "speed_score": speed,
        "notes": notes,
    }


QWEN_CAPABILITY = _capability(
    0.95,
    0.55,
    "Qwen3.8-Flash-Next 125B MoE via Strata (GPU expert cache + experts in RAM). "
    "Vision through the server's image encoder when it is installed with images. "
    "Exclusive with GPU-Ollama.",
)
EXPECTED_CAPABILITIES = {
    "qwen": QWEN_CAPABILITY,
    "unsloth": QWEN_CAPABILITY,
    "swift": _capability(
        0.93,
        0.6,
        "Swift 1.5 — UkisAI's fine-tune of Qwen3.8-Flash-Next via Strata: "
        "~63% fewer thinking tokens by its authors' numbers. Exclusive with GPU-Ollama.",
    ),
    "coder": _capability(
        0.85,
        0.65,
        "Qwen3.8-Flash-Next Coder via Strata: half of the experts kept "
        "(code, tool use, vision); ~32 GB of RAM, weaker outside coding. "
        "Exclusive with GPU-Ollama.",
    ),
    "qwen36": _capability(
        0.85,
        0.8,
        "Qwen3.6-35B-A3B (35B MoE, 3B active) via Strata's own engine path. "
        "Pictures through Unsloth's mmproj (Strata 0.1.45+). Read invoices well "
        "as qwen3.6:35b on Ollama. Exclusive with GPU-Ollama.",
    ),
    "gyro": {},
    "nvfp4": {},
    "hy4-flash": {},
    "inkling-2-mini": {},
    "deepseek41": {},
}


def _incoming(catalog: StrataCatalogV1) -> StrataCatalogV1:
    result = catalog.model_copy(deep=True)
    result.source = "official-mcp"
    result.source_revision = "release-2-image-abc"
    result.fetched_at = datetime(2026, 11, 22, 12, tzinfo=UTC)
    return result


def _new_family() -> StrataFamily:
    return StrataFamily(
        id="newfam",
        title="New family",
        about="new upstream family",
        tag="newfam-",
        kind="own",
        catalog_key="strata_newfam",
        served_name="newfam:reasoning",
        runtime_profile="qwen36_own",
        setup_policy="default",
        disk_layout="strata_standard",
        upstream_present=True,
        origin="official",
        capability={},
    )


def _new_variant() -> StrataVariant:
    return StrataVariant(
        id="newfam-Q8_0",
        setup_id="newfam-Q8_0",
        family="newfam",
        model="Q8_0",
        label="Q8_0",
        download_gb=10.0,
        experts_gb=9.0,
        ram_gb=16,
        upstream_present=True,
        origin="official",
    )


def test_bundled_snapshot_is_exact_and_legacy_compatible():
    catalog = load_bundled_catalog()

    assert catalog.schema_version == 1
    assert catalog.source == "bundled"
    assert catalog.source_revision == "official-v0.2.20"
    assert {family.id for family in catalog.families} == FAMILY_IDS
    assert len(catalog.variants) == 50
    assert {variant.id for variant in catalog.variants} == VARIANT_IDS
    assert all(variant.id == variant.setup_id for variant in catalog.variants)
    assert all(
        variant.upstream_present and variant.origin == "bundled"
        for variant in catalog.variants
        if variant.family != "deepseek41"
    )
    assert all(
        family.upstream_present and family.origin == "bundled"
        for family in catalog.families
        if family.id != "deepseek41"
    )
    assert all(
        not variant.upstream_present and variant.origin == "retired"
        for variant in catalog.variants
        if variant.family == "deepseek41"
    )

    families = family_map(catalog)
    variants = variant_map(catalog)
    assert set(families) == FAMILY_IDS
    assert set(variants) == VARIANT_IDS
    assert families["qwen36"]["min_version"] == "0.1.45"
    assert families["qwen"]["setup_policy"] == "disable_experimental_speed_projection"
    assert families["coder"]["setup_policy"] == "disable_experimental_speed_projection"
    assert families["qwen"]["runtime_profile"] == "flash_next_pack"
    assert families["unsloth"]["runtime_profile"] == "unsloth_budget"
    assert families["qwen36"]["runtime_profile"] == "qwen36_own"
    assert families["gyro"]["runtime_profile"] == "gyro_gguf"
    assert families["nvfp4"]["runtime_profile"] == "nvfp4_pack"
    assert families["hy4-flash"]["runtime_profile"] == "hy4_shape"
    assert families["inkling-2-mini"]["runtime_profile"] == "inkling_shape"
    assert families["deepseek41"]["runtime_profile"] == "unknown"
    assert variants["hy4-flash-SQ-64"]["default_context"] == 131072
    assert variants["inkling-2-mini-SQ-20"]["default_context"] == 32768
    assert variants["inkling-2-mini-UD-IQ1_S"]["min_version"] == "0.2.20"
    assert variants["nvfp4-NVFP4-QAD"]["prepare_gb"] == 110.0
    assert variants["gyro-Gyro-S"]["mem_total"] is True
    assert variants["deepseek-v4.1-flash-UD-Q8_K_XL"]["available"] is False
    assert variants["deepseek-v4.1-flash-UD-Q8_K_XL"].get("experts_gb") is None
    assert families["qwen"]["catalog_key"] == families["unsloth"]["catalog_key"]
    assert families["qwen"]["served_name"] == families["unsloth"]["served_name"]

    for variant_id, expected in EXPECTED_SIZES.items():
        family, model, label, download, experts, ram = expected
        item = variants[variant_id]
        assert (item["family"], item["model"], item["label"]) == (family, model, label)
        assert (item["download_gb"], item["experts_gb"], item["ram_gb"]) == (
            download,
            experts,
            ram,
        )


def test_bundled_curated_capabilities_match_registry_values():
    catalog = load_bundled_catalog()
    for family in catalog.families:
        assert family.capability == EXPECTED_CAPABILITIES[family.id]
        capability = catalog_capability(family)
        assert capability.name == family.catalog_key
        assert capability.provider is ProviderKind.STRATA
        assert capability.provider_model == family.served_name
        if family.capability:
            assert capability.status is ModelStatus.PRODUCTION
            assert capability.modalities == {
                Modality.TEXT,
                Modality.VISION,
                Modality.TOOL_CALLING,
            }
        else:
            assert capability.status is ModelStatus.CANDIDATE
            assert capability.capabilities_unknown is True
        for key, expected in family.capability.items():
            if key != "modalities":
                assert capability.model_dump(mode="json")[key] == expected
    assert catalog_capability(catalog.families[0]) == catalog_capability(catalog.families[3])


def test_merge_adds_new_family_and_quant_without_mutating_previous():
    previous = load_bundled_catalog()
    incoming = _incoming(previous)
    incoming.families.append(_new_family())
    incoming.variants.append(_new_variant())

    merged, counts = merge_catalogs(previous, incoming)

    assert counts == {"added": 2, "updated": 0, "retired": 0, "changed": True}
    assert len(previous.families) == 10 and len(previous.variants) == 50
    assert set(family_map(merged)) == set(family_map(previous)) | {"newfam"}
    assert set(variant_map(merged)) == set(variant_map(previous)) | {"newfam-Q8_0"}
    assert merged.variants[-1].default_context == 65536


def test_bundled_upgrade_adds_history_and_promotes_without_retiring_existing():
    previous = load_bundled_catalog().model_copy(deep=True)
    previous.families = [family for family in previous.families if family.id in {"qwen", "gyro"}]
    previous.variants = [
        variant for variant in previous.variants if variant.family in {"qwen", "gyro"}
    ]
    gyro = next(family for family in previous.families if family.id == "gyro")
    gyro.kind = "unknown"
    gyro.runtime_profile = "unknown"
    gyro.catalog_key = "strata_discovered_gyro"
    gyro.served_name = "unsupported-gyro"
    for variant in previous.variants:
        if variant.family == "gyro":
            variant.default_context = 65536
            variant.label = "discovered"

    merged, counts = merge_curated_bundled(previous)

    assert counts["retired"] == 0 and counts["changed"] is True
    assert len(merged.families) == 10 and len(merged.variants) == 50
    assert merged.source_revision == previous.source_revision
    promoted = next(family for family in merged.families if family.id == "gyro")
    assert promoted.runtime_profile == "gyro_gguf"
    assert promoted.catalog_key == "strata_qwen3_8_flash_next_gyro"
    assert all(
        variant.label != "discovered" for variant in merged.variants if variant.family == "gyro"
    )
    deepseek = next(family for family in merged.families if family.id == "deepseek41")
    assert deepseek.origin == "retired" and not deepseek.upstream_present

    repeated, repeated_counts = merge_curated_bundled(merged)
    assert repeated_counts["changed"] is False


def test_merge_retires_missing_entries_and_reactivates_returned_ones():
    previous = load_bundled_catalog()
    incoming = _incoming(previous)
    incoming.families = [family for family in incoming.families if family.id != "swift"]
    incoming.variants = [variant for variant in incoming.variants if variant.family != "swift"]

    retired, counts = merge_catalogs(previous, incoming)

    # One family and its three variants become tombstones; nothing is deleted.
    assert counts == {"added": 0, "updated": 0, "retired": 4, "changed": True}
    assert len(retired.families) == 10 and len(retired.variants) == 50
    retired_family = next(family for family in retired.families if family.id == "swift")
    assert retired_family.origin == "retired" and not retired_family.upstream_present
    assert all(
        variant.origin == "retired" and not variant.upstream_present
        for variant in retired.variants
        if variant.family == "swift"
    )
    assert all(family.upstream_present for family in previous.families if family.id != "deepseek41")

    repeated, repeated_counts = merge_catalogs(retired, incoming)
    assert repeated_counts == {"added": 0, "updated": 0, "retired": 0, "changed": False}
    assert len(repeated.families) == 10 and len(repeated.variants) == 50

    reactivated, reactivated_counts = merge_catalogs(retired, _incoming(previous))
    assert reactivated_counts == {"added": 0, "updated": 4, "retired": 0, "changed": True}
    assert all(
        family.upstream_present for family in reactivated.families if family.id != "deepseek41"
    )
    assert all(
        variant.upstream_present
        for variant in reactivated.variants
        if variant.family != "deepseek41"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog_key", "strata_qwen_replacement"),
        ("served_name", "qwen-replacement"),
        ("runtime_profile", "qwen36_own"),
        ("setup_policy", "default"),
    ],
)
def test_merge_rejects_family_identity_conflicts(field: str, value: str):
    previous = load_bundled_catalog()
    incoming = _incoming(previous)
    setattr(incoming.families[0], field, value)

    with pytest.raises(CatalogValidationError, match="immutable family identity"):
        merge_catalogs(previous, incoming)


@pytest.mark.parametrize(
    ("field", "value"),
    [("setup_id", "different-setup"), ("family", "swift"), ("model", "Q8_0")],
)
def test_merge_rejects_variant_identity_conflicts(field: str, value: str):
    previous = load_bundled_catalog()
    incoming = _incoming(previous)
    setattr(incoming.variants[0], field, value)

    with pytest.raises(CatalogValidationError, match="immutable variant identity"):
        merge_catalogs(previous, incoming)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../qwen",
        "qwen/evil",
        "qwen\\evil",
        "qwen evil",
        "qwen;rm",
        "qwen$(id)",
        "qwen`id`",
        "qwen..evil",
        "qwen\nnext",
        "",
    ],
)
def test_unsafe_ids_are_rejected(bad_id: str):
    payload = load_bundled_catalog().variants[0].model_dump()
    payload["model"] = bad_id
    with pytest.raises(ValidationError):
        StrataVariant.model_validate(payload)


def test_served_name_allows_colon_but_not_path_or_shell_syntax():
    payload = load_bundled_catalog().families[0].model_dump()
    payload["served_name"] = "qwen3.8:vision"
    assert StrataFamily.model_validate(payload).served_name == "qwen3.8:vision"
    payload["served_name"] = "qwen:latest;id"
    with pytest.raises(ValidationError):
        StrataFamily.model_validate(payload)


def test_duplicate_variant_id_and_family_model_pair_are_rejected():
    payload = load_bundled_catalog().model_dump(mode="json")
    payload["variants"].append(payload["variants"][0].copy())
    with pytest.raises(ValidationError, match="duplicate Strata variant id"):
        StrataCatalogV1.model_validate(payload)

    payload = load_bundled_catalog().model_dump(mode="json")
    duplicate_pair = payload["variants"][0].copy()
    duplicate_pair["id"] = duplicate_pair["setup_id"] = "another-id"
    payload["variants"].append(duplicate_pair)
    with pytest.raises(ValidationError, match="duplicate Strata family/model pair"):
        StrataCatalogV1.model_validate(payload)


def test_duplicate_family_id_and_unknown_variant_family_are_rejected():
    payload = load_bundled_catalog().model_dump(mode="json")
    payload["families"].append(payload["families"][0].copy())
    with pytest.raises(ValidationError, match="duplicate Strata family id"):
        StrataCatalogV1.model_validate(payload)

    payload = load_bundled_catalog().model_dump(mode="json")
    payload["families"] = payload["families"][:1]
    with pytest.raises(ValidationError, match="unknown families"):
        StrataCatalogV1.model_validate(payload)


def test_shared_catalog_key_requires_the_same_served_name():
    payload = load_bundled_catalog().model_dump(mode="json")
    payload["families"][3]["served_name"] = "different-name"
    with pytest.raises(ValidationError, match="different served names"):
        StrataCatalogV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_profile", "future_runtime"),
        ("kind", "future_kind"),
        ("setup_policy", "arbitrary_shell_args"),
        ("disk_layout", "arbitrary_path"),
    ],
)
def test_unknown_runtime_enums_are_rejected(field: str, value: str):
    payload = load_bundled_catalog().families[0].model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        StrataFamily.model_validate(payload)


def test_empty_new_family_capability_is_conservative():
    capability = catalog_capability(_new_family())

    assert capability.status is ModelStatus.CANDIDATE
    assert capability.capability_source == "discovered"
    assert capability.modalities == {Modality.TEXT}
    assert capability.capabilities_unknown is True
    assert capability.local_only is True
    assert capability.supports_tool_calling is False
    assert capability.supports_structured_output is False
    assert capability.thinking_supported is False


@pytest.mark.parametrize("field", ["download_gb", "experts_gb", "ram_gb"])
def test_sizes_must_be_finite_positive_and_bounded(field: str):
    for value in [float("nan"), float("inf"), float("-inf"), 0, -1, MAX_MODEL_SIZE_GB + 1]:
        payload = load_bundled_catalog().variants[0].model_dump()
        payload[field] = value
        with pytest.raises(ValidationError):
            StrataVariant.model_validate(payload)


def test_contexts_are_positive_and_bounded():
    for field in ["default_context", "recommended_context"]:
        for value in [0, -1, MAX_CONTEXT_TOKENS + 1]:
            payload = load_bundled_catalog().variants[0].model_dump()
            payload[field] = value
            with pytest.raises(ValidationError):
                StrataVariant.model_validate(payload)


def test_extra_fields_are_forbidden_at_every_catalog_level():
    catalog_payload = load_bundled_catalog().model_dump(mode="json")
    catalog_payload["download_url"] = "https://example.invalid/model"
    with pytest.raises(ValidationError):
        StrataCatalogV1.model_validate(catalog_payload)

    family_payload = load_bundled_catalog().families[0].model_dump()
    family_payload["shell_args"] = ["--unsafe"]
    with pytest.raises(ValidationError):
        StrataFamily.model_validate(family_payload)

    variant_payload = load_bundled_catalog().variants[0].model_dump()
    variant_payload["path"] = "../../model"
    with pytest.raises(ValidationError):
        StrataVariant.model_validate(variant_payload)


def test_capability_fields_are_validated_without_silent_extra_field_loss():
    payload = load_bundled_catalog().families[0].model_dump()
    payload["capability"]["provider"] = "ollama"
    with pytest.raises(ValidationError, match="unknown ModelCapability fields"):
        StrataFamily.model_validate(payload)
