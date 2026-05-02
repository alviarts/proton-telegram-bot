#!/usr/bin/env python3
"""Add a Proton account to a host-installed Proton Bridge non-interactively.

Bridge ships with an interactive ``--cli`` shell that supports a ``login``
command (``Username:`` / ``Password:`` prompts). Bridge holds an exclusive
file lock on its vault, so the systemd service must be stopped while we
drive the CLI; we restart it on exit.

The CLI prints a Human Verification URL when the account hits CAPTCHA. We
parse that URL out of the output, write it to ``--captcha-url-file``, and
block waiting for ``--captcha-done-flag`` to appear (the calling bot
creates that flag once the user has solved the challenge in their
browser). The bot can also pre-create ``--captcha-done-flag`` to time out.

Usage::

    sudo python3 scripts/bridge_add_account.py vielz99@proton.me '<proton-pw>'

Exit codes:
    0  account added successfully (or was already present)
    1  login failed (incorrect creds, 2FA needed, etc.)
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

SUCCESS_RE = re.compile(r"Account .* was added successfully", re.IGNORECASE)
ALREADY_RE = re.compile(r"already.*log[ -]?in|already.*added", re.IGNORECASE)
CAPTCHA_RE = re.compile(
    r"Human Verification requested.*?(https://verify\.proton\.me/\S+)",
    re.IGNORECASE | re.DOTALL,
)
TWOFA_RE = re.compile(r"two.?factor|2FA|TOTP", re.IGNORECASE)
MAILBOX_PW_RE = re.compile(r"mailbox.*password", re.IGNORECASE)


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


def wait_for_done_flag(flag: str, timeout: int) -> bool:
    if os.path.exists(flag):
        os.unlink(flag)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(flag):
            os.unlink(flag)
            return True
        time.sleep(1)
    return False


def write_captcha_url(path: str, url: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(url)
    os.replace(tmp, path)


def add_account(
    binary: str,
    email: str,
    password: str,
    *,
    captcha_url_file: str,
    captcha_done_flag: str,
    captcha_timeout: int,
    timeout: int = 120,
    log_to_stderr: bool = True,
) -> int:
    sys.stderr.write(f"[+] adding {email} via {binary} --cli ...\n")
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
        child.sendline("login")
        child.expect(r"Username:\s*", timeout=20)
        child.sendline(email)
        child.expect(r"Password:\s*", timeout=20)
        child.sendline(password)

        for _ in range(6):
            idx = child.expect(
                [
                    SUCCESS_RE,
                    ALREADY_RE,
                    CAPTCHA_RE,
                    TWOFA_RE,
                    MAILBOX_PW_RE,
                    r"Incorrect login credentials",
                    r"failed to login",
                    r">>> ",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=timeout,
            )
            if idx == 0:
                final = "success"
                break
            if idx == 1:
                final = "already-added"
                break
            if idx == 2:
                match = child.match
                url = match.group(1) if match else "<unparsed>"
                sys.stderr.write(f"[!] CAPTCHA URL: {url}\n")
                write_captcha_url(captcha_url_file, url)
                ok = wait_for_done_flag(captcha_done_flag, captcha_timeout)
                if not ok:
                    final = "captcha-timeout"
                    break
                child.sendline("")  # press ENTER
                continue
            if idx == 3:
                final = "2fa-required"
                break
            if idx == 4:
                child.sendline(password)
                continue
            if idx in (5, 6):
                final = "incorrect-credentials"
                break
            if idx == 7:
                final = "no-success"
                break
            if idx in (8, 9):
                final = "eof-or-timeout"
                break

        try:
            child.sendline("quit")
            child.expect(pexpect.EOF, timeout=10)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
        child.close()
        return 0 if final in ("success", "already-added") else 1
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
    parser.add_argument("password")
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
        "--captcha-url-file",
        default="/tmp/bridge_captcha_url.txt",
        help="Where to write the CAPTCHA URL when one is requested.",
    )
    parser.add_argument(
        "--captcha-done-flag",
        default="/tmp/bridge_captcha_done.flag",
        help="Path the caller will create once the user has solved the CAPTCHA.",
    )
    parser.add_argument(
        "--captcha-timeout",
        type=int,
        default=600,
        help="Seconds to wait for the user to solve a CAPTCHA before aborting.",
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
        rc = add_account(
            args.bridge_binary,
            args.email,
            args.password,
            captcha_url_file=args.captcha_url_file,
            captcha_done_flag=args.captcha_done_flag,
            captcha_timeout=args.captcha_timeout,
        )
    finally:
        if not args.no_restart:
            sys.stderr.write("[+] starting bridge service ...\n")
            start_bridge(args.service)
    return rc


if __name__ == "__main__":
    sys.exit(main())
