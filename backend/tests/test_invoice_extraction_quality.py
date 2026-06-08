"""Live extraction-quality regression for the document-processing refactor.

Goal: objective confidence in the DATA the pipeline extracts from real invoices,
not just "did it run". We run the refactored text path (parser registry → VLM
OCR fallback) plus ``ai_router.extract_invoice`` against real files from
``example-invoices/`` and score each document with *objective* validators:

* **INN** — Russian 10/12-digit control-digit checksum.
* **Расчётный счёт (407…)** and **корр. счёт (301…)** — 20-digit account control
  key verified against the supplier's **БИК** (ЦБ РФ algorithm). A pass here is a
  mathematical guarantee the digits were OCR'd correctly.
* **БИК / КПП** — format + Russia prefix.
* **Arithmetic** — ``subtotal + tax ≈ total``; ``Σ line amounts ≈ subtotal``;
  ``qty × price ≈ line amount``. A wrong OCR'd digit breaks arithmetic.

Per document we compute confidence = passed / **applicable** validators, where a
validator is *applicable* only when the field is actually present (missing data
is excluded from the score, per the acceptance goal). 100 % = every present,
checkable field is verified correct.

This is a LIVE test: it needs host Ollama (``OLLAMA_URL``, default
``http://localhost:11434``). It skips cleanly when Ollama or the sample files are
absent. It writes a detailed report to ``tests/reports/``.

Run:
    cd backend && OLLAMA_URL=http://localhost:11434 \
        python3 -m pytest tests/test_invoice_extraction_quality.py -s
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import types
import uuid
from pathlib import Path

import httpx
import pytest

# ── File selection ───────────────────────────────────────────────────────────

_INVOICES_DIR = Path(__file__).parent.parent.parent / "example-invoices"
_REPORT_DIR = Path(__file__).parent / "reports"
# Pin both OCR and extraction to a specific Ollama model for a reproducible run
# that matches production (which uses qwen3.5:9b — far better Russian OCR than
# the gemma default). When unset, the configured AI router model is used.
#   INVOICE_QUALITY_MODEL=qwen3.5:9b python3 -m pytest tests/test_invoice_extraction_quality.py -s
_MODEL_OVERRIDE = os.environ.get("INVOICE_QUALITY_MODEL")

_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _supplier_key(stem: str) -> str:
    """Extract supplier name prefix: everything before the invoice number token.

    Handles patterns like 'Supplier № 123', 'Supplier №123', 'Supplier N9 2032'.
    """
    import re
    # Split on '№' (with optional surrounding spaces) or standalone 'N' followed by digits
    m = re.search(r"\s*№\s*|\s+N\d", stem)
    return stem[:m.start()].strip() if m else stem[:40].strip()


def _select_files() -> list[Path]:
    """All JPGs + one PDF per unique supplier (first alphabetically per group)."""
    if not _INVOICES_DIR.is_dir():
        return []
    jpgs = sorted(
        p for p in _INVOICES_DIR.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg") and not p.name.startswith(".")
    )
    all_pdfs = sorted(
        p for p in _INVOICES_DIR.iterdir()
        if p.suffix.lower() == ".pdf" and not p.name.startswith(".")
    )
    # One PDF per supplier — first file alphabetically within each supplier group
    seen: set[str] = set()
    selected_pdfs: list[Path] = []
    for p in all_pdfs:
        key = _supplier_key(p.stem)
        if key not in seen:
            seen.add(key)
            selected_pdfs.append(p)
    return jpgs + selected_pdfs


def _ollama_up() -> bool:
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    try:
        return httpx.get(f"{url}/api/tags", timeout=5.0).status_code == 200
    except Exception:
        return False


# ── Russian objective validators ─────────────────────────────────────────────

def _digits(s) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit()) if s is not None else ""


def inn_valid(inn) -> bool:
    """Validate a Russian INN by its control digits (10- or 12-digit)."""
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
    d = _digits(bik)
    if len(d) != 9 or not d.startswith("04"):
        return False
    regional = int(d[2:5])
    branch = int(d[5:])
    return regional != 0 and (branch == 0 or branch >= 50)


def kpp_valid(kpp) -> bool:
    s = str(kpp or "").strip()
    if len(s) != 9:
        return False
    return s[:4].isdigit() and s[4:6].isalnum() and s[6:].isdigit()


def _account_key_ok(account: str, prefix: str) -> bool:
    """ЦБ РФ control-key check over (prefix + 20-digit account), 23 digits."""
    acc = _digits(account)
    if len(acc) != 20:
        return False
    seq = prefix + acc  # 3 + 20 = 23 digits
    if len(seq) != 23 or not seq.isdigit():
        return False
    weights = [7, 1, 3] * 8  # 24 >= 23
    checksum = sum(int(seq[i]) * weights[i] for i in range(23)) % 10
    return checksum == 0


def settlement_account_valid(account, bik) -> bool:
    """Расчётный счёт (407…) verified against БИК (last 3 digits as prefix)."""
    b = _digits(bik)
    if len(b) != 9:
        return False
    return _account_key_ok(account, b[-3:])


def corr_account_valid(corr, bik) -> bool:
    """Корр. счёт (301…) verified against БИК: prefix '0' + БИК[4:6]."""
    b = _digits(bik)
    if len(b) != 9:
        return False
    return _account_key_ok(corr, "0" + b[4:6])


def date_valid(s) -> bool:
    if not s:
        return False
    txt = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            _dt.datetime.strptime(txt, fmt)
            return True
        except ValueError:
            continue
    return False


def _money(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _approx(a: float, b: float, total: float | None = None) -> bool:
    tol = max(1.0, 0.01 * abs(total if total else b))
    return abs(a - b) <= tol


def arith_total_ok(subtotal: float, tax: float, total: float) -> bool:
    """Validate subtotal/tax/total under both Russian VAT conventions.

    * VAT *added on top* (НДС сверху):  subtotal(net) + tax = total(gross).
    * VAT *included* (НДС в том числе):  total = subtotal(gross) and the tax
      equals the VAT embedded in the gross at 20 % or 10 %
      (``tax = total·r/(1+r)``) — this still verifies the tax digits.
    * Net subtotal with rate-derived total: ``tax = subtotal·r`` and
      ``total = subtotal·(1+r)``.
    """
    if _approx(subtotal + tax, total, total):
        return True
    if _approx(subtotal, total, total):
        for r in (0.2, 0.1):
            if _approx(tax, total * r / (1 + r), total):
                return True
    for r in (0.2, 0.1):
        if _approx(tax, subtotal * r, total) and _approx(total, subtotal * (1 + r), total):
            return True
    return False


# ── Pipeline: refactored text path + extraction ──────────────────────────────

def _extract_text(content: bytes, path: Path) -> tuple[str, str]:
    """Run the refactored text path: parser registry → VLM OCR fallback.

    Returns ``(text, source)`` where source is the parser name or 'ocr'.
    """
    from app.ai.parsers import parse_document
    from app.tasks.extraction import _ocr_image_content, _ocr_pdf_content

    mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
    parsed = parse_document(content, path.name, mime)
    if parsed.text.strip():
        return parsed.text, parsed.parser_name
    if not parsed.needs_ocr:
        return "", parsed.parser_name

    if _MODEL_OVERRIDE:
        # OCR with the pinned model, preprocessing identically to the pipeline.
        # PDFs MUST be rendered page-by-page (fitz → PNG) exactly like
        # _ocr_pdf_content — feeding raw PDF bytes to _preprocess_ocr_page (an
        # image preprocessor) yields an empty transcription.
        import base64

        from app.tasks.extraction import (
            _OCR_PROMPT,
            _ollama_vision_ocr,
            _preprocess_ocr_page,
        )

        if mime == "application/pdf":
            import fitz

            scale = getattr(__import__("app.config", fromlist=["settings"]).settings,
                            "ocr_render_scale", 2.5)
            encs: list[str] = []
            with fitz.open(stream=content, filetype="pdf") as pdf:
                for i in range(pdf.page_count):
                    pixmap = pdf[i].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    encs.append(base64.b64encode(_preprocess_ocr_page(pixmap.tobytes("png"))).decode())
        else:
            encs = [base64.b64encode(_preprocess_ocr_page(content)).decode()]
        return _ollama_vision_ocr(encs, _MODEL_OVERRIDE, _OCR_PROMPT), f"ocr:{_MODEL_OVERRIDE}"

    stub = types.SimpleNamespace(id=uuid.uuid4(), mime_type=mime, file_name=path.name)
    if mime == "application/pdf":
        return _ocr_pdf_content(content, stub), "ocr_pdf"
    return _ocr_image_content(content, mime, stub), "ocr_image"


def _extract_invoice(text: str) -> dict:
    """Run invoice extraction, honoring the pinned model override when set."""
    if _MODEL_OVERRIDE:
        from app.ai.extraction_prompts import EXTRACT_INVOICE_PROMPT, EXTRACT_INVOICE_SYSTEM
        from app.ai.ollama_client import generate_json

        return asyncio.run(
            generate_json(
                EXTRACT_INVOICE_PROMPT.format(text=text[:8000]),
                model=_MODEL_OVERRIDE,
                provider="ollama",
                system=EXTRACT_INVOICE_SYSTEM,
                temperature=0.0,
                max_tokens=8192,
                timeout_seconds=180.0,
            )
        )
    from app.ai.router import ai_router

    return asyncio.run(ai_router.extract_invoice(text))


def _evaluate(data: dict) -> dict:
    """Apply objective validators; return per-field results and a score.

    Only *present* fields are counted (applicable). Score = passed / applicable.
    """
    supplier = data.get("supplier") or {}
    buyer = data.get("buyer") or {}
    checks: list[tuple[str, bool]] = []  # (name, passed); only applicable fields

    def add(name: str, present: bool, ok: bool):
        if present:
            checks.append((name, bool(ok)))

    bik = supplier.get("bank_bik")
    add("supplier_inn", bool(supplier.get("inn")), inn_valid(supplier.get("inn")))
    add("buyer_inn", bool(buyer.get("inn")), inn_valid(buyer.get("inn")))
    add("supplier_bik", bool(bik), bik_valid(bik))
    add("supplier_kpp", bool(supplier.get("kpp")), kpp_valid(supplier.get("kpp")))
    add("buyer_kpp", bool(buyer.get("kpp")), kpp_valid(buyer.get("kpp")))
    add(
        "settlement_account",
        bool(supplier.get("bank_account") and bik),
        settlement_account_valid(supplier.get("bank_account"), bik),
    )
    add(
        "corr_account",
        bool(supplier.get("corr_account") and bik),
        corr_account_valid(supplier.get("corr_account"), bik),
    )
    add("invoice_date", bool(data.get("invoice_date")), date_valid(data.get("invoice_date")))
    add("invoice_number", True, bool(str(data.get("invoice_number") or "").strip()))

    # Arithmetic
    subtotal = _money(data.get("subtotal"))
    tax = _money(data.get("tax_amount"))
    total = _money(data.get("total_amount")) or _money(data.get("total"))
    lines = data.get("lines") or []

    if subtotal is not None and tax is not None and total is not None:
        add("arith_total", True, arith_total_ok(subtotal, tax, total))
    line_amounts = [_money(li.get("amount")) for li in lines if _money(li.get("amount")) is not None]
    if line_amounts and subtotal is not None:
        add("arith_lines_sum", True, _approx(sum(line_amounts), subtotal, subtotal))
    # Per-line qty×price ≈ amount (allow VAT-inclusive lines)
    line_ok = 0
    line_total = 0
    for li in lines:
        q, p, a = _money(li.get("quantity")), _money(li.get("unit_price")), _money(li.get("amount"))
        if q is not None and p is not None and a is not None and a != 0:
            line_total += 1
            tr = _money(li.get("tax_rate")) or 0.0
            if _approx(q * p, a, a) or _approx(q * p * (1 + tr), a, a):
                line_ok += 1
    if line_total:
        add("arith_line_items", True, line_ok == line_total)

    applicable = len(checks)
    passed = sum(1 for _, ok in checks if ok)
    failed = [name for name, ok in checks if not ok]
    return {
        "applicable": applicable,
        "passed": passed,
        "confidence": (passed / applicable) if applicable else None,
        "failed": failed,
        "checks": {name: ok for name, ok in checks},
        "line_count": len(lines),
    }


# ── The test ─────────────────────────────────────────────────────────────────

@pytest.mark.timeout(3600)
def test_invoice_extraction_quality() -> None:
    files = _select_files()
    if not files:
        pytest.skip("example-invoices/ not present")
    if not _ollama_up():
        pytest.skip("host Ollama not reachable (set OLLAMA_URL)")

    results: list[dict] = []
    for path in files:
        content = path.read_bytes()
        rec: dict = {"file": path.name, "size": len(content)}
        try:
            text, source = _extract_text(content, path)
            rec["text_source"] = source
            rec["text_len"] = len(text)
            # Refactor guarantee: never "binary garbage" for these formats.
            assert "�" not in text[:500], f"garbage text for {path.name}"
            if not text.strip():
                rec["status"] = "no_text"
                results.append(rec)
                print(f"  ✗ {path.name}: no text extracted ({source})")
                continue
            data = _extract_invoice(text)
            ev = _evaluate(data)
            rec.update(ev)
            rec["status"] = "ok"
            rec["overall_confidence"] = data.get("overall_confidence")
            _sup = data.get("supplier") or {}
            _buy = data.get("buyer") or {}
            rec["extracted"] = {
                "invoice_number": data.get("invoice_number"),
                "invoice_date": data.get("invoice_date"),
                "subtotal": data.get("subtotal"),
                "tax_amount": data.get("tax_amount"),
                "total_amount": data.get("total_amount") or data.get("total"),
                "supplier_inn": _sup.get("inn"),
                "supplier_bik": _sup.get("bank_bik"),
                "supplier_account": _sup.get("bank_account"),
                "supplier_corr": _sup.get("corr_account"),
                "buyer_inn": _buy.get("inn"),
            }
            conf = ev["confidence"]
            mark = "✓" if conf == 1.0 else ("~" if (conf or 0) >= 0.8 else "✗")
            print(
                f"  {mark} {path.name[:48]:48s} conf={None if conf is None else round(conf,3)} "
                f"({ev['passed']}/{ev['applicable']}) fail={ev['failed']} [{source}]"
            )
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = str(exc)
            print(f"  ! {path.name}: {exc}")
        results.append(rec)

    # ── Aggregate ───────────────────────────────────────────────────────────
    scored = [r for r in results if r.get("confidence") is not None]
    total_applicable = sum(r["applicable"] for r in scored)
    total_passed = sum(r["passed"] for r in scored)
    micro = (total_passed / total_applicable) if total_applicable else 0.0
    macro = (sum(r["confidence"] for r in scored) / len(scored)) if scored else 0.0
    perfect = sum(1 for r in scored if r["confidence"] == 1.0)

    # Per-field aggregate pass rates
    field_stats: dict[str, list[int]] = {}
    for r in scored:
        for name, ok in r.get("checks", {}).items():
            field_stats.setdefault(name, [0, 0])
            field_stats[name][1] += 1
            field_stats[name][0] += 1 if ok else 0

    # Per-source micro-confidence (text-layer PDFs isolate the refactor's text
    # path; OCR images additionally depend on the vision model's digit accuracy).
    def _source_micro(predicate) -> float:
        rs = [r for r in scored if predicate(r.get("text_source", ""))]
        ap = sum(r["applicable"] for r in rs)
        pa = sum(r["passed"] for r in rs)
        return round(pa / ap, 4) if ap else 0.0

    text_micro = _source_micro(lambda s: s == "pdf_text_layer")
    ocr_micro = _source_micro(lambda s: s.startswith("ocr"))

    summary = {
        "files": len(files),
        "scored": len(scored),
        "no_text": sum(1 for r in results if r.get("status") == "no_text"),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "micro_confidence": round(micro, 4),
        "macro_confidence": round(macro, 4),
        "text_layer_micro": text_micro,
        "ocr_micro": ocr_micro,
        "perfect_docs": perfect,
        "field_pass_rates": {
            k: {"passed": v[0], "total": v[1], "rate": round(v[0] / v[1], 3)}
            for k, v in sorted(field_stats.items())
        },
    }

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (_REPORT_DIR / "invoice_extraction_quality.json").write_text(
        json.dumps({"summary": summary, "documents": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== EXTRACTION QUALITY SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # ── Refactor contract (hard) ────────────────────────────────────────────
    # These MUST hold regardless of model quality: every selected file is
    # turned into clean, non-garbage text by the parser registry / OCR path.
    assert summary["errors"] == 0, f"{summary['errors']} files errored — see report"
    assert summary["no_text"] == 0, f"{summary['no_text']} files yielded no text — see report"

    # ── Quality regression floors (model-dependent) ─────────────────────────
    # Text-layer PDFs isolate extraction quality from OCR — should be near
    # perfect. OCR images additionally hinge on the vision model's digit
    # accuracy on long account numbers (the path to 100 % is a stronger vision
    # model + the checksum gate that flags any invalid INN/account for review).
    report = _REPORT_DIR / "invoice_extraction_quality.json"
    assert text_micro >= 0.93, f"text-layer micro {text_micro:.3f} < 0.93 — see {report}"
    assert micro >= 0.85, f"overall micro {micro:.3f} < 0.85 — see {report}"
