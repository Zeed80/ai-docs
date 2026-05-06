"""Shared presentation formatting for workspace and exports."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

MONEY_KEYS = frozenset({
    "amount",
    "total_amount",
    "subtotal",
    "subtotal_amount",
    "tax_amount",
    "unit_price",
    "line_total",
    "paid_amount",
    "open_invoices_amount",
})


def is_money_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in MONEY_KEYS
        or normalized.endswith("_amount")
        or normalized.endswith("_price")
    )


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def format_money(value: Any, *, suffix: str = "") -> str:
    number = to_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    q = number.quantize(Decimal("0.01"))
    text = f"{q:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text}{suffix}"


def format_number(value: Any) -> str:
    number = to_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    text = f"{number:,.4f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",")
