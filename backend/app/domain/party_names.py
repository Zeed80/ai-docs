"""Counterparty names without legal form and quotes, for matching.

``ООО "ИНАТЕК-М"``, ``ООО «ИНАТЕК-М»`` and ``Общество с ограниченной
ответственностью ИНАТЕК-М`` are one supplier; a substring search over the raw
name finds none of them from another (live 2026-10-10: the planner searched
``ООО «ИНАТЕК-М»`` for a supplier with 17 invoices and got 0).
"""

from __future__ import annotations

import re

# Longest first: "акционерное общество" inside "закрытое акционерное
# общество" would otherwise leave "закрытое" behind.
_LEGAL_FORMS = (
    r"общество с ограниченной ответственностью",
    r"закрытое акционерное общество",
    r"публичное акционерное общество",
    r"открытое акционерное общество",
    r"акционерное общество",
    r"индивидуальный предприниматель",
    r"\bооо\b",
    r"\bзао\b",
    r"\bоао\b",
    r"\bпао\b",
    r"\bао\b",
    r"\bип\b",
    r"\bгуп\b",
    r"\bмуп\b",
)
_QUOTES = re.compile(r"[\"'«»“”„()]")


def company_core_name(name: str) -> str:
    """Lowercase name without legal form, quotes and extra spaces."""
    s = (name or "").lower().strip()
    for pattern in _LEGAL_FORMS:
        s = re.sub(pattern, "", s)
    s = _QUOTES.sub("", s)
    return re.sub(r"\s+", " ", s).strip()
