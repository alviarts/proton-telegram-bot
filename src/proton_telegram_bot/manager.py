"""Coordinator that spawns/stops one IMAP listener per registered user."""
from __future__ import annotations

import asyncio
import logging
from email.message import Message

from .crypto import CredentialCipher
from .db import Database, credentials_from_user
from .email_parser import extract_recipients, find_matching_alias, summarize
from .imap_listener import IMAPListener
from .models import AliasStatus

LOGGER = logging.getLogger(__name__)

# Default sync interval in seconds (5 minutes).
DEFAULT_ALIAS_SYNC_INTERVAL = 300


class ListenerManager:
    """Owns the lifecycle of all per-user IMAP listeners and routes new emails."""

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
        self._listeners: dict[int, IMAPListener] = {}
        self._lock = asyncio.Lock()

    async def restore_all(self) -> None:
        chat_ids = await self._db.list_users_with_credentials()
        for chat_id in chat_ids:
            try:
                await self.start_for_user(chat_id)
            except Exception:
                LOGGER.exception("failed to start listener for chat %s", chat_id)

    async def start_for_user(self, chat_id: int) -> None:
        async with self._lock:
            await self._stop_locked(chat_id)
            user = await self._db.get_user(chat_id)
            encrypted = await self._db.get_encrypted_password(chat_id)
            if user is None or encrypted is None:
                LOGGER.warning("no credentials stored for chat %s; skipping", chat_id)
                return
            password = self._cipher.decrypt(encrypted)
            credentials = credentials_from_user(user, password)
            listener = IMAPListener(
                chat_id=chat_id,
                credentials=credentials,
                on_new_message=self._handle_new_message,
                on_aliases_discovered=self._handle_discovered_aliases,
                alias_sync_interval=self._alias_sync_interval,
            )
            listener.start()
            self._listeners[chat_id] = listener
            LOGGER.info("started imap listener for chat %s (%s)", chat_id, credentials.display())

    async def stop_for_user(self, chat_id: int) -> None:
        async with self._lock:
            await self._stop_locked(chat_id)

    async def stop_all(self) -> None:
        async with self._lock:
            for chat_id in list(self._listeners.keys()):
                await self._stop_locked(chat_id)

    async def _stop_locked(self, chat_id: int) -> None:
        listener = self._listeners.pop(chat_id, None)
        if listener is not None:
            await listener.stop()
            LOGGER.info("stopped imap listener for chat %s", chat_id)

    async def _handle_new_message(
        self,
        chat_id: int,
        message: Message,
        uid: str,
    ) -> None:
        # Auto-add any new recipient addresses found in this message.
        recipients = extract_recipients(message)
        if recipients:
            added = await self._db.add_aliases(chat_id, list(recipients))
            if added > 0:
                LOGGER.info(
                    "chat %s auto-added %d alias(es) from uid %s", chat_id, added, uid
                )

        available = await self._db.list_aliases(chat_id, status=AliasStatus.AVAILABLE)
        candidate_emails = {row.email for row in available}
        if not candidate_emails:
            LOGGER.debug("chat %s has no available aliases; ignoring uid %s", chat_id, uid)
            return
        matched = find_matching_alias(message, candidate_emails)
        if matched is None:
            LOGGER.debug("uid %s has no matching alias for chat %s", uid, chat_id)
            return
        alias = next((a for a in available if a.email == matched), None)
        if alias is None:
            return
        message_id = (message.get("Message-Id") or uid).strip()
        await self._db.mark_consumed(alias.id, message_id)
        summary = summarize(message)
        await self._notifier.notify_email_received(chat_id, alias.email, summary)

    async def _handle_discovered_aliases(
        self,
        chat_id: int,
        addresses: set[str],
    ) -> None:
        """Auto-add aliases discovered during inbox scan."""
        added = await self._db.add_aliases(chat_id, list(addresses))
        if added > 0:
            LOGGER.info(
                "chat %s inbox scan added %d new alias(es) from %d discovered",
                chat_id,
                added,
                len(addresses),
            )
            await self._notifier.notify_aliases_discovered(
                chat_id,
                [a for a in addresses if await self._db.find_alias(chat_id, a) is not None],
            )


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
