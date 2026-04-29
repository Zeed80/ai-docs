"""Integration test — extraction pipeline on real invoices via Ollama.

Requires: Ollama running locally with gemma4:e4b model.
Run with: python3 -m pytest tests/test_extraction_integration.py -v -s --timeout=300
Skip if Ollama unavailable: @pytest.mark.skipif
"""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

# Check Ollama availability
def _ollama_available() -> bool:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

OLLAMA_AVAILABLE = _ollama_available()
EXAMPLE_DIR = Path(__file__).parent.parent.parent / "example-invoices"

# Pick a few representative invoices from different suppliers
SAMPLE_INVOICES = [
    "Инатек-М № 15 от 10 января 2024 г..pdf",
    "NVS № 100 от 1 февраля 2024 г..pdf",
    "Xoffmann № ПРЗ2345874 от 02 февраля 2024 г.pdf",
]


@pytest.mark.skipif(not OLLAMA_AVAILABLE, reason="Ollama not available")
@pytest.mark.skipif(not EXAMPLE_DIR.exists(), reason="example-invoices not found")
class TestExtractionIntegration:
    """Integration tests using real invoices and Ollama."""

    def _get_sample_path(self, filename: str) -> Path:
        path = EXAMPLE_DIR / filename
        if not path.exists():
            # Try first available
            pdfs = list(EXAMPLE_DIR.glob("*.pdf"))
            if pdfs:
                return pdfs[0]
            pytest.skip("No PDF files in example-invoices/")
        return path

    @pytest.mark.asyncio
    async def test_classify_real_invoice(self):
        """Classify a real invoice PDF using gemma4:e4b."""
        from app.ai.extraction_prompts import CLASSIFY_SYSTEM, CLASSIFY_PROMPT
        from app.ai.ollama_client import generate_json
        from app.ai.pdf_processor import extract_pdf

        pdf_path = self._get_sample_path(SAMPLE_INVOICES[0])
        content = pdf_path.read_bytes()

        # Extract text
        pdf_data = extract_pdf(content, render_pages=False)
        assert pdf_data.full_text, f"No text extracted from {pdf_path.name}"
        print(f"\n--- Text from {pdf_path.name} ({len(pdf_data.full_text)} chars) ---")
        print(pdf_data.full_text[:500])

        # Classify
        prompt = CLASSIFY_PROMPT.format(text=pdf_data.full_text[:3000])
        result = await generate_json(
            prompt,
            model="gemma4:e4b",
            system=CLASSIFY_SYSTEM,
            timeout_seconds=60.0,
        )

        print(f"\n--- Classification result ---")
        print(result)

        assert "type" in result
        assert result["type"] == "invoice", f"Expected 'invoice', got '{result['type']}'"
        assert "confidence" in result
        assert result["confidence"] >= 0.5, f"Low confidence: {result['confidence']}"

    @pytest.mark.asyncio
    async def test_extract_real_invoice(self):
        """Full extraction of a real invoice with field validation."""
        from app.ai.extraction_prompts import EXTRACT_INVOICE_SYSTEM, EXTRACT_INVOICE_PROMPT
        from app.ai.ollama_client import generate_json
        from app.ai.pdf_processor import extract_pdf
        from app.ai.confidence import validate_arithmetic, compute_field_confidences, compute_overall_confidence

        pdf_path = self._get_sample_path(SAMPLE_INVOICES[0])
        content = pdf_path.read_bytes()

        pdf_data = extract_pdf(content, render_pages=False)
        assert pdf_data.full_text

        # Extract
        prompt = EXTRACT_INVOICE_PROMPT.format(text=pdf_data.full_text[:8000])
        extracted = await generate_json(
            prompt,
            model="gemma4:e4b",
            system=EXTRACT_INVOICE_SYSTEM,
            max_tokens=8192,
            timeout_seconds=180.0,
        )

        print(f"\n--- Extraction result from {pdf_path.name} ---")
        for key in ["invoice_number", "invoice_date", "total_amount", "currency"]:
            print(f"  {key}: {extracted.get(key)}")
        print(f"  lines: {len(extracted.get('lines', []))}")
        print(f"  supplier: {extracted.get('supplier', {}).get('name')}")

        # Basic field presence
        assert extracted.get("invoice_number"), "invoice_number not extracted"
        assert extracted.get("lines"), "No line items extracted"
        assert len(extracted["lines"]) >= 1

        # Validate arithmetic
        errors = validate_arithmetic(extracted)
        print(f"\n--- Validation errors: {len(errors)} ---")
        for e in errors:
            print(f"  {e['field']}: {e['message']}")

        # Confidence scoring
        ai_confs = extracted.get("field_confidences", {})
        field_confs = compute_field_confidences(extracted, ai_confs, errors)
        overall = compute_overall_confidence(field_confs)
        print(f"\n--- Overall confidence: {overall:.2f} ---")
        for fc in field_confs:
            print(f"  {fc.field_name}: {fc.confidence:.2f} ({fc.reason})")

        assert overall > 0.3, f"Overall confidence too low: {overall}"

    @pytest.mark.asyncio
    async def test_bbox_binding(self):
        """Test bbox binding on a real invoice."""
        from app.ai.extraction_prompts import EXTRACT_INVOICE_SYSTEM, EXTRACT_INVOICE_PROMPT
        from app.ai.ollama_client import generate_json
        from app.ai.pdf_processor import extract_pdf, bind_bboxes

        pdf_path = self._get_sample_path(SAMPLE_INVOICES[0])
        content = pdf_path.read_bytes()

        pdf_data = extract_pdf(content, render_pages=False)

        prompt = EXTRACT_INVOICE_PROMPT.format(text=pdf_data.full_text[:8000])
        extracted = await generate_json(
            prompt,
            model="gemma4:e4b",
            system=EXTRACT_INVOICE_SYSTEM,
            max_tokens=8192,
            timeout_seconds=180.0,
        )

        # Build field→value map for bbox binding
        field_values = {}
        for key in ["invoice_number", "invoice_date", "total_amount"]:
            val = extracted.get(key)
            if val is not None:
                field_values[key] = str(val)

        bbox_map = bind_bboxes(pdf_data.pages, field_values)
        print(f"\n--- Bbox binding ({len(bbox_map)}/{len(field_values)} fields) ---")
        for field, bbox in bbox_map.items():
            if bbox:
                print(f"  {field}: page={bbox['page']}, x={bbox['x']:.1f}, y={bbox['y']:.1f}")
            else:
                print(f"  {field}: not found")

        # At least one field should have bbox
        bound = sum(1 for v in bbox_map.values() if v is not None)
        print(f"  Bound: {bound}/{len(field_values)}")

    @pytest.mark.asyncio
    async def test_multiple_invoices(self):
        """Test extraction across multiple suppliers."""
        from app.ai.extraction_prompts import CLASSIFY_SYSTEM, CLASSIFY_PROMPT
        from app.ai.ollama_client import generate_json
        from app.ai.pdf_processor import extract_pdf

        results = []
        for filename in SAMPLE_INVOICES:
            path = EXAMPLE_DIR / filename
            if not path.exists():
                continue

            content = path.read_bytes()
            pdf_data = extract_pdf(content, render_pages=False)
            if not pdf_data.full_text:
                results.append({"file": filename, "status": "no_text"})
                continue

            prompt = CLASSIFY_PROMPT.format(text=pdf_data.full_text[:3000])
            try:
                result = await generate_json(
                    prompt,
                    model="gemma4:e4b",
                    system=CLASSIFY_SYSTEM,
                    timeout_seconds=60.0,
                )
                results.append({
                    "file": filename,
                    "type": result.get("type"),
                    "confidence": result.get("confidence"),
                    "status": "ok",
                })
            except Exception as e:
                results.append({"file": filename, "status": "error", "error": str(e)})

        print(f"\n--- Multi-invoice classification ({len(results)} files) ---")
        for r in results:
            print(f"  {r['file']}: {r.get('type', '?')} ({r.get('confidence', '?')}) [{r['status']}]")

        ok_count = sum(1 for r in results if r["status"] == "ok")
        assert ok_count >= 1, "No invoices successfully classified"

        # All should be classified as invoice
        invoice_count = sum(1 for r in results if r.get("type") == "invoice")
        print(f"  Classified as invoice: {invoice_count}/{ok_count}")
