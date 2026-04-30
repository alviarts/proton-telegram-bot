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


async def test_handle_message_marks_alias_consumed_and_notifies(db: Database) -> None:
    chat_id = 100
    await db.upsert_user(chat_id)
    await db.add_aliases(chat_id, ["vielz50@proton.me", "vielz51@proton.me"])
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


async def test_handle_message_auto_adds_new_recipient(db: Database) -> None:
    """An email to an unknown address auto-adds it, consumes, and notifies."""
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
    # Auto-add creates the alias and immediately consumes it.
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1] == "someone-else@proton.me"
    all_aliases = await db.list_aliases(chat_id)
    assert sorted(a.email for a in all_aliases) == ["only@proton.me", "someone-else@proton.me"]
    consumed = await db.list_aliases(chat_id, status=AliasStatus.CONSUMED)
    assert [a.email for a in consumed] == ["someone-else@proton.me"]


async def test_discovered_aliases_adds_and_notifies(db: Database) -> None:
    """Inbox scan discovers new addresses and notifies the user."""
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
