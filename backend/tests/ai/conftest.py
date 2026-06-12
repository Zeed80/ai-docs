"""Shared fixtures for the agent unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_recipe_lookup(request, monkeypatch):
    """Disable recipe retrieval (embedding round-trip) in agent unit tests.

    The orchestrator consults the recipe store on tool-shaped turns; unit
    tests must not depend on a live embedding model or Qdrant. Tests that
    exercise recipes opt out with ``@pytest.mark.recipes``.
    """
    if request.node.get_closest_marker("recipes"):
        return

    from app.ai import recipes

    async def _none(text: str):
        return None

    monkeypatch.setattr(recipes, "find_recipe", _none)
