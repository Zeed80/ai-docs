"""Unit tests for the checksum safety gate (_checksum_issues).

A present-but-invalid ИНН / bank account must be reported so the pipeline holds
the invoice for mandatory human review instead of auto-approving it.
"""

from __future__ import annotations

# Ensure the real extraction module (test_extraction_api may install a mock).
import importlib
import sys


def _checksum_issues(extracted):
    mod = sys.modules.get("app.tasks.extraction")
    if mod is None or not hasattr(mod, "_checksum_issues"):
        sys.modules.pop("app.tasks.extraction", None)
        mod = importlib.import_module("app.tasks.extraction")
    return mod._checksum_issues(extracted)


_VALID = {
    "supplier": {
        "inn": "7707083893",
        "bank_bik": "044525225",
        "bank_account": "40702810400000000225",
        "corr_account": "30101810400000000225",
    },
    "buyer": {"inn": "5036167355"},
}


def test_valid_invoice_has_no_issues():
    assert _checksum_issues(_VALID) == []


def test_absent_fields_are_not_flagged():
    # Missing data is excluded — only present-and-invalid is an issue.
    assert _checksum_issues({"supplier": {}, "buyer": {}}) == []


def test_broken_supplier_inn_flagged():
    bad = {**_VALID, "supplier": {**_VALID["supplier"], "inn": "7707083894"}}
    assert _checksum_issues(bad) == ["supplier.inn"]


def test_broken_account_flagged():
    bad = {
        **_VALID,
        "supplier": {**_VALID["supplier"], "bank_account": "40702810400000000226"},
    }
    assert "supplier.bank_account" in _checksum_issues(bad)


def test_broken_corr_account_flagged():
    bad = {
        **_VALID,
        "supplier": {**_VALID["supplier"], "corr_account": "30101810400000000226"},
    }
    assert "supplier.corr_account" in _checksum_issues(bad)


def test_broken_buyer_inn_flagged():
    bad = {**_VALID, "buyer": {"inn": "5036167356"}}
    assert _checksum_issues(bad) == ["buyer.inn"]
