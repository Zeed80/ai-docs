"""E28: the artifact broker between work orders and the script supervisor.

Inputs come only from the owner's own WorkArtifact rows, as bytes, after
their content hash matches both the hash the caller declared and the one on
record — a file changed after the work was accepted is refused, and no host
path ever reaches the run. Outputs are validated in memory first (the
quarantine): flat safe name, recomputed digest and size, no executable
format. Only then are they stored at a content-addressed path, so nothing is
overwritten, and registered as data — never as a tool. Archives are kept as
opaque files and never extracted here.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkArtifact, WorkOrder

INPUT_MAX_FILES = 20
INPUT_MAX_BYTES = 20 * 1024 * 1024
OUTPUT_MAX_FILES = 20
OUTPUT_MAX_BYTES = 50 * 1024 * 1024
RESERVED_NAMES = frozenset({"main.py"})
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# Magic numbers of native executables and libraries.
_EXECUTABLE_MAGIC = (
    b"\x7fELF",  # Linux
    b"MZ",  # Windows PE
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",  # Mach-O
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
)
_EXECUTABLE_SUFFIXES = (".exe", ".dll", ".so", ".dylib", ".elf", ".bin", ".com", ".msi")


class BrokerRefused(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ScriptInputRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: uuid.UUID
    sha256: str
    name: str


def _check_name(name: str) -> None:
    if not _SAFE_NAME.fullmatch(name or "") or ".." in name:
        raise BrokerRefused("unsafe_name", name[:100])


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def materialize_inputs(
    db: AsyncSession,
    *,
    owner_key: str,
    refs: list[ScriptInputRef],
    fetch: Callable[[str], bytes] | None = None,
) -> dict[str, bytes]:
    """The run's input files, by name, or BrokerRefused."""
    if fetch is None:
        from app.storage import download_file as fetch
    if len(refs) > INPUT_MAX_FILES:
        raise BrokerRefused("too_many_inputs", str(len(refs)))
    files: dict[str, bytes] = {}
    total = 0
    for ref in refs:
        _check_name(ref.name)
        if ref.name in RESERVED_NAMES or ref.name in files:
            raise BrokerRefused("duplicate_or_reserved_name", ref.name)
        row = (
            await db.execute(
                select(WorkArtifact, WorkOrder.owner_key)
                .join(WorkOrder, WorkOrder.id == WorkArtifact.work_order_id)
                .where(WorkArtifact.id == ref.artifact_id)
            )
        ).first()
        if row is None or row[1] != owner_key:
            # Someone else's artifact is indistinguishable from a missing one.
            raise BrokerRefused("artifact_not_found", str(ref.artifact_id))
        artifact: WorkArtifact = row[0]
        if (artifact.metadata_ or {}).get("revoked_at"):
            raise BrokerRefused("artifact_revoked", str(ref.artifact_id))
        if not artifact.uri or not artifact.content_hash:
            raise BrokerRefused("artifact_not_materializable", str(ref.artifact_id))
        if artifact.content_hash != ref.sha256:
            raise BrokerRefused("artifact_changed", str(ref.artifact_id))
        data = fetch(artifact.uri)
        if _sha256(data) != ref.sha256:
            raise BrokerRefused("artifact_changed", str(ref.artifact_id))
        total += len(data)
        if total > INPUT_MAX_BYTES:
            raise BrokerRefused("inputs_too_large", str(total))
        files[ref.name] = data
    return files


def _looks_executable(name: str, data: bytes) -> bool:
    return name.lower().endswith(_EXECUTABLE_SUFFIXES) or data.startswith(_EXECUTABLE_MAGIC)


async def register_outputs(
    db: AsyncSession,
    *,
    work_order_id: uuid.UUID,
    step_id: uuid.UUID | None,
    run_id: str,
    outputs: list[dict[str, Any]],
    store: Callable[[bytes, str, str], Any] | None = None,
    artifact_type: str = "script_output",
    path_prefix: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[WorkArtifact]:
    """Validate every output, then store and register all of them, or none."""
    if store is None:
        from app.storage import upload_file as store
    if len(outputs) > OUTPUT_MAX_FILES:
        raise BrokerRefused("too_many_outputs", str(len(outputs)))
    validated: list[tuple[str, bytes, str, str]] = []
    total = 0
    seen: set[str] = set()
    for item in outputs:
        name = str(item.get("name") or "")
        _check_name(name)
        if name in seen:
            raise BrokerRefused("duplicate_output", name)
        seen.add(name)
        data = base64.b64decode(item.get("content_b64") or "")
        digest = _sha256(data)
        if digest != item.get("sha256") or len(data) != item.get("size"):
            raise BrokerRefused("output_digest_mismatch", name)
        total += len(data)
        if total > OUTPUT_MAX_BYTES:
            raise BrokerRefused("outputs_too_large", str(total))
        if _looks_executable(name, data):
            raise BrokerRefused("executable_output", name)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        validated.append((name, data, digest, content_type))
    rows: list[WorkArtifact] = []
    for name, data, digest, content_type in validated:
        # Content-addressed: the same bytes land at the same path, other bytes
        # elsewhere — an artifact is never overwritten.
        path = f"{path_prefix or f'script-runs/{run_id}'}/{digest}"
        store(data, path, content_type)
        row = WorkArtifact(
            work_order_id=work_order_id,
            step_id=step_id,
            artifact_type=artifact_type,
            name=name,
            uri=path,
            content_hash=digest,
            content_type=content_type,
            size_bytes=len(data),
            metadata_={
                "run_id": run_id,
                "quarantine": "passed",
                "executable": False,
                **(metadata or {}),
            },
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows
