from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Document, DocumentStatus, FileExtensionAllowlist, QuarantineEntry
from app.storage import get_presigned_url
from app.tasks.imap_client import ParsedAttachment
from app.tasks.ingest import _store_attachment


def test_imap_attachment_is_quarantined_when_extension_is_not_allowed(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.storage.upload_file", lambda *args, **kwargs: "ok")
    with Session(engine) as session:
        session.add(FileExtensionAllowlist(extension=".pdf", is_allowed=True))
        session.flush()

        doc = _store_attachment(
            session,
            ParsedAttachment(
                filename="payload.exe",
                content=b"binary",
                content_type="application/octet-stream",
                size=6,
                sha256="a" * 64,
            ),
            email_message_id=uuid.uuid4(),
            mailbox="procurement",
        )
        session.commit()

        assert doc is not None
        saved = session.query(Document).one()
        quarantine = session.query(QuarantineEntry).one()
        assert saved.status == DocumentStatus.suspicious
        assert quarantine.document_id == saved.id
        assert quarantine.reason == "extension_not_allowed"


def test_imap_attachment_is_ingested_when_extension_is_allowed(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.storage.upload_file", lambda *args, **kwargs: "ok")
    with Session(engine) as session:
        session.add(FileExtensionAllowlist(extension=".pdf", is_allowed=True))
        session.flush()

        doc = _store_attachment(
            session,
            ParsedAttachment(
                filename="invoice.pdf",
                content=b"%PDF",
                content_type="application/pdf",
                size=4,
                sha256="b" * 64,
            ),
            email_message_id=uuid.uuid4(),
            mailbox="accounting",
        )
        session.commit()

        assert doc is not None
        assert session.query(Document).one().status == DocumentStatus.ingested
        assert session.query(QuarantineEntry).count() == 0


def test_minio_presigned_url_accepts_seconds_expiry(monkeypatch) -> None:
    class FakeClient:
        def presigned_get_object(self, bucket, storage_path, expires):
            assert bucket == "documents"
            assert storage_path == "exports/job/file.xlsx"
            assert expires == timedelta(seconds=3600)
            return "http://minio/presigned"

    monkeypatch.setattr("app.storage.get_minio_client", lambda: FakeClient())
    monkeypatch.setattr("app.storage.settings.minio_bucket", "documents")

    assert get_presigned_url("exports/job/file.xlsx", expiry=3600) == "http://minio/presigned"
