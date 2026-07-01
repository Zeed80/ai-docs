"""Catalog consistency: capabilities.yml ↔ _DISPATCH (Phase 1 refactor).

The hand-curated manifest must never drift from the dispatcher's routing table.
The action enum the model sees is injected from _DISPATCH, so a mismatch would
mean the model is offered actions that cannot be routed (or vice versa).
"""

from app.api.capability_router import (
    capability_action_map,
    validate_capability_catalog,
)
from app.ai.agent_loop import _load_capabilities


def test_catalog_is_consistent_with_dispatch():
    problems = validate_capability_catalog()
    assert problems == [], "Catalog drift:\n" + "\n".join(problems)


def test_action_enum_injected_into_tool_schema():
    tools, _skill_map = _load_capabilities()
    by_name = {t["function"]["name"]: t for t in tools}
    # documents capability must expose its action enum from _DISPATCH.
    action_prop = by_name["documents"]["function"]["parameters"]["properties"]["action"]
    enum = action_prop.get("enum")
    assert enum, "action enum not injected"
    assert set(enum) == set(capability_action_map()["documents"])
    # The model must be able to pick a real gated action like approve on invoices.
    inv_enum = by_name["invoices"]["function"]["parameters"]["properties"]["action"]["enum"]
    assert "approve" in inv_enum


def test_image_studio_accept_techdraw_is_gated_and_dispatched():
    from app.ai.capability_manifest import load_capability_manifest

    assert "accept_techdraw" in capability_action_map()["image_studio"]
    manifest = load_capability_manifest()
    image_studio = manifest.by_name["image_studio"]
    assert "accept_techdraw" in image_studio.gate_actions
    assert "accept" not in image_studio.gate_actions  # diffusion accept stays ungated


def test_image_studio_diffusion_actions_are_non_recipeable():
    from app.ai.capability_manifest import load_capability_manifest

    manifest = load_capability_manifest()
    image_studio = manifest.by_name["image_studio"]
    non_recipeable = set(image_studio.non_recipeable_actions)
    assert {"generate", "iterate", "accept", "accept_techdraw"} <= non_recipeable
