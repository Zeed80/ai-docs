"""E28: script inputs only from the owner's own unchanged artifacts; outputs
validated before they are stored, never overwritten, never executable."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.db.models import WorkArtifact
from app.domain.script_broker import (
    BrokerRefused,
    ScriptInputRef,
    materialize_inputs,
    register_outputs,
)
from app.domain.work_orders import create_work_order


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes: list[str] = []

    def fetch(self, path: str) -> bytes:
        return self.objects[path]

    def put(self, data: bytes, path: str, _content_type: str) -> str:
        self.writes.append(path)
        self.objects[path] = data
        return path


async def _artifact(db, store, owner: str, data: bytes, name="in.csv"):
    order = await create_work_order(db, owner_key=owner, objective="Script input")
    path = f"artifacts/{uuid.uuid4().hex}"
    store.objects[path] = data
    row = WorkArtifact(
        work_order_id=order.id,
        artifact_type="upload",
        name=name,
        uri=path,
        content_hash=_sha(data),
        size_bytes=len(data),
    )
    db.add(row)
    await db.flush()
    return row


def _output(name: str, data: bytes, **overrides):
    return {
        "name": name,
        "size": len(data),
        "sha256": _sha(data),
        "content_b64": base64.b64encode(data).decode(),
        **overrides,
    }


@pytest.mark.asyncio
async def test_the_owners_unchanged_artifact_is_materialized(db_session):
    store = _Store()
    row = await _artifact(db_session, store, "alice", b"a,b\n1,2\n")
    files = await materialize_inputs(
        db_session,
        owner_key="alice",
        refs=[ScriptInputRef(artifact_id=row.id, sha256=row.content_hash, name="data.csv")],
        fetch=store.fetch,
    )
    assert files == {"data.csv": b"a,b\n1,2\n"}


@pytest.mark.asyncio
async def test_bobs_artifact_is_not_found_for_alice(db_session):
    store = _Store()
    row = await _artifact(db_session, store, "bob", b"secret")
    with pytest.raises(BrokerRefused) as refused:
        await materialize_inputs(
            db_session,
            owner_key="alice",
            refs=[ScriptInputRef(artifact_id=row.id, sha256=row.content_hash, name="x.txt")],
            fetch=store.fetch,
        )
    assert refused.value.reason == "artifact_not_found"


@pytest.mark.asyncio
async def test_an_input_changed_after_acceptance_is_refused(db_session):
    store = _Store()
    row = await _artifact(db_session, store, "alice", b"v1")
    store.objects[row.uri] = b"v2 swapped in storage"
    with pytest.raises(BrokerRefused) as refused:
        await materialize_inputs(
            db_session,
            owner_key="alice",
            refs=[ScriptInputRef(artifact_id=row.id, sha256=_sha(b"v1"), name="x.txt")],
            fetch=store.fetch,
        )
    assert refused.value.reason == "artifact_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["../etc/passwd", "/etc/passwd", "sub/file.txt", ".hidden", "a..b"]
)
async def test_input_names_cannot_carry_a_path(db_session, name):
    store = _Store()
    row = await _artifact(db_session, store, "alice", b"x")
    with pytest.raises(BrokerRefused) as refused:
        await materialize_inputs(
            db_session,
            owner_key="alice",
            refs=[ScriptInputRef(artifact_id=row.id, sha256=row.content_hash, name=name)],
            fetch=store.fetch,
        )
    assert refused.value.reason == "unsafe_name"


@pytest.mark.asyncio
async def test_too_many_input_files_are_refused(db_session):
    refs = [
        ScriptInputRef(artifact_id=uuid.uuid4(), sha256="0" * 64, name=f"f{i}.txt")
        for i in range(21)
    ]
    with pytest.raises(BrokerRefused) as refused:
        await materialize_inputs(db_session, owner_key="alice", refs=refs, fetch=lambda _p: b"")
    assert refused.value.reason == "too_many_inputs"


@pytest.mark.asyncio
async def test_outputs_are_validated_then_stored_content_addressed(db_session):
    store = _Store()
    order = await create_work_order(db_session, owner_key="alice", objective="Script output")
    rows = await register_outputs(
        db_session,
        work_order_id=order.id,
        step_id=None,
        run_id="run-1",
        outputs=[_output("result.csv", b"x,1\n")],
        store=store.put,
    )
    [row] = rows
    assert row.uri == f"script-runs/run-1/{_sha(b'x,1' + bytes([10]))}"
    assert row.content_type == "text/csv"
    assert row.metadata_["executable"] is False
    # The same bytes again land at the same path; different bytes elsewhere.
    again = await register_outputs(
        db_session,
        work_order_id=order.id,
        step_id=None,
        run_id="run-1",
        outputs=[_output("result.csv", b"x,2\n")],
        store=store.put,
    )
    assert again[0].uri != row.uri
    assert store.objects[row.uri] == b"x,1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (_output("../escape.txt", b"x"), "unsafe_name"),
        (_output("/etc/cron.d/x", b"x"), "unsafe_name"),
        (_output("tool", b"\x7fELF\x02\x01\x01" + b"\0" * 32), "executable_output"),
        (_output("helper.so", b"not really"), "executable_output"),
        (_output("data.txt", b"x", sha256="0" * 64), "output_digest_mismatch"),
    ],
)
async def test_a_bad_output_registers_nothing(db_session, output, reason):
    store = _Store()
    order = await create_work_order(db_session, owner_key="alice", objective="Bad output")
    with pytest.raises(BrokerRefused) as refused:
        await register_outputs(
            db_session,
            work_order_id=order.id,
            step_id=None,
            run_id="run-bad",
            outputs=[_output("fine.txt", b"ok"), output],
            store=store.put,
        )
    assert refused.value.reason == reason
    assert store.writes == []  # validated first: nothing stored
    registered = await db_session.scalars(
        select(WorkArtifact).where(WorkArtifact.work_order_id == order.id)
    )
    assert list(registered) == []


@pytest.mark.asyncio
async def test_many_small_outputs_are_refused(db_session):
    order = await create_work_order(db_session, owner_key="alice", objective="Many outputs")
    with pytest.raises(BrokerRefused) as refused:
        await register_outputs(
            db_session,
            work_order_id=order.id,
            step_id=None,
            run_id="run-many",
            outputs=[_output(f"f{i}.txt", b"x") for i in range(21)],
            store=_Store().put,
        )
    assert refused.value.reason == "too_many_outputs"


@pytest.mark.asyncio
async def test_a_zip_bomb_stays_an_unextracted_file(db_session):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.bin", b"\0" * (200 * 1024 * 1024))
    bomb = buffer.getvalue()
    assert len(bomb) < 1024 * 1024
    store = _Store()
    order = await create_work_order(db_session, owner_key="alice", objective="Zip output")
    [row] = await register_outputs(
        db_session,
        work_order_id=order.id,
        step_id=None,
        run_id="run-zip",
        outputs=[_output("result.zip", bomb)],
        store=store.put,
    )
    assert row.size_bytes == len(bomb)
    assert store.objects[row.uri] == bomb  # stored as the bytes it was, not unpacked
