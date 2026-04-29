"""Confidence scoring — combines AI self-reported confidence with deterministic checks."""

import re
from dataclasses import dataclass

from app.db.models import ConfidenceReason


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
        ai_conf = ai_confidences.get(field_name, 0.5)

        if value is None:
            results.append(FieldConfidence(
                field_name=field_name,
                value=None,
                confidence=0.0,
                reason=ConfidenceReason.missing_field,
            ))
            continue

        # Start with AI confidence
        confidence = ai_conf

        # Deterministic checks
        reason = ConfidenceReason.high_quality_ocr

        if field_name in error_fields:
            confidence = min(confidence, 0.3)
            reason = ConfidenceReason.arithmetic_error
        elif field_name in warning_fields:
            confidence = min(confidence, 0.6)
            reason = ConfidenceReason.ambiguous_value

        # Format checks
        str_value = str(value)
        if field_name == "invoice_date" and not _is_valid_date(str_value):
            confidence = min(confidence, 0.4)
            reason = ConfidenceReason.format_mismatch

        results.append(FieldConfidence(
            field_name=field_name,
            value=str_value,
            confidence=round(confidence, 2),
            reason=reason,
        ))

    # Supplier fields
    supplier = extracted.get("supplier", {}) or {}
    if supplier:
        def _add_supplier(key: str, validator=None, default_conf: float = 0.85):
            val = supplier.get(key)
            if not val:
                return
            conf = validator(str(val)) if validator else default_conf
            if isinstance(conf, bool):
                conf = 0.95 if conf else 0.3
            results.append(FieldConfidence(
                field_name=f"supplier.{key}",
                value=str(val),
                confidence=round(conf, 2),
                reason=ConfidenceReason.high_quality_ocr if conf > 0.5 else ConfidenceReason.format_mismatch,
            ))

        _add_supplier("name")
        _add_supplier("inn", _is_valid_inn)
        _add_supplier("kpp", _is_valid_kpp)
        _add_supplier("address")
        _add_supplier("phone")
        _add_supplier("email")
        _add_supplier("bank_name")
        _add_supplier("bank_bik", _is_valid_bik)
        _add_supplier("bank_account", _is_valid_account)
        _add_supplier("corr_account", _is_valid_account)

    # Buyer fields
    buyer = extracted.get("buyer", {}) or {}
    if buyer:
        def _add_buyer(key: str, validator=None, default_conf: float = 0.85):
            val = buyer.get(key)
            if not val:
                return
            conf = validator(str(val)) if validator else default_conf
            if isinstance(conf, bool):
                conf = 0.95 if conf else 0.3
            results.append(FieldConfidence(
                field_name=f"buyer.{key}",
                value=str(val),
                confidence=round(conf, 2),
                reason=ConfidenceReason.high_quality_ocr if conf > 0.5 else ConfidenceReason.format_mismatch,
            ))

        _add_buyer("name")
        _add_buyer("inn", _is_valid_inn)
        _add_buyer("kpp", _is_valid_kpp)
        _add_buyer("address")

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
    """Compute overall document confidence from field confidences."""
    if not field_confidences:
        return 0.0

    # Weighted: critical fields (amounts, dates) weigh more
    critical = {"total_amount", "invoice_number", "invoice_date", "supplier.inn"}
    weighted_sum = 0.0
    weight_total = 0.0

    for fc in field_confidences:
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

    # Check line amounts sum ≈ subtotal
    if subtotal is not None and lines:
        line_sum = sum(l.get("amount", 0) or 0 for l in lines)
        if abs(line_sum - subtotal) > 1.0:
            errors.append({
                "field": "subtotal",
                "error_type": "arithmetic",
                "message": f"Sum of lines ({line_sum}) ≠ subtotal ({subtotal})",
                "expected": str(line_sum),
                "actual": str(subtotal),
                "severity": "error",
            })

    # Check subtotal + tax ≈ total
    if subtotal is not None and tax_amount is not None and total_amount is not None:
        expected_total = round(subtotal + tax_amount, 2)
        if abs(expected_total - total_amount) > 1.0:
            errors.append({
                "field": "total_amount",
                "error_type": "arithmetic",
                "message": f"subtotal + tax ({expected_total}) ≠ total ({total_amount})",
                "expected": str(expected_total),
                "actual": str(total_amount),
                "severity": "error",
            })

    return errors


def _is_valid_date(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def _is_valid_inn(value: str) -> bool:
    return bool(re.match(r"^\d{10}$|^\d{12}$", value))


def _is_valid_kpp(value: str) -> bool:
    return bool(re.match(r"^\d{9}$", value))


def _is_valid_bik(value: str) -> bool:
    clean = re.sub(r"\s", "", value)
    return bool(re.match(r"^\d{9}$", clean))


def _is_valid_account(value: str) -> bool:
    clean = re.sub(r"\s", "", value)
    return bool(re.match(r"^\d{20}$", clean))
