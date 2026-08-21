"""Э6: comparing an invoice line against the supplier's catalog.

The failure this guards against is a comparison that lies confidently: catalogs
quote net prices per pack, invoices gross prices per unit, so an unnormalised
check reports every line as a 20%+ overpayment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.catalog_price_check import compare_line, match_score, normalize_catalog_price


def test_net_catalog_price_matches_gross_invoice_price():
    result = compare_line(
        description="Фреза концевая Ø8",
        invoice_unit_price=120.0,
        invoice_includes_vat=True,
        catalog_price=100.0,
        catalog_name="Фреза концевая 8 мм",
        catalog_entry_id="e1",
        includes_vat=False,
        vat_rate=0.20,
        pack_size=None,
    )
    assert result.verdict == "ok", "VAT difference must not read as an overpayment"
    assert result.difference_pct == 0.0


def test_pack_price_is_divided_before_comparison():
    result = compare_line(
        description="Пластина сменная",
        invoice_unit_price=60.0,
        invoice_includes_vat=False,
        catalog_price=500.0,  # per pack of 10
        catalog_name="Пластина",
        catalog_entry_id="e2",
        includes_vat=False,
        vat_rate=0.0,
        pack_size=10,
    )
    assert result.pack_normalized is True
    assert result.catalog_price == 50.0
    assert result.verdict == "above_catalog"


def test_unknown_vat_basis_is_flagged_not_hidden():
    result = compare_line(
        description="Сверло",
        invoice_unit_price=100.0,
        invoice_includes_vat=True,
        catalog_price=100.0,
        catalog_name="Сверло",
        catalog_entry_id="e3",
        includes_vat=None,
        vat_rate=None,
        pack_size=None,
    )
    assert result.vat_assumed is True
    assert any("НДС" in note for note in result.notes)


def test_expired_price_list_is_reported():
    result = compare_line(
        description="Метчик",
        invoice_unit_price=100.0,
        invoice_includes_vat=False,
        catalog_price=100.0,
        catalog_name="Метчик",
        catalog_entry_id="e4",
        includes_vat=False,
        vat_rate=0.0,
        pack_size=None,
        valid_until=datetime.now(UTC) - timedelta(days=30),
    )
    assert result.catalog_stale_days is not None
    assert any("истёк" in note for note in result.notes)


def test_missing_price_is_unknown_not_zero():
    result = compare_line(
        description="Позиция без цены",
        invoice_unit_price=None,
        invoice_includes_vat=True,
        catalog_price=100.0,
        catalog_name="X",
        catalog_entry_id="e5",
        includes_vat=False,
        vat_rate=0.2,
        pack_size=None,
    )
    assert result.verdict == "unknown"


def test_part_number_match_beats_word_overlap():
    assert match_score("Фреза MT-E2-12 концевая", "Совсем другое", "MT-E2-12") == 1.0
    assert match_score("Фреза концевая", "Сверло спиральное", None) < 0.5


def test_normalize_accepts_percentage_style_vat_rate():
    price, assumed, packed = normalize_catalog_price(
        100.0, includes_vat=False, vat_rate=20, pack_size=None, target_includes_vat=True
    )
    assert round(price, 2) == 120.0
    assert assumed is False and packed is False


# ── Э6: canonical matching ──────────────────────────────────────────────────


def test_strong_name_match_is_automatic():
    from app.domain.canonical_matching import best_match

    match = best_match(
        "Фреза концевая Ø12 твердосплавная",
        [("1", "Фреза концевая", None), ("2", "Сверло спиральное", ["сверло"])],
    )
    assert match.decision == "auto"
    assert match.canonical_item_id == "1"


def test_alias_is_considered():
    from app.domain.canonical_matching import best_match

    match = best_match("Сверло Ø6", [("2", "Сверло спиральное по металлу", ["сверло"])])
    assert match.canonical_item_id == "2"


def test_unrelated_item_is_left_unmapped():
    from app.domain.canonical_matching import best_match

    match = best_match(
        "Ящик для инструмента", [("1", "Фреза концевая", None), ("2", "Сверло", None)]
    )
    assert match.decision == "none"
    assert match.canonical_item_id is None


def test_middling_match_goes_to_review_not_auto():
    from app.domain.canonical_matching import best_match

    match = best_match("Фреза дисковая отрезная 63х2", [("1", "Фреза концевая 12", None)])
    assert match.decision in {"review", "none"}
    assert match.decision != "auto"
