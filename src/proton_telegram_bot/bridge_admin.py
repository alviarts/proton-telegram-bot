"""Programmatic management of a host-side Proton Bridge install.

This module wraps the two helper scripts in ``scripts/`` so that the
``/connect`` command can:

1. Take the user's *Proton account* password (not the random per-account
   IMAP password Bridge generates).
2. Drive ``bridge --cli login`` to register the account with Bridge.
3. If Proton requests human verification, surface the CAPTCHA URL to the
   Telegram chat and wait for the user to confirm completion.
4. Decrypt Bridge's vault to extract the now-known IMAP password.
5. Hand the resulting credentials back to the bot so it can save them to
   the DB and start an IMAP listener — without the user ever having to
   open Bridge's GUI or copy/paste a 22-character random string.

The actual privileged work (``systemctl``, reading ``pass``, running the
``bridge`` binary) lives in ``scripts/bridge_add_account.py`` and
``scripts/bridge_decrypt_vault.py``. We invoke those as subprocesses so
the bot itself doesn't need to talk to systemd or the keychain directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from .config import Settings

LOGGER = logging.getLogger(__name__)


class BridgeAdminError(Exception):
    """Raised for unrecoverable failures while driving Bridge."""


@dataclasses.dataclass(frozen=True)
class CaptchaRequired:
    url: str


@dataclasses.dataclass(frozen=True)
class LoginSucceeded:
    pass


@dataclasses.dataclass(frozen=True)
class LoginFailed:
    reason: str


BridgeEvent = CaptchaRequired | LoginSucceeded | LoginFailed


@dataclasses.dataclass(frozen=True)
class BridgeImapCredentials:
    email: str
    imap_username: str
    imap_password: str


class BridgeAdmin:
    """Drive a host Proton Bridge from the bot.

    The instance is reusable across requests; each ``add_account`` call is
    serialised through an internal lock because Bridge holds an exclusive
    lock on its vault during ``--cli`` sessions.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._settings.bridge_admin_enabled

    async def add_account(
        self,
        email: str,
        proton_password: str,
    ) -> AsyncIterator[BridgeEvent]:
        """Yield events as Bridge processes a login.

        The caller is expected to:
            - Forward any ``CaptchaRequired`` URL to the Telegram chat.
            - Call :meth:`acknowledge_captcha` once the user confirms they
              have completed the verification.

        The generator terminates with exactly one of ``LoginSucceeded`` or
        ``LoginFailed``.
        """
        if not self._settings.bridge_admin_enabled:
            raise BridgeAdminError(
                "BRIDGE_ADMIN_ENABLED=false; auto-add disabled."
            )
        async with self._lock:
            async for event in self._run_add_account(email, proton_password):
                yield event

    async def _run_add_account(
        self, email: str, proton_password: str
    ) -> AsyncIterator[BridgeEvent]:
        s = self._settings
        # Wipe stale signal files so we don't accidentally re-trigger a
        # previous CAPTCHA flow.
        for path in (s.bridge_captcha_url_file, s.bridge_captcha_done_flag):
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()

        argv = [
            *(["sudo", "-n"] if s.bridge_sudo else []),
            s.bridge_python,
            str(s.bridge_add_account_script),
            email,
            proton_password,
            "--captcha-url-file",
            str(s.bridge_captcha_url_file),
            "--captcha-done-flag",
            str(s.bridge_captcha_done_flag),
            "--captcha-timeout",
            str(s.bridge_captcha_timeout_seconds),
        ]
        LOGGER.info("starting bridge --cli login for %s", email)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        captcha_seen: set[str] = set()
        try:
            assert process.stdout is not None
            while True:
                if process.returncode is not None:
                    break
                # Poll the captcha file every second; also drain stderr to
                # keep the pipe from blocking the child.
                with contextlib.suppress(FileNotFoundError):
                    url = Path(s.bridge_captcha_url_file).read_text().strip()
                    if url and url not in captcha_seen:
                        captcha_seen.add(url)
                        LOGGER.info("CAPTCHA required for %s: %s", email, url)
                        yield CaptchaRequired(url=url)
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except TimeoutError:
                    pass
            stdout = await process.stdout.read()
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            LOGGER.debug(
                "bridge_add_account exited rc=%s stdout=%r stderr=%r",
                process.returncode,
                stdout[-500:],
                stderr[-500:],
            )
            if process.returncode == 0:
                yield LoginSucceeded()
            else:
                tail = (stderr or stdout).decode("utf-8", errors="replace")
                yield LoginFailed(reason=tail.strip().splitlines()[-1] if tail.strip() else "unknown")
        finally:
            if process.returncode is None:
                # Send SIGTERM first so the helper script's ``finally``
                # block can restart the Bridge service before exiting.
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=15)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()

    async def acknowledge_captcha(self) -> None:
        """Signal the helper script that the user has finished the CAPTCHA."""
        flag = self._settings.bridge_captcha_done_flag
        Path(flag).touch()

    async def cancel_captcha(self) -> None:
        """Best-effort cleanup if the user backs out of /connect mid-flight.

        The ``bridge_add_account.py`` helper stops the Bridge systemd
        service before driving ``bridge --cli``.  If we kill that helper
        (e.g. because the async-generator is GC'd on /cancel), its
        ``finally`` block never runs and Bridge stays down.  We
        explicitly restart Bridge here so IMAP listeners can reconnect.
        """
        for path in (
            self._settings.bridge_captcha_url_file,
            self._settings.bridge_captcha_done_flag,
        ):
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()
        await self._ensure_bridge_running()

    async def _ensure_bridge_running(self) -> None:
        """Restart the Bridge systemd service if it is not active."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", "protonmail-bridge.service",
            )
            await proc.wait()
            if proc.returncode != 0:
                LOGGER.info("Bridge service is not running; restarting it")
                start = await asyncio.create_subprocess_exec(
                    "systemctl", "start", "protonmail-bridge.service",
                )
                await start.wait()
        except Exception:
            LOGGER.exception("failed to ensure Bridge service is running")

    async def fetch_imap_credentials(self, email: str) -> BridgeImapCredentials | None:
        """Return the Bridge-managed IMAP creds for ``email``, or ``None``.

        Bridge stores its per-account state in an AES-GCM-encrypted vault
        keyed by an entry in the system ``pass`` keychain. We invoke the
        helper script to decrypt the vault and pluck out the matching
        user.
        """
        s = self._settings
        if not s.bridge_admin_enabled:
            return None

        # Read the raw vault key via the configured shell command.
        key_proc = await asyncio.create_subprocess_shell(
            s.bridge_vault_key_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw_key, key_err = await key_proc.communicate()
        if key_proc.returncode != 0 or not raw_key.strip():
            raise BridgeAdminError(
                f"failed to read vault key (rc={key_proc.returncode}): "
                f"{key_err.decode('utf-8', errors='replace')!r}"
            )

        argv = [
            *(["sudo", "-n"] if s.bridge_sudo else []),
            s.bridge_python,
            str(s.bridge_decrypt_vault_script),
            str(s.bridge_vault_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await proc.communicate(input=raw_key)
        if proc.returncode != 0:
            raise BridgeAdminError(
                f"failed to decrypt vault (rc={proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')!r}"
            )

        try:
            data = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BridgeAdminError(f"vault dump was not JSON: {exc}") from exc

        for user in data.get("Users", []):
            if (user.get("PrimaryEmail") or "").lower() != email.lower():
                continue
            imap_username = user.get("ImapUsername") or email
            imap_password = user.get("ImapPassword")
            if not imap_password:
                return None
            return BridgeImapCredentials(
                email=email,
                imap_username=imap_username,
                imap_password=imap_password,
            )
        return None
