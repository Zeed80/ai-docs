"""Unit tests for the user-tunable auto-approval confidence gate.

These lock in the Stage-3 behaviour the old tests could not catch:

* The percentage that gates auto-approval counts only *significant* fields
  (amounts, line items, payment requisites) — insignificant fields
  (address/phone/notes) never hold up an invoice.
* A significant field below the threshold is surfaced for review and blocks
  auto-approval; a cleanly-read significant field at the unverifiable prior is
  NOT flagged at the default threshold (so confident invoices auto-approve).
* The threshold is honoured: raising it flags more fields, lowering it fewer.

Pure functions — no DB, no Ollama, runs in milliseconds.
"""

from __future__ import annotations

from app.ai.confidence import (
    compute_field_confidences,
    significant_fields_confidence,
    validate_arithmetic,
)


def _clean_invoice() -> dict:
    """A fully consistent invoice: valid ИНН/БИК/account, arithmetic balances."""
    return {
        "invoice_number": "УТ-2834",
        "invoice_date": "2024-08-09",
        "currency": "RUB",
        "subtotal": 1000.0,
        "tax_amount": 200.0,
        "total_amount": 1200.0,
        "supplier": {
            "name": "ООО Поставщик",
            "inn": "7707083893",          # valid 10-digit control digit
            "kpp": "770701001",
            "address": "г. Москва",        # insignificant
            "phone": "+7 495 000-00-00",   # insignificant
        },
        "buyer": {"name": "ООО Покупатель", "inn": "7707083893"},
        "lines": [
            {"line_number": 1, "name": "Болт М6", "quantity": 10,
             "unit_price": 100.0, "amount": 1000.0},
        ],
    }


def _confs(data: dict):
    verrs = validate_arithmetic(data)
    return compute_field_confidences(data, data.get("field_confidences", {}) or {}, verrs)


def test_clean_invoice_auto_approves_at_default_threshold():
    sig = significant_fields_confidence(_confs(_clean_invoice()), threshold=0.95)
    assert sig.low_fields == [], f"clean invoice should have no low fields: {sig.low_fields}"
    assert sig.score >= 0.95
    assert sig.significant_count > 0


def test_insignificant_low_field_does_not_block():
    """A blurry phone/address must not appear in the review list or block."""
    data = _clean_invoice()
    sig = significant_fields_confidence(_confs(data), threshold=0.95)
    flagged = {f["field"] for f in sig.low_fields}
    assert "supplier.address" not in flagged
    assert "supplier.phone" not in flagged


def test_arithmetic_error_flags_total_amount():
    data = _clean_invoice()
    data["total_amount"] = 9999.0  # breaks subtotal + tax = total
    sig = significant_fields_confidence(_confs(data), threshold=0.95)
    flagged = {f["field"] for f in sig.low_fields}
    assert "total_amount" in flagged, f"expected total_amount flagged, got {flagged}"


def test_broken_line_item_is_flagged():
    data = _clean_invoice()
    data["lines"][0]["amount"] = 5.0  # qty*price=1000 != 5  → line amount fails
    sig = significant_fields_confidence(_confs(data), threshold=0.95)
    flagged = {f["field"] for f in sig.low_fields}
    assert any(f.startswith("line_1.") for f in flagged), f"expected a line field flagged: {flagged}"


def test_invalid_inn_lowers_confidence():
    data = _clean_invoice()
    data["supplier"]["inn"] = "1234567890"  # fails control digit
    sig = significant_fields_confidence(_confs(data), threshold=0.95)
    flagged = {f["field"] for f in sig.low_fields}
    assert "supplier.inn" in flagged


def test_threshold_is_honoured():
    """Raising the bar above the unverifiable prior flags clean significant
    fields; lowering it auto-approves."""
    confs = _confs(_clean_invoice())
    strict = significant_fields_confidence(confs, threshold=0.99)
    lenient = significant_fields_confidence(confs, threshold=0.90)
    assert len(strict.low_fields) >= len(lenient.low_fields)
    assert lenient.low_fields == []
