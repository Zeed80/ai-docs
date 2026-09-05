"""Catalog consistency: capabilities.yml ↔ _DISPATCH (Phase 1 refactor).

The hand-curated manifest must never drift from the dispatcher's routing table.
The action enum the model sees is injected from _DISPATCH, so a mismatch would
mean the model is offered actions that cannot be routed (or vice versa).
"""

from app.ai.agent_loop import _load_capabilities
from app.api.capability_router import (
    capability_action_map,
    validate_capability_catalog,
)


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

# The backlog this file used to tolerate is closed: tech (10), memory (4) and
# workspace (3) grants now all resolve to real dispatch entries. There is no
# allowed remainder any more — a granted action that does not exist means the
# agent is told "unknown action" while believing it had the right, which is
# indistinguishable, from the model's side, from its own mistake.


def test_no_email_action_is_granted_without_a_route():
    """Three email.templates.* actions were in gateway.yml's allowlist and in
    no dispatch table, so an agent explicitly given the right got "unknown
    action" — indistinguishable from its own mistake."""
    from app.api.capability_router import validate_gateway_grants

    email_problems = [p for p in validate_gateway_grants() if "'email." in p]
    assert email_problems == [], "\n".join(email_problems)


def test_every_granted_action_is_routable():
    """Ни одно выданное право не должно вести в несуществующее действие."""
    from app.api.capability_router import validate_gateway_grants

    problems = validate_gateway_grants()
    assert problems == [], "нерабочие права:\n" + "\n".join(problems)


def test_the_technologist_role_can_actually_reach_its_own_work():
    """Половина роли технолога была недостижима: эндпоинты существовали,
    gateway.yml раздавал права, а маршрутов в _DISPATCH не было."""
    from app.api.capability_router import capability_action_map

    tech = set(capability_action_map()["tech"])
    assert {
        "generate_tp_from_drawing",
        "analyze_surfaces",
        "select_equipment_for_op",
        "calculate_cutting_params",
        "normcontrol_check",
        "normcontrol_resolve",
        "blank_spec_set",
        "surface_specs_list",
        "export_gost_forms",
        "operation_template_create",
    } <= tech


def test_memory_prune_is_gated():
    """prune безвозвратно удаляет эпизодические факты, а память объявлена
    защищённой настройкой — чистка не должна проходить молча."""
    from app.ai.capability_manifest import load_capability_manifest

    memory = next(c for c in load_capability_manifest().capabilities if c.name == "memory")
    assert "prune" in memory.gate_actions
