"""Coordinator that spawns/stops one IMAP listener per primary Proton account.

Each chat may own one or more ``primary_accounts`` rows (one per Proton Mail
account that the user has logged into Bridge). The manager runs exactly one
listener per primary so emails from different accounts can be monitored in
parallel.
"""
from __future__ import annotations

import asyncio
import logging
from email.message import Message

from .crypto import CredentialCipher
from .db import Database, credentials_from_primary
from .email_parser import extract_recipients, find_matching_alias, summarize
from .imap_listener import IMAPListener

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
        await self._notifier.notify_email_received(
            chat_id, active.email, summary
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
    ) -> None:  # pragma: no cover - implemented by the bot module
        raise NotImplementedError

    async def notify_aliases_discovered(
        self,
        chat_id: int,
        aliases: list[str],
    ) -> None:  # pragma: no cover - implemented by the bot module
        pass
