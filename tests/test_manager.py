"""Tests for ListenerManager dispatch logic (without actual IMAP)."""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from proton_telegram_bot.crypto import CredentialCipher
from proton_telegram_bot.db import Database
from proton_telegram_bot.email_parser import parse_message
from proton_telegram_bot.manager import ListenerManager, Notifier
from proton_telegram_bot.models import AliasStatus


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, str]]] = []
        self.discovery_calls: list[tuple[int, list[str]]] = []

    async def notify_email_received(self, chat_id, alias_email, summary) -> None:
        self.calls.append((chat_id, alias_email, summary))

    async def notify_aliases_discovered(self, chat_id, aliases) -> None:
        self.discovery_calls.append((chat_id, aliases))


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "x.sqlite3")
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


def _make_message(to: str = "vielz50@proton.me") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "partner@biz.example"
    msg["To"] = to
    msg["Subject"] = "Halo"
    msg.set_content("Pesan pertama.")
    return parse_message(msg.as_bytes())  # type: ignore[return-value]


async def test_handle_message_forwards_only_active_alias(db: Database) -> None:
    """Lock-mode: only emails to the chat's *active* alias get forwarded."""
    chat_id = 100
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["vielz50@proton.me", "vielz51@proton.me"])
    active = await db.find_alias(chat_id, "vielz50@proton.me")
    assert active is not None
    await db.set_active_alias(chat_id, active.id)
    notifier = _RecordingNotifier()
    cipher = CredentialCipher(CredentialCipher.generate_key())
    manager = ListenerManager(db=db, cipher=cipher, notifier=notifier)

    await manager._handle_new_message(chat_id, _make_message("vielz50@proton.me"), "1")

    available = await db.list_aliases(chat_id, status=AliasStatus.AVAILABLE)
    consumed = await db.list_aliases(chat_id, status=AliasStatus.CONSUMED)
    assert [a.email for a in available] == ["vielz51@proton.me"]
    assert [a.email for a in consumed] == ["vielz50@proton.me"]
    assert len(notifier.calls) == 1
    received_chat, received_email, _ = notifier.calls[0]
    assert (received_chat, received_email) == (chat_id, "vielz50@proton.me")
    # Active alias is auto-released after the email is forwarded.
    assert await db.get_active_alias(chat_id) is None


async def test_handle_message_ignores_when_no_active_alias(db: Database) -> None:
    """No active alias = nothing is forwarded; new recipients are still tracked."""
    chat_id = 7
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["only@proton.me"])
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(chat_id, _make_message("someone-else@proton.me"), "1")
    # No notification because no alias is locked-in for this chat.
    assert notifier.calls == []
    # New recipient was still auto-added so /list shows it later.
    all_aliases = await db.list_aliases(chat_id)
    assert sorted(a.email for a in all_aliases) == ["only@proton.me", "someone-else@proton.me"]
    # Nothing got consumed.
    assert await db.list_aliases(chat_id, status=AliasStatus.CONSUMED) == []


async def test_handle_message_ignores_other_aliases_when_locked(db: Database) -> None:
    """Email to a non-active alias must NOT be forwarded, even if the alias exists."""
    chat_id = 200
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["a@proton.me", "b@proton.me"])
    active = await db.find_alias(chat_id, "a@proton.me")
    assert active is not None
    await db.set_active_alias(chat_id, active.id)
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )

    await manager._handle_new_message(chat_id, _make_message("b@proton.me"), "10")

    assert notifier.calls == []
    # Active alias still locked, neither alias consumed.
    assert (await db.get_active_alias(chat_id)).email == "a@proton.me"
    assert await db.list_aliases(chat_id, status=AliasStatus.CONSUMED) == []


async def test_discovered_aliases_adds_and_notifies(db: Database) -> None:
    """Inbox scan discovers new addresses and notifies only the new ones."""
    chat_id = 42
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["existing@proton.me"])
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_discovered_aliases(
        chat_id, {"existing@proton.me", "new1@proton.me", "new2@proton.me"}
    )
    all_aliases = await db.list_aliases(chat_id)
    assert sorted(a.email for a in all_aliases) == [
        "existing@proton.me",
        "new1@proton.me",
        "new2@proton.me",
    ]
    assert len(notifier.discovery_calls) == 1
    assert notifier.discovery_calls[0][0] == chat_id
    # Only the NEW aliases should be in the notification, not the pre-existing one.
    assert sorted(notifier.discovery_calls[0][1]) == ["new1@proton.me", "new2@proton.me"]


async def test_handle_message_does_not_rematch_consumed_alias(db: Database) -> None:
    chat_id = 9
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["x@proton.me"])
    alias = await db.find_alias(chat_id, "x@proton.me")
    assert alias is not None
    await db.mark_consumed(alias.id, "<m1>")
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(chat_id, _make_message("x@proton.me"), "2")
    # Already consumed: no second notification.
    assert notifier.calls == []
