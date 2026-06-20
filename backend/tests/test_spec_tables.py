"""Spec tables end-to-end: SQL execution, full-data guarantee, API build/patch."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.db.models import (
    CanonicalItem,
    Document,
    InventoryItem,
    Invoice,
    InvoiceLine,
    Party,
)
from app.domain import table_spec as ts


@pytest_asyncio.fixture
async def seeded(db_session):
    """Three invoices from two suppliers; one contains ⌀5 mills."""
    romashka = Party(name="ООО Ромашка", inn="7701234567")
    lutik = Party(name="АО Лютик", inn="7707654321")
    db_session.add_all([romashka, lutik])
    await db_session.flush()

    canonical = CanonicalItem(
        name="Фреза концевая 5 мм", aliases=["фреза 5", "endmill 5mm"], unit="шт"
    )
    db_session.add(canonical)
    await db_session.flush()

    async def _invoice(number, supplier, date, total, tax, lines):
        doc = Document(
            file_name=f"{number}.pdf",
            file_hash=f"hash-{number}",
            file_size=1024,
            mime_type="application/pdf",
            storage_path=f"documents/{number}.pdf",
        )
        db_session.add(doc)
        await db_session.flush()
        inv = Invoice(
            document_id=doc.id, invoice_number=number,
            invoice_date=datetime(*date, tzinfo=UTC),
            supplier_id=supplier.id, total_amount=total, tax_amount=tax,
        )
        db_session.add(inv)
        await db_session.flush()
        for idx, (desc, qty, price, canon) in enumerate(lines, start=1):
            db_session.add(InvoiceLine(
                invoice_id=inv.id, line_number=idx, description=desc,
                quantity=qty, unit="шт", unit_price=price, amount=qty * price,
                canonical_item_id=canon.id if canon else None,
            ))
        await db_session.flush()
        return inv

    await _invoice("INV-001", romashka, (2026, 5, 10), 12000.0, 2000.0, [
        ("Фреза концевая ⌀5 мм Z4", 10, 800.0, canonical),
        ("Болт М8х40 DIN 933", 100, 40.0, None),
    ])
    await _invoice("INV-002", lutik, (2026, 5, 20), 5000.0, 833.33, [
        ("Фреза дисковая 50 мм", 2, 2500.0, None),
    ])
    await _invoice("INV-003", romashka, (2026, 6, 1), 900.0, 150.0, [
        ("Сверло 5 мм HSS", 30, 30.0, None),
    ])
    return {"romashka": romashka, "lutik": lutik}


def _user_spec() -> ts.TableSpec:
    """Точная спецификация из примера пользователя."""
    return ts.TableSpec(
        source="invoices",
        title="Счета",
        columns=[
            ts.ColumnSpec(field="supplier_name", header="Поставщик"),
            ts.ColumnSpec(field="invoice_number", header="Номер счета"),
            ts.ColumnSpec(field="invoice_date", header="Дата счета"),
            ts.ColumnSpec(field="items_list", header="Перечень товаров"),
            ts.ColumnSpec(field="total_amount", header="Общая сумма счета"),
        ],
        sort=[ts.SortSpec(field="invoice_date", dir="asc")],
    )


@pytest.mark.asyncio
async def test_execute_user_example_full_data(db_session, seeded):
    result = await ts.execute_spec(db_session, _user_spec())
    assert result.total == 3 and len(result.rows) == 3 and not result.truncated
    headers = [c["header"] for c in result.columns]
    assert headers == [
        "Поставщик", "Номер счета", "Дата счета", "Перечень товаров", "Общая сумма счета",
    ]
    first = result.rows[0]
    assert first["supplier_name"] == "ООО Ромашка"
    assert first["invoice_date"] == "10.05.2026"
    # Line items aggregated into one cell, newline-separated, in line order.
    items = first["items_list"]
    assert "Фреза концевая ⌀5 мм Z4 — 10 шт" in items
    assert "Болт М8х40 DIN 933 — 100 шт" in items
    assert items.index("Фреза") < items.index("Болт")
    assert "\n" in items


@pytest.mark.asyncio
async def test_smart_filter_mills_diameter_5(db_session, seeded):
    """«фрезы диаметра 5» finds the ⌀5 mill invoice, not the 50 mm one."""
    spec = _user_spec()
    spec.filters = [ts.FilterSpec(field="items_list", op="smart", value="фрезы диаметра 5")]
    result = await ts.execute_spec(db_session, spec)
    numbers = [r["invoice_number"] for r in result.rows]
    assert numbers == ["INV-001"]  # 50 мм ≠ 5; сверло — не фреза
    assert result.total == 1


@pytest.mark.asyncio
async def test_smart_filter_on_items_source(db_session, seeded):
    spec = ts.TableSpec(
        source="invoice_items",
        columns=[ts.ColumnSpec(field="description"), ts.ColumnSpec(field="supplier_name")],
        filters=[ts.FilterSpec(field="description", op="smart", value="фрезы")],
    )
    result = await ts.execute_spec(db_session, spec)
    descriptions = sorted(r["description"] for r in result.rows)
    assert descriptions == ["Фреза дисковая 50 мм", "Фреза концевая ⌀5 мм Z4"]


@pytest.mark.asyncio
async def test_sort_and_structured_filters(db_session, seeded):
    spec = _user_spec()
    spec.sort = [ts.SortSpec(field="total_amount", dir="desc")]
    spec.filters = [ts.FilterSpec(field="supplier_name", op="contains", value="ромашка")]
    result = await ts.execute_spec(db_session, spec)
    assert [r["invoice_number"] for r in result.rows] == ["INV-001", "INV-003"]
    assert result.total == 2


@pytest.mark.asyncio
async def test_api_build_then_patch_command(client, seeded):
    # 1. Построение по спецификации пользователя.
    resp = await client.post("/api/workspace/agent/spec-table", json={
        "canvas_id": "agent:spec-table",
        "spec": _user_spec().model_dump(mode="json"),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    assert data["total"] == 3 and data["shown"] == 3
    assert "полные данные" in data["message"]

    # 2. «добавь столбец с ндс перед суммой» — детерминированный патч.
    resp = await client.post("/api/workspace/agent/spec-table/patch", json={
        "canvas_id": "agent:spec-table",
        "command": "добавь столбец с ндс перед суммой",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "published"
    fields = [c["field"] for c in data["spec"]["columns"]]
    assert fields.index("tax_amount") == fields.index("total_amount") - 1

    # 3. Сортировка командой.
    resp = await client.post("/api/workspace/agent/spec-table/patch", json={
        "canvas_id": "agent:spec-table",
        "command": "отсортируй по сумме по убыванию",
    })
    assert resp.json()["spec"]["sort"] == [{"field": "total_amount", "dir": "desc"}]

    # 4. Smart-фильтр командой «покажи только…».
    resp = await client.post("/api/workspace/agent/spec-table/patch", json={
        "canvas_id": "agent:spec-table",
        "command": "покажи только фрезы диаметра 5",
    })
    data = resp.json()
    assert data["total"] == 1

    # 5. Блок на Рабочем столе обновлён и хранит spec.
    from app.domain.workspace import get_workspace_block
    block = get_workspace_block("agent:spec-table")
    assert block and block["spec"]["filters"][0]["op"] == "smart"
    assert block["total_rows"] == 1


@pytest.mark.asyncio
async def test_api_patch_unrecognized_command(client, seeded):
    await client.post("/api/workspace/agent/spec-table", json={
        "spec": _user_spec().model_dump(mode="json"),
    })
    resp = await client.post("/api/workspace/agent/spec-table/patch", json={
        "command": "сделай красиво",
    })
    assert resp.json()["status"] == "unrecognized"


@pytest.mark.asyncio
async def test_api_rejects_unknown_fields(client, seeded):
    resp = await client.post("/api/workspace/agent/spec-table", json={
        "spec": {"source": "invoices", "columns": [{"field": "password_hash"}]},
    })
    assert resp.json()["status"] == "error"
    assert "password_hash" in resp.json()["message"]


@pytest_asyncio.fixture
async def warehouse_seeded(db_session):
    canonical = CanonicalItem(
        name="Фреза концевая 5 мм", aliases=["фреза 5"], unit="шт"
    )
    db_session.add(canonical)
    await db_session.flush()
    db_session.add_all([
        InventoryItem(name="Фреза концевая ⌀5 мм Z4", sku="FR-5-Z4", unit="шт",
                      current_qty=3, min_qty=10, location="A-01",
                      canonical_item_id=canonical.id),
        InventoryItem(name="Фреза дисковая 50 мм", sku="FR-50-D", unit="шт",
                      current_qty=12, min_qty=2, location="A-02"),
        InventoryItem(name="Болт М8х40 DIN 933", sku="BLT-M8-40", unit="шт",
                      current_qty=500, min_qty=100, location="B-11"),
    ])
    await db_session.flush()


@pytest.mark.asyncio
async def test_warehouse_source_with_deficit(db_session, warehouse_seeded):
    spec = ts.TableSpec(
        source="warehouse",
        columns=[
            ts.ColumnSpec(field="name"),
            ts.ColumnSpec(field="current_qty"),
            ts.ColumnSpec(field="min_qty"),
            ts.ColumnSpec(field="below_min"),
        ],
        sort=[ts.SortSpec(field="name", dir="asc")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 3
    by_name = {r["name"]: r for r in result.rows}
    assert by_name["Фреза концевая ⌀5 мм Z4"]["below_min"] == "Да"  # 3 < 10
    assert by_name["Болт М8х40 DIN 933"]["below_min"] == "Нет"

    # «Только дефицит» — структурный фильтр по вычисляемому полю.
    spec.filters = [ts.FilterSpec(field="below_min", op="eq", value="Да")]
    result = await ts.execute_spec(db_session, spec)
    assert [r["name"] for r in result.rows] == ["Фреза концевая ⌀5 мм Z4"]


@pytest.mark.asyncio
async def test_warehouse_smart_filter(db_session, warehouse_seeded):
    """«фрезы диаметра 5» на складе: ⌀5 найдена, 50 мм и болты — нет."""
    spec = ts.TableSpec(
        source="warehouse",
        columns=[ts.ColumnSpec(field="name"), ts.ColumnSpec(field="current_qty")],
        filters=[ts.FilterSpec(field="name", op="smart", value="фрезы диаметра 5")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert [r["name"] for r in result.rows] == ["Фреза концевая ⌀5 мм Z4"]


def test_warehouse_nl_commands():
    spec = ts.TableSpec(
        source="warehouse",
        columns=[ts.ColumnSpec(field="name"), ts.ColumnSpec(field="current_qty")],
    )
    cmd = ts.parse_patch_command("добавь столбец место хранения после остатка", spec)
    assert cmd.ops[0].field == "location" and cmd.ops[0].after == "current_qty"
    cmd = ts.parse_patch_command("отсортируй по остатку", spec)
    assert cmd.ops[0].field == "current_qty"
    cmd = ts.parse_patch_command("покажи только фрезы", spec)
    assert cmd.ops[1].filter.op == "smart"


@pytest.mark.asyncio
async def test_documents_source(db_session, seeded):
    spec = ts.TableSpec(
        source="documents",
        columns=[
            ts.ColumnSpec(field="file_name"),
            ts.ColumnSpec(field="status"),
            ts.ColumnSpec(field="created_at"),
        ],
        sort=[ts.SortSpec(field="file_name", dir="asc")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 3  # три документа из seeded-счетов
    assert [r["file_name"] for r in result.rows] == [
        "INV-001.pdf", "INV-002.pdf", "INV-003.pdf",
    ]
    # Enum-статус сериализуется в строку.
    assert result.rows[0]["status"] == "ingested"


@pytest.mark.asyncio
async def test_payments_source_with_supplier_join(db_session, seeded):
    from datetime import UTC, datetime

    from sqlalchemy import select as sa_select

    from app.db.models import Invoice, PaymentSchedule

    invoice = (await db_session.execute(
        sa_select(Invoice).where(Invoice.invoice_number == "INV-001")
    )).scalar_one()
    db_session.add(PaymentSchedule(
        invoice_id=invoice.id, due_date=datetime(2026, 6, 20, tzinfo=UTC),
        amount=12000.0, status="overdue",
    ))
    await db_session.flush()

    spec = ts.TableSpec(
        source="payments",
        columns=[
            ts.ColumnSpec(field="supplier_name"),
            ts.ColumnSpec(field="invoice_number"),
            ts.ColumnSpec(field="due_date"),
            ts.ColumnSpec(field="amount"),
            ts.ColumnSpec(field="status"),
        ],
        filters=[ts.FilterSpec(field="status", op="eq", value="overdue")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 1
    row = result.rows[0]
    assert row["supplier_name"] == "ООО Ромашка"
    assert row["invoice_number"] == "INV-001"
    assert row["due_date"] == "20.06.2026"


@pytest.mark.asyncio
async def test_emails_source_computed_fields(db_session):
    from datetime import UTC, datetime

    from app.db.models import EmailMessage

    db_session.add_all([
        EmailMessage(mailbox="procurement", from_address="supplier@romashka.ru",
                     subject="Счёт на фрезы", has_attachments=True,
                     attachment_count=2, is_inbound=True,
                     received_at=datetime(2026, 6, 10, tzinfo=UTC)),
        EmailMessage(mailbox="procurement", from_address="buyer@ptsai.ru",
                     subject="Запрос КП", has_attachments=False,
                     attachment_count=0, is_inbound=False),
    ])
    await db_session.flush()

    spec = ts.TableSpec(
        source="emails",
        columns=[
            ts.ColumnSpec(field="subject"),
            ts.ColumnSpec(field="has_attachments"),
            ts.ColumnSpec(field="direction"),
        ],
        sort=[ts.SortSpec(field="subject", dir="asc")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 2
    by_subject = {r["subject"]: r for r in result.rows}
    assert by_subject["Счёт на фрезы"]["has_attachments"] == "Да"
    assert by_subject["Счёт на фрезы"]["direction"] == "Входящее"
    assert by_subject["Запрос КП"]["direction"] == "Исходящее"

    # Smart-фильтр по теме.
    spec.filters = [ts.FilterSpec(field="subject", op="smart", value="фрезы")]
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 1


@pytest.mark.asyncio
async def test_drawings_source_json_title_block(db_session):
    from app.db.models import Drawing

    db_session.add(Drawing(
        drawing_number="АБВГ.123456.001", revision="А",
        filename="val_privoda.dxf", format="dxf",
        drawing_type="detail", part_class="shaft",
        title_block={"title": "Вал привода", "material": "Сталь 40Х"},
    ))
    await db_session.flush()

    spec = ts.TableSpec(
        source="drawings",
        columns=[
            ts.ColumnSpec(field="drawing_number"),
            ts.ColumnSpec(field="title"),
            ts.ColumnSpec(field="material"),
            ts.ColumnSpec(field="status"),
        ],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 1
    row = result.rows[0]
    assert row["title"] == "Вал привода"
    assert row["material"] == "Сталь 40Х"
    assert row["status"] == "uploaded"


@pytest.mark.asyncio
async def test_anomalies_source(db_session):
    import uuid as uuid_module

    from app.db.models import AnomalyCard, AnomalySeverity, AnomalyStatus, AnomalyType

    db_session.add(AnomalyCard(
        anomaly_type=AnomalyType.price_spike, severity=AnomalySeverity.critical,
        status=AnomalyStatus.open, entity_type="invoice",
        entity_id=uuid_module.uuid4(), title="Скачок цены на фрезы +40%",
    ))
    await db_session.flush()

    spec = ts.TableSpec(
        source="anomalies",
        columns=[
            ts.ColumnSpec(field="title"),
            ts.ColumnSpec(field="severity"),
            ts.ColumnSpec(field="status"),
        ],
        filters=[ts.FilterSpec(field="status", op="eq", value="open")],
    )
    result = await ts.execute_spec(db_session, spec)
    assert result.total == 1
    assert result.rows[0]["severity"] == "critical"


def test_nl_commands_on_new_sources():
    payments = ts.TableSpec(
        source="payments",
        columns=[ts.ColumnSpec(field="supplier_name"), ts.ColumnSpec(field="amount")],
    )
    cmd = ts.parse_patch_command("добавь столбец срок оплаты перед суммой", payments)
    assert cmd.ops[0].field == "due_date" and cmd.ops[0].before == "amount"

    docs = ts.TableSpec(
        source="documents",
        columns=[ts.ColumnSpec(field="file_name"), ts.ColumnSpec(field="created_at")],
    )
    cmd = ts.parse_patch_command("отсортируй по дате загрузки по убыванию", docs)
    assert cmd.ops[0].field == "created_at" and cmd.ops[0].dir == "desc"

    drawings = ts.TableSpec(
        source="drawings",
        columns=[ts.ColumnSpec(field="drawing_number"), ts.ColumnSpec(field="title")],
    )
    cmd = ts.parse_patch_command("добавь столбец материал после наименования", drawings)
    assert cmd.ops[0].field == "material" and cmd.ops[0].after == "title"


@pytest.mark.asyncio
async def test_catalog_endpoint(client):
    resp = await client.get("/api/workspace/agent/spec-table/catalog")
    assert resp.status_code == 200
    catalog = resp.json()
    assert "group_by" in catalog["_spec_format"]  # grouping is discoverable
    sources = catalog["sources"]
    assert "invoices" in sources
    keys = [f["key"] for f in sources["invoices"]["fields"]]
    assert "items_list" in keys and "tax_amount" in keys


# ── parse_patch_command: multi-item "оставь только X и Y" ─────────────────────


def test_only_multi_item_creates_separate_contains():
    """'оставь только резцы и пластины' → clear_filters + two contains ops (OR semantics)."""
    spec = ts.TableSpec(
        source="invoice_items",
        columns=[ts.ColumnSpec(field="item_name"), ts.ColumnSpec(field="quantity")],
    )
    cmd = ts.parse_patch_command("оставь только резцы и пластины", spec)
    assert cmd is not None
    ops = cmd.ops
    assert ops[0].op == "clear_filters"
    filter_ops = [o for o in ops if o.op == "add_filter"]
    assert len(filter_ops) == 2
    values = {o.filter.value for o in filter_ops}
    # Stems: "резц" and "пластин" (or similar prefix)
    assert all(o.filter.op == "contains" for o in filter_ops)
    # For invoice_items source the default filter target is "description"
    assert len({o.filter.field for o in filter_ops}) == 1  # consistent field
    # Both stems must be present and non-empty
    assert all(len(v) >= 3 for v in values)


def test_group_by_validation():
    """group_by accepts real fields, rejects unknown ones."""
    ok = ts.TableSpec(source="invoice_items", group_by=["supplier_name"])
    assert ts.validate_spec(ok) == []
    bad = ts.TableSpec(source="invoice_items", group_by=["nonsense_field"])
    probs = ts.validate_spec(bad)
    assert any("group_by" in p for p in probs)


def test_filter_op_like_alias_maps_to_contains():
    """Models reaching for SQL 'like'/'ilike' must not silently match nothing."""
    assert ts.FilterSpec(field="description", op="ilike", value="фрез").op == "contains"
    assert ts.FilterSpec(field="description", op="LIKE", value="фрез").op == "contains"


def test_reconcile_ops_enforces_grouping_and_sort():
    """A worker spec lacking group/sort the user asked for is reconciled."""
    spec = ts.TableSpec(
        source="invoice_items",
        columns=[ts.ColumnSpec(field="description"), ts.ColumnSpec(field="supplier_name")],
        filters=[ts.FilterSpec(field="description", op="contains", value="фрез")],
    )
    ops, notes = ts.reconcile_ops(
        spec,
        "Выведи все фрезы и резцы, отсортируй по дате по убыванию и объедини по поставщикам",
    )
    by_op = {o.op: o for o in ops}
    assert "set_group_by" in by_op and by_op["set_group_by"].field == "supplier_name"
    assert "set_sort" in by_op and by_op["set_sort"].field == "invoice_date"
    assert by_op["set_sort"].dir == "desc"
    # Applying them yields a grouped, date-sorted spec.
    patched = ts.apply_patch(spec, ops)
    assert patched.group_by == ["supplier_name"]


def test_reconcile_ops_idempotent_when_already_satisfied():
    spec = ts.TableSpec(
        source="invoice_items",
        group_by=["supplier_name"],
        sort=[ts.SortSpec(field="invoice_date", dir="desc")],
    )
    ops, _ = ts.reconcile_ops(spec, "объедини по поставщикам, сортируй по дате по убыванию")
    assert ops == []


def test_only_single_item_keeps_smart_filter():
    """'оставь только фрезы' (no 'и') → single smart filter, not contains."""
    spec = ts.TableSpec(
        source="invoice_items",
        columns=[ts.ColumnSpec(field="item_name")],
    )
    cmd = ts.parse_patch_command("оставь только фрезы", spec)
    assert cmd is not None
    filter_ops = [o for o in cmd.ops if o.op == "add_filter"]
    assert len(filter_ops) == 1
    assert filter_ops[0].filter.op == "smart"


# ── parse_patch_command: "и X" filter-add continuation ────────────────────────


def test_and_filter_adds_without_clearing():
    """'и пластины' → add_filter without clear_filters (additive)."""
    spec = ts.TableSpec(
        source="invoice_items",
        columns=[ts.ColumnSpec(field="item_name")],
        filters=[ts.FilterSpec(field="item_name", op="contains", value="резц")],
    )
    cmd = ts.parse_patch_command("и пластины", spec)
    assert cmd is not None
    assert not any(o.op == "clear_filters" for o in cmd.ops)
    filter_ops = [o for o in cmd.ops if o.op == "add_filter"]
    assert len(filter_ops) >= 1
    assert all(o.filter.op == "contains" for o in filter_ops)
    assert all(len(o.filter.value) >= 3 for o in filter_ops)


def test_and_filter_also_pattern():
    """'а также сверла' → add_filter."""
    spec = ts.TableSpec(source="invoice_items", columns=[ts.ColumnSpec(field="item_name")])
    cmd = ts.parse_patch_command("а также сверла", spec)
    assert cmd is not None
    assert any(o.op == "add_filter" for o in cmd.ops)


def test_and_filter_question_word_ignored():
    """'и почему так' must not match the filter-add pattern."""
    spec = ts.TableSpec(source="invoice_items", columns=[ts.ColumnSpec(field="item_name")])
    cmd = ts.parse_patch_command("и почему так", spec)
    assert cmd is None
