from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def create_encrypted_backup(
    *,
    database_path: Path,
    storage_root: Path,
    output_dir: Path,
    encryption_key: str,
) -> Path:
    if not encryption_key:
        raise ValueError("BACKUP_ENCRYPTION_KEY is required")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = _build_archive(database_path=database_path, storage_root=storage_root)
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key_bytes(encryption_key)).encrypt(nonce, archive, None)
    payload = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }
    target = output_dir / f"workspace-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz.aesgcm.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def _build_archive(*, database_path: Path, storage_root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if database_path.exists():
            tar.add(database_path, arcname="database/app.db")
        if storage_root.exists():
            tar.add(storage_root, arcname="storage")
    return buffer.getvalue()


def _key_bytes(encryption_key: str) -> bytes:
    return hashlib.sha256(encryption_key.encode("utf-8")).digest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create encrypted local backup.")
    parser.add_argument("--database", default="data/app.db")
    parser.add_argument("--storage-root", default="data/storage")
    parser.add_argument("--output-dir", default="data/backups")
    args = parser.parse_args()
    backup_path = create_encrypted_backup(
        database_path=Path(args.database),
        storage_root=Path(args.storage_root),
        output_dir=Path(args.output_dir),
        encryption_key=os.getenv("BACKUP_ENCRYPTION_KEY", ""),
    )
    print(backup_path)


if __name__ == "__main__":
    main()
