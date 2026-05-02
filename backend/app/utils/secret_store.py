"""Symmetric encryption for sensitive config values stored in Redis.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The encryption key is derived
from APP_SECRET_KEY so no separate key management is needed in dev.
In production, set APP_SECRET_KEY to a strong random value.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    from app.config import settings
    # Derive a 32-byte key from APP_SECRET_KEY via SHA-256
    raw = settings.app_secret_key.encode()
    key_bytes = hashlib.sha256(raw).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a URL-safe base64 token."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`. Returns "" on failure."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return ""


def mask(value: str, visible: int = 4) -> str:
    """Return a masked version for display: last *visible* chars visible."""
    if not value or len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]
