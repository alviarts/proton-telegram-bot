"""Unit tests for the PR-F lock-active reminder middleware.

The reminder is a small bubble posted by ``_maybe_send_lock_reminder``
at the tail of the main commands (``/list``, ``/cekimap``,
``/genaddr``, ``/history``, ``/accounts``) whenever the chat has a
locked alias. It carries two action buttons (``🔓 Unlock`` and
``🔄 Ganti alias``) and de-duplicates itself by deleting the previous
reminder for the chat before posting a new one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from proton_telegram_bot.bot import (
    CB_LOCK_REMINDER_PICK_NEW,
    CB_LOCK_REMINDER_UNLOCK,
    _maybe_send_lock_reminder,
)
from proton_telegram_bot.db import Database


class _FakeBot:
    """Minimal bot stub: records ``delete_message`` calls."""

    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class _FakeMessage:
    """Telegram ``Message`` stub.

    ``reply_text`` returns a child message with a deterministic
    ``message_id`` so callers can assert what was posted and what
    keyboard was attached.
    """

    next_message_id = 1000

    def __init__(self) -> None:
        self.reply_calls: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        self.reply_calls.append((text, kwargs))
        # Each reply gets a unique id so tests can distinguish the
        # first vs. second reminder.
        msg_id = _FakeMessage.next_message_id
        _FakeMessage.next_message_id += 1

        class _Reply:
            message_id = msg_id

        return _Reply()


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, chat_id: int) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.effective_message = _FakeMessage()


class _FakeContext:
    """Stand-in for ``ContextTypes.DEFAULT_TYPE``.

    We only need ``bot``, ``chat_data``, and the ``application``
    plumbing that ``_bot_db`` walks to resolve the database. The
    database itself is injected via ``application.bot_data['db']``.
    """

    def __init__(self, bot: _FakeBot, db: Database) -> None:
        self.bot = bot
        self.chat_data: dict[str, Any] = {}

        class _Application:
            def __init__(self) -> None:
                self.bot_data: dict[str, Any] = {"db": db}

        self.application = _Application()


@pytest.fixture
async def chat_with_locked_alias(tmp_path: Path):
    """Database with a single chat, primary, alias, and that alias locked."""
    db = Database(tmp_path / "bot.sqlite3")
    await db.connect()
    chat_id = 100
    await db.upsert_user(chat_id)
    primary_id = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz@proton.me",
        encrypted_password="x",
        use_ssl=False,
    )
    await db.add_aliases(chat_id, ["alias1@proton.me"], primary_id=primary_id)
    aliases = await db.list_aliases(chat_id, primary_id=primary_id)
    await db.set_active_alias(chat_id, aliases[0].id)
    try:
        yield db, chat_id, aliases[0]
    finally:
        await db.close()


async def test_reminder_skipped_when_no_active_alias(tmp_path: Path) -> None:
    """No active alias → reminder must NOT post."""
    db = Database(tmp_path / "bot.sqlite3")
    await db.connect()
    try:
        chat_id = 99
        await db.upsert_user(chat_id)
        bot = _FakeBot()
        update = _FakeUpdate(chat_id)
        ctx = _FakeContext(bot, db)
        await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]
        assert update.effective_message.reply_calls == []
        assert ctx.chat_data == {}
    finally:
        await db.close()


async def test_reminder_posts_with_two_buttons_when_locked(
    chat_with_locked_alias,
) -> None:
    """A locked alias triggers a reminder with both action buttons."""
    db, chat_id, alias = chat_with_locked_alias
    bot = _FakeBot()
    update = _FakeUpdate(chat_id)
    ctx = _FakeContext(bot, db)
    await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]

    assert len(update.effective_message.reply_calls) == 1
    text, kwargs = update.effective_message.reply_calls[0]
    assert alias.email in text
    # HTML-formatted (so the embedded <code> renders as tap-to-copy).
    assert kwargs.get("parse_mode") == "HTML"
    keyboard = kwargs.get("reply_markup")
    assert keyboard is not None
    flat = [b for row in keyboard.inline_keyboard for b in row]
    callbacks = {b.callback_data for b in flat}
    assert CB_LOCK_REMINDER_UNLOCK in callbacks
    assert CB_LOCK_REMINDER_PICK_NEW in callbacks
    # message_id is cached so the next post can de-dupe.
    assert ctx.chat_data["lock_reminder_msg_id"] is not None


async def test_reminder_deletes_previous_before_posting_new(
    chat_with_locked_alias,
) -> None:
    """Posting a second reminder must delete the first one."""
    db, chat_id, _alias = chat_with_locked_alias
    bot = _FakeBot()
    update = _FakeUpdate(chat_id)
    ctx = _FakeContext(bot, db)

    await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]
    first_id = ctx.chat_data["lock_reminder_msg_id"]
    assert isinstance(first_id, int)

    await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]
    second_id = ctx.chat_data["lock_reminder_msg_id"]
    assert isinstance(second_id, int)
    assert second_id != first_id

    # The first reminder was removed via bot.delete_message.
    assert (chat_id, first_id) in bot.deleted


async def test_reminder_swallows_send_errors(
    chat_with_locked_alias, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram failures during reply_text must NOT propagate to the
    host command — the reminder is best-effort."""
    db, chat_id, _alias = chat_with_locked_alias
    bot = _FakeBot()
    update = _FakeUpdate(chat_id)
    ctx = _FakeContext(bot, db)

    async def _boom(text: str, **kwargs: Any) -> Any:
        raise RuntimeError("telegram is down")

    update.effective_message.reply_text = _boom  # type: ignore[method-assign]
    # Must not raise.
    await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]
    # Nothing got cached because the reply failed.
    assert "lock_reminder_msg_id" not in ctx.chat_data


async def test_reminder_swallows_delete_errors(
    chat_with_locked_alias,
) -> None:
    """If deleting the previous reminder fails (Telegram returns the
    standard "message can't be deleted" 400), we must still post the
    new reminder rather than crashing."""
    db, chat_id, _alias = chat_with_locked_alias
    bot = _FakeBot()

    async def _boom(*, chat_id: int, message_id: int) -> None:
        raise RuntimeError("can't delete")

    bot.delete_message = _boom  # type: ignore[method-assign]
    update = _FakeUpdate(chat_id)
    ctx = _FakeContext(bot, db)
    # Pre-seed a stale id so the helper attempts a delete.
    ctx.chat_data["lock_reminder_msg_id"] = 9999
    await _maybe_send_lock_reminder(update, ctx)  # type: ignore[arg-type]
    # New reminder was still posted.
    assert len(update.effective_message.reply_calls) == 1
    assert "lock_reminder_msg_id" in ctx.chat_data
