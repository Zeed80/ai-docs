"""Named sheet templates — ready-made spreadsheet layouts with formulas.

Each template is a column schema (with computed columns) plus optional seed
rows, so the agent (or user) can instantiate a useful sheet in one step instead
of building it column by column. Formulas use the same engine as ad-hoc sheets
(column-key or A1 references; SUM/ROUND/IF/…).
"""

from __future__ import annotations

from typing import Any


def _col(key: str, header: str, type_: str = "text",
         formula: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"key": key, "header": header, "type": type_,
                         "editable": True}
    if formula:
        c["formula"] = formula
    return c


SHEET_TEMPLATES: dict[str, dict[str, Any]] = {
    "estimate": {
        "title": "Смета",
        "synonyms": ("смета", "расчёт стоимости", "калькуляция"),
        "columns": [
            _col("item", "Позиция"),
            _col("quantity", "Кол-во", "number"),
            _col("unit", "Ед."),
            _col("price", "Цена", "number"),
            _col("amount", "Сумма", "number", "quantity*price"),
            _col("vat", "НДС 20%", "number", "ROUND(amount*0.2, 2)"),
            _col("total", "Итого с НДС", "number", "amount+vat"),
        ],
        "rows": [{}, {}, {}],
    },
    "price_comparison": {
        "title": "Сравнение цен",
        "synonyms": ("сравнение цен", "сравнить цены", "тендер", "выбор поставщика"),
        "columns": [
            _col("item", "Товар"),
            _col("supplier", "Поставщик"),
            _col("quantity", "Кол-во", "number"),
            _col("price", "Цена за ед.", "number"),
            _col("amount", "Сумма", "number", "quantity*price"),
        ],
        "rows": [{}, {}, {}],
    },
    "budget": {
        "title": "Бюджет (план/факт)",
        "synonyms": ("бюджет", "план факт", "план/факт", "отклонения"),
        "columns": [
            _col("article", "Статья"),
            _col("plan", "План", "number"),
            _col("fact", "Факт", "number"),
            _col("deviation", "Отклонение", "number", "fact-plan"),
            _col("pct", "Откл. %", "number", "ROUND(IF(plan, (fact-plan)/plan*100, 0), 1)"),
        ],
        "rows": [{}, {}, {}],
    },
    "payment_schedule": {
        "title": "График платежей",
        "synonyms": ("график платежей", "платежи", "рассрочка"),
        "columns": [
            _col("supplier", "Поставщик"),
            _col("invoice", "Счёт"),
            _col("due_date", "Срок оплаты", "date"),
            _col("amount", "Сумма", "number"),
            _col("paid", "Оплачено", "number"),
            _col("balance", "Остаток", "number", "amount-paid"),
        ],
        "rows": [{}, {}, {}],
    },
}


def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "title": tpl["title"],
            "columns": [c["header"] for c in tpl["columns"]],
        }
        for key, tpl in SHEET_TEMPLATES.items()
    ]


def resolve_template(name: str) -> dict[str, Any] | None:
    """Find a template by key or a synonym (case-insensitive)."""
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in SHEET_TEMPLATES:
        return SHEET_TEMPLATES[n]
    for tpl in SHEET_TEMPLATES.values():
        if n == tpl["title"].lower() or any(n == s for s in tpl.get("synonyms", ())):
            return tpl
    # Loose contains match as a fallback.
    for tpl in SHEET_TEMPLATES.values():
        if any(s in n or n in s for s in tpl.get("synonyms", ())):
            return tpl
    return None
