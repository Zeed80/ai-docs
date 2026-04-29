from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any


def create_signed_file_token(
    *,
    storage_path: str,
    filename: str,
    content_type: str | None,
    secret: str,
    ttl_seconds: int,
    document_id: str | None = None,
    artifact_id: str | None = None,
) -> tuple[str, int]:
    expires_at = int(time.time()) + ttl_seconds
    payload = {
        "storage_path": str(Path(storage_path)),
        "filename": Path(filename).name or "download.bin",
        "content_type": content_type,
        "document_id": document_id,
        "artifact_id": artifact_id,
        "exp": expires_at,
    }
    payload_part = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _sign(payload_part, secret)
    return f"{payload_part}.{signature}", expires_at


def verify_signed_file_token(token: str, *, secret: str) -> dict[str, Any]:
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Invalid signed file token") from exc
    expected = _sign(payload_part, secret)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid signed file token signature")
    try:
        payload = json.loads(_unb64(payload_part).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid signed file token payload") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Signed file token expired")
    storage_path = Path(str(payload.get("storage_path", "")))
    if not storage_path.exists() or not storage_path.is_file():
        raise ValueError("Signed file target not found")
    return payload


def _sign(payload_part: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)
