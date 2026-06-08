"""Objective validators for Russian invoice fields (control-digit checksums).

A passing checksum is a mathematical guarantee the digits are correct, so these
ground confidence scoring (and the extraction-quality regression) in reality
instead of the model's self-reported guess. All functions are pure and never
raise.
"""

from __future__ import annotations


def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit()) if value is not None else ""


def inn_valid(inn) -> bool:
    """Validate a Russian ИНН by its control digits (10- or 12-digit)."""
    d = _digits(inn)

    def cd(weights: list[int]) -> int:
        return (sum(int(d[i]) * weights[i] for i in range(len(weights))) % 11) % 10

    if len(d) == 10:
        return cd([2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(d[9])
    if len(d) == 12:
        n11 = cd([7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        n12 = cd([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        return n11 == int(d[10]) and n12 == int(d[11])
    return False


def bik_valid(bik) -> bool:
    """Validate a Russian БИК by ЦБ РФ structure: 04 RRR CCC.

    - Digits 0-1: "04" (Russia)
    - Digits 2-4 (regional code): must be > 0
    - Digits 5-8 (branch code): 000 (RCC head office) or >= 050 (standard branch)
    """
    d = _digits(bik)
    if len(d) != 9 or not d.startswith("04"):
        return False
    regional = int(d[2:5])
    branch = int(d[5:])
    if regional == 0:
        return False
    if branch != 0 and branch < 50:
        return False
    return True


def kpp_valid(kpp) -> bool:
    s = str(kpp or "").strip()
    return len(s) == 9 and s[:4].isdigit() and s[4:6].isalnum() and s[6:].isdigit()


def _account_key_ok(account, prefix: str) -> bool:
    """ЦБ РФ control-key check over (prefix + 20-digit account) = 23 digits."""
    acc = _digits(account)
    if len(acc) != 20:
        return False
    seq = prefix + acc
    if len(seq) != 23 or not seq.isdigit():
        return False
    weights = [7, 1, 3] * 8
    return sum(int(seq[i]) * weights[i] for i in range(23)) % 10 == 0


def settlement_account_valid(account, bik) -> bool:
    """Расчётный счёт (407…) verified against БИК (last 3 digits as prefix)."""
    b = _digits(bik)
    return len(b) == 9 and _account_key_ok(account, b[-3:])


def corr_account_valid(corr, bik) -> bool:
    """Корр. счёт (301…) verified against БИК: prefix '0' + БИК[4:6]."""
    b = _digits(bik)
    return len(b) == 9 and _account_key_ok(corr, "0" + b[4:6])


def account_valid(account, bik) -> bool:
    """True if the 20-digit account passes the control key as р/с or к/с."""
    return settlement_account_valid(account, bik) or corr_account_valid(account, bik)


def _money(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _approx(a: float, b: float, total: float | None = None) -> bool:
    tol = max(1.0, 0.01 * abs(total if total else b))
    return abs(a - b) <= tol


def arith_total_ok(subtotal, tax, total) -> bool:
    """Validate subtotal/tax/total under both Russian VAT conventions.

    * VAT added on top (НДС сверху):  subtotal + tax = total.
    * VAT included (НДС в том числе):  total = subtotal and the tax equals the
      VAT embedded in the gross at 20 % or 10 % (``tax = total·r/(1+r)``).
    * Net subtotal with rate-derived total: ``tax = subtotal·r`` and
      ``total = subtotal·(1+r)``.
    """
    s, t, g = _money(subtotal), _money(tax), _money(total)
    if s is None or t is None or g is None:
        return False
    if _approx(s + t, g, g):
        return True
    if _approx(s, g, g):
        for r in (0.2, 0.1):
            if _approx(t, g * r / (1 + r), g):
                return True
    for r in (0.2, 0.1):
        if _approx(t, s * r, g) and _approx(g, s * (1 + r), g):
            return True
    return False
