"""
Secret encryption at rest.

All sensitive admin-managed values (SSH passwords, Oracle DB passwords, LLM API
keys) are stored as Fernet ciphertext in PostgreSQL and only decrypted at
use-time. The Fernet key is derived from the APP_SECRET_KEY environment variable
so any passphrase works as the key material.
"""
import os
import base64
import hashlib
import sys

from cryptography.fernet import Fernet, InvalidToken

_DEV_FALLBACK = "dev-insecure-change-me"
_warned = False


def _fernet() -> Fernet:
    global _warned
    raw = os.getenv("APP_SECRET_KEY")
    if not raw:
        if not _warned:
            print(
                "[crypto] WARNING: APP_SECRET_KEY is not set — using an insecure dev key. "
                "Secrets will not be portable across deployments. Set APP_SECRET_KEY in production.",
                file=sys.stderr,
            )
            _warned = True
        raw = _DEV_FALLBACK
    # Derive a valid 32-byte urlsafe-base64 Fernet key from any passphrase.
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a string to a Fernet token. None/empty pass through unchanged."""
    if not plaintext:
        return plaintext
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Decrypt a Fernet token back to plaintext. None/empty pass through.

    Returns None if the token cannot be decrypted (wrong key / corrupted data)
    so callers degrade gracefully instead of crashing.
    """
    if not token:
        return token
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        print("[crypto] WARNING: failed to decrypt a secret (key mismatch?).", file=sys.stderr)
        return None
