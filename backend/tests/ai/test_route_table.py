"""routes.yml as the single source of keyword routing: structure + behaviour."""

from __future__ import annotations

from app.ai import route_table

# ── Structural validation of routes.yml ────────────────────────────────────────


def test_routes_yml_loads_and_is_structurally_valid():
    table = route_table._table()
    assert table, "routes.yml must load"

    for name, spec in table["skill_to_spec"].items():
        assert spec.get("path"), f"{name} lacks skill_to_spec.path"

    # Marker lists of the fast paths must be present and normalized.
    for key in (
        "workspace_request_markers",
        "flow_status_markers",
        "relational_markers",
        "entity_domain_markers",
    ):
        markers = table.get(key)
        assert markers, f"{key} must not be empty"
        for marker in markers:
            assert marker == route_table.normalize(marker), (
                f"marker {marker!r} in {key} is not normalized"
            )


# ── Parity with the old hardcoded heuristics ───────────────────────────────────


def test_flow_status_markers():
    assert route_table.is_flow_status_query("Что требует внимания?")
    assert route_table.is_flow_status_query("дай сводку по документам")
    assert not route_table.is_flow_status_query("сколько счетов от Ромашки")


def test_needs_document_retrieval_skips_pure_workspace_query():
    # Plain table/list request — answered straight from SQL, no RAG needed.
    assert not route_table.needs_document_retrieval("покажи список счетов")


def test_needs_document_retrieval_content_marker_forces_retrieval():
    assert route_table.needs_document_retrieval("о чём этот документ")


def test_needs_document_retrieval_relational_override_on_workspace_shaped_query():
    # Workspace-shaped (mentions "поставщик") but actually a relationship
    # question — must NOT be skipped just because it looks like a list request.
    assert route_table.needs_document_retrieval("что связано с этим поставщиком")
    assert route_table.needs_document_retrieval("история аномалий у этого поставщика")
    assert route_table.needs_document_retrieval("цепочка согласования по этому счету")


def test_needs_document_retrieval_relational_word_alone_is_not_enough():
    # "связ"/"истор" without an entity-domain word shouldn't force anything
    # special here — falls through to the default (True), since it's not a
    # workspace/flow-status query in the first place.
    assert route_table.needs_document_retrieval("расскажи историю кота")


def test_supplier_name_extraction():
    assert route_table.extract_supplier_name("счета поставщика «Ромашка»") == "Ромашка"
    # Generic attribute requests must not produce a name.
    assert route_table.extract_supplier_name("лучший поставщик по trust score") is None
    assert route_table.extract_supplier_name("сгруппируй по поставщикам") is None


# ── Chips ──────────────────────────────────────────────────────────────────────


def test_chips_for_invoice_intent():
    chips = route_table.chips_for("invoice_list", "выведи счета")
    labels = [chip["label"] for chip in chips]
    assert "Открыть счета" in labels
    assert "Экспорт в Excel" in labels


def test_chips_keyword_match_is_normalized():
    # "счёт" in the user text must match the normalized "счет" keyword.
    chips = route_table.chips_for("general", "покажи счёт от Ромашки")
    assert any(chip["label"] == "Открыть счета" for chip in chips)


def test_chips_capped_at_four_with_workspace():
    chips = route_table.chips_for(
        "invoice_list", "счета поставщиков с аномалиями", workspace_required=True
    )
    assert len(chips) <= 4


# ── Prompt sections ────────────────────────────────────────────────────────────


# ── has_specific_filter_content: proactive-path gate ──────────────────────────


# ── techdraw vs diffusion routing ───────────────────────────────────────────────
