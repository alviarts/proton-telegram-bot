#!/usr/bin/env python3
"""Remove a Proton account from a host-installed Proton Bridge.

Counterpart to ``bridge_add_account.py``. Drives ``bridge --cli`` to run::

    delete <email>

which removes the account from Bridge's keychain (a logout + key purge in
one step). Bridge holds an exclusive lock on its vault during ``--cli``
sessions, so the systemd service is stopped while we drive the CLI and
restarted on exit.

Usage::

    sudo python3 scripts/bridge_remove_account.py vielz99@proton.me

Exit codes:
    0  account removed (or was not present to begin with)
    1  delete command rejected by Bridge
    2  pexpect / subprocess error
   64  bad arguments
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

import pexpect

DELETED_RE = re.compile(
    r"(account .* (?:was )?(?:deleted|removed|logged out)"
    r"|disconnected the account"
    r"|account.*not found"
    r"|no account)",
    re.IGNORECASE,
)
NOT_FOUND_RE = re.compile(r"(not found|no account|unknown account)", re.IGNORECASE)


def stop_bridge(service: str, lock_path: str | None) -> None:
    subprocess.run(["systemctl", "stop", service], check=True)
    if not lock_path:
        return
    for _ in range(20):
        time.sleep(0.5)
        if not os.path.exists(lock_path):
            return
    sys.stderr.write(f"warning: lock {lock_path} still present after 10s\n")


def start_bridge(service: str) -> None:
    subprocess.run(["systemctl", "start", service], check=True)


def remove_account(
    binary: str,
    email: str,
    *,
    timeout: int = 60,
    log_to_stderr: bool = True,
) -> int:
    sys.stderr.write(f"[+] deleting {email} via {binary} --cli ...\n")
    env = dict(os.environ)
    env.setdefault("HOME", "/root")
    env.setdefault("GNUPGHOME", "/root/.gnupg")
    env.setdefault("PASSWORD_STORE_DIR", "/root/.password-store")

    child = pexpect.spawn(
        binary,
        ["--cli", "-l", "warn"],
        env=env,
        timeout=timeout,
        encoding="utf-8",
    )
    if log_to_stderr:
        child.logfile_read = sys.stderr

    final = "error"
    try:
        child.expect(r">>> ", timeout=30)
        child.sendline(f"delete {email}")
        # Bridge prompts for confirmation: "Are you sure ... (y/N)".
        for _ in range(4):
            idx = child.expect(
                [
                    r"\(y/N\)|\(yes/no\)|confirm",
                    DELETED_RE,
                    NOT_FOUND_RE,
                    r">>> ",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=timeout,
            )
            if idx == 0:
                child.sendline("yes")
                continue
            if idx == 1:
                final = "removed"
                break
            if idx == 2:
                final = "not-found"
                break
            if idx == 3:
                # Returned to prompt without an explicit success line; treat
                # as success since the delete subcommand printed nothing.
                final = "removed"
                break
            if idx in (4, 5):
                final = "eof-or-timeout"
                break

        try:
            child.sendline("quit")
            child.expect(pexpect.EOF, timeout=10)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
        child.close()
        return 0 if final in ("removed", "not-found") else 1
    except (pexpect.TIMEOUT, pexpect.EOF) as exc:
        sys.stderr.write(f"[!] pexpect failed: {exc}\n")
        try:
            child.terminate(force=True)
        except Exception:
            pass
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument(
        "--bridge-binary",
        default="/usr/lib/protonmail/bridge/bridge",
        help="Path to the Bridge launcher binary (the ``--cli`` host).",
    )
    parser.add_argument("--service", default="protonmail-bridge.service")
    parser.add_argument(
        "--lock-path",
        default="/root/.cache/protonmail/bridge-v3/bridge-v3.lock",
        help="Lock file to wait for after stopping bridge (empty to skip).",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Don't start protonmail-bridge.service on exit (useful for tests).",
    )
    args = parser.parse_args()

    sys.stderr.write("[+] stopping bridge service ...\n")
    stop_bridge(args.service, args.lock_path or None)
    try:
        rc = remove_account(args.bridge_binary, args.email)
    finally:
        if not args.no_restart:
            sys.stderr.write("[+] starting bridge service ...\n")
            start_bridge(args.service)
    return rc


if __name__ == "__main__":
    sys.exit(main())
