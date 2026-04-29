from __future__ import annotations

import json

from scripts.encrypted_backup import create_encrypted_backup


def test_encrypted_backup_creates_aesgcm_payload(tmp_path) -> None:
    database = tmp_path / "app.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    database.write_bytes(b"sqlite")
    (storage / "document.txt").write_text("hello", encoding="utf-8")

    backup = create_encrypted_backup(
        database_path=database,
        storage_root=storage,
        output_dir=tmp_path / "backups",
        encryption_key="test-secret",
    )

    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["algorithm"] == "AES-256-GCM"
    assert payload["nonce"]
    assert payload["ciphertext"]


def test_encrypted_backup_requires_key(tmp_path) -> None:
    try:
        create_encrypted_backup(
            database_path=tmp_path / "missing.db",
            storage_root=tmp_path / "missing-storage",
            output_dir=tmp_path / "backups",
            encryption_key="",
        )
    except ValueError as exc:
        assert "BACKUP_ENCRYPTION_KEY" in str(exc)
    else:
        raise AssertionError("Expected encrypted backup to require a key")
