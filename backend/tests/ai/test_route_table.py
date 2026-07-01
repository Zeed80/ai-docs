"""routes.yml as the single source of keyword routing: structure + behaviour."""

from __future__ import annotations

from app.ai import route_table

# ── Structural validation of routes.yml ────────────────────────────────────────


def test_routes_yml_loads_and_is_structurally_valid():
    table = route_table._table()
    assert table, "routes.yml must load"

    for route in table["routes"]:
        assert route.get("intent"), "every route needs an intent"
        assert route.get("role"), f"route {route.get('intent')} needs a role"
        assert route.get("keywords"), f"route {route.get('intent')} needs keywords"
        # Keywords must be pre-normalized: the matcher compares against
        # normalize(text) which lowercases and folds ё→е.
        for kw in route["keywords"]:
            assert kw == route_table.normalize(kw), (
                f"keyword {kw!r} in route {route['intent']} is not normalized (lower, ё→е)"
            )

    # Every canvas maps to a skill that has an HTTP spec (direct-repair path).
    for canvas_id, skill in table["canvas_to_skill"].items():
        spec = table["skill_to_spec"].get(skill)
        assert spec and spec.get("path"), f"{canvas_id} → {skill} lacks skill_to_spec.path"

    # Marker lists used by the heuristics must be present and normalized.
    for key in (
        "workspace_request_markers",
        "table_edit_markers",
        "existing_table_markers",
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


def test_workspace_request_markers():
    assert route_table.is_workspace_request("Выведи полный список документов в таблицу")
    assert route_table.is_workspace_request("сделай отчёт по платежам")  # ё folded
    assert not route_table.is_workspace_request("привет, как дела?")


def test_table_edit_and_existing_table_markers():
    assert route_table.is_table_edit_request("Добавь столбец с ИНН")
    assert route_table.references_existing_table("добавь в уже открытую таблицу")
    assert route_table.references_existing_table("добавь данные в неё")  # ё folded
    assert not route_table.is_table_edit_request("покажи счёт")


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


def test_match_route_table_edit():
    route = route_table.match_route("Добавь столбец с названием поставщика перед номером счета")
    assert route is not None
    assert route["intent"] == "table_edit"
    assert route["role"] == "invoice_specialist"
    canvas = route_table.resolve_canvas_from_route(
        route, "добавь столбец с названием поставщика"
    )
    assert canvas == "agent:invoice-items-grouped"


def test_match_route_tech_process():
    route = route_table.match_route("Разработай техпроцесс для детали")
    assert route is not None
    assert route["role"] == "technologist"


def test_supplier_name_extraction():
    assert route_table.extract_supplier_name('счета поставщика «Ромашка»') == "Ромашка"
    # Generic attribute requests must not produce a name.
    assert route_table.extract_supplier_name("лучший поставщик по trust score") is None
    assert route_table.extract_supplier_name("сгруппируй по поставщикам") is None


def test_supplier_grouping_detection():
    assert route_table.is_supplier_grouping_request("сгруппируй товары по поставщикам")
    # A named supplier → filter request, not group-by.
    assert not route_table.is_supplier_grouping_request(
        "выведи товары поставщика «Ромашка» в таблицу"
    )


def test_fallback_canvas_rules():
    assert route_table.fallback_canvas("выведи все товары списком") == "agent:invoice-items"
    assert (
        route_table.fallback_canvas("товары по поставщикам")
        == "agent:invoice-items-by-supplier"
    )
    assert route_table.fallback_canvas("список счетов") == "agent:invoices"
    assert route_table.fallback_canvas("про погоду") is None


def test_canvas_skill_mappings():
    skill = route_table.canvas_to_skill("agent:invoice-items-by-supplier")
    assert skill == "workspace.invoice_items_by_supplier_table"
    spec = route_table.skill_spec(skill)
    assert spec["path"].startswith("/api/workspace/")
    assert route_table.canvas_to_skill(None) is None


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


def test_prompt_sections_rendered():
    text = route_table.prompt_sections()
    assert "technologist" in text
    assert "секретарь-оркестратор" in text
    assert "tech.generate_tp_from_drawing" in text


# ── has_specific_filter_content: proactive-path gate ──────────────────────────


def test_filter_content_pure_listing_no_filter():
    """Generic listing requests have no residual filter content."""
    assert not route_table.has_specific_filter_content("выведи все счета")
    assert not route_table.has_specific_filter_content("список всех счетов")
    assert not route_table.has_specific_filter_content("покажи всех поставщиков")
    assert not route_table.has_specific_filter_content("таблицу счетов")
    assert not route_table.has_specific_filter_content("покажи все документы")


def test_filter_content_item_name_is_residual():
    """A specific item/product name is residual content — proactive must be skipped."""
    assert route_table.has_specific_filter_content("выведи все фрезы со всех счетов")
    assert route_table.has_specific_filter_content("выведи все резцы со всех счетов")
    assert route_table.has_specific_filter_content("все болты в таблице")
    assert route_table.has_specific_filter_content("покажи сверла из счетов")


def test_filter_content_time_period_is_residual():
    """Time/period qualifiers are filter content that static skills can't express."""
    assert route_table.has_specific_filter_content("список счетов за май")
    assert route_table.has_specific_filter_content("счета за январь 2025")
    assert route_table.has_specific_filter_content("выведи счета последнего квартала")


def test_filter_content_geo_name_is_residual():
    """Geographic names are filter criteria."""
    assert route_table.has_specific_filter_content("таблицу поставщиков из Москвы")
    assert route_table.has_specific_filter_content("поставщики Казань")


def test_filter_content_with_matched_route_no_extra():
    """Route keywords fully explain the message — no residual."""
    invoice_list_route = route_table.match_route("список счетов")
    assert not route_table.has_specific_filter_content("список счетов", invoice_list_route)


def test_filter_content_with_matched_route_has_extra():
    """Route matched but message has extra content not covered by route keywords."""
    invoice_list_route = route_table.match_route("список счетов")
    assert route_table.has_specific_filter_content("список счетов за май", invoice_list_route)
    assert route_table.has_specific_filter_content("список счетов фрезы", invoice_list_route)


def test_filter_content_function_words_not_residual():
    """Common Russian function words don't trigger filter detection."""
    assert not route_table.has_specific_filter_content("покажи мне все счета пожалуйста")
    assert not route_table.has_specific_filter_content("выведи для нас таблицу поставщиков")


# ── techdraw vs diffusion routing ───────────────────────────────────────────────


def test_techdraw_request_part_plus_precision():
    assert route_table.is_techdraw_request("начерти вал 50h6 длиной 120 с шероховатостью Ra0.8")


def test_techdraw_request_standalone_phrase():
    assert route_table.is_techdraw_request("сделай точный чертеж по гост")


def test_techdraw_request_chertezh_word_alone_with_precision():
    """No part noun, but 'чертеж' + a precision marker is still enough."""
    assert route_table.is_techdraw_request("чертеж с допуском H7 и шероховатостью Ra0.8")


def test_not_techdraw_request_plain_sketch():
    assert not route_table.is_techdraw_request("нарисуй эскиз установки детали на станке")


def test_not_techdraw_request_photo_edit():
    assert not route_table.is_techdraw_request("отредактируй фото детали, убери фон")


def test_not_techdraw_request_part_noun_without_precision():
    """Mentioning a part alone (no precision marker) is still a sketch request."""
    assert not route_table.is_techdraw_request("нарисуй вал для иллюстрации в письме")
