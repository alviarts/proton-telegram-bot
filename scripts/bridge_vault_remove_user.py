#!/usr/bin/env python3
"""Remove a Proton user entry from Bridge's encrypted ``vault.enc``.

Why bypass ``bridge --cli delete``?
-----------------------------------
The interactive ``delete`` command needs the *username* (no ``@domain``)
and a confirmation prompt, and silently no-ops on accounts that are in
"locked" state because the keychain helper is unavailable (common on
headless VPS installs without a working DBus / SecretService). When that
happens, the runtime forgets about the user but the encrypted vault
file still has the entry — so any subsequent ``decrypt`` (e.g. our
``bridge_decrypt_vault.py``) keeps returning stale credentials and the
bot's ``/connect`` flow incorrectly skips the Proton login + recovery
email setup, dead-ending in a "no such user" Bridge IMAP error.

This script edits the vault file directly, on disk, with the same
AES-GCM scheme Bridge uses internally:

    msgpack.unpackb(file_bytes) -> {Version, Data}
    Data = nonce(12) || ciphertext+tag (AES-GCM, key=SHA256(rawKey))
    plaintext = msgpack.unpackb(...)
    plaintext["Users"] = [u for u in plaintext["Users"] if u doesn't match]
    new_ct = AESGCM(key).encrypt(new_nonce, msgpack.packb(plaintext), None)
    file_bytes = msgpack.packb({Version, Data: new_nonce + new_ct})

Bridge MUST be stopped before running this; the systemd service holds
an exclusive lock on the vault while it's running and will overwrite
our edits on shutdown otherwise.

Usage::

    pass show docker-credential-helpers/<key>/bridge-vault-key \
        | python3 scripts/bridge_vault_remove_user.py vielz74@proton.me \
            [vault_path]

Match is case-insensitive on ``PrimaryEmail`` *or* ``Username`` so the
caller can pass either ``vielz74`` or ``vielz74@proton.me``.

Exit codes:
    0  user removed (or was not present to begin with)
    1  decrypt or write failed
   64  bad arguments
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import msgpack
except ImportError as exc:  # pragma: no cover - optional dep
    sys.stderr.write("msgpack is required: pip install msgpack\n")
    raise SystemExit(1) from exc


DEFAULT_VAULT = "/root/.config/protonmail/bridge-v3/vault.enc"


def _matches(user: dict, target: str) -> bool:
    """Return True if the vault user entry matches ``target``."""
    target = target.strip().lower()
    primary = (user.get("PrimaryEmail") or "").lower()
    username = (user.get("Username") or "").lower()
    return target == primary or target == username


def remove_user(vault_path: str, raw_key_b64: str, target: str) -> tuple[bool, int]:
    """Decrypt vault, drop the matching user, re-encrypt back in place.

    Returns ``(removed, remaining_count)``. ``removed`` is True if at
    least one user was filtered out; False if no entry matched.
    """
    raw_key = base64.b64decode(raw_key_b64)
    key = hashlib.sha256(raw_key).digest()

    with open(vault_path, "rb") as fh:
        buf = fh.read()

    file_obj = msgpack.unpackb(buf, raw=False)
    data = file_obj["Data"]
    nonce, ct = data[:12], data[12:]
    aes = AESGCM(key)
    plaintext = aes.decrypt(nonce, ct, None)
    vault = msgpack.unpackb(plaintext, raw=False)

    users = vault.get("Users") or []
    kept = [u for u in users if not (isinstance(u, dict) and _matches(u, target))]
    removed = len(kept) != len(users)
    if not removed:
        return False, len(users)

    vault["Users"] = kept
    new_plain = msgpack.packb(vault, use_bin_type=True)
    new_nonce = secrets.token_bytes(12)
    new_ct = aes.encrypt(new_nonce, new_plain, None)
    file_obj["Data"] = new_nonce + new_ct
    new_buf = msgpack.packb(file_obj, use_bin_type=True)

    # Atomic write so we don't leave a half-written vault on disk if
    # this process crashes mid-write — Bridge would otherwise refuse
    # to start with a corrupted file.
    dir_name = os.path.dirname(vault_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".vault.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(new_buf)
        # Match the original mode/owner to keep Bridge happy.
        try:
            st = os.stat(vault_path)
            os.chmod(tmp_path, st.st_mode & 0o777)
            try:
                os.chown(tmp_path, st.st_uid, st.st_gid)
            except (PermissionError, AttributeError):
                pass
        except FileNotFoundError:
            pass
        os.replace(tmp_path, vault_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise

    return True, len(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        help=(
            "Email or username to drop (e.g. 'vielz74@proton.me' or "
            "'vielz74'). Match is case-insensitive."
        ),
    )
    parser.add_argument("vault_path", nargs="?", default=DEFAULT_VAULT)
    parser.add_argument(
        "--key",
        help="Raw vault key (base64); if omitted, read one line from stdin.",
    )
    args = parser.parse_args()

    raw_key_b64 = args.key.strip() if args.key else sys.stdin.readline().strip()
    if not raw_key_b64:
        sys.stderr.write("vault key not provided on stdin\n")
        return 64

    try:
        removed, remaining = remove_user(
            args.vault_path, raw_key_b64, args.target
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"vault file not found: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"vault edit failed: {type(exc).__name__}: {exc}\n")
        return 1

    if removed:
        sys.stderr.write(
            f"[+] removed {args.target!r} from vault; {remaining} user(s) left\n"
        )
    else:
        sys.stderr.write(
            f"[+] {args.target!r} was not in vault; {remaining} user(s) untouched\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
