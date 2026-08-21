"""Unpacking supplier catalog archives.

Suppliers routinely send one .zip/.7z/.rar with five price lists inside. The
archive itself is a Document; every member becomes a child Document linked back
with link_type="archive_member" and gets its own ingestion job, so "в архиве 5
каталогов, обработано 3" is a real, visible state.

Unpacking happens in the worker and every limit is enforced *while streaming*,
before anything is written: a zip bomb declares a small compressed size and a
harmless header, so the declared size is never trusted.
"""

from __future__ import annotations

import io
import tarfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from app.domain.pipeline import CATALOG_PIPELINE_STEP_DEFINITIONS as CATALOG_STEPS
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

MAX_MEMBERS = 200
MAX_TOTAL_MULTIPLIER = 20  # of settings.max_upload_size_mb
MAX_COMPRESSION_RATIO = 100
MEMBER_SUFFIXES = {
    ".pdf", ".xlsx", ".xls", ".xlsm", ".csv", ".json", ".txt", ".docx", ".xml", ".html", ".htm",
}


class ArchiveRejected(Exception):
    """The archive is unsafe or unusable — refuse it with a readable reason."""


@dataclass
class ExtractedMember:
    name: str
    data: bytes


def _limits() -> tuple[int, int]:
    from app.config import settings

    per_file = settings.max_upload_size_mb * 1024 * 1024
    return per_file, per_file * MAX_TOTAL_MULTIPLIER


def _safe_member_name(name: str) -> str:
    """Reject traversal and absolute paths; keep only the basename."""
    if name.startswith("/") or ".." in Path(name).parts:
        raise ArchiveRejected(f"Небезопасный путь внутри архива: {name}")
    return Path(name).name


def _accept_member(name: str) -> bool:
    base = Path(name).name
    if not base or base.startswith("."):
        return False
    return Path(base).suffix.lower() in MEMBER_SUFFIXES


def _read_limited(stream, limit: int, name: str) -> bytes:
    """Read at most `limit`+1 bytes so an over-large member fails before storage."""
    chunks: list[bytes] = []
    read = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        read += len(chunk)
        if read > limit:
            raise ArchiveRejected(
                f"Файл «{name}» в архиве больше допустимого размера — архив отклонён."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def extract_catalog_archive(data: bytes, filename: str) -> list[ExtractedMember]:
    suffix = Path(filename).suffix.lower()
    per_file, total_limit = _limits()

    if suffix == ".zip":
        members = _extract_zip(data, per_file, total_limit)
    elif suffix == ".7z":
        members = _extract_7z(data, per_file, total_limit)
    elif suffix == ".rar":
        members = _extract_rar(data, per_file, total_limit)
    elif suffix in {".tar", ".gz", ".tgz", ".bz2", ".xz"}:
        members = _extract_tar(data, per_file, total_limit)
    else:
        raise ArchiveRejected(f"Формат архива «{suffix}» не поддерживается.")

    if not members:
        raise ArchiveRejected(
            "В архиве нет файлов, пригодных для каталога "
            f"({', '.join(sorted(MEMBER_SUFFIXES))})."
        )
    return members


def _check_count(count: int) -> None:
    if count > MAX_MEMBERS:
        raise ArchiveRejected(f"В архиве больше {MAX_MEMBERS} файлов — отклонено.")


def _check_total(total: int, limit: int) -> None:
    if total > limit:
        raise ArchiveRejected(
            f"Суммарный распакованный размер превышает {limit // (1024 * 1024)} МБ — отклонено."
        )


def _extract_zip(data: bytes, per_file: int, total_limit: int) -> list[ExtractedMember]:
    out: list[ExtractedMember] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        _check_count(len(infos))
        compressed = sum(i.compress_size for i in infos) or 1
        declared = sum(i.file_size for i in infos)
        if declared / compressed > MAX_COMPRESSION_RATIO:
            raise ArchiveRejected("Подозрительная степень сжатия (архив-бомба) — отклонено.")
        for info in infos:
            name = _safe_member_name(info.filename)
            if not _accept_member(name):
                continue
            with zf.open(info) as fh:
                payload = _read_limited(fh, per_file, name)
            total += len(payload)
            _check_total(total, total_limit)
            out.append(ExtractedMember(name=name, data=payload))
    return out


def _extract_tar(data: bytes, per_file: int, total_limit: int) -> list[ExtractedMember]:
    out: list[ExtractedMember] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        _check_count(len(members))
        for member in members:
            if member.issym() or member.islnk():
                raise ArchiveRejected(f"Симлинк внутри архива: {member.name}")
            name = _safe_member_name(member.name)
            if not _accept_member(name):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            payload = _read_limited(fh, per_file, name)
            total += len(payload)
            _check_total(total, total_limit)
            out.append(ExtractedMember(name=name, data=payload))
    return out


def _extract_7z(data: bytes, per_file: int, total_limit: int) -> list[ExtractedMember]:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise ArchiveRejected("Поддержка .7z не установлена на сервере.") from exc

    out: list[ExtractedMember] = []
    total = 0
    with py7zr.SevenZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.getnames() if _accept_member(_safe_member_name(n))]
        _check_count(len(zf.getnames()))
        if not names:
            return out
        for name, bio in (zf.read(names) or {}).items():
            payload = bio.read()
            if len(payload) > per_file:
                raise ArchiveRejected(
                    f"Файл «{name}» в архиве больше допустимого размера — архив отклонён."
                )
            total += len(payload)
            _check_total(total, total_limit)
            out.append(ExtractedMember(name=_safe_member_name(name), data=payload))
    return out


def _extract_rar(data: bytes, per_file: int, total_limit: int) -> list[ExtractedMember]:
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise ArchiveRejected("Поддержка .rar не установлена на сервере.") from exc

    out: list[ExtractedMember] = []
    total = 0
    try:
        rf = rarfile.RarFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — rarfile raises several types
        raise ArchiveRejected(f"Архив .rar не читается: {exc}") from exc
    with rf:
        infos = [i for i in rf.infolist() if not i.is_dir()]
        _check_count(len(infos))
        for info in infos:
            name = _safe_member_name(info.filename)
            if not _accept_member(name):
                continue
            with rf.open(info) as fh:
                payload = _read_limited(fh, per_file, name)
            total += len(payload)
            _check_total(total, total_limit)
            out.append(ExtractedMember(name=name, data=payload))
    return out


@celery_app.task(
    bind=True,
    name="catalog.unpack_archive",
    max_retries=0,
    soft_time_limit=1800,
    time_limit=1860,
)
def unpack_catalog_archive(self, document_id: str) -> dict:
    from app.tasks.drawing_analysis import run_async

    return run_async(_unpack_async(document_id))


async def _unpack_async(document_id: str) -> dict:
    from sqlalchemy import select

    from app.db.models import Document, DocumentProcessingJob, ToolSupplier
    from app.db.session import _get_session_factory
    from app.domain import processing_jobs as pj
    from app.domain.catalog_documents import register_catalog_document
    from app.storage import download_file
    from app.tasks.catalog_ingest import ingest_catalog_document

    doc_uuid = uuid.UUID(document_id)
    factory = _get_session_factory()

    async with factory() as db:
        doc = await db.get(Document, doc_uuid)
        if not doc:
            return {"error": f"Document {document_id} not found"}
        supplier_id = (doc.metadata_ or {}).get("tool_supplier_id")
        if not supplier_id:
            return {"error": "Document is not linked to a tool supplier"}
        supplier = await db.get(ToolSupplier, uuid.UUID(supplier_id))
        if not supplier:
            return {"error": f"Supplier {supplier_id} not found"}
        job = (
            await db.execute(
                select(DocumentProcessingJob)
                .where(DocumentProcessingJob.document_id == doc_uuid)
                .order_by(DocumentProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        storage_path, filename = doc.storage_path, doc.file_name

        try:
            data = download_file(storage_path)
            members = extract_catalog_archive(data, filename)
        except ArchiveRejected as exc:
            if job is not None:
                pj.set_job_step(job, "unpack", "failed", error=str(exc), defs=CATALOG_STEPS)
                pj.finish_job(job, "failed", error=str(exc))
                await db.commit()
            logger.warning("catalog_archive_rejected", document_id=document_id, reason=str(exc))
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            message = f"Не удалось распаковать архив: {exc}"[:400]
            if job is not None:
                pj.set_job_step(job, "unpack", "failed", error=message, defs=CATALOG_STEPS)
                pj.finish_job(job, "failed", error=message)
                await db.commit()
            raise

        child_ids: list[str] = []
        failures: list[str] = []
        for index, member in enumerate(members, start=1):
            try:
                registered = await register_catalog_document(
                    db,
                    supplier=supplier,
                    file_bytes=member.data,
                    filename=member.name,
                    source_channel="archive",
                    parent_document_id=doc_uuid,
                    metadata={"archive_name": filename},
                )
                child_ids.append(str(registered.document.id))
            except Exception as exc:  # noqa: BLE001 — one bad member must not
                # cost the other four; record it and continue.
                failures.append(f"{member.name}: {exc}"[:200])
                logger.warning(
                    "catalog_archive_member_failed", member=member.name, error=str(exc)
                )
            if job is not None:
                pj.set_step_progress(job, "unpack", index, len(members), defs=CATALOG_STEPS)

        if job is not None:
            pj.set_job_step(
                job,
                "unpack",
                "done",
                defs=CATALOG_STEPS,
                members_total=len(members),
                members_registered=len(child_ids),
            )
            for key in ("parse", "normalize", "entries", "canonical", "embedding", "graph"):
                pj.set_job_step(job, key, "skipped", defs=CATALOG_STEPS)
            pj.finish_job(job, "done")
            if failures:
                job.error = "; ".join(failures)[:500]
            meta = dict(doc.metadata_ or {})
            meta.update({"is_archive": True, "member_document_ids": child_ids})
            if failures:
                meta["member_failures"] = failures
            doc.metadata_ = meta
        await db.commit()

    for child_id in child_ids:
        try:
            ingest_catalog_document.delay(child_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_member_enqueue_failed", document_id=child_id, error=str(exc))

    logger.info(
        "catalog_archive_unpacked",
        document_id=document_id,
        members=len(members),
        registered=len(child_ids),
        failed=len(failures),
    )
    return {
        "document_id": document_id,
        "members": len(members),
        "registered": len(child_ids),
        "failed": failures,
    }
