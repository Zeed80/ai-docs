"""Switching the embedding model must not silently break the vector stores.

Live migration (2026-08-21) from qwen3-embedding:8b (4096) to
qwen3-embedding:4b (2560) on a dedicated CPU node. Two failure modes existed:
name-scoped collections start empty under the new name (recoverable only by
reindexing), and fixed-name ones keep the old dimension and reject every write.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.vector import qdrant_store


def _named(name: str) -> MagicMock:
    """MagicMock whose .name is the given string (name= is reserved in ctor)."""
    mock = MagicMock()
    mock.name = name
    return mock


def test_ensure_collection_defaults_to_the_active_dimension(monkeypatch):
    """The dimension comes from the assigned model, not a module constant."""
    monkeypatch.setattr(qdrant_store, "active_vector_size", lambda default=4096: 2560)
    created: dict = {}

    client = MagicMock()
    client.get_collections.return_value = MagicMock(collections=[])
    client.create_collection.side_effect = lambda **kw: created.update(kw)
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)

    qdrant_store.ensure_collection(collection_name="probe")
    assert created["vectors_config"].size == 2560


def test_existing_collection_with_other_dimension_is_left_alone_by_default(monkeypatch):
    """Data is never dropped implicitly — the mismatch is reported instead."""
    monkeypatch.setattr(qdrant_store, "active_vector_size", lambda default=4096: 2560)
    client = MagicMock()
    client.get_collections.return_value = MagicMock(collections=[_named("probe")])
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    monkeypatch.setattr(qdrant_store, "_collection_vector_size", lambda c, n: 4096)

    qdrant_store.ensure_collection(collection_name="probe")
    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()


def test_reindex_path_recreates_a_stale_dimension(monkeypatch):
    """…but the explicit reindex path rebuilds it, because it refills it."""
    monkeypatch.setattr(qdrant_store, "active_vector_size", lambda default=4096: 2560)
    client = MagicMock()
    client.get_collections.return_value = MagicMock(collections=[_named("probe")])
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    monkeypatch.setattr(qdrant_store, "_collection_vector_size", lambda c, n: 4096)

    qdrant_store.ensure_collection(collection_name="probe", recreate_on_mismatch=True)
    client.delete_collection.assert_called_once_with("probe")
    client.create_collection.assert_called_once()
