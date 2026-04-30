from __future__ import annotations

import pytest

from proton_telegram_bot.crypto import CredentialCipher


def test_round_trip() -> None:
    key = CredentialCipher.generate_key()
    cipher = CredentialCipher(key)
    plaintext = "super-secret-bridge-password"
    token = cipher.encrypt(plaintext)
    assert token != plaintext
    assert cipher.decrypt(token) == plaintext


def test_wrong_key_fails() -> None:
    cipher_a = CredentialCipher(CredentialCipher.generate_key())
    cipher_b = CredentialCipher(CredentialCipher.generate_key())
    token = cipher_a.encrypt("hello")
    with pytest.raises(ValueError):
        cipher_b.decrypt(token)


def test_invalid_key_rejected() -> None:
    with pytest.raises(ValueError):
        CredentialCipher("not-a-real-fernet-key")
    with pytest.raises(ValueError):
        CredentialCipher("")
