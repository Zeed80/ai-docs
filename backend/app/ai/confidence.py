"""Confidence scoring — grounds the AI's self-reported confidence in objective
deterministic checks (control-digit checksums, arithmetic). A field that passes
its check is reported as verified-high; one that fails is reported low — so the
percentages reflect reality, not the model's guess."""

import re
from dataclasses import dataclass

from app.ai import ru_validators as rv
from app.db.models import ConfidenceReason

# Confidence levels for objectively *verifiable* fields.
_VERIFIED = 0.98   # checksum / arithmetic passed → near-certain
_FAILED = 0.25     # checksum / arithmetic failed → almost certainly wrong
# Prior for present fields we cannot verify objectively (names, free text).
# High, because the extraction model is strong; it just can't be *proven*.
_UNVERIFIABLE = 0.9

_AMOUNT_FIELDS = {"subtotal", "tax_amount", "total_amount"}


@dataclass
class FieldConfidence:
    field_name: str
    value: str | None
    confidence: float
    reason: ConfidenceReason


def compute_field_confidences(
    extracted: dict,
    ai_confidences: dict[str, float],
    validation_errors: list[dict],
) -> list[FieldConfidence]:
    """Compute final confidence for each extracted field.

    Combines:
    1. AI self-reported confidence
    2. Deterministic format checks
    3. Validation results (arithmetic, consistency)
    """
    results: list[FieldConfidence] = []
    error_fields = {e["field"] for e in validation_errors if e.get("severity") == "error"}
    warning_fields = {e["field"] for e in validation_errors if e.get("severity") == "warning"}

    top_fields = [
        "invoice_number", "invoice_date", "due_date", "validity_date", "currency",
        "payment_id", "notes",
        "subtotal", "tax_amount", "total_amount",
    ]

    for field_name in top_fields:
        value = extracted.get(field_name)

        if value is None:
            results.append(FieldConfidence(
                field_name=field_name,
                value=None,
                confidence=0.0,
                reason=ConfidenceReason.missing_field,
            ))
            continue

        str_value = str(value)
        reason = ConfidenceReason.high_quality_ocr

        if field_name in _AMOUNT_FIELDS:
            # Grounded in arithmetic: verified-consistent → high, else low.
            if field_name in error_fields:
                confidence, reason = _FAILED, ConfidenceReason.arithmetic_error
            elif field_name in warning_fields:
                confidence, reason = 0.6, ConfidenceReason.ambiguous_value
            else:
                confidence = _VERIFIED
        elif field_name == "invoice_date":
            if _is_valid_date(str_value):
                confidence = 0.92
            else:
                confidence, reason = 0.4, ConfidenceReason.format_mismatch
        else:
            # Not objectively verifiable (number, currency, notes): trust the
            # model's reported confidence, but never below a sensible prior.
            confidence = max(ai_confidences.get(field_name, 0.0), _UNVERIFIABLE)
            if field_name in error_fields:
                confidence, reason = _FAILED, ConfidenceReason.arithmetic_error
            elif field_name in warning_fields:
                confidence, reason = 0.6, ConfidenceReason.ambiguous_value

        results.append(FieldConfidence(
            field_name=field_name,
            value=str_value,
            confidence=round(confidence, 2),
            reason=reason,
        ))

    def _add_party_field(prefix: str, data: dict, key: str, checksum=None):
        """Add a party field. ``checksum`` (bool result) → verified/failed;
        otherwise a present-but-unverifiable prior."""
        val = data.get(key)
        if not val:
            return
        if checksum is not None:
            ok = bool(checksum(val))
            conf = _VERIFIED if ok else _FAILED
            reason = ConfidenceReason.high_quality_ocr if ok else ConfidenceReason.format_mismatch
        else:
            conf, reason = _UNVERIFIABLE, ConfidenceReason.high_quality_ocr
        results.append(FieldConfidence(
            field_name=f"{prefix}.{key}",
            value=str(val),
            confidence=round(conf, 2),
            reason=reason,
        ))

    # Supplier fields — ИНН/БИК/счета verified by control-digit checksums.
    supplier = extracted.get("supplier", {}) or {}
    if supplier:
        sbik = supplier.get("bank_bik")
        _add_party_field("supplier", supplier, "name")
        _add_party_field("supplier", supplier, "inn", rv.inn_valid)
        _add_party_field("supplier", supplier, "kpp", rv.kpp_valid)
        _add_party_field("supplier", supplier, "address")
        _add_party_field("supplier", supplier, "phone")
        _add_party_field("supplier", supplier, "email")
        _add_party_field("supplier", supplier, "bank_name")
        _add_party_field("supplier", supplier, "bank_bik", rv.bik_valid)
        _add_party_field("supplier", supplier, "bank_account", lambda v: rv.account_valid(v, sbik))
        _add_party_field("supplier", supplier, "corr_account", lambda v: rv.corr_account_valid(v, sbik))

    # Buyer fields
    buyer = extracted.get("buyer", {}) or {}
    if buyer:
        _add_party_field("buyer", buyer, "name")
        _add_party_field("buyer", buyer, "inn", rv.inn_valid)
        _add_party_field("buyer", buyer, "kpp", rv.kpp_valid)
        _add_party_field("buyer", buyer, "address")

    # Line-level SKU fields (first few lines for ExtractionPanel display)
    for line in (extracted.get("lines") or [])[:20]:
        n = line.get("line_number", "?")
        sku = line.get("sku")
        if sku:
            results.append(FieldConfidence(
                field_name=f"line_{n}.sku",
                value=str(sku),
                confidence=0.9,
                reason=ConfidenceReason.high_quality_ocr,
            ))

    return results


def compute_overall_confidence(field_confidences: list[FieldConfidence]) -> float:
    """Compute overall document confidence from field confidences.

    Only filled fields (value is not None) are counted — absent fields are
    simply not in the denominator, so a document with fewer fields doesn't
    get penalised for having blank optional columns.
    """
    if not field_confidences:
        return 0.0

    filled = [fc for fc in field_confidences if fc.value is not None]
    if not filled:
        return 0.0

    critical = {"total_amount", "invoice_number", "invoice_date", "supplier.inn"}
    weighted_sum = 0.0
    weight_total = 0.0

    for fc in filled:
        weight = 2.0 if fc.field_name in critical else 1.0
        weighted_sum += fc.confidence * weight
        weight_total += weight

    return round(weighted_sum / weight_total, 2) if weight_total > 0 else 0.0


def validate_arithmetic(extracted: dict) -> list[dict]:
    """Deterministic arithmetic validation of invoice data."""
    errors: list[dict] = []

    lines = extracted.get("lines", [])
    subtotal = extracted.get("subtotal")
    tax_amount = extracted.get("tax_amount")
    total_amount = extracted.get("total_amount")

    # Check each line: quantity × unit_price ≈ amount
    for line in lines:
        qty = line.get("quantity")
        price = line.get("unit_price")
        amount = line.get("amount")
        if qty is not None and price is not None and amount is not None:
            expected = round(qty * price, 2)
            if abs(expected - amount) > 0.5:
                errors.append({
                    "field": f"line_{line.get('line_number', '?')}.amount",
                    "error_type": "arithmetic",
                    "message": f"Line amount mismatch: {qty} × {price} = {expected}, got {amount}",
                    "expected": str(expected),
                    "actual": str(amount),
                    "severity": "error",
                })

    # Check line amounts sum ≈ subtotal (net lines) OR ≈ total (VAT-inclusive
    # lines) — both conventions occur on real Russian invoices.
    if subtotal is not None and lines:
        line_sum = sum(l.get("amount", 0) or 0 for l in lines)
        matches_subtotal = abs(line_sum - subtotal) <= max(1.0, 0.01 * abs(subtotal))
        matches_total = (
            total_amount is not None
            and abs(line_sum - total_amount) <= max(1.0, 0.01 * abs(total_amount))
        )
        if not matches_subtotal and not matches_total:
            errors.append({
                "field": "subtotal",
                "error_type": "arithmetic",
                "message": f"Sum of lines ({line_sum}) ≠ subtotal ({subtotal})",
                "expected": str(line_sum),
                "actual": str(subtotal),
                "severity": "error",
            })

    # Check subtotal / tax / total reconcile under either VAT convention
    # (НДС сверху OR НДС в том числе) — see ru_validators.arith_total_ok.
    if subtotal is not None and tax_amount is not None and total_amount is not None:
        if not rv.arith_total_ok(subtotal, tax_amount, total_amount):
            errors.append({
                "field": "total_amount",
                "error_type": "arithmetic",
                "message": f"subtotal ({subtotal}) + tax ({tax_amount}) ≠ total ({total_amount})",
                "expected": "consistent VAT total",
                "actual": str(total_amount),
                "severity": "error",
            })

    return errors


def _is_valid_date(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))
