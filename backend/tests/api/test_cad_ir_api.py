"""API tests for the vectorize/CAD-IR surface of /api/image-gen."""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_storage(monkeypatch):
    """In-memory MinIO stand-in for both the API module and the IR store."""
    blobs: dict[str, bytes] = {}

    def _upload(content: bytes, path: str, content_type: str = "application/octet-stream") -> str:
        blobs[path] = content
        return path

    def _download(path: str) -> bytes:
        if path not in blobs:
            raise KeyError(path)
        return blobs[path]

    for mod in ("app.services.cad_ir_store", "app.api.image_generation"):
        monkeypatch.setattr(f"{mod}.upload_file", _upload)
        monkeypatch.setattr(f"{mod}.download_file", _download)
    return blobs


async def _mark_full_check_current(db_session, generation_id: str) -> int:
    """Mark the current revision as checked without invoking external models."""
    import uuid

    import sqlalchemy as sa

    from app.db.models import CadIrRevision, ImageGeneration

    gen_id = uuid.UUID(generation_id)
    revision = (
        await db_session.execute(
            sa.select(sa.func.max(CadIrRevision.revision)).where(
                CadIrRevision.generation_id == gen_id,
            )
        )
    ).scalar_one()
    gen = await db_session.get(ImageGeneration, gen_id)
    assert gen is not None
    gen.params = {
        **(gen.params or {}),
        "full_check_revision": revision,
        "full_check_status": "passed",
    }
    await db_session.commit()
    return int(revision)


@pytest.mark.asyncio
async def test_generate_vectorize_requires_source(client):
    resp = await client.post("/api/image-gen/generate", json={"operation": "vectorize"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_blank_sheet_creates_revision_zero(client, fake_storage):
    resp = await client.post("/api/image-gen/blank-sheet", json={"format": "A4", "title": "Эскиз"})
    assert resp.status_code == 200
    gen = resp.json()
    assert gen["operation"] == "vectorize"
    assert gen["status"] == "done"
    assert gen["params"]["ir_path"] in fake_storage
    assert gen["params"]["dxf_path"] in fake_storage
    assert gen["params"]["svg_path"] in fake_storage

    ir_resp = await client.get(f"/api/image-gen/{gen['id']}/ir")
    assert ir_resp.status_code == 200
    data = ir_resp.json()
    assert data["revision"] == 0
    assert data["ir"]["sheet"]["format"] == "A4"
    assert data["ir"]["scale"] == pytest.approx(0.25)
    assert data["ir"]["entities"] == []


@pytest.mark.asyncio
async def test_blank_sheet_with_frame_adds_editable_stamp_entities(client, fake_storage):
    """Ф5.5: with_frame=True must produce a real, editable frame + ГОСТ
    2.104 stamp — not just a static picture (Segments for the border/grid,
    a TextEntity for the designation the user can click and edit)."""
    resp = await client.post(
        "/api/image-gen/blank-sheet",
        json={
            "format": "A4",
            "landscape": True,
            "with_frame": True,
            "title": "Вал приводной",
            "designation": "АБВГ.12345.001",
            "company": "ООО Завод",
        },
    )
    assert resp.status_code == 200
    gen = resp.json()

    ir_resp = await client.get(f"/api/image-gen/{gen['id']}/ir")
    data = ir_resp.json()["ir"]
    assert data["sheet"]["frame"] is True
    assert data["sheet"]["title_block"]["detected"] is True
    segments = [e for e in data["entities"] if e["type"] == "segment"]
    texts = [e for e in data["entities"] if e["type"] == "text"]
    assert len(segments) >= 4  # sheet border, at minimum
    assert any(t["text"] == "Вал приводной" for t in texts)
    assert any(t["text"] == "АБВГ.12345.001" for t in texts)
    assert all(e["assurance"] == "human_approved" for e in data["entities"])

    dxf = fake_storage[gen["params"]["dxf_path"]]
    assert b"LINE" in dxf
    assert "12345".encode() in dxf


@pytest.mark.asyncio
async def test_blank_sheet_without_frame_is_still_empty(client, fake_storage):
    resp = await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})
    ir_resp = await client.get(f"/api/image-gen/{resp.json()['id']}/ir")
    data = ir_resp.json()["ir"]
    assert data["sheet"]["frame"] is False
    assert data["entities"] == []


@pytest.mark.asyncio
async def test_patch_ir_add_update_delete_cycle(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    add = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment",
            "p1": {"x": 100, "y": 100},
            "p2": {"x": 500, "y": 100},
            "line_class": "contour",
            "width_class": "main",
        }}]},
    )
    assert add.status_code == 200
    body = add.json()
    assert body["revision"] == 1
    assert body["origin"] == "editor"
    assert len(body["ir"]["entities"]) == 1
    entity_id = body["ir"]["entities"][0]["id"]
    assert body["ir"]["entities"][0]["origin"] == "human"

    # DXF was re-rendered for the new revision and contains our line.
    dxf = fake_storage[body["summary"] and (await client.get(f"/api/image-gen/{gen_id}")).json()["params"]["dxf_path"]]
    assert b"LINE" in dxf

    upd = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "update", "entity_id": entity_id, "entity": {
            "type": "segment",
            "p1": {"x": 100, "y": 100},
            "p2": {"x": 500, "y": 300},
        }}]},
    )
    assert upd.status_code == 200
    assert upd.json()["revision"] == 2
    assert upd.json()["ir"]["entities"][0]["p2"]["y"] == 300

    dele = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "delete", "entity_id": entity_id}]},
    )
    assert dele.status_code == 200
    assert dele.json()["ir"]["entities"] == []
    assert dele.json()["revision"] == 3


async def test_patch_ir_set_sheet_format_derives_scale(client, fake_storage):
    # A blank A4 sheet knows its own format already, but set_sheet_format
    # must recompute mm/px from the format and mark the source authoritative
    # (B6 one-step scale confirmation). Switching A4→A3 doubles the long
    # side (297→420) so mm/px must grow accordingly.
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "set_sheet_format", "sheet_format": "A3"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"] == "review"
    ir = body["ir"]
    assert ir["scale_source"] == "sheet_format"
    assert ir["sheet"]["format"] == "A3"
    # Scale uses both paper axes; it cannot silently stretch one axis to make
    # a non-matching aspect ratio look exact.
    w, h = ir["source"]["image_width"], ir["source"]["image_height"]
    assert ir["scale"] == pytest.approx(((297.0 / w) + (420.0 / h)) / 2, rel=1e-6)


async def test_patch_ir_set_sheet_format_rejects_unknown(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "set_sheet_format", "sheet_format": "B7"}]},
    )
    assert resp.status_code == 400


async def test_patch_ir_set_title_block_fills_stamp(client, fake_storage):
    # C3: filling the основная надпись stores structured fields, renders the
    # stamp labels, and clears the "title block incomplete" ЕСКД finding.
    gen = (await client.post(
        "/api/image-gen/blank-sheet", json={"format": "A4", "with_frame": True}
    )).json()
    gen_id = gen["id"]

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "set_title_block", "title_block": {
            "designation": "АБВГ.301256.001",
            "name": "Вал ведущий",
            "material": "Сталь 45 ГОСТ 1050",
            "scale": "1:2",
            "mass_kg": 3.4,
        }}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    ir = body["ir"]
    assert ir["sheet"]["title_block"]["fields"]["designation"] == "АБВГ.301256.001"
    labels = [
        e for e in ir["entities"]
        if e["type"] == "text" and "title_block_text" in (e.get("evidence") or [])
    ]
    assert any(e["text"] == "Вал ведущий" for e in labels)
    codes = {i["code"] for i in ir["validation"]["issues"]}
    assert "ESKD_TITLE_BLOCK_INCOMPLETE" not in codes

    # DXF export carries the stamp text.
    dxf = fake_storage[(await client.get(f"/api/image-gen/{gen_id}")).json()["params"]["dxf_path"]]
    assert "Вал ведущий".encode() in dxf


async def test_patch_ir_add_annotation_and_export(client, fake_storage):
    # C4: a structured annotation adds via the same add op, validates, and
    # lands in the DXF export as text.
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "annotation", "kind": "roughness", "value": "3.2",
            "position": {"x": 100, "y": 100}, "line_class": "dim", "width_class": "thin",
        }}]},
    )
    assert resp.status_code == 200
    ir = resp.json()["ir"]
    ann = [e for e in ir["entities"] if e["type"] == "annotation"]
    assert len(ann) == 1 and ann[0]["kind"] == "roughness"
    dxf = fake_storage[(await client.get(f"/api/image-gen/{gen_id}")).json()["params"]["dxf_path"]]
    assert "Ra 3.2".encode() in dxf


async def test_patch_ir_add_invalid_annotation_flags_validation(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "annotation", "kind": "roughness", "value": "3.0",  # off-series
            "position": {"x": 100, "y": 100}, "line_class": "dim", "width_class": "thin",
        }}]},
    )
    assert resp.status_code == 200
    codes = {i["code"] for i in resp.json()["ir"]["validation"]["issues"]}
    assert "ESKD_ANNOTATION_INVALID" in codes


async def test_release_manifest_blocks_until_accepted(client, fake_storage, monkeypatch):
    # C5: manifest is 409 before acceptance, then reproducible after.
    async def _no_llm(png_bytes, **kwargs):
        return []

    monkeypatch.setattr("app.ai.cad_validate.run_llm_review_levels", _no_llm)

    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 100, "y": 100}, "p2": {"x": 500, "y": 100},
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    blocked = await client.get(f"/api/image-gen/{gen_id}/release-manifest")
    assert blocked.status_code == 409

    # Full-check + accept, then the manifest releases.
    await client.post(f"/api/image-gen/{gen_id}/ir/full-check")
    accepted = await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")
    assert accepted.status_code == 200, accepted.text

    resp = await client.get(f"/api/image-gen/{gen_id}/release-manifest")
    assert resp.status_code == 200
    m = resp.json()
    assert m["fully_reproducible"] is True
    assert m["dxf_version"] == "R2010"
    assert m["approval"]["accepted_by"]
    assert m["manifest_sha256"]

    pkg = await client.get(f"/api/image-gen/{gen_id}/release-package")
    assert pkg.status_code == 200
    assert pkg.headers["content-type"] == "application/zip"
    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(pkg.content))
    assert "manifest.json" in zf.namelist()
    assert "drawing.dxf" in zf.namelist()


async def _add_segment(client, gen_id, p1, p2):
    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": p1, "p2": p2,
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    return resp.json()["ir"]["entities"][-1]["id"]


@pytest.mark.asyncio
async def test_patch_ir_move_translates_entity(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    eid = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 10, "y": 0})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "move", "entity_id": eid, "dx": 5, "dy": 7}]},
    )
    assert resp.status_code == 200
    entity = resp.json()["ir"]["entities"][0]
    assert entity["id"] == eid
    assert entity["p1"] == {"x": 5, "y": 7}
    assert entity["p2"] == {"x": 15, "y": 7}


@pytest.mark.asyncio
async def test_patch_ir_copy_creates_new_entity_with_new_id(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    eid = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 10, "y": 0})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "copy", "entity_id": eid, "dx": 100, "dy": 0}]},
    )
    assert resp.status_code == 200
    entities = resp.json()["ir"]["entities"]
    assert len(entities) == 2
    ids = {e["id"] for e in entities}
    assert eid in ids
    new = next(e for e in entities if e["id"] != eid)
    assert new["p1"] == {"x": 100, "y": 0}


@pytest.mark.asyncio
async def test_patch_ir_mirror_reflects_across_line(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    eid = await _add_segment(client, gen_id, {"x": 10, "y": 0}, {"x": 20, "y": 0})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{
            "op": "mirror", "entity_id": eid,
            "mirror_p1": {"x": 0, "y": 0}, "mirror_p2": {"x": 0, "y": 1},
        }]},
    )
    assert resp.status_code == 200
    entity = resp.json()["ir"]["entities"][0]
    assert entity["p1"]["x"] == pytest.approx(-10)
    assert entity["p2"]["x"] == pytest.approx(-20)


@pytest.mark.asyncio
async def test_patch_ir_fillet_two_segments(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    e1 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 100, "y": 0})
    e2 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 0, "y": 100})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "fillet", "entity_id": e1, "entity_id_2": e2, "value": 10}]},
    )
    assert resp.status_code == 200
    entities = resp.json()["ir"]["entities"]
    assert len(entities) == 3  # two trimmed segments + one new arc
    arcs = [e for e in entities if e["type"] == "arc"]
    assert len(arcs) == 1
    assert arcs[0]["radius"] == pytest.approx(10)


@pytest.mark.asyncio
async def test_patch_ir_chamfer_two_segments(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    e1 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 100, "y": 0})
    e2 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 0, "y": 100})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "chamfer", "entity_id": e1, "entity_id_2": e2, "value": 10}]},
    )
    assert resp.status_code == 200
    entities = resp.json()["ir"]["entities"]
    assert len(entities) == 3


@pytest.mark.asyncio
async def test_patch_ir_fillet_rejects_collinear_segments_with_422(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    e1 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 100, "y": 0})
    e2 = await _add_segment(client, gen_id, {"x": 100, "y": 0}, {"x": 200, "y": 0})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "fillet", "entity_id": e1, "entity_id_2": e2, "value": 5}]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_ir_fillet_rejects_non_segment_entities(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    e1 = await _add_segment(client, gen_id, {"x": 0, "y": 0}, {"x": 100, "y": 0})
    circ = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "circle", "center": {"x": 50, "y": 50}, "radius": 10,
        }}]},
    )
    e2 = circ.json()["ir"]["entities"][-1]["id"]

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "fillet", "entity_id": e1, "entity_id_2": e2, "value": 5}]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_ir_hatch_click_adds_region_inside_closed_square(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    corners = [(50, 50), (250, 50), (250, 250), (50, 250)]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        await _add_segment(client, gen_id, {"x": x1, "y": y1}, {"x": x2, "y": y2})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "hatch_click", "click_x": 150, "click_y": 150}]},
    )
    assert resp.status_code == 200
    entities = resp.json()["ir"]["entities"]
    hatches = [e for e in entities if e["type"] == "hatch"]
    assert len(hatches) == 1


@pytest.mark.asyncio
async def test_patch_ir_hatch_click_outside_enclosure_returns_422(client, fake_storage):
    gen_id = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()["id"]
    await _add_segment(client, gen_id, {"x": 50, "y": 50}, {"x": 250, "y": 50})

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "hatch_click", "click_x": 150, "click_y": 150}]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_ir_add_dimension_renders_native_dxf_dimension(client, fake_storage):
    """Ф5.1: the manual dimension tool's PATCH payload must round-trip
    through validate_ir and reach the DXF export as a real arrowed leader,
    not just a bare line."""
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    add = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "dimension",
            "kind": "diameter",
            "p1": {"x": 100, "y": 100},
            "p2": {"x": 300, "y": 100},
            "text": "40",
            "value_mm": 40.0,
            "line_class": "dim",
            "width_class": "thin",
        }}]},
    )
    assert add.status_code == 200
    body = add.json()
    assert body["ir"]["entities"][0]["type"] == "dimension"
    assert body["ir"]["entities"][0]["kind"] == "diameter"

    gen_full = (await client.get(f"/api/image-gen/{gen_id}")).json()
    dxf = fake_storage[gen_full["params"]["dxf_path"]]
    assert b"DIMENSION" in dxf
    assert b"40" in dxf


@pytest.mark.asyncio
async def test_ir_revert_restores_earlier_revision_as_new_one(client, fake_storage):
    """Ф5.2 undo/redo backend: revert must append a NEW revision carrying the
    old content, never delete history (append-only, matches project audit
    philosophy) — the frontend drives undo/redo by walking revision numbers
    and calling this repeatedly."""
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    add1 = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 0, "y": 0}, "p2": {"x": 10, "y": 0},
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    assert add1.json()["revision"] == 1
    assert len(add1.json()["ir"]["entities"]) == 1

    add2 = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 0, "y": 20}, "p2": {"x": 10, "y": 20},
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    assert add2.json()["revision"] == 2
    assert len(add2.json()["ir"]["entities"]) == 2

    # "Undo" the second add: revert to revision 1's content.
    undo = await client.post(f"/api/image-gen/{gen_id}/ir/revert", json={"revision": 1})
    assert undo.status_code == 200
    body = undo.json()
    assert body["revision"] == 3  # new revision, history not overwritten
    assert body["origin"] == "revert"
    assert len(body["ir"]["entities"]) == 1

    # "Redo": revert forward to revision 2's content.
    redo = await client.post(f"/api/image-gen/{gen_id}/ir/revert", json={"revision": 2})
    assert redo.status_code == 200
    assert redo.json()["revision"] == 4
    assert len(redo.json()["ir"]["entities"]) == 2

    # GET /ir now reflects the redo, current = revision 4's content.
    current = await client.get(f"/api/image-gen/{gen_id}/ir")
    assert current.json()["revision"] == 4
    assert len(current.json()["ir"]["entities"]) == 2


@pytest.mark.asyncio
async def test_ir_revert_unknown_revision_404s(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.post(f"/api/image-gen/{gen['id']}/ir/revert", json={"revision": 99})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_ir_rejects_bad_entity_and_unknown_id(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    bad = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "add", "entity": {"type": "segment", "p1": {"x": 0, "y": 0}}}]},
    )
    assert bad.status_code == 422
    missing = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "confirm", "entity_id": "nope"}]},
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_patch_ir_errors_carry_a_typed_code(client, fake_storage):
    """Ф5.9: PATCH failures are structured ({code, message}), not bare
    prose — a caller (frontend or the agent's dispatcher) can branch on
    ``code`` without parsing Russian text."""
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()

    missing_entity = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "confirm", "entity_id": "nope"}]},
    )
    assert missing_entity.json()["detail"]["code"] == "entity_not_found"

    invalid = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "add", "entity": {"type": "segment", "p1": {"x": 0, "y": 0}}}]},
    )
    assert invalid.json()["detail"]["code"] == "invalid_entity"

    e1 = await _add_segment(client, gen["id"], {"x": 0, "y": 0}, {"x": 100, "y": 0})
    e2 = await _add_segment(client, gen["id"], {"x": 100, "y": 0}, {"x": 200, "y": 0})
    collinear = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "fillet", "entity_id": e1, "entity_id_2": e2, "value": 5}]},
    )
    assert collinear.json()["detail"]["code"] == "fillet_chamfer_geometry_invalid"

    missing_field = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "move", "entity_id": e1}]},
    )
    assert missing_field.json()["detail"]["code"] == "missing_field"


@pytest.mark.asyncio
async def test_patch_ir_batch_failure_saves_no_partial_revision(client, fake_storage):
    """Ф5.9 transactional contract: a batch is all-or-nothing. If op #2 in a
    batch fails, op #1's mutation must NOT have been persisted as a new
    revision — the whole request commits together or not at all."""
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    before = await client.get(f"/api/image-gen/{gen_id}/ir")
    assert before.json()["revision"] == 0

    resp = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [
            {"op": "add", "entity": {
                "type": "segment", "p1": {"x": 0, "y": 0}, "p2": {"x": 10, "y": 0},
                "line_class": "contour", "width_class": "main",
            }},
            {"op": "confirm", "entity_id": "does-not-exist"},
        ]},
    )
    assert resp.status_code == 404

    after = await client.get(f"/api/image-gen/{gen_id}/ir")
    assert after.json()["revision"] == 0  # unchanged — the add from op #1 never landed
    assert after.json()["ir"]["entities"] == []


@pytest.mark.asyncio
async def test_set_scale_clears_scale_warning(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.patch(
        f"/api/image-gen/{gen['id']}/ir",
        json={"ops": [{"op": "set_scale", "scale": 0.5}]},
    )
    assert resp.status_code == 200
    assert resp.json()["ir"]["scale"] == 0.5
    assert resp.json()["origin"] == "review"


@pytest.mark.asyncio
async def test_accept_vectorize_gate_and_validation(client, fake_storage, db_session):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    unchecked = await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")
    assert unchecked.status_code == 409
    assert "полную проверку" in unchecked.text

    await _mark_full_check_current(db_session, gen_id)
    ok = await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")
    assert ok.status_code == 200
    assert ok.json()["accepted"] is True
    assert ok.json()["accepted_revision"] == 0

    edited = await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 10, "y": 10}, "p2": {"x": 50, "y": 10},
        }}]},
    )
    assert edited.status_code == 200
    refreshed = await client.get(f"/api/image-gen/{gen_id}")
    assert refreshed.json()["accepted"] is False
    assert refreshed.json()["accepted_revision"] is None
    assert "full_check_revision" not in refreshed.json()["params"]


@pytest.mark.asyncio
async def test_feature_tree_candidates_endpoint_returns_ranked_hypotheses(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    corners = [(50, 50), (250, 50), (250, 250), (50, 250)]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        await _add_segment(client, gen_id, {"x": x1, "y": y1}, {"x": x2, "y": y2})

    resp = await client.get(f"/api/image-gen/{gen_id}/ir/feature-tree-candidates")
    assert resp.status_code == 200
    candidates = resp.json()["candidates"]
    assert len(candidates) >= 2
    scores = [c["score"] for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(c["missing_data"] for c in candidates if c["score"] < 0.5)


@pytest.mark.asyncio
async def test_feature_tree_step_requires_acceptance(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.post(f"/api/image-gen/{gen['id']}/ir/feature-tree-candidates/0/step")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_feature_tree_step_honestly_reports_missing_cad_kernel(
    client, fake_storage, db_session, monkeypatch
):
    from app.services.cad_kernel import CadKernelUnavailable

    async def _unavailable(*args, **kwargs):
        raise CadKernelUnavailable("cad-kernel unavailable")

    monkeypatch.setattr("app.services.cad_kernel.compile_candidate", _unavailable)
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    await _add_segment(client, gen_id, {"x": 50, "y": 50}, {"x": 250, "y": 50})
    await _mark_full_check_current(db_session, gen_id)
    await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")

    resp = await client.post(f"/api/image-gen/{gen_id}/ir/feature-tree-candidates/0/step")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_feature_tree_compile_persists_revision_bound_step_fcstd_stl(
    client, fake_storage, db_session, monkeypatch
):
    from app.services.cad_kernel import CadKernelArtifacts

    async def _compile(candidate, **kwargs):
        assert kwargs["confirm_assumptions"] is False
        assert kwargs["metadata"]["ir_revision"] == 5
        extrude = next(feature for feature in candidate.features if feature.kind == "extrude")
        assert extrude.params["depth_mm"] == pytest.approx(22.0)
        hole = next(feature for feature in candidate.features if feature.kind == "hole")
        assert hole.params["center_x_mm"] == pytest.approx(12.5)
        assert hole.params["center_y_mm"] == pytest.approx(12.5)
        assert hole.params["through"] is False
        assert hole.params["depth_mm"] == pytest.approx(8.0)
        assert [feature.kind for feature in candidate.features] == ["extrude", "boss", "pocket", "fillet", "hole"]
        boss = candidate.features[1]
        assert boss.params["profile"] == "circle"
        assert boss.params["depth_mm"] == pytest.approx(5.0)
        pocket = candidate.features[2]
        assert pocket.params["profile"] == "rectangle"
        assert pocket.params["width_mm"] == pytest.approx(5.0)
        fillet = candidate.features[3]
        assert fillet.params["size_mm"] == pytest.approx(1.0)
        assert candidate.missing_data == []
        return CadKernelArtifacts(
            step=b"ISO-10303-21;\nEND-ISO-10303-21;",
            fcstd=b"PK\x03\x04fcstd",
            stl=b"solid model\nendsolid model\n",
            report={"valid": True, "solid_count": 1, "volume_mm3": 1200.0, "warnings": []},
        )

    monkeypatch.setattr("app.services.cad_kernel.compile_candidate", _compile)
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    for p1, p2 in (
        ((0, 0), (100, 0)), ((100, 0), (100, 100)),
        ((100, 100), (0, 100)), ((0, 100), (0, 0)),
    ):
        await _add_segment(client, gen_id, {"x": p1[0], "y": p1[1]}, {"x": p2[0], "y": p2[1]})
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "circle", "center": {"x": 50, "y": 50}, "radius": 10,
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    await _mark_full_check_current(db_session, gen_id)
    assert (await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")).status_code == 200

    invalid = await client.post(
        f"/api/image-gen/{gen_id}/ir/feature-tree-candidates/0/step",
        json={"feature_overrides": [{"feature_index": 0, "through": True}]},
    )
    assert invalid.status_code == 422

    compiled = await client.post(
        f"/api/image-gen/{gen_id}/ir/feature-tree-candidates/0/step",
        json={"confirm_assumptions": False, "feature_overrides": [
            {"feature_index": 0, "depth_mm": 22.0},
            {"feature_index": 1, "through": False, "depth_mm": 8.0},
        ], "added_features": [
            {"kind": "boss", "profile": "circle", "center_x_mm": 18.0, "center_y_mm": 18.0, "depth_mm": 5.0, "diameter_mm": 4.0},
            {"kind": "pocket", "profile": "rectangle", "center_x_mm": 8.0, "center_y_mm": 8.0, "depth_mm": 3.0, "width_mm": 5.0, "height_mm": 6.0},
            {"kind": "fillet", "edge_key": "edge-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "size_mm": 1.0},
        ]},
    )
    assert compiled.status_code == 200
    assert compiled.content.startswith(b"ISO-10303-21")
    assert compiled.headers["x-cad-revision"] == "5"
    assert (await client.get(f"/api/image-gen/{gen_id}/artifact?kind=fcstd")).content.startswith(b"PK")
    assert (await client.get(f"/api/image-gen/{gen_id}/artifact?kind=stl")).content.startswith(b"solid")
    detail = (await client.get(f"/api/image-gen/{gen_id}")).json()
    assert detail["params"]["cad_artifact_revision"] == 5
    assert detail["params"]["cad_report"]["solid_count"] == 1
    assert detail["params"]["cad_feature_tree"]["features"][4]["params"]["through"] is False
    assert detail["params"]["cad_feature_overrides"][1]["depth_mm"] == 8.0
    assert detail["params"]["cad_added_features"][0]["kind"] == "boss"

    ir = (await client.get(f"/api/image-gen/{gen_id}/ir")).json()["ir"]
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{
            "op": "move", "entity_id": ir["entities"][0]["id"], "dx": 1, "dy": 0,
        }]},
    )
    assert (await client.get(f"/api/image-gen/{gen_id}/artifact?kind=step")).status_code == 409
    detail = (await client.get(f"/api/image-gen/{gen_id}")).json()
    assert "cad_artifact_revision" not in detail["params"]
    assert "cad_report" not in detail["params"]


@pytest.mark.asyncio
async def test_feature_tree_step_unknown_candidate_index_404s(client, fake_storage, db_session):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    await _mark_full_check_current(db_session, gen_id)
    await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")
    resp = await client.post(f"/api/image-gen/{gen_id}/ir/feature-tree-candidates/99/step")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_promote_to_drawing_requires_acceptance_first(client, fake_storage, db_session):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    resp = await client.post(f"/api/image-gen/{gen['id']}/promote-to-drawing")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_promote_to_drawing_creates_drawing_with_hole_features(client, fake_storage, db_session):
    """Ф6.2 end-to-end: blank sheet -> draw a circle via PATCH -> accept ->
    promote -> a real Drawing with a hole DrawingFeature exists in the DB."""
    import uuid as _uuid

    import sqlalchemy as sa

    from app.db.models import Drawing, DrawingFeature

    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "circle", "center": {"x": 200, "y": 300}, "radius": 30,
            "line_class": "contour", "width_class": "main",
        }}]},
    )
    await _mark_full_check_current(db_session, gen_id)
    await client.post(f"/api/image-gen/{gen_id}/accept-vectorize")

    resp = await client.post(f"/api/image-gen/{gen_id}/promote-to-drawing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["features"] == 1

    drawing = await db_session.get(Drawing, _uuid.UUID(body["drawing_id"]))
    assert drawing is not None
    assert drawing.metadata_["source_generation_id"] == gen_id
    features = (
        await db_session.execute(sa.select(DrawingFeature).where(DrawingFeature.drawing_id == drawing.id))
    ).scalars().all()
    assert len(features) == 1
    assert features[0].feature_type == "hole"


@pytest.mark.asyncio
async def test_accept_vectorize_blocked_by_validation_errors(client, fake_storage, db_session):
    import uuid as _uuid

    from app.db.models import ImageGeneration, ImageGenStatus

    gen = ImageGeneration(
        owner_sub="dev-user",
        operation="vectorize",
        status=ImageGenStatus.done,
        params={"validation": {"errors": 2}},
        source_image_paths=[],
    )
    db_session.add(gen)
    await db_session.commit()
    resp = await client.post(f"/api/image-gen/{gen.id}/accept-vectorize")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_full_check_merges_llm_issues_into_a_new_revision(client, fake_storage, monkeypatch):
    """Ф7.2: POST .../ir/full-check runs levels 6-7 and saves them as a new
    revision, without disturbing the deterministic levels 1-5 already there."""
    from app.ai.cad_ir.schema import ValidationIssueIR

    async def _fake_llm_review(png_bytes, **kwargs):
        return [
            ValidationIssueIR(code="NORMCONTROL_LLM", severity="warn", message_ru="нет базы", level=6),
        ]

    monkeypatch.setattr("app.ai.cad_validate.run_llm_review_levels", _fake_llm_review)

    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    # blank-sheet has no scale issue (scale is known), but add a segment so there's SOME level-1..5 signal to preserve.
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 0, "y": 0}, "p2": {"x": 1, "y": 1},  # tiny -> GEOM_DEGENERATE
            "line_class": "contour", "width_class": "main",
        }}]},
    )

    resp = await client.post(f"/api/image-gen/{gen_id}/ir/full-check")
    assert resp.status_code == 200
    body = resp.json()
    codes = {i["code"] for i in body["ir"]["validation"]["issues"]}
    assert "NORMCONTROL_LLM" in codes
    assert "GEOM_DEGENERATE" in codes  # level 1-5 result preserved, not wiped
    detail = await client.get(f"/api/image-gen/{gen_id}")
    assert detail.json()["params"]["full_check_revision"] == body["revision"]


@pytest.mark.asyncio
async def test_full_check_is_fail_closed_when_local_model_is_unavailable(
    client, fake_storage, monkeypatch
):
    from app.ai.cad_validate import FullCheckUnavailableError

    async def _unavailable(*args, **kwargs):
        raise FullCheckUnavailableError("local model offline")

    monkeypatch.setattr("app.ai.cad_validate.run_llm_review_levels", _unavailable)
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()

    response = await client.post(f"/api/image-gen/{gen['id']}/ir/full-check")
    assert response.status_code == 503
    detail = (await client.get(f"/api/image-gen/{gen['id']}")).json()
    assert detail["params"]["full_check_status"] == "unavailable"
    assert "full_check_revision" not in detail["params"]


@pytest.mark.asyncio
async def test_accept_vectorize_rejects_unresolved_source_regions(
    client, fake_storage, db_session
):
    import uuid

    from app.ai.cad_ir.schema import SourceRegion, UnresolvedRegion
    from app.db.models import ImageGeneration
    from app.services import cad_ir_store

    gen_out = (await client.post(
        "/api/image-gen/blank-sheet", json={"format": "A4"}
    )).json()
    gen = await db_session.get(ImageGeneration, uuid.UUID(gen_out["id"]))
    revision = await cad_ir_store.latest_revision(db_session, gen.id)
    ir = cad_ir_store.load_ir(revision)
    ir.unresolved_regions = [
        UnresolvedRegion(
            region=SourceRegion(x0=10, y0=10, x1=40, y1=40),
            ink_pixels=100,
        )
    ]
    row = await cad_ir_store.save_revision(
        db_session, gen, ir, origin="auto", created_by="dev-user"
    )
    gen.params = {
        **(gen.params or {}),
        "full_check_revision": row.revision,
        "full_check_status": "passed",
    }
    await db_session.commit()

    response = await client.post(f"/api/image-gen/{gen.id}/accept-vectorize")
    assert response.status_code == 409
    assert "нераспознанные области" in response.text


@pytest.mark.asyncio
async def test_full_check_replaces_stale_llm_issues_not_accumulates(client, fake_storage, monkeypatch):
    from app.ai.cad_ir.schema import ValidationIssueIR

    calls = {"n": 0}

    async def _fake_llm_review(png_bytes, **kwargs):
        calls["n"] += 1
        return [ValidationIssueIR(code="VLM_CRITIC", severity="info", message_ru=f"проверка {calls['n']}", level=7)]

    monkeypatch.setattr("app.ai.cad_validate.run_llm_review_levels", _fake_llm_review)

    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]

    await client.post(f"/api/image-gen/{gen_id}/ir/full-check")
    resp2 = await client.post(f"/api/image-gen/{gen_id}/ir/full-check")
    critic_issues = [i for i in resp2.json()["ir"]["validation"]["issues"] if i["code"] == "VLM_CRITIC"]
    assert len(critic_issues) == 1  # not 2 — the stale one from the first call was replaced


@pytest.mark.asyncio
async def test_full_check_resolves_norm_citations_against_ingested_corpus(
    client, fake_storage, monkeypatch, db_session
):
    """Ф9: a deterministic ESKD_LINE_WEIGHT issue's plain "ГОСТ 2.303-68"
    citation resolves to the actual ingested NormativeDocument, end to end
    through the full-check endpoint."""
    from app.db.models import NormativeDocument

    db_session.add(NormativeDocument(code="ГОСТ 2.303-68", title="Линии", document_type="ГОСТ"))
    await db_session.commit()

    async def _no_llm_issues(png_bytes, **kwargs):
        return []

    monkeypatch.setattr("app.ai.cad_validate.run_llm_review_levels", _no_llm_issues)

    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A4"})).json()
    gen_id = gen["id"]
    await client.patch(
        f"/api/image-gen/{gen_id}/ir",
        json={"ops": [{"op": "add", "entity": {
            "type": "segment", "p1": {"x": 0, "y": 0}, "p2": {"x": 100, "y": 0},
            "line_class": "axis", "width_class": "main",  # -> ESKD_LINE_WEIGHT
        }}]},
    )

    resp = await client.post(f"/api/image-gen/{gen_id}/ir/full-check")
    issue = next(i for i in resp.json()["ir"]["validation"]["issues"] if i["code"] == "ESKD_LINE_WEIGHT")
    assert issue["norm_clause_text"] == "ГОСТ 2.303-68 — Линии"


@pytest.mark.asyncio
async def test_artifact_svg_and_ir_kinds(client, fake_storage):
    gen = (await client.post("/api/image-gen/blank-sheet", json={"format": "A3"})).json()
    svg = await client.get(f"/api/image-gen/{gen['id']}/artifact?kind=svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg")
    ir = await client.get(f"/api/image-gen/{gen['id']}/artifact?kind=ir")
    assert ir.status_code == 200
    assert b"schema_version" in ir.content
