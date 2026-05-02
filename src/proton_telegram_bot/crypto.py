"""Symmetric encryption helpers for storing IMAP passwords at rest."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    """Encrypts and decrypts short credential strings using Fernet (AES-128-CBC + HMAC)."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("encryption key must be a non-empty string")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a 32-byte url-safe base64 Fernet key. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("failed to decrypt credential (wrong key or corrupted data)") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")
