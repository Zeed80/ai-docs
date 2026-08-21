"""Comparing invoice lines against the supplier's own catalog.

The trap this module exists to avoid: catalogs normally quote a price per PACK,
EXCLUDING VAT, while an invoice line quotes per UNIT, usually INCLUDING VAT. A
naive comparison then reports a 20-45% "overpayment" on every single line —
worse than no comparison at all, because people stop trusting the real ones.

Everything here is fail-open on unknowns: when the catalog does not say whether
its price includes VAT, the result carries `vat_assumed=True` and the caller
must show that rather than silently normalising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_VAT_RATE = 0.20
STALE_AFTER_DAYS = 180


@dataclass
class PriceComparisonResult:
    line_description: str | None
    invoice_price: float | None
    catalog_price: float | None
    catalog_entry_id: str | None = None
    catalog_name: str | None = None
    difference_pct: float | None = None
    verdict: str = "unknown"  # ok | above_catalog | below_catalog | unknown
    vat_assumed: bool = False
    pack_normalized: bool = False
    catalog_stale_days: int | None = None
    notes: list[str] = field(default_factory=list)


def normalize_catalog_price(
    price: float,
    *,
    includes_vat: bool | None,
    vat_rate: float | None,
    pack_size: float | None,
    target_includes_vat: bool,
) -> tuple[float, bool, bool]:
    """Bring a catalog price to per-unit and to the invoice's VAT basis.

    Returns (price, vat_assumed, pack_normalized).
    """
    vat_assumed = False
    pack_normalized = False

    if pack_size and pack_size > 1:
        price = price / pack_size
        pack_normalized = True

    if includes_vat is None:
        # Russian tool catalogs quote net prices far more often than gross, but
        # this is an assumption and must travel with the number.
        includes_vat = False
        vat_assumed = True

    rate = vat_rate if vat_rate is not None else DEFAULT_VAT_RATE
    if rate > 1:  # given as a percentage
        rate = rate / 100

    if target_includes_vat and not includes_vat:
        price = price * (1 + rate)
    elif not target_includes_vat and includes_vat:
        price = price / (1 + rate)
    return price, vat_assumed, pack_normalized


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", lowered).strip()


def match_score(line_text: str, entry_name: str, part_number: str | None) -> float:
    """Cheap lexical score; the vector match lives in the API layer."""
    line_norm = normalize_text(line_text)
    if not line_norm:
        return 0.0
    if part_number and normalize_text(part_number) and normalize_text(part_number) in line_norm:
        return 1.0
    line_tokens = set(line_norm.split())
    entry_tokens = set(normalize_text(entry_name).split())
    if not entry_tokens:
        return 0.0
    overlap = len(line_tokens & entry_tokens)
    return overlap / max(len(entry_tokens), 1)


def compare_line(
    *,
    description: str | None,
    invoice_unit_price: float | None,
    invoice_includes_vat: bool,
    catalog_price: float | None,
    catalog_name: str | None,
    catalog_entry_id: str | None,
    includes_vat: bool | None,
    vat_rate: float | None,
    pack_size: float | None,
    valid_until: datetime | None = None,
    catalog_recorded_at: datetime | None = None,
    tolerance_pct: float = 5.0,
) -> PriceComparisonResult:
    result = PriceComparisonResult(
        line_description=description,
        invoice_price=invoice_unit_price,
        catalog_price=catalog_price,
        catalog_entry_id=catalog_entry_id,
        catalog_name=catalog_name,
    )
    if not invoice_unit_price or not catalog_price:
        result.notes.append("нет цены для сравнения")
        return result

    normalized, vat_assumed, pack_normalized = normalize_catalog_price(
        catalog_price,
        includes_vat=includes_vat,
        vat_rate=vat_rate,
        pack_size=pack_size,
        target_includes_vat=invoice_includes_vat,
    )
    result.catalog_price = round(normalized, 4)
    result.vat_assumed = vat_assumed
    result.pack_normalized = pack_normalized
    if vat_assumed:
        result.notes.append("в каталоге не указано, включён ли НДС — принято «без НДС»")
    if pack_normalized:
        result.notes.append("цена каталога пересчитана на единицу из упаковки")

    now = datetime.now(UTC)
    reference = valid_until or catalog_recorded_at
    if reference is not None:
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        age_days = (now - reference).days
        if valid_until is not None and now > reference:
            result.catalog_stale_days = age_days
            result.notes.append(f"срок действия прайса истёк {age_days} дн. назад")
        elif age_days > STALE_AFTER_DAYS:
            result.catalog_stale_days = age_days
            result.notes.append(f"прайсу {age_days} дн. — цена может быть неактуальна")

    diff_pct = (invoice_unit_price - normalized) / normalized * 100
    result.difference_pct = round(diff_pct, 2)
    if abs(diff_pct) <= tolerance_pct:
        result.verdict = "ok"
    elif diff_pct > 0:
        result.verdict = "above_catalog"
    else:
        result.verdict = "below_catalog"
    return result
