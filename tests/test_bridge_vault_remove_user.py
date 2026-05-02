"""End-to-end test for ``scripts/bridge_vault_remove_user.py``.

We construct a real Bridge-format vault (msgpack + AES-GCM), run the
script as a subprocess, and verify the rewritten file still decrypts
and no longer contains the dropped user.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import subprocess
import sys
from pathlib import Path

import msgpack
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge_vault_remove_user.py"


def _make_vault(path: Path, raw_key: bytes, users: list[dict]) -> None:
    key = hashlib.sha256(raw_key).digest()
    vault = {"Settings": {}, "Users": users}
    plaintext = msgpack.packb(vault, use_bin_type=True)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    file_obj = {"Version": 1, "Data": nonce + ct}
    path.write_bytes(msgpack.packb(file_obj, use_bin_type=True))


def _decrypt_vault(path: Path, raw_key: bytes) -> dict:
    key = hashlib.sha256(raw_key).digest()
    file_obj = msgpack.unpackb(path.read_bytes(), raw=False)
    data = file_obj["Data"]
    nonce, ct = data[:12], data[12:]
    plaintext = AESGCM(key).decrypt(nonce, ct, None)
    return msgpack.unpackb(plaintext, raw=False)


def _run(target: str, vault_path: Path, raw_key: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), target, str(vault_path)],
        input=base64.b64encode(raw_key),
        capture_output=True,
        check=False,
    )


def test_removes_matching_user_by_email(tmp_path: Path) -> None:
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [
            {"PrimaryEmail": "vielz99@proton.me", "Username": "vielz99"},
            {"PrimaryEmail": "keep@proton.me", "Username": "keep"},
        ],
    )

    result = _run("vielz99@proton.me", vault, raw_key)
    assert result.returncode == 0, result.stderr.decode()

    after = _decrypt_vault(vault, raw_key)
    assert [u["PrimaryEmail"] for u in after["Users"]] == ["keep@proton.me"]


def test_removes_matching_user_by_bare_username(tmp_path: Path) -> None:
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [
            {"PrimaryEmail": "vielz99@proton.me", "Username": "vielz99"},
            {"PrimaryEmail": "keep@proton.me", "Username": "keep"},
        ],
    )

    result = _run("vielz99", vault, raw_key)
    assert result.returncode == 0, result.stderr.decode()
    assert [u["Username"] for u in _decrypt_vault(vault, raw_key)["Users"]] == ["keep"]


def test_no_op_when_user_missing_still_succeeds(tmp_path: Path) -> None:
    """Removing an absent user is a no-op (idempotent), not an error."""
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [{"PrimaryEmail": "alice@proton.me", "Username": "alice"}],
    )
    original = vault.read_bytes()

    result = _run("nobody@proton.me", vault, raw_key)
    assert result.returncode == 0
    # File is unchanged when no user matches.
    assert vault.read_bytes() == original


def test_match_is_case_insensitive(tmp_path: Path) -> None:
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [{"PrimaryEmail": "Vielz99@Proton.Me", "Username": "Vielz99"}],
    )

    result = _run("vielz99@proton.me", vault, raw_key)
    assert result.returncode == 0
    assert _decrypt_vault(vault, raw_key)["Users"] == []


def test_rewritten_vault_uses_fresh_nonce(tmp_path: Path) -> None:
    """Re-encrypt with a new nonce so AES-GCM stays semantically secure."""
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [
            {"PrimaryEmail": "drop@proton.me", "Username": "drop"},
            {"PrimaryEmail": "keep@proton.me", "Username": "keep"},
        ],
    )
    before_nonce = msgpack.unpackb(vault.read_bytes(), raw=False)["Data"][:12]

    result = _run("drop@proton.me", vault, raw_key)
    assert result.returncode == 0

    after_nonce = msgpack.unpackb(vault.read_bytes(), raw=False)["Data"][:12]
    assert after_nonce != before_nonce


def test_missing_key_on_stdin_returns_64(tmp_path: Path) -> None:
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(vault, raw_key, [])

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "anyone@proton.me", str(vault)],
        input=b"",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 64


def test_missing_vault_file_returns_1(tmp_path: Path) -> None:
    raw_key = secrets.token_bytes(32)
    result = _run("a@b.com", tmp_path / "does-not-exist.enc", raw_key)
    assert result.returncode == 1


@pytest.mark.parametrize("target", ["", "  "])
def test_argparse_rejects_blank_target(tmp_path: Path, target: str) -> None:
    """``argparse`` accepts blanks, but the script just no-ops then.

    This pins down that we don't accidentally treat ``""`` as "delete all
    users" — every entry has a non-empty ``PrimaryEmail`` so a blank
    target matches nothing.
    """
    raw_key = secrets.token_bytes(32)
    vault = tmp_path / "vault.enc"
    _make_vault(
        vault,
        raw_key,
        [{"PrimaryEmail": "a@b.com", "Username": "a"}],
    )
    original = vault.read_bytes()
    result = _run(target, vault, raw_key)
    assert result.returncode == 0
    assert vault.read_bytes() == original
