"""Catalog rows must survive a price list that looks like a real one.

Live measurement (2026-08-21) on a two-row CSV with columns
«Наименование, Артикул, Цена»: `created=0 skipped=2`. The task reported
success, the supplier card stayed empty. Cause: the ingest required a
`tool_type` value per row, and supplier price lists essentially never carry a
"тип инструмента" column.
"""

from __future__ import annotations

import pytest

from app.tasks.drawing_analysis import _infer_tool_type, _normalize_header


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Фреза концевая Ø12 твердосплавная", "endmill"),
        ("Концевая фреза 6 мм, 4 зуба", "endmill"),
        ("Фреза дисковая 63х5", "milling_cutter"),
        ("Фреза торцовая 45°", "milling_cutter"),
        ("Сверло спиральное Ø5 HSS-Co", "drill"),
        ("Метчик М8х1.25", "tap"),
        ("Развёртка машинная 10H7", "reamer"),
        ("Пластина сменная CNMG 120408", "insert"),
        ("Резец проходной упорный 25х25", "turning_tool"),
        ("Оправка для торцевых фрез", "holder"),
        ("Патрон цанговый ER32", "holder"),
        ("Резьбофреза М6", "thread_mill"),
        ("Круг шлифовальный 200х20", "grinder"),
    ],
)
def test_tool_type_is_inferred_from_the_item_name(name, expected):
    assert _infer_tool_type(name) == expected


def test_specific_marker_wins_over_generic_one():
    """«фреза концевая» must not be swallowed by the generic «фреза»."""
    assert _infer_tool_type("Фреза концевая 10") == "endmill"
    assert _infer_tool_type("Фреза дисковая 80") == "milling_cutter"


def test_unknown_name_yields_none_so_caller_can_fall_back_to_other():
    assert _infer_tool_type("Ящик инструментальный 5 секций") is None
    assert _infer_tool_type("") is None
    assert _infer_tool_type(None) is None


def test_headers_of_a_typical_russian_price_list_are_recognised():
    assert _normalize_header("Наименование") == "name"
    assert _normalize_header("Артикул") == "part_number"
    assert _normalize_header("Цена") == "price"


@pytest.mark.asyncio
async def test_rows_without_tool_type_column_are_kept(db_session):
    """The exact live case: three-column price list, no type column at all."""
    from sqlalchemy import select

    from app.db.models import ToolCatalogEntry, ToolSupplier
    from app.tasks.drawing_analysis import _create_catalog_entries_from_rows

    supplier = ToolSupplier(name="ООО Прайс Без Типа", is_active=True)
    db_session.add(supplier)
    await db_session.commit()

    rows = [
        {"name": "Фреза концевая D12", "part_number": "MT-12", "price": 1500.0},
        {"name": "Сверло D5 HSS", "part_number": "DR-5", "price": 300.0},
        {"name": "Ящик инструментальный", "part_number": "BOX-1", "price": 900.0},
        {"part_number": "NO-NAME", "price": 10.0},  # no name — legitimately skipped
    ]
    result = await _create_catalog_entries_from_rows(db_session, supplier.id, rows)
    await db_session.commit()

    assert result["created"] == 3, result
    assert result["skipped"] == 1
    assert result["skipped_by_reason"] == {"no_name": 1}

    entries = (
        await db_session.execute(
            select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id)
        )
    ).scalars().all()
    by_part = {e.part_number: e for e in entries}
    assert by_part["MT-12"].tool_type.value == "endmill"
    assert by_part["DR-5"].tool_type.value == "drill"
    # Unrecognised item is stored as "other", never dropped.
    assert by_part["BOX-1"].tool_type.value == "other"


# ── File formats ────────────────────────────────────────────────────────────


def _xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    """Build a real .xlsx in memory."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_header_is_found_below_a_banner():
    """Real price lists open with a company banner and a date, not a header."""
    from app.tasks.drawing_analysis import _parse_excel_catalog

    data = _xlsx_bytes({
        "Прайс": [
            ["ООО «Ромашка» — прайс-лист"],
            ["от 01.08.2026, цены без НДС"],
            [],
            ["Наименование", "Артикул", "Цена"],
            ["Фреза концевая D12", "MT-12", 1500],
            ["Сверло D5", "DR-5", 300],
        ]
    })
    rows = _parse_excel_catalog(data)
    assert len(rows) == 2, rows
    assert rows[0]["name"] == "Фреза концевая D12"
    assert rows[0]["part_number"] == "MT-12"


def test_excel_reads_every_sheet():
    from app.tasks.drawing_analysis import _parse_excel_catalog

    data = _xlsx_bytes({
        "Фрезы": [["Наименование", "Артикул", "Цена"], ["Фреза D10", "F-10", 900]],
        "Свёрла": [["Наименование", "Артикул", "Цена"], ["Сверло D8", "S-8", 400]],
    })
    rows = _parse_excel_catalog(data)
    assert {r["part_number"] for r in rows} == {"F-10", "S-8"}
    assert {r["_sheet"] for r in rows} == {"Фрезы", "Свёрла"}


def test_excel_category_separator_is_attached_to_following_rows():
    """A lone cell is a category header — often the only type hint in the file."""
    from app.tasks.drawing_analysis import _parse_excel_catalog

    data = _xlsx_bytes({
        "Прайс": [
            ["Наименование", "Артикул", "Цена"],
            ["Фрезы концевые"],
            ["D12 четырёхзубая", "MT-12", 1500],
        ]
    })
    rows = _parse_excel_catalog(data)
    assert len(rows) == 1
    assert rows[0]["_category"] == "Фрезы концевые"


def test_header_detection_refuses_a_banner_only_sheet():
    """Without a recognisable header the parser must yield nothing, not treat
    the banner as column names."""
    from app.tasks.drawing_analysis import _find_header_row

    idx, headers = _find_header_row([("ООО «Ромашка»",), ("от 01.08.2026",)])
    assert idx == -1 and headers == []


@pytest.mark.asyncio
async def test_unknown_format_falls_back_to_generic_parser(monkeypatch):
    """A .txt price list must not be silently ignored."""
    from app.tasks import drawing_analysis as da

    async def fake_llm(text, **kwargs):
        assert "Фреза" in text
        return [{"name": "Фреза концевая D12", "part_number": "MT-12"}]

    monkeypatch.setattr(da, "_parse_catalog_text_via_llm", fake_llm)
    payload = ("Прайс-лист ООО Ромашка\nФреза концевая D12 — 1500 руб\n" * 3).encode()
    rows = await da._parse_catalog_file(payload, ".txt", "price.txt")
    assert rows and rows[0]["part_number"] == "MT-12"


@pytest.mark.asyncio
async def test_pdf_without_tables_falls_back_to_text_extraction(monkeypatch):
    """PDF with free layout yielded zero rows before — no text fallback existed."""
    from app.tasks import drawing_analysis as da

    called = {}

    async def fake_llm(text, **kwargs):
        called["text"] = text
        return [{"name": "Пластина CNMG", "part_number": "CN-1"}]

    class _Page:
        def extract_tables(self):
            return []

        def extract_text(self):
            return "Пластина CNMG 120408 — 450 руб"

    class _Pdf:
        pages = [_Page(), _Page()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(da, "_parse_catalog_text_via_llm", fake_llm)
    monkeypatch.setitem(__import__("sys").modules, "pdfplumber", type("M", (), {"open": staticmethod(lambda *_a, **_k: _Pdf())}))

    rows = await da._parse_pdf_catalog(b"%PDF-1.4 fake")
    assert rows and rows[0]["part_number"] == "CN-1"
    assert "CNMG" in called["text"]
