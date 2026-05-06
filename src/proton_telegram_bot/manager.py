"""Coordinator that spawns/stops one IMAP listener per primary Proton account.

Each chat may own one or more ``primary_accounts`` rows (one per Proton Mail
account that the user has logged into Bridge). The manager runs exactly one
listener per primary so emails from different accounts can be monitored in
parallel.
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.message import Message

from aioimaplib import aioimaplib

from .crypto import CredentialCipher
from .db import Database, credentials_from_primary
from .email_parser import (
    extract_recipients,
    extract_sender,
    find_matching_alias,
    parse_message,
    summarize,
)
from .imap_listener import IMAPListener
from .models import BridgeCredentials

LOGGER = logging.getLogger(__name__)

# Default sync interval in seconds (5 minutes).
DEFAULT_ALIAS_SYNC_INTERVAL = 300


class ListenerManager:
    """Owns the lifecycle of all per-primary IMAP listeners and routes new emails."""

    def __init__(
        self,
        db: Database,
        cipher: CredentialCipher,
        notifier: Notifier,
        alias_sync_interval: int = DEFAULT_ALIAS_SYNC_INTERVAL,
    ) -> None:
        self._db = db
        self._cipher = cipher
        self._notifier = notifier
        self._alias_sync_interval = alias_sync_interval
        # Listeners keyed by primary_id so a chat with several Proton accounts
        # can run multiple IMAP sessions concurrently.
        self._listeners: dict[int, IMAPListener] = {}
        self._lock = asyncio.Lock()

    async def restore_all(self) -> None:
        primary_ids = await self._db.list_all_primary_ids()
        for primary_id in primary_ids:
            try:
                await self.start_for_primary(primary_id)
            except Exception:
                LOGGER.exception(
                    "failed to start listener for primary %s", primary_id
                )

    async def start_for_primary(self, primary_id: int) -> None:
        async with self._lock:
            await self._stop_locked(primary_id)
            primary = await self._db.get_primary_account_by_id(primary_id)
            encrypted = await self._db.get_primary_encrypted_password(primary_id)
            if primary is None or encrypted is None:
                LOGGER.warning(
                    "no credentials stored for primary %s; skipping", primary_id
                )
                return
            password = self._cipher.decrypt(encrypted)
            credentials = credentials_from_primary(primary, password)
            listener = IMAPListener(
                chat_id=primary.chat_id,
                primary_id=primary_id,
                credentials=credentials,
                on_new_message=self._handle_new_message,
                on_aliases_discovered=self._handle_discovered_aliases,
                alias_sync_interval=self._alias_sync_interval,
            )
            listener.start()
            self._listeners[primary_id] = listener
            LOGGER.info(
                "started imap listener for primary %s chat %s (%s)",
                primary_id,
                primary.chat_id,
                credentials.display(),
            )

    async def stop_for_primary(self, primary_id: int) -> None:
        async with self._lock:
            await self._stop_locked(primary_id)

    async def stop_for_user(self, chat_id: int) -> None:
        """Stop every listener that belongs to ``chat_id``.

        Used by the /disconnect-all flow.
        """
        async with self._lock:
            primary_ids = [
                pid
                for pid, listener in self._listeners.items()
                if listener.chat_id == chat_id
            ]
            for pid in primary_ids:
                await self._stop_locked(pid)

    async def fetch_recent_for_active(
        self, chat_id: int, limit: int = 5
    ) -> list[Message]:
        """Open a fresh IMAP session and fetch the last ``limit`` messages
        targeting the chat's currently active alias.

        Returns an empty list when:

        * no alias is locked,
        * the primary's credentials are missing,
        * the IMAP server returns no matching mail.

        This is the backbone of the ``/inbox`` recovery command: when an
        email arrives but never appears in the chat (Telegram API
        timeout, listener crash, etc.) the user can pull it back via a
        manual fetch instead of waiting for the next poll cycle.

        We deliberately use a *fresh* connection rather than reusing
        the running listener's client, so the listener's poll loop is
        not interrupted and any concurrency bugs in aioimaplib's state
        machine cannot starve regular forwards.
        """
        active = await self._db.get_active_alias(chat_id)
        if active is None:
            return []
        primary = await self._db.get_primary_account_by_id(active.primary_id)
        encrypted = await self._db.get_primary_encrypted_password(
            active.primary_id
        )
        if primary is None or encrypted is None:
            return []
        try:
            password = self._cipher.decrypt(encrypted)
        except Exception:
            LOGGER.debug(
                "decrypt failed for primary %s in /inbox", active.primary_id
            )
            return []
        creds = credentials_from_primary(primary, password)
        target = active.email.lower()
        return await _imap_fetch_recent_for_alias(creds, target, limit=limit)

    def poke_user(self, chat_id: int) -> bool:
        """Trigger an immediate IMAP poll for every listener owned by
        ``chat_id``. Returns False if none are running."""
        poked = False
        for listener in self._listeners.values():
            if listener.chat_id == chat_id:
                listener.poke()
                poked = True
        return poked

    def poke_primary(self, primary_id: int) -> bool:
        listener = self._listeners.get(primary_id)
        if listener is None:
            return False
        listener.poke()
        return True

    async def stop_all(self) -> None:
        async with self._lock:
            for primary_id in list(self._listeners.keys()):
                await self._stop_locked(primary_id)

    async def _stop_locked(self, primary_id: int) -> None:
        listener = self._listeners.pop(primary_id, None)
        if listener is not None:
            await listener.stop()
            LOGGER.info("stopped imap listener for primary %s", primary_id)

    async def _handle_new_message(
        self,
        chat_id: int,
        primary_id: int,
        message: Message,
        uid: str,
    ) -> None:
        # Auto-add any new recipient addresses found in this message, scoped
        # to the primary account they came from. The new aliases will only
        # become forwarding targets after the user explicitly picks them in
        # /list (strict lock-mode below).
        recipients = extract_recipients(message)
        if recipients:
            added = await self._db.add_aliases(
                chat_id, list(recipients), primary_id=primary_id
            )
            if added > 0:
                LOGGER.info(
                    "chat %s primary %s auto-added %d alias(es) from uid %s",
                    chat_id,
                    primary_id,
                    added,
                    uid,
                )

        # Routing rule (STRICT lock-mode):
        #
        # An email is forwarded ONLY when the chat has an explicit active
        # alias lock AND the lock belongs to *this* listener's primary AND
        # the message targets that locked alias. In every other case the
        # email is dropped.
        #
        # Rationale: without strict gating, every alias auto-discovered from
        # the inbox immediately becomes a forwarding target, so /unlock has
        # no effective "stop" semantics and users get spammed with mail to
        # aliases they did not pick. Picking from /list is the only way to
        # opt in. /unlock returns to the silent state.
        active = await self._db.get_active_alias(chat_id)
        if active is None or active.primary_id != primary_id:
            LOGGER.debug(
                "chat %s primary %s has no active lock for this primary; "
                "ignoring uid %s",
                chat_id,
                primary_id,
                uid,
            )
            return
        matched = find_matching_alias(message, {active.email})
        if matched is None:
            LOGGER.debug(
                "uid %s does not target active alias %s for chat %s",
                uid,
                active.email,
                chat_id,
            )
            return
        summary = summarize(message)
        # Surface the From-header so the notifier can decide whether to
        # auto-label this alias or defer until the user confirms they
        # actually saw the email. The previous implementation called
        # ``record_alias_sender`` here unconditionally, which mislabeled
        # aliases when the subsequent ``send_message`` failed (Telegram
        # API timeout, polling disconnect, etc.) — the user complained:
        # "email tdk keluar dibot saya tetapi otomatis kelabel devin".
        # Recording is now deferred to the notifier's "✓ Tandai sudah
        # dibaca" callback in ``bot.py``; this method is purely metadata
        # plumbing.
        sender_email, sender_domain = extract_sender(message)
        await self._notifier.notify_email_received(
            chat_id,
            active.email,
            summary,
            alias_id=active.id,
            sender_email=sender_email,
            sender_domain=sender_domain,
        )

    async def _handle_discovered_aliases(
        self,
        chat_id: int,
        primary_id: int,
        addresses: set[str],
    ) -> None:
        """Auto-add aliases discovered during inbox scan, scoped to a primary."""
        existing = {
            a.email
            for a in await self._db.list_aliases(chat_id, primary_id=primary_id)
        }
        added = await self._db.add_aliases(
            chat_id, list(addresses), primary_id=primary_id
        )
        if added > 0:
            new_aliases = sorted(addresses - existing)
            LOGGER.info(
                "chat %s primary %s inbox scan added %d new alias(es) from %d discovered",
                chat_id,
                primary_id,
                added,
                len(addresses),
            )
            await self._notifier.notify_aliases_discovered(chat_id, new_aliases)


class Notifier:
    """Tiny adapter so the manager can deliver notifications without importing telegram types."""

    async def notify_email_received(
        self,
        chat_id: int,
        alias_email: str,
        summary: dict[str, str],
        *,
        alias_id: int | None = None,
        sender_email: str = "",
        sender_domain: str = "",
    ) -> None:  # pragma: no cover - implemented by the bot module
        raise NotImplementedError

    async def notify_aliases_discovered(
        self,
        chat_id: int,
        aliases: list[str],
    ) -> None:  # pragma: no cover - implemented by the bot module
        pass


# IMAP FETCH responses use ``* <seq> FETCH ...`` lines; aioimaplib strips
# the leading ``* `` so we accept either form. Used by the manual /inbox
# fetch helper below to skip status lines and find the literal payload.
_FETCH_LINE_RE = re.compile(rb"^(?:\*\s+)?\d+\s+FETCH\b", re.IGNORECASE)


def _parse_uids(lines: list[bytes | str]) -> list[int]:
    """Mirror of ``IMAPListener._parse_uids`` — kept module-level so the
    on-demand /inbox fetcher doesn't need a listener instance.
    """
    uids: list[int] = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("ascii", errors="ignore")
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0].upper() == "SEARCH":
            tokens = tokens[1:]
        if not tokens or not all(t.isdigit() for t in tokens):
            continue
        uids.extend(int(t) for t in tokens)
    return uids


def _extract_rfc822_payload(lines: list[bytes | str]) -> bytes | None:
    for index, line in enumerate(lines):
        if isinstance(line, str):
            line_bytes = line.encode()
        elif isinstance(line, (bytes, bytearray)):
            line_bytes = bytes(line)
        else:
            continue
        if _FETCH_LINE_RE.match(line_bytes) and index + 1 < len(lines):
            payload = lines[index + 1]
            if isinstance(payload, str):
                return payload.encode("utf-8", errors="replace")
            if isinstance(payload, (bytes, bytearray)):
                return bytes(payload)
    return None


async def _imap_fetch_recent_for_alias(
    creds: BridgeCredentials,
    target_alias: str,
    *,
    limit: int = 5,
) -> list[Message]:
    """Open a one-shot IMAP session, fetch the last ``limit`` messages
    addressed to ``target_alias``, and return them as parsed
    ``email.message.Message`` objects.

    The active background listener is *not* touched — Bridge accepts
    parallel sessions cheaply, and this avoids any risk of stalling
    real-time forwards.
    """
    if creds.use_ssl:
        client = aioimaplib.IMAP4_SSL(
            host=creds.host, port=creds.port, timeout=30
        )
    else:
        client = aioimaplib.IMAP4(host=creds.host, port=creds.port, timeout=30)
    messages: list[Message] = []
    try:
        await client.wait_hello_from_server()
        await client.login(creds.username, creds.password)
        await client.select("INBOX")
        # Two-step search: (1) ``TO`` is the cheapest server-side filter
        # for messages targeting this alias; (2) fall back to ``ALL`` and
        # filter client-side because Proton Bridge sometimes doesn't
        # honour ``TO`` for plus-addressed/bcc'd mail.
        target = target_alias.strip().lower()
        primary_attempt = await client.uid_search(f'TO "{target}"')
        uids = _parse_uids(primary_attempt.lines)
        if not uids:
            fallback = await client.uid_search("ALL")
            uids = _parse_uids(fallback.lines)
            client_side_filter = True
        else:
            client_side_filter = False
        # Most recent first, capped at limit (the server returns
        # ascending UIDs).
        uids.sort(reverse=True)
        for uid in uids:
            if len(messages) >= limit:
                break
            response = await client.uid("fetch", str(uid), "(RFC822)")
            if response.result != "OK":
                continue
            raw = _extract_rfc822_payload(response.lines)
            if raw is None:
                continue
            try:
                msg = parse_message(raw)
            except Exception:
                LOGGER.debug("parse failed for /inbox UID %s", uid, exc_info=True)
                continue
            if client_side_filter:
                recipients = extract_recipients(msg)
                if target not in recipients:
                    continue
            # Skip /cekimap probe mails just like the live listener does.
            subject_header = msg.get("Subject") or ""
            if "[health-check]" in subject_header.lower():
                continue
            messages.append(msg)
    finally:
        try:
            await client.logout()
        except Exception:
            pass
    return messages
