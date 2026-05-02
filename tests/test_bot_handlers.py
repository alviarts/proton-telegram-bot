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
