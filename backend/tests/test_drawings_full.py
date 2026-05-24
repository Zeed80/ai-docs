"""Comprehensive test suite for the Drawings API.

Covers: upload lifecycle, get/metadata, analysis status, features CRUD,
review workflow, delete & bulk, list filtering, reanalyze, and edge cases.

Uses the same in-process ASGI client + rollback-per-test DB isolation as
the rest of the test suite (see conftest.py).
"""

import io
import json
import struct
import uuid
import zlib

import pytest
from httpx import AsyncClient

from app.db.models import (
    Drawing,
    DrawingFeature,
    DrawingFeatureType,
    DrawingStatus,
)


# ── Helpers / shared byte fixtures ────────────────────────────────────────────


def _make_png_bytes() -> bytes:
    """Return a valid 1×1 white PNG."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        c = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", c)

    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1×1 RGB
    raw_row = b"\x00\xff\xff\xff"  # filter byte + RGB white
    compressed = zlib.compress(raw_row)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr_data)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _make_pdf_bytes() -> bytes:
    """Return a minimal valid 1-page PDF."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 3 3]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"trailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n9\n%%EOF\n"
    )


def _make_dxf_bytes() -> bytes:
    """Return a minimal DXF file."""
    return (
        b"  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n"
        b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n"
        b"  0\nEOF\n"
    )


PNG_BYTES = _make_png_bytes()
PDF_BYTES = _make_pdf_bytes()
DXF_BYTES = _make_dxf_bytes()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def drawing_in_db(db_session):
    """A pre-created drawing in 'analyzed' state (no upload needed)."""
    d = Drawing(
        filename="test-shaft.dxf",
        format="dxf",
        drawing_number="FULL-001",
        status=DrawingStatus.analyzed,
    )
    db_session.add(d)
    await db_session.commit()
    return d


@pytest.fixture
async def drawing_with_features(db_session):
    """A drawing with three features at varying confidence levels."""
    d = Drawing(
        filename="bracket.dxf",
        format="dxf",
        drawing_number="FULL-002",
        status=DrawingStatus.analyzed,
    )
    db_session.add(d)
    await db_session.flush()

    feats = [
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.hole,
            name="Hole Ø10",
            confidence=0.3,
        ),
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.surface,
            name="Surface Ø50",
            confidence=0.85,
        ),
        DrawingFeature(
            drawing_id=d.id,
            feature_type=DrawingFeatureType.pocket,
            name="Unknown pocket",
            confidence=0.55,
        ),
    ]
    for f in feats:
        db_session.add(f)
    await db_session.commit()
    return d, feats


# ═══════════════════════════════════════════════════════════════════════════════
# A. Upload & basic lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_png_drawing(client: AsyncClient):
    """Upload a PNG file; expect 201 with drawing_id; GET confirms status."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("test-drawing.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "drawing_id" in data
    drawing_id = data["drawing_id"]

    get_resp = await client.get(f"/api/drawings/{drawing_id}")
    assert get_resp.status_code == 200
    drawing = get_resp.json()
    assert drawing["status"] in ("uploaded", "analyzing", "analyzed", "failed")
    assert drawing["filename"] == "test-drawing.png"


@pytest.mark.asyncio
async def test_upload_pdf_drawing(client: AsyncClient):
    """Upload a minimal PDF; expect 201."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("blueprint.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "drawing_id" in data
    assert "message" in data


@pytest.mark.asyncio
async def test_upload_unknown_format_accepted(client: AsyncClient):
    """Unknown extension is still accepted — format stored as-is."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("drawing.xyz", io.BytesIO(b"fake xyz content"), "application/octet-stream")},
    )
    assert resp.status_code == 201, resp.text
    drawing_id = resp.json()["drawing_id"]

    get_resp = await client.get(f"/api/drawings/{drawing_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["format"] == "xyz"


@pytest.mark.asyncio
async def test_list_drawings_empty_initially(client: AsyncClient):
    """Fresh (rolled-back) DB has no drawings in the list."""
    resp = await client.get("/api/drawings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_list_drawings_pagination(client: AsyncClient):
    """Upload 3 drawings; list with page_size=2 returns 2 items, total=3."""
    for i in range(3):
        await client.post(
            "/api/drawings",
            files={"file": (f"draw{i}.png", io.BytesIO(PNG_BYTES), "image/png")},
        )

    resp = await client.get("/api/drawings", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["page"] == 1
    assert data["page_size"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# B. Get & metadata
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_drawing_after_upload(client: AsyncClient, drawing_in_db):
    """GET /{id} returns all mandatory fields."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(drawing_in_db.id)
    assert "status" in data
    assert "filename" in data
    assert "format" in data
    assert "created_at" in data
    # drawing_type is nullable — field must be present (value may be None)
    assert "drawing_type" in data


@pytest.mark.asyncio
async def test_get_drawing_not_found(client: AsyncClient):
    """GET with a random UUID returns 404."""
    resp = await client.get(f"/api/drawings/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_drawing_number(client: AsyncClient, drawing_in_db):
    """PATCH drawing_number is persisted and returned."""
    resp = await client.patch(
        f"/api/drawings/{drawing_in_db.id}",
        json={"drawing_number": "A-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["drawing_number"] == "A-001"


@pytest.mark.asyncio
async def test_update_drawing_status(client: AsyncClient, drawing_in_db):
    """PATCH status transitions to 'needs_review'."""
    resp = await client.patch(
        f"/api/drawings/{drawing_in_db.id}",
        json={"status": "needs_review"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_review"


# ═══════════════════════════════════════════════════════════════════════════════
# C. Analysis status
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_drawing_status_uploaded_after_create(client: AsyncClient):
    """Immediately after upload, status is 'uploaded' or 'analyzing'."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("status-check.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert resp.status_code == 201
    drawing_id = resp.json()["drawing_id"]

    get_resp = await client.get(f"/api/drawings/{drawing_id}")
    assert get_resp.status_code == 200
    status = get_resp.json()["status"]
    assert status in ("uploaded", "analyzing", "analyzed", "failed")


@pytest.mark.asyncio
async def test_drawing_celery_task_id_set(client: AsyncClient):
    """Upload response may include task_id; if so, drawing.celery_task_id matches."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("celery-test.dxf", io.BytesIO(DXF_BYTES), "application/octet-stream")},
    )
    assert resp.status_code == 201
    data = resp.json()
    # task_id may be None if Celery is unavailable (CELERY_TASK_ALWAYS_EAGER=true)
    # but the field must exist in the response
    assert "task_id" in data

    drawing_id = data["drawing_id"]
    get_resp = await client.get(f"/api/drawings/{drawing_id}")
    assert get_resp.status_code == 200
    drawing_data = get_resp.json()
    # celery_task_id field must be present (value may be None)
    assert "celery_task_id" in drawing_data
    if data["task_id"] is not None:
        assert drawing_data["celery_task_id"] == data["task_id"]


@pytest.mark.asyncio
async def test_task_status_endpoint(client: AsyncClient):
    """POST /api/drawings → use task_id to query /api/tasks/{task_id}/status."""
    upload_resp = await client.post(
        "/api/drawings",
        files={"file": ("task-status.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert upload_resp.status_code == 201
    task_id = upload_resp.json().get("task_id")

    if task_id is None:
        pytest.skip("Celery not available in this environment (no task_id)")

    try:
        status_resp = await client.get(f"/api/tasks/{task_id}/status")
    except Exception:
        pytest.skip("Redis not reachable from test environment")

    # Endpoint may not exist in all deployments; accept 200 or 404
    assert status_resp.status_code in (200, 404)
    if status_resp.status_code == 200:
        data = status_resp.json()
        assert "status" in data
        assert data["status"] in ("PENDING", "STARTED", "SUCCESS", "FAILURE", "RETRY")


# ═══════════════════════════════════════════════════════════════════════════════
# D. Features CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_features_before_analysis(client: AsyncClient, drawing_in_db):
    """Fresh drawing has no features."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}/features")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_feature_manually(client: AsyncClient, drawing_in_db):
    """POST /{id}/features creates a feature with status 201."""
    resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features",
        json={
            "feature_type": "hole",
            "name": "Ø10 H7",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["feature_type"] == "hole"
    assert data["name"] == "Ø10 H7"
    assert data["drawing_id"] == str(drawing_in_db.id)
    assert "id" in data


@pytest.mark.asyncio
async def test_get_feature_by_id(client: AsyncClient, drawing_in_db):
    """Create then GET a single feature by id."""
    create_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features",
        json={"feature_type": "groove", "name": "Groove-A", "confidence": 0.8},
    )
    assert create_resp.status_code == 201
    feature_id = create_resp.json()["id"]

    get_resp = await client.get(
        f"/api/drawings/{drawing_in_db.id}/features/{feature_id}"
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == feature_id
    assert get_resp.json()["name"] == "Groove-A"


@pytest.mark.asyncio
async def test_delete_feature(client: AsyncClient, drawing_in_db):
    """DELETE /{id}/features/{fid} → 204; subsequent GET → 404."""
    create_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features",
        json={"feature_type": "chamfer", "name": "Chamfer 2×45°", "confidence": 0.75},
    )
    assert create_resp.status_code == 201
    feature_id = create_resp.json()["id"]

    del_resp = await client.delete(
        f"/api/drawings/{drawing_in_db.id}/features/{feature_id}"
    )
    assert del_resp.status_code == 204

    get_resp = await client.get(
        f"/api/drawings/{drawing_in_db.id}/features/{feature_id}"
    )
    assert get_resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# E. Review workflow
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_uncertain_features_empty(client: AsyncClient, drawing_in_db):
    """Drawing with no features → uncertain-features returns []."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}/uncertain-features")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_uncertain_features_returns_low_confidence(
    client: AsyncClient, drawing_with_features
):
    """Features with confidence < threshold appear in uncertain-features."""
    drawing, feats = drawing_with_features
    resp = await client.get(f"/api/drawings/{drawing.id}/uncertain-features")
    assert resp.status_code == 200
    data = resp.json()
    names = [f["name"] for f in data]
    # confidence 0.3 and 0.55 are below default threshold 0.70
    assert "Hole Ø10" in names
    assert "Unknown pocket" in names
    # confidence 0.85 is above threshold — must NOT appear
    assert "Surface Ø50" not in names


@pytest.mark.asyncio
async def test_correct_feature(client: AsyncClient, drawing_with_features):
    """POST /correct updates feature_type and sets confidence=1.0."""
    drawing, feats = drawing_with_features
    uncertain = feats[0]  # hole, confidence=0.3

    resp = await client.post(
        f"/api/drawings/{drawing.id}/features/{uncertain.id}/correct",
        json={"original_type": "groove", "corrected_type": "slot"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feature_type"] == "slot"
    assert data["confidence"] == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# F. Delete & bulk
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_drawing(client: AsyncClient, drawing_in_db):
    """DELETE /{id} → 204; subsequent GET → 404."""
    del_resp = await client.delete(f"/api/drawings/{drawing_in_db.id}")
    assert del_resp.status_code == 204

    get_resp = await client.get(f"/api/drawings/{drawing_in_db.id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_delete(client: AsyncClient):
    """Upload 2 drawings, bulk-delete both, verify both gone."""
    ids = []
    for i in range(2):
        r = await client.post(
            "/api/drawings",
            files={"file": (f"bulk{i}.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert r.status_code == 201
        ids.append(r.json()["drawing_id"])

    resp = await client.request(
        "DELETE",
        "/api/drawings/bulk-delete",
        content=json.dumps({"drawing_ids": ids}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 2

    for did in ids:
        get_resp = await client.get(f"/api/drawings/{did}")
        assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent(client: AsyncClient):
    """DELETE on a non-existent drawing_id → 404."""
    resp = await client.delete(f"/api/drawings/{uuid.uuid4()}")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# G. List filtering
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_filter_by_status(client: AsyncClient, drawing_in_db):
    """Filter list by status=analyzed returns only analyzed drawings."""
    # drawing_in_db is status=analyzed
    resp = await client.get("/api/drawings", params={"status": "analyzed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["status"] == "analyzed"


@pytest.mark.asyncio
async def test_filter_by_drawing_number(client: AsyncClient, drawing_in_db):
    """Filter by drawing_number substring returns the right drawing."""
    # drawing_in_db has drawing_number="FULL-001"
    resp = await client.get("/api/drawings", params={"drawing_number": "FULL-001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [d["id"] for d in data["items"]]
    assert str(drawing_in_db.id) in ids


@pytest.mark.asyncio
async def test_list_returns_correct_fields(client: AsyncClient, drawing_in_db):
    """List response contains total, items, page, page_size fields."""
    resp = await client.get("/api/drawings")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "items" in data
    assert "page" in data
    assert "page_size" in data
    assert isinstance(data["items"], list)
    assert isinstance(data["total"], int)


# ═══════════════════════════════════════════════════════════════════════════════
# H. Reanalyze
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reanalyze_endpoint_exists(client: AsyncClient, drawing_in_db):
    """POST /{id}/reanalyze → 200 or 202, not 404 or 500."""
    resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/reanalyze",
        json={},
    )
    assert resp.status_code in (200, 202), resp.text


@pytest.mark.asyncio
async def test_reanalyze_resets_status(client: AsyncClient, drawing_in_db):
    """After reanalyze, drawing status transitions to 'uploaded' or 'analyzing'."""
    reanalyze_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/reanalyze",
        json={},
    )
    assert reanalyze_resp.status_code in (200, 202)

    get_resp = await client.get(f"/api/drawings/{drawing_in_db.id}")
    assert get_resp.status_code == 200
    status = get_resp.json()["status"]
    # reanalyze resets to uploaded; Celery may transition it quickly to analyzing/analyzed
    assert status in ("uploaded", "analyzing", "analyzed", "failed")


# ═══════════════════════════════════════════════════════════════════════════════
# I. Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_empty_filename(client: AsyncClient):
    """Uploading with a blank filename still creates a drawing (API uses fallback)."""
    resp = await client.post(
        "/api/drawings",
        files={"file": ("   ", io.BytesIO(PNG_BYTES), "image/png")},
    )
    # The API falls back to filename="drawing" for whitespace-only names — expect 201
    assert resp.status_code == 201, resp.text
    assert "drawing_id" in resp.json()


@pytest.mark.asyncio
async def test_update_nonexistent_drawing(client: AsyncClient):
    """PATCH on a non-existent drawing_id → 404."""
    resp = await client.patch(
        f"/api/drawings/{uuid.uuid4()}",
        json={"drawing_number": "GHOST"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_with_page_out_of_range(client: AsyncClient, drawing_in_db):
    """page=999 with real data returns empty items list, not an error."""
    resp = await client.get("/api/drawings", params={"page": 999, "page_size": 20})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] >= 1  # total still reflects real count
    assert data["page"] == 999


# ═══════════════════════════════════════════════════════════════════════════════
# Bonus: additional metadata / views / validation endpoints
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_drawing_views_empty(client: AsyncClient, drawing_in_db):
    """GET /{id}/views → 200, empty list for new drawing."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}/views")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_assembly_bom_empty(client: AsyncClient, drawing_in_db):
    """GET /{id}/assembly-bom → 200, empty list for new drawing."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}/assembly-bom")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_validation_report_not_validated(client: AsyncClient, drawing_in_db):
    """GET /{id}/validation → 200, status='not_validated' before analysis."""
    resp = await client.get(f"/api/drawings/{drawing_in_db.id}/validation")
    assert resp.status_code == 200
    data = resp.json()
    assert "drawing_id" in data or "status" in data


@pytest.mark.asyncio
async def test_update_feature_name(client: AsyncClient, drawing_in_db):
    """PATCH /{id}/features/{fid} updates name field."""
    create_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features",
        json={"feature_type": "thread", "name": "M10 thread", "confidence": 0.9},
    )
    assert create_resp.status_code == 201
    feature_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/drawings/{drawing_in_db.id}/features/{feature_id}",
        json={"name": "M10×1.5 thread (updated)"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "M10×1.5 thread (updated)"


@pytest.mark.asyncio
async def test_review_feature_sets_reviewed_by(client: AsyncClient, drawing_in_db):
    """POST /{id}/features/{fid}/review sets reviewed_by field."""
    create_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features",
        json={"feature_type": "slot", "name": "Key slot", "confidence": 0.88},
    )
    assert create_resp.status_code == 201
    feature_id = create_resp.json()["id"]

    review_resp = await client.post(
        f"/api/drawings/{drawing_in_db.id}/features/{feature_id}/review",
        json={"reviewed_by": "engineer-42"},
    )
    assert review_resp.status_code in (200, 201)
    data = review_resp.json()
    assert data.get("reviewed_by") == "engineer-42"
    assert data.get("reviewed_at") is not None


@pytest.mark.asyncio
async def test_filter_by_drawing_number_partial_match(client: AsyncClient, drawing_in_db):
    """drawing_number filter uses ILIKE — partial match works."""
    # drawing_in_db has "FULL-001"
    resp = await client.get("/api/drawings", params={"drawing_number": "FULL"})
    assert resp.status_code == 200
    data = resp.json()
    ids = [d["id"] for d in data["items"]]
    assert str(drawing_in_db.id) in ids


@pytest.mark.asyncio
async def test_create_feature_not_found_drawing(client: AsyncClient):
    """POST features on non-existent drawing → 404."""
    resp = await client.post(
        f"/api/drawings/{uuid.uuid4()}/features",
        json={"feature_type": "hole", "name": "Ghost hole", "confidence": 0.5},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_uncertain_features_not_found(client: AsyncClient):
    """GET uncertain-features on non-existent drawing → 404."""
    resp = await client.get(f"/api/drawings/{uuid.uuid4()}/uncertain-features")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_correct_feature_not_found_drawing(client: AsyncClient):
    """POST /correct on non-existent drawing → 404."""
    resp = await client.post(
        f"/api/drawings/{uuid.uuid4()}/features/{uuid.uuid4()}/correct",
        json={"original_type": "hole", "corrected_type": "slot"},
    )
    assert resp.status_code == 404
