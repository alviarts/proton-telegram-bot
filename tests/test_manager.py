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

    async def notify_email_received(self, chat_id, alias_email, summary) -> None:
        self.calls.append((chat_id, alias_email, summary))


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


async def test_handle_message_ignores_unmatched_recipient(db: Database) -> None:
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
    assert notifier.calls == []
    available = await db.list_aliases(chat_id, status=AliasStatus.AVAILABLE)
    assert [a.email for a in available] == ["only@proton.me"]


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
