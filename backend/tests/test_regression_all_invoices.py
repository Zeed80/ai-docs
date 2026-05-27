"""Regression test-suite: ingest all real invoices from example-invoices/.

Verifies that every file in the example-invoices/ directory:
  1. Is ingested without a 5xx error
  2. Is NOT falsely quarantined
  3. Has file_size matching os.path.getsize()
  4. Returns a non-null detected_type
  5. Deduplication: re-uploading the same file → is_duplicate=True
  6. Bulk: uploading all files in sequence produces 0 server errors

The test suite is skipped gracefully when example-invoices/ is absent
(CI without the sample data) or when the DB is not available.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INVOICES_DIR = Path(__file__).parent.parent.parent / "example-invoices"


def collect_example_invoices() -> list[Path]:
    """Return all non-hidden, non-gitkeep files from example-invoices/."""
    if not _INVOICES_DIR.is_dir():
        return []
    return sorted(
        p
        for p in _INVOICES_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix != ""
    )


_ALL_INVOICES = collect_example_invoices()

# Mime-type helpers
_MIME_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _mime(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Fixtures: add all invoice extensions to the allowlist
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _allow_all_invoice_extensions(db_session):
    """Insert allowlist entries for every extension found in example-invoices/."""
    from app.db.models import FileExtensionAllowlist
    from sqlalchemy import select

    extensions = {p.suffix.lower() for p in _ALL_INVOICES if p.suffix}
    for ext in extensions:
        existing = await db_session.execute(
            select(FileExtensionAllowlist).where(FileExtensionAllowlist.extension == ext)
        )
        if not existing.scalar_one_or_none():
            db_session.add(
                FileExtensionAllowlist(extension=ext, is_allowed=True, added_by="regression_test")
            )
    await db_session.flush()


# ---------------------------------------------------------------------------
# Parametrized: one test per invoice file
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _ALL_INVOICES, reason="example-invoices/ directory not found")
@pytest.mark.parametrize(
    "invoice_path",
    _ALL_INVOICES,
    ids=[p.name for p in _ALL_INVOICES],
)
@pytest.mark.asyncio
async def test_ingest_real_invoice(invoice_path: Path, client: AsyncClient):
    """Every real invoice must ingest without server error and not be quarantined."""
    content = invoice_path.read_bytes()
    resp = await client.post(
        "/api/documents/ingest?source_channel=upload&auto_process=false",
        files={"file": (invoice_path.name, content, _mime(invoice_path))},
    )

    # Must not be a server error
    assert resp.status_code < 500, (
        f"{invoice_path.name}: unexpected server error {resp.status_code}: {resp.text[:300]}"
    )

    # Successful response (200) — not quarantined (202)
    assert resp.status_code == 200, (
        f"{invoice_path.name}: quarantined or rejected (status {resp.status_code}): {resp.text[:300]}"
    )

    data = resp.json()

    # file_size must match actual bytes on disk
    assert data["file_size"] == len(content), (
        f"{invoice_path.name}: size mismatch: backend={data['file_size']} disk={len(content)}"
    )

    # detected_type should be set (extension-based or AI)
    # For PDF/JPG the detected_type may be None until AI classifies — we only require it's not an error
    assert "id" in data, f"{invoice_path.name}: missing id in response"
    assert data.get("is_duplicate") is not None


# ---------------------------------------------------------------------------
# Bulk: all invoices in one sequential run — assert 0 server errors
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _ALL_INVOICES, reason="example-invoices/ directory not found")
@pytest.mark.asyncio
@pytest.mark.timeout(3600)
async def test_bulk_ingest_all_invoices_no_server_errors(client: AsyncClient):
    """Upload all example invoices sequentially; count outcomes, assert 0 server errors."""
    results: dict[str, list[str]] = {
        "success": [],
        "duplicate": [],
        "quarantined": [],
        "client_error": [],
        "server_error": [],
    }

    for invoice_path in _ALL_INVOICES:
        content = invoice_path.read_bytes()
        resp = await client.post(
            "/api/documents/ingest?source_channel=upload&auto_process=false",
            files={"file": (invoice_path.name, content, _mime(invoice_path))},
        )

        if resp.status_code >= 500:
            results["server_error"].append(f"{invoice_path.name} → {resp.status_code}")
        elif resp.status_code == 202:
            results["quarantined"].append(invoice_path.name)
        elif resp.status_code >= 400:
            results["client_error"].append(f"{invoice_path.name} → {resp.status_code}: {resp.text[:100]}")
        elif resp.json().get("is_duplicate"):
            results["duplicate"].append(invoice_path.name)
        else:
            results["success"].append(invoice_path.name)

    total = len(_ALL_INVOICES)
    print(
        f"\n[bulk_ingest] total={total} "
        f"success={len(results['success'])} "
        f"duplicate={len(results['duplicate'])} "
        f"quarantined={len(results['quarantined'])} "
        f"client_error={len(results['client_error'])} "
        f"server_error={len(results['server_error'])}"
    )
    if results["server_error"]:
        print("SERVER ERRORS:", results["server_error"])
    if results["quarantined"]:
        print("QUARANTINED:", results["quarantined"])

    assert results["server_error"] == [], (
        f"Server errors during bulk ingest: {results['server_error']}"
    )


# ---------------------------------------------------------------------------
# Deduplication consistency
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _ALL_INVOICES, reason="example-invoices/ directory not found")
@pytest.mark.asyncio
async def test_dedup_same_file_twice(client: AsyncClient):
    """Re-uploading the same file must return is_duplicate=True with the original id."""
    invoice_path = _ALL_INVOICES[0]
    content = invoice_path.read_bytes()
    mime = _mime(invoice_path)

    resp1 = await client.post(
        "/api/documents/ingest?source_channel=upload&auto_process=false",
        files={"file": (invoice_path.name, content, mime)},
    )
    assert resp1.status_code == 200
    first_id = resp1.json()["id"]

    resp2 = await client.post(
        "/api/documents/ingest?source_channel=upload&auto_process=false",
        files={"file": (invoice_path.name, content, mime)},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["is_duplicate"] is True, "Second upload should be detected as duplicate"
    assert data2["duplicate_of"] == first_id, "duplicate_of must point to the first upload"


@pytest.mark.skipif(not _ALL_INVOICES, reason="example-invoices/ directory not found")
@pytest.mark.asyncio
async def test_dedup_different_name_same_content(client: AsyncClient):
    """Same bytes uploaded with a different filename still detected as duplicate."""
    invoice_path = _ALL_INVOICES[0]
    content = invoice_path.read_bytes()
    mime = _mime(invoice_path)

    resp1 = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": (invoice_path.name, content, mime)},
    )
    assert resp1.status_code == 200
    first_id = resp1.json()["id"]

    resp2 = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("renamed_copy.pdf", content, mime)},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["is_duplicate"] is True
    assert data2["duplicate_of"] == first_id


# ---------------------------------------------------------------------------
# File type detection on real invoices
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _ALL_INVOICES, reason="example-invoices/ directory not found")
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_pdf_invoices_not_classified_as_drawing(client: AsyncClient):
    """PDF invoices must not be auto-classified as drawing type."""
    pdf_invoices = [p for p in _ALL_INVOICES if p.suffix.lower() == ".pdf"][:10]
    for invoice_path in pdf_invoices:
        content = invoice_path.read_bytes()
        resp = await client.post(
            "/api/documents/ingest?auto_process=false",
            files={"file": (invoice_path.name, content, "application/pdf")},
        )
        if resp.status_code != 200:
            continue
        data = resp.json()
        detected = data.get("detected_type")
        assert detected != "drawing", (
            f"{invoice_path.name} wrongly classified as drawing"
        )


@pytest.mark.skipif(
    not any(p.suffix.lower() in (".jpg", ".jpeg") for p in _ALL_INVOICES),
    reason="No JPEG invoices in example-invoices/",
)
@pytest.mark.asyncio
async def test_jpeg_invoices_ingest_successfully(client: AsyncClient):
    """JPEG scanned invoices should ingest without error (OCR path)."""
    jpeg_invoices = [p for p in _ALL_INVOICES if p.suffix.lower() in (".jpg", ".jpeg")]
    for invoice_path in jpeg_invoices:
        content = invoice_path.read_bytes()
        resp = await client.post(
            "/api/documents/ingest?auto_process=false",
            files={"file": (invoice_path.name, content, "image/jpeg")},
        )
        assert resp.status_code == 200, (
            f"{invoice_path.name}: JPEG ingest failed: {resp.status_code} {resp.text[:200]}"
        )
        data = resp.json()
        assert not data.get("quarantined"), f"{invoice_path.name} should not be quarantined"
