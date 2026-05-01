"""Wiring smoke-tests for the Telegram command registry.

The fully-mocked end-to-end behaviour of each command is hard to test
without a Telegram server; here we just confirm that the new commands are
registered and reachable from ``build_handlers()``.
"""
from __future__ import annotations

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
)

from proton_telegram_bot.bot import build_handlers


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
