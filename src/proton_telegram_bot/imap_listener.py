"""Long-running IMAP listener for a single Proton Bridge account.

Uses short-interval polling (default 5 s) instead of IMAP IDLE so that new
mail is detected reliably even when the server's IDLE implementation is
incomplete (as is the case with Proton Mail Bridge).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from email.message import Message

from aioimaplib import aioimaplib

from .email_parser import extract_recipients, parse_message
from .models import BridgeCredentials

LOGGER = logging.getLogger(__name__)

NewMessageCallback = Callable[[int, Message, str], Awaitable[None]]
"""Callback signature: (chat_id, parsed_message, raw_uid)."""

AliasDiscoveryCallback = Callable[[int, set[str]], Awaitable[None]]
"""Callback signature: (chat_id, discovered_addresses)."""

# How often the listener polls for new messages (seconds).
POLL_INTERVAL_SECONDS = 5
RECONNECT_BACKOFF_SECONDS = (5, 15, 30, 60, 120)
FETCH_RESPONSE_RE = re.compile(rb"^\* \d+ FETCH ", re.IGNORECASE)


class IMAPListener:
    """Watches the INBOX for one user and dispatches new messages to a callback."""

    def __init__(
        self,
        chat_id: int,
        credentials: BridgeCredentials,
        on_new_message: NewMessageCallback,
        on_aliases_discovered: AliasDiscoveryCallback | None = None,
        alias_sync_interval: int = 300,
        mailbox: str = "INBOX",
    ) -> None:
        self.chat_id = chat_id
        self._credentials = credentials
        self._on_new_message = on_new_message
        self._on_aliases_discovered = on_aliases_discovered
        self._alias_sync_interval = alias_sync_interval
        self._mailbox = mailbox
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_seen_uid: int = 0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(), name=f"imap-listener-{self.chat_id}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._run_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = RECONNECT_BACKOFF_SECONDS[
                    min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)
                ]
                LOGGER.warning(
                    "imap session for chat %s failed (%s); reconnecting in %ss",
                    self.chat_id,
                    exc,
                    delay,
                )
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    continue

    async def _run_session(self) -> None:
        client = self._build_client()
        try:
            await client.wait_hello_from_server()
            await client.login(self._credentials.username, self._credentials.password)
            await client.select(self._mailbox)
            await self._initialize_uid_baseline(client)

            # Scan inbox for alias discovery at startup.
            await self._scan_inbox_aliases(client)

            seconds_since_sync = 0
            # Poll for new messages every POLL_INTERVAL_SECONDS.
            while not self._stop_event.is_set():
                await self._fetch_new_messages(client)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=POLL_INTERVAL_SECONDS
                    )
                except TimeoutError:
                    pass

                # Re-issue NOOP to keep the connection alive and trigger
                # server-side mailbox updates before the next search.
                try:
                    await client.noop()
                except Exception:
                    LOGGER.debug("NOOP failed for chat %s, reconnecting", self.chat_id)
                    break

                # Periodic alias sync.
                seconds_since_sync += POLL_INTERVAL_SECONDS
                if seconds_since_sync >= self._alias_sync_interval:
                    seconds_since_sync = 0
                    await self._scan_inbox_aliases(client)
        finally:
            try:
                await client.logout()
            except Exception:
                pass

    def _build_client(self) -> aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL:
        if self._credentials.use_ssl:
            return aioimaplib.IMAP4_SSL(
                host=self._credentials.host, port=self._credentials.port, timeout=30
            )
        return aioimaplib.IMAP4(
            host=self._credentials.host, port=self._credentials.port, timeout=30
        )

    async def _initialize_uid_baseline(self, client: aioimaplib.IMAP4) -> None:
        # Only seed the baseline on the *first* successful session — on reconnects
        # we must preserve the previous high-water mark so messages that arrived
        # while the connection was down are still picked up by _fetch_new_messages.
        if self._last_seen_uid > 0:
            LOGGER.debug(
                "chat %s preserving baseline UID %s across reconnect",
                self.chat_id,
                self._last_seen_uid,
            )
            return
        response = await client.uid_search("ALL")
        if response.result != "OK":
            return
        uids = self._parse_uids(response.lines)
        self._last_seen_uid = max(uids, default=0)
        LOGGER.debug(
            "chat %s baseline UID = %s (mailbox=%s)",
            self.chat_id,
            self._last_seen_uid,
            self._mailbox,
        )

    async def _scan_inbox_aliases(self, client: aioimaplib.IMAP4) -> None:
        """Scan all messages in the inbox and report discovered recipient addresses."""
        if self._on_aliases_discovered is None:
            return
        response = await client.uid_search("ALL")
        if response.result != "OK":
            return
        uids = self._parse_uids(response.lines)
        if not uids:
            return
        discovered: set[str] = set()
        for uid in uids:
            try:
                raw = await self._fetch_headers(client, uid)
                if raw is None:
                    continue
                msg = parse_message(raw)
                discovered.update(extract_recipients(msg))
            except Exception:
                LOGGER.debug("failed to fetch headers for UID %s", uid)
        if discovered:
            LOGGER.info(
                "chat %s alias scan discovered %d addresses",
                self.chat_id,
                len(discovered),
            )
            await self._on_aliases_discovered(self.chat_id, discovered)

    async def _fetch_headers(
        self, client: aioimaplib.IMAP4, uid: int
    ) -> bytes | None:
        """Fetch only the header portion of a message (lighter than full RFC822)."""
        response = await client.uid(
            "fetch", str(uid), "(BODY.PEEK[HEADER])"
        )
        if response.result != "OK":
            return None
        return self._extract_rfc822_payload(response.lines)

    async def _fetch_new_messages(self, client: aioimaplib.IMAP4) -> None:
        response = await client.uid_search(f"UID {self._last_seen_uid + 1}:*")
        if response.result != "OK":
            return
        uids = sorted(uid for uid in self._parse_uids(response.lines) if uid > self._last_seen_uid)
        for uid in uids:
            try:
                await self._fetch_and_dispatch(client, uid)
            except Exception:
                LOGGER.exception("failed to fetch/dispatch UID %s for chat %s", uid, self.chat_id)
            finally:
                self._last_seen_uid = max(self._last_seen_uid, uid)

    async def _fetch_and_dispatch(self, client: aioimaplib.IMAP4, uid: int) -> None:
        response = await client.uid("fetch", str(uid), "(RFC822)")
        if response.result != "OK":
            return
        raw_message = self._extract_rfc822_payload(response.lines)
        if raw_message is None:
            LOGGER.debug("no RFC822 payload returned for UID %s", uid)
            return
        message = parse_message(raw_message)
        await self._on_new_message(self.chat_id, message, str(uid))

    @staticmethod
    def _parse_uids(lines: list[bytes | str]) -> list[int]:
        """Extract UIDs from an IMAP SEARCH response.

        Only digits that follow the "SEARCH" keyword are treated as UIDs.
        Other untagged data (e.g. "JBND92 OK command completed in 1253 microsec",
        "OK [HIGHESTMODSEQ 42]", "12 EXISTS") may also appear in
        ``response.lines`` and must be ignored, otherwise we will try to fetch
        bogus UIDs that don't exist.
        """
        uids: list[int] = []
        for line in lines:
            if isinstance(line, bytes):
                line = line.decode("ascii", errors="ignore")
            tokens = line.split()
            try:
                idx = tokens.index("SEARCH")
            except ValueError:
                continue
            for token in tokens[idx + 1 :]:
                if token.isdigit():
                    uids.append(int(token))
                else:
                    # Stop at the first non-digit token to avoid picking up
                    # MODSEQ values or other annotations after the UID list.
                    break
        return uids

    @staticmethod
    def _extract_rfc822_payload(lines: list[bytes | str]) -> bytes | None:
        # aioimaplib returns the FETCH response interleaved across lines.
        # The element immediately following a FETCH line is the literal payload.
        for index, line in enumerate(lines):
            line_bytes = line.encode() if isinstance(line, str) else line
            if FETCH_RESPONSE_RE.match(line_bytes) and index + 1 < len(lines):
                payload = lines[index + 1]
                if isinstance(payload, str):
                    return payload.encode("utf-8", errors="replace")
                return payload
        return None
