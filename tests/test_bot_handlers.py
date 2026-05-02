"""Wiring smoke-tests for the Telegram command registry.

The fully-mocked end-to-end behaviour of each command is hard to test
without a Telegram server; here we just confirm that the new commands are
registered and reachable from ``build_handlers()``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
)

from proton_telegram_bot.bot import _pick_primary_for_genaddr, build_handlers
from proton_telegram_bot.db import Database


def _command_names(handlers) -> set[str]:
    names: set[str] = set()
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            names |= set(handler.commands)
        elif isinstance(handler, ConversationHandler):
            for entry in handler.entry_points:
                if isinstance(entry, CommandHandler):
                    names |= set(entry.commands)
    return names


def test_build_handlers_registers_new_commands() -> None:
    handlers = build_handlers()
    commands = _command_names(handlers)
    # Existing commands still wired
    assert "start" in commands
    assert "list" in commands
    assert "connect" in commands
    # New commands wired by this PR
    assert "accounts" in commands
    assert "setprotonpw" in commands
    assert "genaddr" in commands
    # Health check wiring
    assert "cekimap" in commands


def test_build_handlers_has_setprotonpw_conversation() -> None:
    handlers = build_handlers()
    conv_names = {
        h.name
        for h in handlers
        if isinstance(h, ConversationHandler) and h.name
    }
    assert "setprotonpw" in conv_names
    assert "connect" in conv_names
    assert "sync" in conv_names


def test_build_handlers_has_callback_query_router() -> None:
    handlers = build_handlers()
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)


@pytest.mark.asyncio
async def test_send_connect_log_passthrough_no_tracker() -> None:
    """When ``tracker`` is None, :func:`_send_connect_log` must call
    ``reply_text`` with the args verbatim and return the message
    object — i.e. it degrades to a plain reply_text. No tracking.
    """
    from proton_telegram_bot.bot import _send_connect_log

    sent: list[tuple[str, dict]] = []

    class _FakeMessage:
        message_id = 99

        async def reply_text(self, text, **kw):
            sent.append((text, kw))
            return self

    class _FakeUpdate:
        effective_message = _FakeMessage()

    msg = await _send_connect_log(
        _FakeUpdate(),  # type: ignore[arg-type]
        None,
        "hi",
        parse_mode="HTML",
    )
    assert msg is _FakeUpdate.effective_message
    assert sent == [("hi", {"parse_mode": "HTML"})]


@pytest.mark.asyncio
async def test_send_connect_log_tracks_when_tracker_given() -> None:
    """With a tracker, :func:`_send_connect_log` must register the
    returned message id so the cleanup task knows to delete it.
    """
    from proton_telegram_bot.bot import _send_connect_log
    from proton_telegram_bot.task_message_tracker import TaskMessageTracker

    class _FakeBot:
        async def delete_message(self, *, chat_id, message_id):
            pass

    class _FakeMessage:
        message_id = 1234

        async def reply_text(self, text, **kw):
            return self

    class _FakeUpdate:
        effective_message = _FakeMessage()

    tracker = TaskMessageTracker(_FakeBot(), chat_id=42)
    await _send_connect_log(
        _FakeUpdate(),  # type: ignore[arg-type]
        tracker,
        "log line",
    )
    assert len(tracker) == 1


def test_resolve_connect_email_appends_default_domain() -> None:
    """When the user types just a username (no @), :func:`_resolve_connect_email`
    must auto-suffix ``@proton.me`` and flag that the suffix happened.
    The flag drives the user-facing hint that points at the fallback
    domains (``@protonmail.com`` / ``@pm.me``).
    """
    from proton_telegram_bot.bot import _resolve_connect_email

    email, was_suffixed = _resolve_connect_email("vielz883")
    assert email == "vielz883@proton.me"
    assert was_suffixed is True


def test_resolve_connect_email_passes_through_explicit_domain() -> None:
    """Explicit ``@domain`` inputs are normalised (lowercase, trimmed)
    but never re-suffixed; ``was_suffixed`` reports false so the
    domain hint isn't shown.
    """
    from proton_telegram_bot.bot import _resolve_connect_email

    for raw in ("vielz@protonmail.com", " vielz@PM.me ", "VIELZ@proton.me"):
        email, was_suffixed = _resolve_connect_email(raw)
        assert email == raw.strip().lower()
        assert was_suffixed is False


def test_post_connect_keyboard_omits_setpw_when_master_saved() -> None:
    """Request #9: when /connect already auto-saved the Proton master
    password, the post-connect CTA keyboard must drop the redundant
    "🔐 Simpan password Proton" row.
    """
    from proton_telegram_bot.bot import (
        CB_QUICK_GENADDR,
        CB_QUICK_SETPW,
        _build_post_connect_keyboard,
    )

    with_setpw = _build_post_connect_keyboard(42, master_password_saved=False)
    has_setpw = any(
        btn.callback_data and btn.callback_data.startswith(CB_QUICK_SETPW)
        for row in with_setpw.inline_keyboard
        for btn in row
    )
    assert has_setpw, "default keyboard must include the Simpan password row"

    without_setpw = _build_post_connect_keyboard(42, master_password_saved=True)
    has_setpw2 = any(
        btn.callback_data and btn.callback_data.startswith(CB_QUICK_SETPW)
        for row in without_setpw.inline_keyboard
        for btn in row
    )
    assert not has_setpw2, "auto-saved keyboard must skip the Simpan row"
    # Generate-20 button is still present (it's the next CTA the user
    # actually needs).
    has_genaddr = any(
        btn.callback_data and btn.callback_data.startswith(CB_QUICK_GENADDR)
        for row in without_setpw.inline_keyboard
        for btn in row
    )
    assert has_genaddr


def test_connect_again_callback_is_a_connect_conv_entry_point() -> None:
    """Post-disconnect "🔌 Connect lagi" button must enter the connect
    conversation directly. This guards against accidentally dropping
    the CallbackQueryHandler from connect_conv.entry_points and
    silently breaking the shortcut.
    """
    from proton_telegram_bot.bot import CB_CONNECT_AGAIN

    handlers = build_handlers()
    connect_conv = next(
        h
        for h in handlers
        if isinstance(h, ConversationHandler) and h.name == "connect"
    )
    callback_entries = [
        e for e in connect_conv.entry_points if isinstance(e, CallbackQueryHandler)
    ]
    assert callback_entries, "connect_conv must accept a callback entry point"
    assert any(
        CB_CONNECT_AGAIN in e.pattern.pattern for e in callback_entries
    ), f"no entry point matches {CB_CONNECT_AGAIN!r}"


# ----------------------------- _pick_primary_for_genaddr ---------------------


@pytest.fixture
async def populated_db(tmp_path: Path):
    """Database pre-loaded with three primaries on one chat."""
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    chat_id = 100
    await database.upsert_user(chat_id)
    ids = []
    for email in ("vielz22@proton.me", "vielz43@proton.me", "vielz64@proton.me"):
        ids.append(
            await database.add_primary_account(
                chat_id=chat_id,
                email=email,
                host="127.0.0.1",
                port=1143,
                username=email,
                encrypted_password="x",
                use_ssl=False,
            )
        )
    try:
        yield database, chat_id, ids
    finally:
        await database.close()


async def test_pick_primary_matches_base_local_part(populated_db) -> None:
    """``/genaddr vielz64`` picks ``vielz64@proton.me`` even when another is active."""
    db, chat_id, _ids = populated_db
    primaries = await db.list_primary_accounts(chat_id)
    primary, reason = await _pick_primary_for_genaddr(
        db=db, chat_id=chat_id, primaries=primaries, base="vielz64"
    )
    assert primary.email == "vielz64@proton.me"
    assert reason == "cocok dengan base"


async def test_pick_primary_falls_back_to_active_when_no_match(populated_db) -> None:
    """Unmatched base + active primary set -> use the active primary."""
    db, chat_id, _ids = populated_db
    primaries = await db.list_primary_accounts(chat_id)
    # Pick the second one as active, regardless of insert order.
    target = next(p for p in primaries if p.email == "vielz43@proton.me")
    await db.set_active_primary(chat_id, target.id)
    primary, reason = await _pick_primary_for_genaddr(
        db=db, chat_id=chat_id, primaries=primaries, base="something-else"
    )
    assert primary.email == "vielz43@proton.me"
    assert reason == "akun aktif"


async def test_pick_primary_default_to_first_when_nothing_matches(populated_db) -> None:
    """No base match, no active primary, no active alias -> first primary wins."""
    db, chat_id, _ids = populated_db
    primaries = await db.list_primary_accounts(chat_id)
    primary, reason = await _pick_primary_for_genaddr(
        db=db, chat_id=chat_id, primaries=primaries, base="zzz-nothing-matches"
    )
    assert primary == primaries[0]
    assert reason == "default (akun pertama)"


async def test_pick_primary_base_match_wins_over_active(populated_db) -> None:
    """Base match has highest priority -- even if a different primary is active."""
    db, chat_id, _ids = populated_db
    primaries = await db.list_primary_accounts(chat_id)
    other = next(p for p in primaries if p.email == "vielz22@proton.me")
    await db.set_active_primary(chat_id, other.id)
    primary, reason = await _pick_primary_for_genaddr(
        db=db, chat_id=chat_id, primaries=primaries, base="vielz64"
    )
    assert primary.email == "vielz64@proton.me"
    assert reason == "cocok dengan base"
