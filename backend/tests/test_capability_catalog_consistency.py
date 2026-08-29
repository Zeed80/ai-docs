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


# ── Ф6.9: the direction nobody was checking ────────────────────────────────

# Pre-existing mismatches outside the e-mail subsystem. Recorded, not fixed:
# tech/workspace/memory are other people's capabilities and other phases' work.
# The number must not grow — a new entry here means somebody granted an action
# that does not exist, and the agent will be told "unknown action" while
# believing it had the right.
_KNOWN_BROKEN_GRANT_PREFIXES = ("tech.", "workspace.", "memory.")
_KNOWN_BROKEN_GRANT_COUNT = 17


def test_no_email_action_is_granted_without_a_route():
    """Three email.templates.* actions were in gateway.yml's allowlist and in
    no dispatch table, so an agent explicitly given the right got "unknown
    action" — indistinguishable from its own mistake."""
    from app.api.capability_router import validate_gateway_grants

    email_problems = [p for p in validate_gateway_grants() if "'email." in p]
    assert email_problems == [], "\n".join(email_problems)


def test_the_backlog_of_broken_grants_does_not_grow():
    from app.api.capability_router import validate_gateway_grants

    problems = validate_gateway_grants()
    unexpected = [
        p for p in problems
        if not any(f"'{prefix}" in p for prefix in _KNOWN_BROKEN_GRANT_PREFIXES)
    ]
    assert unexpected == [], "новые нерабочие права:\n" + "\n".join(unexpected)
    assert len(problems) <= _KNOWN_BROKEN_GRANT_COUNT, (
        f"было {_KNOWN_BROKEN_GRANT_COUNT}, стало {len(problems)}:\n"
        + "\n".join(problems)
    )
