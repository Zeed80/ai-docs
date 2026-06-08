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
# Default prior for present fields we cannot verify objectively (names, free text,
# line descriptions). Matches the application default auto-approve threshold (0.95)
# so unverifiable fields sit right at the gate: they auto-approve at default
# threshold but are caught when the caller raises it above this value.
# Callers that know the current threshold should pass it as unverifiable_prior so
# the gate is meaningful at any operator-configured level.
_UNVERIFIABLE_DEFAULT = 0.95

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
    *,
    unverifiable_prior: float = _UNVERIFIABLE_DEFAULT,
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
                # Warning on subtotal = discount-invoice pattern: the LINE amounts
                # came from a pre-discount column, but subtotal/tax/total are
                # self-consistent.  Subtotal is CORRECT — only the individual line
                # amounts need human review.  Do not reduce subtotal confidence so
                # the document can still auto-approve; the warning remains in
                # validation_errors for the review UI.
                if field_name == "subtotal":
                    confidence = _VERIFIED
                    # reason stays high_quality_ocr — subtotal value is verified
                else:
                    confidence, reason = 0.6, ConfidenceReason.ambiguous_value
            else:
                confidence = _VERIFIED
        elif field_name == "invoice_date":
            if _is_valid_date(str_value):
                confidence = 0.96
            else:
                confidence, reason = 0.4, ConfidenceReason.format_mismatch
        else:
            # Not objectively verifiable (number, currency, notes): trust the
            # model's reported confidence, but never below a sensible prior.
            confidence = max(ai_confidences.get(field_name) or 0.0, unverifiable_prior)
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
            conf, reason = unverifiable_prior, ConfidenceReason.high_quality_ocr
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

    # Line-level fields — товары are *significant*: a wrong quantity/price/amount
    # changes what is being paid for, so each present line field gets a grounded
    # confidence. The per-line ``amount`` is verified arithmetically (qty×price);
    # name/qty/price/sku get the present-but-unverifiable prior unless that line's
    # arithmetic failed, which casts doubt on its numbers.
    for line in (extracted.get("lines") or [])[:20]:
        n = line.get("line_number", "?")
        line_amount_failed = f"line_{n}.amount" in error_fields

        def _line_field(key: str, *, numeric: bool):
            val = line.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                return
            if key == "amount":
                conf = _FAILED if line_amount_failed else _VERIFIED
                rsn = ConfidenceReason.arithmetic_error if line_amount_failed else ConfidenceReason.high_quality_ocr
            elif numeric and line_amount_failed:
                # qty / unit_price implicated by a broken line equation
                conf, rsn = 0.6, ConfidenceReason.ambiguous_value
            else:
                conf, rsn = unverifiable_prior, ConfidenceReason.high_quality_ocr
            results.append(FieldConfidence(
                field_name=f"line_{n}.{key}",
                value=str(val),
                confidence=round(conf, 2),
                reason=rsn,
            ))

        _line_field("description", numeric=False)
        _line_field("sku", numeric=False)
        _line_field("quantity", numeric=True)
        _line_field("unit_price", numeric=True)
        _line_field("amount", numeric=True)

    return results


# Significant fields — those whose low confidence must block auto-approval and be
# surfaced for human review: amounts, payment requisites (ИНН/КПП/БИК/счета) and
# line items (товары). Insignificant fields (address, phone, email, notes, bank
# name, free-text supplier/buyer name) are deliberately excluded from the gate so
# a blurry phone number never holds up an otherwise-correct invoice.
_SIGNIFICANT_TOP = {
    "invoice_number", "invoice_date",
    "subtotal", "tax_amount", "total_amount",
}
_SIGNIFICANT_PARTY_SUFFIXES = {"inn", "kpp", "bank_bik", "bank_account", "corr_account"}
_SIGNIFICANT_LINE_SUFFIXES = {"description", "sku", "quantity", "unit_price", "amount"}


def _is_significant(field_name: str) -> bool:
    if field_name in _SIGNIFICANT_TOP:
        return True
    if field_name.startswith(("supplier.", "buyer.")):
        return field_name.split(".", 1)[1] in _SIGNIFICANT_PARTY_SUFFIXES
    if field_name.startswith("line_"):
        suffix = field_name.split(".", 1)[1] if "." in field_name else ""
        return suffix in _SIGNIFICANT_LINE_SUFFIXES
    return False


@dataclass
class SignificantConfidence:
    score: float                       # weighted confidence over significant fields
    low_fields: list[dict]             # significant fields below the threshold
    significant_count: int


def significant_fields_confidence(
    field_confidences: list[FieldConfidence],
    threshold: float,
) -> SignificantConfidence:
    """Confidence over *significant* fields only, plus the list of significant
    fields that fall below ``threshold`` (the ones a human should verify).

    Critical fields (total/number/date/ИНН) are weighted 2× — consistent with
    :func:`compute_overall_confidence` — so they dominate the gate decision.
    Only present significant fields are counted; absent ones don't penalise.
    """
    critical = {"total_amount", "invoice_number", "invoice_date", "supplier.inn"}
    significant = [
        fc for fc in field_confidences
        if fc.value is not None and _is_significant(fc.field_name)
    ]
    if not significant:
        return SignificantConfidence(score=0.0, low_fields=[], significant_count=0)

    weighted_sum = 0.0
    weight_total = 0.0
    low_fields: list[dict] = []
    for fc in significant:
        weight = 2.0 if fc.field_name in critical else 1.0
        weighted_sum += fc.confidence * weight
        weight_total += weight
        if fc.confidence < threshold:
            low_fields.append({
                "field": fc.field_name,
                "value": fc.value,
                "confidence": fc.confidence,
                "reason": fc.reason.value if hasattr(fc.reason, "value") else str(fc.reason),
            })

    score = round(weighted_sum / weight_total, 2) if weight_total else 0.0
    return SignificantConfidence(
        score=score,
        low_fields=low_fields,
        significant_count=len(significant),
    )


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
            # Discount-invoice pattern: lines > subtotal but subtotal/tax/total
            # are self-consistent.  This means the LLM read the pre-discount
            # "Сумма без скидки" column instead of the final "Сумма" column.
            # Totals are still reliable; line amounts need human verification.
            # Flag as warning (not error) so subtotal confidence stays high.
            _totals_ok = (
                tax_amount is not None
                and total_amount is not None
                and line_sum > float(subtotal)
                and rv.arith_total_ok(subtotal, tax_amount, total_amount)
            )
            if _totals_ok:
                errors.append({
                    "field": "subtotal",
                    "error_type": "arithmetic",
                    "message": (
                        f"Sum of line amounts ({line_sum}) > subtotal ({subtotal}): "
                        "line amounts may be from pre-discount 'Сумма без скидки' column; "
                        "please verify individual line amounts"
                    ),
                    "expected": str(subtotal),
                    "actual": str(line_sum),
                    "severity": "warning",
                })
            else:
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
            # Discount-invoice pattern: subtotal > total_amount because the LLM
            # picked the pre-discount column ("Сумма без скидки") instead of the
            # final post-discount column ("Сумма"). Detect by checking whether
            # total_amount is self-consistent under a VAT-included convention
            # (total_amount is the gross and tax is embedded in it). When that
            # check passes, the subtotal — not the total — is the wrong field.
            try:
                s = float(subtotal)
                t = float(tax_amount)
                g = float(total_amount)
                total_self_consistent = (
                    s > g and (
                        rv.arith_total_ok(g, t, g)           # В т.ч. НДС: gross == total
                        or rv.arith_total_ok(g - t, t, g)    # НДС сверху: net + tax = total
                    )
                )
            except (TypeError, ValueError):
                total_self_consistent = False

            if total_self_consistent:
                # Flag subtotal (the pre-discount gross): total_amount is correct.
                errors.append({
                    "field": "subtotal",
                    "error_type": "arithmetic",
                    "message": (
                        f"subtotal ({subtotal}) > total ({total_amount}): likely the "
                        "pre-discount 'Сумма без скидки' column was used instead of "
                        "the final post-discount 'Сумма' column"
                    ),
                    "expected": "post-discount net or gross consistent with total",
                    "actual": str(subtotal),
                    "severity": "error",
                })
            else:
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
