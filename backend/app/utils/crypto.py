"""Password encryption helpers for mailbox credentials.

Thin wrappers around secret_store so callers don't import it directly.
"""

from app.utils.secret_store import decrypt, encrypt

encrypt_password = encrypt
decrypt_password = decrypt
