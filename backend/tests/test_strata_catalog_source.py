"""Official Strata catalog adapter and durable last-known-good snapshots."""

import pytest

from app.ai.providers import strata_catalog_source as source
from app.ai.providers import strata_catalog_store as store
from app.ai.providers.strata_catalog import load_bundled_catalog
from app.db.models import StrataCatalogSnapshot


def _mcp_payload():
    return {
        "families": [
            {
                "family": "qwen",
                "sizes": [
                    {
                        "model": "IQ2_XS",
                        "about": "smallest",
                        "download_gb": 59,
                        "experts_gb": 31,
                        "ram_needed_gb": 50,
                        "engine_min": "0.2.1",
                    },
                    {
                        "model": "IQ4_NL",
                        "about": "new official variant",
                        "download_gb": 120,
                        "experts_gb": 75,
                        "ram_needed_gb": 100,
                        "engine_min": "0.2.1",
                    },
                ],
            },
            {
                "family": "never-seen-before",
                "sizes": [{"model": "Q4", "download_gb": 1, "experts_gb": 1, "ram_needed_gb": 2}],
            },
        ]
    }


def test_mcp_adapter_adds_official_variant_and_read_only_family():
    result = source.parse_model_table(
        _mcp_payload(), previous=load_bundled_catalog(), revision="image:test"
    )
    assert {variant.id for variant in result.catalog.variants} == {
        "IQ2_XS",
        "IQ4_NL",
        "never-seen-before-Q4",
    }
    new = next(variant for variant in result.catalog.variants if variant.id == "IQ4_NL")
    assert new.setup_id == "IQ4_NL"
    assert new.ram_gb == 100
    assert result.catalog.families[0].catalog_key == "strata_qwen3_8_flash_next"
    discovered = next(f for f in result.catalog.families if f.id == "never-seen-before")
    assert discovered.runtime_profile == "unknown"
    assert discovered.capability == {}
    assert "только для чтения" in result.warnings[0]


def test_curated_profile_promotes_previously_unknown_family():
    previous = load_bundled_catalog().model_copy(deep=True)
    old = next(family for family in previous.families if family.id == "gyro")
    for field, value in {
        "kind": "unknown",
        "catalog_key": "strata_discovered_gyro",
        "served_name": "unsupported-gyro",
        "runtime_profile": "unknown",
        "setup_policy": "unknown",
        "disk_layout": "unknown",
    }.items():
        setattr(old, field, value)

    result = source.parse_model_table(
        {
            "families": [
                {
                    "family": "gyro",
                    "title": "Qwen3.8-Flash-Next (Gyro)",
                    "sizes": [
                        {
                            "model": "Gyro-S",
                            "download_gb": 58.5,
                            "experts_gb": 25.8,
                            "ram_needed_gb": 27.6,
                            "mem_total": True,
                            "format": "gguf",
                            "engine_min": "0.1.60",
                        }
                    ],
                }
            ]
        },
        previous=previous,
        revision="image:new",
    )

    promoted = result.catalog.families[0]
    assert promoted.runtime_profile == "gyro_gguf"
    assert promoted.catalog_key == "strata_qwen3_8_flash_next_gyro"
    variant = result.catalog.variants[0]
    assert variant.mem_total is True and variant.format == "gguf"
    assert variant.engine_min == "0.1.60"
    assert variant.min_version is None  # engine version is not the server requirement

    from app.ai.providers.strata_catalog import merge_catalogs

    merged, _counts = merge_catalogs(previous, result.catalog)
    assert next(f for f in merged.families if f.id == "gyro").runtime_profile == "gyro_gguf"


def test_profile_promotion_restores_curated_variant_defaults():
    previous = load_bundled_catalog().model_copy(deep=True)
    selected = {"nvfp4": "NVFP4-QAD", "inkling-2-mini": "SQ-20"}
    for family in previous.families:
        if family.id in selected:
            family.kind = "unknown"
            family.runtime_profile = "unknown"
    for variant in previous.variants:
        if variant.family in selected:
            variant.default_context = 65536
            variant.prepare_gb = None
            variant.label = "discovered"

    payload = {"families": []}
    for family in previous.families:
        if family.id not in selected:
            continue
        old = next(
            variant
            for variant in previous.variants
            if variant.family == family.id and variant.model == selected[family.id]
        )
        payload["families"].append(
            {
                "family": family.id,
                "sizes": [
                    {
                        "model": old.model,
                        "download_gb": old.download_gb,
                        "experts_gb": old.experts_gb,
                        "ram_needed_gb": old.ram_gb,
                    }
                ],
            }
        )

    result = source.parse_model_table(payload, previous=previous, revision="image:new")
    qad = next(variant for variant in result.catalog.variants if variant.model == "NVFP4-QAD")
    inkling = next(variant for variant in result.catalog.variants if variant.model == "SQ-20")
    assert qad.prepare_gb == 110
    assert inkling.default_context == 32768
    assert inkling.label != "discovered"


def test_full_bundled_release_round_trips_through_mcp_adapter():
    bundled = load_bundled_catalog()
    payload = {
        "families": [
            {
                "family": family.id,
                "title": family.title,
                "about": family.about,
                "sizes": [
                    {
                        "model": variant.model,
                        "about": variant.label,
                        "download_gb": variant.download_gb,
                        "experts_gb": variant.experts_gb,
                        "ram_needed_gb": variant.ram_gb,
                        "mem_total": variant.mem_total,
                        "format": variant.format,
                        "engine_min": variant.engine_min,
                    }
                    for variant in bundled.variants
                    if variant.family == family.id and variant.upstream_present
                ],
            }
            for family in bundled.families
            if family.upstream_present
        ]
    }

    result = source.parse_model_table(payload, previous=bundled, revision="official-v0.2.20-test")

    expected = {variant.id for variant in bundled.variants if variant.upstream_present}
    assert len(result.catalog.families) == 9
    assert len(result.catalog.variants) == 44
    assert {variant.id for variant in result.catalog.variants} == expected
    assert result.warnings == []

    from app.ai.providers.strata_catalog import merge_catalogs

    merged, counts = merge_catalogs(bundled, result.catalog)
    # The first MCP refresh changes only the provenance of bundled active entries.
    assert counts == {"added": 0, "updated": 53, "retired": 0, "changed": True}
    deepseek = next(family for family in merged.families if family.id == "deepseek41")
    assert deepseek.origin == "retired" and not deepseek.upstream_present
    assert all(
        variant.origin == "retired" and not variant.upstream_present
        for variant in merged.variants
        if variant.family == "deepseek41"
    )


def test_discovered_family_cannot_be_written_or_downloaded(monkeypatch, tmp_path):
    from app.ai.providers import strata_manager

    result = source.parse_model_table(
        _mcp_payload(), previous=load_bundled_catalog(), revision="image:test"
    )
    monkeypatch.setattr(store, "current_catalog", lambda: result.catalog)
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    assert strata_manager.variant_supported("IQ4_NL") is True
    assert strata_manager.variant_supported("never-seen-before-Q4") is False
    with pytest.raises(strata_manager.StrataError, match="не подтверждён"):
        strata_manager.write_desired(
            model="never-seen-before-Q4", context=65536, vision=True, reinstall=False
        )


def test_mcp_adapter_fails_closed_on_empty_or_unknown_catalog():
    with pytest.raises(source.StrataCatalogSourceError):
        source.parse_model_table(
            {"families": [{"family": "unknown", "sizes": []}]},
            previous=load_bundled_catalog(),
            revision="image:test",
        )


@pytest.mark.asyncio
async def test_snapshot_registry_and_cache_survive_redis_flush(db_session, monkeypatch):
    cache = {}
    monkeypatch.setattr(store, "_redis_get_json", lambda key: cache.get(key))
    monkeypatch.setattr(store, "_redis_set_json", lambda key, value: cache.__setitem__(key, value))
    monkeypatch.setattr(store, "_MEMORY_CATALOG", None)
    catalog = await store.ensure_strata_catalog(db_session)
    assert len(catalog.variants) == 50
    assert "strata:catalog:v1" in cache
    cache.clear()
    monkeypatch.setattr(store, "_MEMORY_CATALOG", None)
    restored = await store.ensure_strata_catalog(db_session)
    assert restored.source_revision == catalog.source_revision
    entries = (
        (
            await db_session.execute(
                __import__("sqlalchemy")
                .select(
                    __import__(
                        "app.db.models", fromlist=["ModelCatalogRuntimeEntry"]
                    ).ModelCatalogRuntimeEntry
                )
                .where(
                    __import__(
                        "app.db.models", fromlist=["ModelCatalogRuntimeEntry"]
                    ).ModelCatalogRuntimeEntry.provider
                    == "strata"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 9
    assert {entry.verification_status for entry in entries} == {"manual", "discovered"}


@pytest.mark.asyncio
async def test_existing_snapshot_receives_bundled_curated_upgrade(db_session, monkeypatch):
    cache = {}
    monkeypatch.setattr(store, "_redis_get_json", lambda key: cache.get(key))
    monkeypatch.setattr(store, "_redis_set_json", lambda key, value: cache.__setitem__(key, value))
    monkeypatch.setattr(store, "_MEMORY_CATALOG", None)
    previous = load_bundled_catalog().model_copy(deep=True)
    previous.families = [family for family in previous.families if family.id == "qwen"]
    previous.variants = [variant for variant in previous.variants if variant.family == "qwen"]
    db_session.add(store._new_snapshot(previous))
    await db_session.commit()

    upgraded = await store.ensure_strata_catalog(db_session)

    assert len(upgraded.families) == 10 and len(upgraded.variants) == 50
    assert next(f for f in upgraded.families if f.id == "deepseek41").origin == "retired"
    snapshots = (
        (await db_session.execute(__import__("sqlalchemy").select(StrataCatalogSnapshot)))
        .scalars()
        .all()
    )
    assert len(snapshots) == 2
    await store.ensure_strata_catalog(db_session)
    snapshots = (
        (await db_session.execute(__import__("sqlalchemy").select(StrataCatalogSnapshot)))
        .scalars()
        .all()
    )
    assert len(snapshots) == 2


@pytest.mark.asyncio
async def test_refresh_keeps_last_known_good_on_source_failure(db_session, monkeypatch):
    cache = {}
    monkeypatch.setattr(store, "_redis_get_json", lambda key: cache.get(key))
    monkeypatch.setattr(store, "_redis_set_json", lambda key, value: cache.__setitem__(key, value))
    await store.ensure_strata_catalog(db_session)

    async def broken_loader():
        raise source.StrataCatalogSourceError("reader unavailable")

    with pytest.raises(source.StrataCatalogSourceError):
        await store.refresh_strata_catalog(db_session, broken_loader)
    assert store.current_catalog().source_revision == "official-v0.2.20"
    assert "reader unavailable" in store.catalog_status()["last_error"]


@pytest.mark.asyncio
async def test_refresh_persists_tombstones_and_adds_variant(db_session, monkeypatch):
    cache = {}
    monkeypatch.setattr(store, "_redis_get_json", lambda key: cache.get(key))
    monkeypatch.setattr(store, "_redis_set_json", lambda key, value: cache.__setitem__(key, value))
    monkeypatch.setattr(store, "_MEMORY_CATALOG", None)
    hydrated = []

    async def mark_hydrated(db):
        hydrated.append(True)

    monkeypatch.setattr(store, "hydrate_runtime_cache", mark_hydrated)
    await store.ensure_strata_catalog(db_session)

    async def loader():
        return source.parse_model_table(
            _mcp_payload(), previous=load_bundled_catalog(), revision="image:new"
        )

    merged, counts = await store.refresh_strata_catalog(db_session, loader)
    assert counts["changed"] is True
    assert "IQ4_NL" in {variant.id for variant in merged.variants}
    retired = next(variant for variant in merged.variants if variant.id == "IQ3_S")
    assert retired.origin == "retired"
    assert retired.upstream_present is False
    assert hydrated
    snapshots = (
        (await db_session.execute(__import__("sqlalchemy").select(StrataCatalogSnapshot)))
        .scalars()
        .all()
    )
    assert len(snapshots) == 2


@pytest.mark.asyncio
async def test_official_removal_is_plan_then_apply_without_path_interpretation(monkeypatch):
    calls = []

    async def runner(code, *, write_data=False):
        calls.append((code, write_data))
        if "confirm=False" in code:
            return {"summary": "plan", "plan": {"model": "iq4_nl", "gb": 12.5}}, {}
        return {"summary": "deleted", "plan": {"model": "iq4_nl", "gb": 12.5}, "failed": []}, {}

    monkeypatch.setattr(source, "_run_python_json", runner)
    result = await source.remove_official_variant(
        {"id": "IQ4_NL", "setup_id": "IQ4_NL", "family": "qwen", "model": "IQ4_NL"}
    )
    assert result["plan"]["gb"] == 12.5
    assert [write for _code, write in calls] == [False, True]
    assert "family=" not in calls[0][0]


@pytest.mark.asyncio
async def test_refresh_endpoint_maps_source_failure_to_502(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.api.strata_api import strata_refresh_catalog

    async def broken():
        raise source.StrataCatalogSourceError("offline")

    monkeypatch.setattr(source, "load_official_catalog", broken)
    with pytest.raises(HTTPException) as error:
        await strata_refresh_catalog(db_session)
    assert error.value.status_code == 502
