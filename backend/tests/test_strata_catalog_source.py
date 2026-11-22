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
    assert len(catalog.variants) == 13
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
    assert len(entries) == 4
    assert {entry.verification_status for entry in entries} == {"manual"}


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
    assert store.current_catalog().source_revision == "legacy-static-v1"
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
