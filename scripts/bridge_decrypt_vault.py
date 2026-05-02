#!/usr/bin/env python3
"""Decrypt Proton Bridge ``vault.enc`` and dump the user list.

Bridge stores per-account state — including the random IMAP "BridgePass"
that Bridge displays in its GUI as the IMAP password — in a single
AES-GCM-encrypted file. The encryption key lives in the system keychain
(``pass`` / SecretService); we never decrypt the key, we just read the
already-decrypted bytes printed by ``pass show``.

This is the same logic Bridge uses to read its own vault — see
``internal/vault/{vault,types_file}.go`` in the upstream repo. Format:

    msgpack-Unmarshal(file_bytes) -> {Version, Data}
    Data = nonce(12) || ciphertext+tag (AES-GCM, key=SHA256(rawKey))
    msgpack-Unmarshal(plaintext) -> Vault{Settings, Users, ...}

Usage::

    pass show docker-credential-helpers/<key>/bridge-vault-key \
        | python3 scripts/bridge_decrypt_vault.py [vault_path]

The script reads the raw key from stdin (one line) and prints a JSON object
with ``Users`` containing UserID, Username, PrimaryEmail and the IMAP
password (Bridge's ``BridgePass`` re-encoded as base64url, the form Bridge
itself displays in its GUI).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import msgpack
except ImportError as exc:  # pragma: no cover - optional dep
    sys.stderr.write(
        "msgpack is required: pip install msgpack\n"
    )
    raise SystemExit(1) from exc


DEFAULT_VAULT = "/root/.config/protonmail/bridge-v3/vault.enc"


def imap_password_from_bridge_pass(raw: bytes) -> str:
    """Encode Bridge's 16-byte BridgePass into the base64url IMAP password.

    Bridge displays the IMAP password as base64url (``-_``) of the raw
    bytes with padding stripped. The IMAP server accepts that exact
    string.
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decrypt(vault_path: str, raw_key_b64: str) -> dict:
    raw_key = base64.b64decode(raw_key_b64)
    key = hashlib.sha256(raw_key).digest()

    with open(vault_path, "rb") as fh:
        buf = fh.read()

    file = msgpack.unpackb(buf, raw=False)
    data = file["Data"]
    nonce, ct = data[:12], data[12:]
    plaintext = AESGCM(key).decrypt(nonce, ct, None)
    return msgpack.unpackb(plaintext, raw=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_path", nargs="?", default=DEFAULT_VAULT)
    parser.add_argument(
        "--key",
        help="Raw vault key (base64); if omitted, read one line from stdin.",
    )
    args = parser.parse_args()

    raw_key_b64 = args.key.strip() if args.key else sys.stdin.readline().strip()
    if not raw_key_b64:
        sys.stderr.write("vault key not provided\n")
        return 64

    obj = decrypt(args.vault_path, raw_key_b64)

    users = []
    for u in obj.get("Users", []) or []:
        if not isinstance(u, dict):
            continue
        bp = u.get("BridgePass") or b""
        users.append(
            {
                "UserID": u.get("UserID"),
                "Username": u.get("Username"),
                "PrimaryEmail": u.get("PrimaryEmail"),
                "ImapUsername": u.get("PrimaryEmail"),
                "ImapPassword": imap_password_from_bridge_pass(bp) if bp else None,
            }
        )
    json.dump({"Users": users}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
