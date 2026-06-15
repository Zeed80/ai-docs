"""Symmetric encryption for provider API keys stored in the database.

Keys are encrypted at rest with Fernet (AES-128-CBC + HMAC) using a key derived
from ``settings.app_secret_key``. Plaintext keys never leave this module except
through :func:`decrypt`; the API surfaces only :func:`mask`.
"""

from __future__ import annotations

import base64
import hashlib

import structlog
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = structlog.get_logger()


def _fernet() -> Fernet:
    """Derive a stable Fernet key from the app secret.

    Fernet requires a 32-byte url-safe base64 key. We derive it deterministically
    from ``app_secret_key`` so existing ciphertext stays decryptable across
    restarts as long as the secret is unchanged.
    """
    digest = hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    """Encrypt a secret. Empty input returns an empty string (treated as unset)."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str | None) -> str:
    """Decrypt a secret. Returns "" on empty/invalid input (fail-closed)."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception) as exc:  # noqa: BLE001
        logger.warning("secret_box_decrypt_failed", error=str(exc))
        return ""


def mask(plaintext: str | None) -> str:
    """Return a display-safe mask of a secret, e.g. ``sk-…a1b2``.

    Never returns more than the last 4 characters. Empty input → "".
    """
    if not plaintext:
        return ""
    tail = plaintext[-4:]
    prefix = plaintext[:3] if len(plaintext) > 8 else ""
    return f"{prefix}…{tail}" if prefix else f"…{tail}"
