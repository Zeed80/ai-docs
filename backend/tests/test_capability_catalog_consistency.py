"""Catalog consistency: capabilities.yml ↔ _DISPATCH (Phase 1 refactor).

The hand-curated manifest must never drift from the dispatcher's routing table.
The action enum the model sees is injected from _DISPATCH, so a mismatch would
mean the model is offered actions that cannot be routed (or vice versa).
"""

import re
from pathlib import Path

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


def test_every_active_catalog_operation_has_a_fail_closed_effect_classification():
    """E03: the inventory must not silently omit a newly active operation.

    The inventory is intentionally a documentation artifact, rather than a
    runtime policy source.  It must nevertheless cover the live ``TOOLS``
    catalog exactly; unknown is an allowed conservative result and explicitly
    prohibits automatic retry.
    """
    from app.ai.tool_catalog import TOOLS

    inventory = (
        Path(__file__).resolve().parents[2]
        / "docs/agent-employee-delivery/tool-effect-inventory.md"
    ).read_text(encoding="utf-8")
    row_pattern = re.compile(
        r"^\| `(?P<operation>[^`]+)` \|.*\| "
        r"`(?P<classification>read-only|one-db-commit|db-async-enqueue|"
        r"external-dispatch|browser-script-mcp|unknown)`; auto-retry "
        r"(?P<retry>[^|]+) \|$",
        re.MULTILINE,
    )
    rows = list(row_pattern.finditer(inventory))
    classifications = {match["operation"]: match for match in rows}

    assert len(classifications) == len(rows), "duplicate operation rows in E03 inventory"
    assert set(classifications) == set(TOOLS), (
        "E03 inventory drift: "
        f"missing={sorted(set(TOOLS) - set(classifications))}; "
        f"stale={sorted(set(classifications) - set(TOOLS))}"
    )
    assert all(
        match["retry"].strip() == "prohibited"
        for match in classifications.values()
        if match["classification"] == "unknown"
    ), "unknown classifications must prohibit automatic retry"


def test_one_db_commit_adapter_allowlist_is_exact_reviewed_e05_2_9_subset():
    """E05.2.9 adds two DB-only, non-gated local operations."""

    from app.ai.tool_catalog import TOOLS
    from app.ai.tool_transport import ONE_DB_COMMIT_OPERATIONS

    inventory = (
        Path(__file__).resolve().parents[2]
        / "docs/agent-employee-delivery/tool-effect-inventory.md"
    ).read_text(encoding="utf-8")
    row_pattern = re.compile(
        r"^\| `(?P<operation>[^`]+)` \|.*\| "
        r"`(?P<classification>read-only|one-db-commit|db-async-enqueue|"
        r"external-dispatch|browser-script-mcp|unknown)`; auto-retry "
        r"(?P<retry>[^|]+) \|$",
        re.MULTILINE,
    )
    classifications = {
        match["operation"]: match["classification"] for match in row_pattern.finditer(inventory)
    }
    expected = frozenset(
        {
            "analytics.calendar_create_reminder",
            "analytics.collection_add_item",
            "analytics.collection_close",
            "analytics.collection_create",
            "analytics.compare_align",
            "analytics.compare_create",
            "analytics.table_create_view",
            "analytics.table_inline_edit",
            "documents.link",
            "email.draft",
            "email.templates.create",
            "email.templates.update",
            "invoices.update",
            "invoices.validate",
            "memory.source_propose",
            "normalization.create_norm_card",
            "normalization.update_canonical_item",
            "normalization.update_norm_card",
            "payments.create_schedule",
            "procurement.create_request",
            "suppliers.update",
            "tool_catalog.create_supplier",
            "warehouse.adjust_stock",
            "warehouse.create_item",
            "warehouse.create_receipt",
            "warehouse.update_item",
        }
    )

    assert ONE_DB_COMMIT_OPERATIONS == expected
    assert {classifications[name] for name in ONE_DB_COMMIT_OPERATIONS} == {"one-db-commit"}
    assert {TOOLS[name].effect for name in ONE_DB_COMMIT_OPERATIONS} == {"write"}
    e05_2_4_slice = {
        "email.templates.create",
        "email.templates.update",
        "suppliers.update",
    }
    assert all(not TOOLS[name].admin_only for name in e05_2_4_slice)
    e05_2_5_slice = {"procurement.create_request"}
    assert all(not TOOLS[name].admin_only for name in e05_2_5_slice)
    e05_2_6_slice = {"documents.link", "email.draft", "payments.create_schedule"}
    assert all(not TOOLS[name].admin_only for name in e05_2_6_slice)
    e05_2_7_slice = {
        "normalization.create_norm_card",
        "normalization.update_canonical_item",
        "normalization.update_norm_card",
    }
    assert all(not TOOLS[name].admin_only for name in e05_2_7_slice)
    e05_2_8_slice = {"invoices.update", "tool_catalog.create_supplier"}
    assert all(not TOOLS[name].admin_only for name in e05_2_8_slice)
    e05_2_9_slice = {"invoices.validate", "memory.source_propose"}
    assert all(not TOOLS[name].admin_only for name in e05_2_9_slice)
    excluded = {
        "documents.ingest": "db-async-enqueue",
        "email.send": "external-dispatch",
        "warehouse.bulk_confirm": "unknown",
        "warehouse.confirm_receipt": "unknown",
        "email.templates.from_message": "one-db-commit",
        "email.templates.render": "one-db-commit",
        "email.compose": "one-db-commit",
        "email.reply": "one-db-commit",
        "procurement.create_contract": "one-db-commit",
        "procurement.update_contract": "one-db-commit",
        "procurement.update_request": "one-db-commit",
        "procurement.send_rfq": "external-dispatch",
        "suppliers.trust_score": "one-db-commit",
        "sheets.create": "one-db-commit",
        "analytics.auto_approval_check": "one-db-commit",
        "analytics.auto_approval_create": "one-db-commit",
        "payments.mark_paid": "one-db-commit",
        "invoices.approve": "one-db-commit",
        "invoices.receive": "one-db-commit",
        "memory.promotion_decide": "one-db-commit",
        "memory.source_discover": "one-db-commit",
        "sheets.add_row": "one-db-commit",
    }
    assert not (ONE_DB_COMMIT_OPERATIONS & excluded.keys())
    assert {name: classifications[name] for name in excluded} == excluded

    by_route: dict[tuple[str, str], set[str]] = {}
    for name, tool in TOOLS.items():
        by_route.setdefault((tool.method, tool.path), set()).add(name)
    new_slice = expected - {
        "analytics.calendar_create_reminder",
        "analytics.collection_create",
        "analytics.table_create_view",
        "warehouse.create_item",
    }
    route_owners = {name: by_route[(TOOLS[name].method, TOOLS[name].path)] for name in new_slice}
    assert route_owners == {
        "analytics.collection_add_item": {"analytics.collection_add_item"},
        "analytics.collection_close": {"analytics.collection_close"},
        "analytics.compare_align": {"analytics.compare_align"},
        "analytics.compare_create": {
            "analytics.compare_create",
            "procurement.create_contract",
        },
        "analytics.table_inline_edit": {"analytics.table_inline_edit"},
        "email.templates.create": {"email.templates.create"},
        "email.templates.update": {"email.templates.update"},
        "procurement.create_request": {"procurement.create_request"},
        "suppliers.update": {"suppliers.update"},
        "warehouse.adjust_stock": {"warehouse.adjust_stock"},
        "warehouse.create_receipt": {"warehouse.create_receipt"},
        "warehouse.update_item": {"warehouse.update_item"},
        "documents.link": {"documents.link"},
        "email.draft": {"email.draft"},
        "payments.create_schedule": {"payments.create_schedule"},
        "invoices.update": {"invoices.update"},
        "invoices.validate": {"invoices.validate"},
        "memory.source_propose": {"memory.source_propose"},
        "normalization.create_norm_card": {"normalization.create_norm_card"},
        "normalization.update_canonical_item": {"normalization.update_canonical_item"},
        "normalization.update_norm_card": {"normalization.update_norm_card"},
        "tool_catalog.create_supplier": {"tool_catalog.create_supplier"},
    }

    from app.ai.capability_manifest import load_capability_manifest

    warehouse = load_capability_manifest().by_name["warehouse"]
    assert not {
        "adjust_stock",
        "create_receipt",
        "update_item",
    } & set(warehouse.gate_actions)
    procurement = load_capability_manifest().by_name["procurement"]
    assert "create_request" not in procurement.gate_actions
    documents = load_capability_manifest().by_name["documents"]
    assert "link" not in documents.gate_actions
    email = load_capability_manifest().by_name["email"]
    assert "draft" not in email.gate_actions
    payments = load_capability_manifest().by_name["payments"]
    assert "create_schedule" not in payments.gate_actions
    normalization = load_capability_manifest().by_name["normalization"]
    assert not {
        "create_norm_card",
        "update_canonical_item",
        "update_norm_card",
    } & set(normalization.gate_actions)
    invoices = load_capability_manifest().by_name["invoices"]
    assert not {"update", "validate"} & set(invoices.gate_actions)
    tool_catalog = load_capability_manifest().by_name["tool_catalog"]
    assert "create_supplier" not in tool_catalog.gate_actions
    memory = load_capability_manifest().by_name["memory"]
    assert "source_propose" not in memory.gate_actions

    from app.ai.gateway_config import gateway_config

    assert not (e05_2_4_slice & gateway_config.approval_gates)
    assert not (e05_2_5_slice & gateway_config.approval_gates)
    assert not (e05_2_6_slice & gateway_config.approval_gates)
    assert not (e05_2_7_slice & gateway_config.approval_gates)
    assert not (e05_2_8_slice & gateway_config.approval_gates)
    assert not (e05_2_9_slice & gateway_config.approval_gates)
