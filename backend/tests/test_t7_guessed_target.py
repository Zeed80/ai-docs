"""T7b: a guessed target is judged by code from this turn's reads (E52)."""

import json

from app.ai.agent_loop import guessed_target

STEEL = "11111111-1111-1111-1111-111111111111"
COPPER = "22222222-2222-2222-2222-222222222222"


def _turn(*reads, request="Обнови телефон поставщика ООО «Эвал-Никель»"):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": request}]
    for index, (name, arguments, result) in enumerate(reads):
        call_id = f"c{index}"
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
    return messages


def _envelope(data):
    return {"version": 1, "status": "succeeded", "data": data}


UPDATE = {"action": "update", "supplier_id": COPPER, "body": {"contact_phone": "+7 495 222-33-44"}}
S_REC = {"id": STEEL, "name": "ООО «Эвал-Сталь»"}
C_REC = {"id": COPPER, "name": "ООО «Эвал-Медь»"}
LIST_ALL = (
    "suppliers",
    {"action": "list"},
    _envelope({"items": [S_REC, C_REC], "total": 2}),
)


def test_a_change_after_the_named_record_was_not_found_is_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        LIST_ALL,
    )
    assert guessed_target("suppliers", UPDATE, messages) == "requested_record_not_found"


def test_one_of_several_found_by_name_is_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал"},
            _envelope({"results": [S_REC, C_REC], "total": 2}),
        ),
    )
    assert guessed_target("suppliers", UPDATE, messages) == "one_of_several"


def test_a_record_read_alone_is_confirmed():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Медь"},
            _envelope({"results": [C_REC], "total": 1}),
        ),
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_records_picked_by_a_filter_are_not_a_guess():
    """Bulk work on a filtered list (status, amount) is the user's criterion."""
    messages = _turn(
        (
            "suppliers",
            {"action": "list", "filters": {"status": "active"}},
            _envelope({"items": [S_REC, C_REC], "total": 2}),
        ),
        request="Отметь всех активных поставщиков",
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_an_id_the_user_named_is_not_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        request=f"Обнови телефон поставщика {COPPER}",
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_a_read_from_an_earlier_turn_does_not_count():
    earlier = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
    )
    messages = earlier + [
        {"role": "assistant", "content": "Нет такого"},
        {"role": "user", "content": "Ок, меди"},
    ]
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_creates_and_reads_are_never_judged():
    messages = _turn(
        ("suppliers", {"action": "search", "query": "x"}, _envelope({"results": [], "total": 0})),
    )
    assert guessed_target("suppliers", {"action": "search", "query": "y"}, messages) is None
    assert (
        guessed_target("analytics", {"action": "calendar_create_reminder", "body": {}}, messages)
        is None
    )


def _request(text):
    return [{"role": "user", "content": text}]


def _update(**body):
    return {"action": "update", "supplier_id": COPPER, "body": body}


def test_a_field_the_request_never_named_is_reported():
    """E52: asked for the KPP, the employee also wrote the address."""
    from app.ai.agent_loop import unrequested_fields

    messages = _request(
        "Возьми из договора ДП-201 КПП ООО «Эвал-Сталь» и запиши его в карточку поставщика."
    )
    assert unrequested_fields(
        "suppliers", _update(kpp="770101001", address="Москва"), messages
    ) == ["address"]
    assert unrequested_fields("suppliers", _update(kpp="770101001"), messages) == []


def test_requisites_do_not_cover_the_bik_and_a_number_is_a_phone():
    from app.ai.agent_loop import unrequested_fields

    account = _request("Обнови расчётный счёт в карточке поставщика по письму.")
    assert unrequested_fields(
        "suppliers", _update(bank_account="4070", bank_bik="0445"), account
    ) == ["bank_bik"]
    # E52: "о смене реквизитов. Обнови расчётный счёт" — the BIK was not asked for.
    requisites = _request("Письмо о смене реквизитов. Обнови расчётный счёт ООО «Эвал-Медь».")
    assert unrequested_fields(
        "suppliers", _update(bank_account="4070", bank_bik="0445"), requisites
    ) == ["bank_bik"]
    phone = _request("Возьми из письма новый номер и обнови карточку.")
    assert unrequested_fields("suppliers", _update(contact_phone="+7"), phone) == []


def test_unlisted_fields_and_other_tools_are_not_judged():
    from app.ai.agent_loop import unrequested_fields

    messages = _request("Обнови карточку")
    assert unrequested_fields("suppliers", _update(notes="x"), messages) == []
    assert (
        unrequested_fields(
            "invoices", {"action": "update", "invoice_id": COPPER, "body": {"notes": "x"}}, messages
        )
        == []
    )


def test_a_yes_to_the_agents_proposal_names_its_fields():
    from app.ai.agent_loop import unrequested_fields

    messages = [
        {"role": "user", "content": "Что в письме Эвал-Меди?"},
        {
            "role": "assistant",
            "content": "В письме новый телефон +7 495 100-20-30. Записать его в карточку?",
        },
        {"role": "user", "content": "Да"},
    ]
    assert (
        unrequested_fields("suppliers", _update(contact_phone="+7 495 100-20-30"), messages) == []
    )
    assert unrequested_fields("suppliers", _update(address="Москва"), messages) == ["address"]


INV = "33333333-3333-3333-3333-333333333333"
NEAR = "44444444-4444-4444-4444-444444444444"
NOTE = {"action": "update", "invoice_id": INV, "body": {"notes": "x"}}


def test_the_exact_record_among_fuzzy_neighbours_is_confirmed():
    """E52 repeat: a search by number also returned EV-5310; EV-5301 is named."""
    messages = _turn(
        (
            "invoices",
            {"action": "list", "filters": {"invoice_number": "EV-5301"}},
            _envelope(
                {
                    "items": [
                        {"id": INV, "invoice_number": "EV-5301"},
                        {"id": NEAR, "invoice_number": "EV-5310"},
                    ]
                }
            ),
        ),
        request="Добавь заметку к счёту EV-5301",
    )
    assert guessed_target("invoices", NOTE, messages) is None


def test_a_lone_near_match_is_still_a_guess():
    messages = _turn(
        (
            "invoices",
            {"action": "list", "filters": {"invoice_number": "EV-9999"}},
            _envelope({"items": [{"id": INV, "invoice_number": "EV-9998"}]}),
        ),
        request="Добавь заметку к счёту EV-9999",
    )
    assert guessed_target("invoices", NOTE, messages) == "one_of_several"


def test_a_name_inside_a_nested_field_names_the_record():
    messages = _turn(
        (
            "invoices",
            {"action": "list", "filters": {"name": "Эвал-Медь"}},
            _envelope(
                {
                    "items": [
                        {"id": INV, "supplier": {"name": "ООО «Эвал-Медь»"}},
                        {"id": NEAR, "supplier": {"name": "ООО «Эвал-Сталь»"}},
                    ]
                }
            ),
        ),
    )
    assert guessed_target("invoices", NOTE, messages) is None


def test_a_record_chosen_by_a_filter_after_an_empty_lookup_is_confirmed():
    messages = _turn(
        ("invoices", {"action": "list", "filters": {"q": "утвержд"}}, _envelope({"items": []})),
        (
            "invoices",
            {"action": "list", "filters": {"status": "approved"}},
            _envelope({"items": [{"id": INV}, {"id": NEAR}]}),
        ),
        request="Добавь заметку ко всем утверждённым счетам",
    )
    assert guessed_target("invoices", NOTE, messages) is None


def test_a_reminder_or_link_pointing_at_a_near_match_is_a_guess():
    """E52 repeat: EV-9999 did not exist; reminders and links went to EV-9998."""
    messages = _turn(
        (
            "invoices",
            {"action": "list", "filters": {"invoice_number": "EV-9999"}},
            _envelope({"items": [{"id": INV, "invoice_number": "EV-9998"}]}),
        ),
        request="Напомни в понедельник про оплату счёта EV-9999",
    )
    reminder = {
        "action": "calendar_create_reminder",
        "body": {
            "remind_at": "2026-10-19T09:00:00+03:00",
            "message": "Оплата",
            "entity_type": "invoice",
            "entity_id": INV,
        },
    }
    assert guessed_target("analytics", reminder, messages) == "one_of_several"
    doc = "55555555-5555-5555-5555-555555555555"
    link = {
        "action": "link",
        "document_id": doc,
        "body": {"linked_entity_type": "invoice", "linked_entity_id": INV},
    }
    assert guessed_target("documents", link, messages) == "one_of_several"


def test_a_reminder_on_the_named_invoice_is_not_a_guess():
    messages = _turn(
        (
            "invoices",
            {"action": "list", "filters": {"invoice_number": "EV-5301"}},
            _envelope({"items": [{"id": INV, "invoice_number": "EV-5301"}]}),
        ),
        request="Напомни про оплату счёта EV-5301",
    )
    reminder = {
        "action": "calendar_create_reminder",
        "body": {"message": "x", "entity_type": "invoice", "entity_id": INV},
    }
    assert guessed_target("analytics", reminder, messages) is None


ITEM10 = "66666666-6666-6666-6666-666666666666"
ITEM12 = "77777777-7777-7777-7777-777777777777"
SHEETS = _envelope(
    {
        "items": [
            {"id": ITEM10, "name": "Лист 09Г2С 10 мм", "sku": "EV-LS-10"},
            {"id": ITEM12, "name": "Лист 09Г2С 12 мм", "sku": "EV-LS-12"},
        ]
    }
)
MIN_QTY = {"action": "update_item", "item_id": ITEM10, "body": {"min_qty": 20}}


def test_every_lookup_word_names_one_sheet_among_its_neighbours():
    """E52 targeted run: "09Г2С 10" also returned the 12 mm sheet."""
    messages = _turn(
        ("warehouse", {"action": "list_inventory", "search": "09Г2С 10"}, SHEETS),
        request="Поставь минимальный остаток 20 для листа 09Г2С 10 мм",
    )
    assert guessed_target("warehouse", MIN_QTY, messages) is None
    vague = _turn(
        ("warehouse", {"action": "list_inventory", "search": "09Г2С"}, SHEETS),
        request="Поставь минимум 20 для листа 09Г2С",
    )
    assert guessed_target("warehouse", MIN_QTY, vague) == "one_of_several"


def test_a_record_the_request_names_is_not_a_guess_after_an_empty_lookup():
    """A group request: one lookup found nothing, the items came from a list."""
    messages = _turn(
        (
            "warehouse",
            {"action": "list_inventory", "search": "листы 09Г2С"},
            _envelope({"items": []}),
        ),
        ("warehouse", {"action": "list_inventory"}, SHEETS),
        request="Перенеси лист 09Г2С 10 мм и лист 09Г2С 12 мм на стеллаж D-01",
    )
    assert guessed_target("warehouse", MIN_QTY, messages) is None


def test_a_set_the_user_asked_for_is_not_a_choice_among_look_alikes():
    """E52: "Пометь все утверждённые счета ООО «Эвал-Сталь»" — every match is meant."""
    invoices = _envelope(
        {
            "items": [
                {"id": INV, "invoice_number": "EV-3301", "supplier": {"name": "ООО «Эвал-Сталь»"}},
                {"id": NEAR, "invoice_number": "EV-3303", "supplier": {"name": "ООО «Эвал-Сталь»"}},
            ]
        }
    )
    lookup = ("invoices", {"action": "list", "filters": {"name": "Эвал-Сталь"}}, invoices)
    group = _turn(
        lookup, request="Пометь все утверждённые счета ООО «Эвал-Сталь» пометкой «готов к оплате»."
    )
    assert guessed_target("invoices", NOTE, group) is None
    single = _turn(lookup, request="Пометь счёт ООО «Эвал-Сталь» пометкой «готов к оплате».")
    assert guessed_target("invoices", NOTE, single) == "one_of_several"


def test_an_empty_lookup_does_not_judge_a_target_no_read_showed():
    """E52 catalog cases: the supplier came from resolve_supplier, a catalog
    search found nothing — the empty search says nothing about the supplier."""
    messages = _turn(
        (
            "tool_catalog",
            {"action": "search_positions", "query": "проволока 2 мм"},
            _envelope({"items": []}),
        ),
        request="В каталоге ООО «Эвал-Медь» найди медную проволоку 2 мм",
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def _reminder(message):
    return {
        "action": "calendar_create_reminder",
        "body": {"remind_at": "2026-12-01T09:00:00+03:00", "message": message},
    }


def test_an_unattached_reminder_on_an_ambiguous_number_is_a_guess():
    """T8h (E52): two suppliers have EV-5701; the reminder named it in text only."""
    twins = _envelope(
        {
            "items": [
                {"id": INV, "invoice_number": "EV-5701"},
                {"id": NEAR, "invoice_number": "EV-5701"},
            ]
        }
    )
    messages = _turn(
        ("invoices", {"action": "list", "filters": {"invoice_number": "EV-5701"}}, twins),
        request="Напомни мне в день срока проверить оплату счёта EV-5701.",
    )
    assert (
        guessed_target("analytics", _reminder("Проверить оплату счёта EV-5701"), messages)
        == "one_of_several"
    )


def test_an_unattached_reminder_on_a_missing_number_is_a_guess():
    near = _envelope({"items": [{"id": NEAR, "invoice_number": "EV-9998"}]})
    messages = _turn(
        ("invoices", {"action": "list", "filters": {"invoice_number": "EV-9999"}}, near),
        request="Напомни в понедельник про оплату счёта EV-9999.",
    )
    assert (
        guessed_target("analytics", _reminder("Оплата счёта EV-9999"), messages)
        == "requested_record_not_found"
    )


def test_an_unattached_reminder_on_a_unique_or_unlooked_number_passes():
    one = _envelope({"items": [{"id": INV, "invoice_number": "EV-5301"}]})
    messages = _turn(
        ("invoices", {"action": "list", "filters": {"invoice_number": "EV-5301"}}, one),
        request="Напомни про оплату счёта EV-5301.",
    )
    assert guessed_target("analytics", _reminder("Оплата счёта EV-5301"), messages) is None
    assert guessed_target("analytics", _reminder("Позвонить по ДП-77"), messages) is None


def test_a_number_the_request_names_on_two_records_is_still_ambiguous():
    """E52 T8 run: "EV-5701" named two invoices; a reminder went on each."""
    twins = _envelope(
        {
            "items": [
                {"id": INV, "invoice_number": "EV-5701"},
                {"id": NEAR, "invoice_number": "EV-5701"},
            ]
        }
    )
    messages = _turn(
        ("invoices", {"action": "list", "query": "EV-5701"}, twins),
        request="Напомни мне в день срока проверить оплату счёта EV-5701.",
    )
    reminder = {
        "action": "calendar_create_reminder",
        "body": {"message": "x", "entity_type": "invoice", "entity_id": INV},
    }
    assert guessed_target("analytics", reminder, messages) == "one_of_several"
