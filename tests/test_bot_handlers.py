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
    # Service-label feature
    assert "services" in commands
    assert "aliasinfo" in commands
    assert "cleanmail" in commands
    assert "resetbot" in commands


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
    assert "tag_service" in conv_names


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


@pytest.mark.asyncio
async def test_cmd_resetbot_acks_then_schedules_exit(monkeypatch) -> None:
    """``/resetbot`` MUST (a) reply with a confirmation, then (b) schedule
    a hard ``os._exit`` via the running loop. We patch ``os._exit`` so the
    test process survives the call, and assert the loop callback target
    matches the patched function.
    """
    from proton_telegram_bot import bot as bot_module

    exit_calls: list[int] = []

    def _fake_exit(code: int) -> None:
        exit_calls.append(code)

    monkeypatch.setattr(bot_module.os, "_exit", _fake_exit)

    sent: list[str] = []

    class _FakeMessage:
        message_id = 4242

        async def reply_text(self, text, **kw):
            sent.append(text)
            return self

    class _FakeUser:
        id = 123

    class _FakeChat:
        id = 456

    class _FakeUpdate:
        effective_message = _FakeMessage()
        effective_user = _FakeUser()
        effective_chat = _FakeChat()

    fake_settings = type("S", (), {"allowed_user_ids": []})()
    fake_app = type("A", (), {"bot_data": {"settings": fake_settings}})()
    fake_ctx = type("C", (), {"application": fake_app})()

    import asyncio

    await bot_module.cmd_resetbot(_FakeUpdate(), fake_ctx)  # type: ignore[arg-type]

    # Acknowledged before exit.
    assert sent and "restart" in sent[0].lower()
    # Loop scheduled the hard exit; let it fire.
    await asyncio.sleep(1.2)
    assert exit_calls == [0], "cmd_resetbot must invoke os._exit(0)"


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


# --------------------------------------- service-label rendering


def test_format_alias_with_labels_collapses_to_email_when_empty() -> None:
    from proton_telegram_bot.bot import _format_alias_with_labels

    out = _format_alias_with_labels("vielz008@proton.me", [])
    assert "vielz008@proton.me" in out
    assert "📧" not in out  # No labels → no service block.


def test_format_alias_with_labels_renders_active_lock() -> None:
    from proton_telegram_bot.bot import _format_alias_with_labels

    out = _format_alias_with_labels(
        "vielz008@proton.me", ["Devin"], is_active=True
    )
    assert "🔒" in out
    assert "Devin" in out


def test_format_alias_with_labels_caps_with_overflow_tail() -> None:
    from proton_telegram_bot.bot import _format_alias_with_labels

    out = _format_alias_with_labels(
        "x@p.me",
        ["Devin", "GitHub", "Stripe", "Google", "OpenAI"],
        max_labels=3,
    )
    assert "Devin" in out and "GitHub" in out and "Stripe" in out
    assert "+2" in out
    assert "Google" not in out
    assert "OpenAI" not in out


def test_render_email_message_includes_service_label() -> None:
    from proton_telegram_bot.bot import _render_email_message

    summary = {
        "from": "no-reply@cognition.ai",
        "subject": "Devin code-review feedback",
        "date": "Sat, 03 May 2026 03:00:00 +0000",
        "body": "Halo!",
        "to": "vielz008@proton.me",
    }
    out = _render_email_message(
        "vielz008@proton.me", summary, service_label="Devin"
    )
    assert "🏷️" in out
    assert "Devin" in out
    out_no_label = _render_email_message(
        "vielz008@proton.me", summary, service_label=None
    )
    assert "🏷️" not in out_no_label


def test_build_tag_service_keyboard_skips_when_overflow() -> None:
    from proton_telegram_bot.bot import _build_tag_service_keyboard

    very_long_domain = "a" * 70 + ".com"
    out = _build_tag_service_keyboard(
        alias_id=1, sender_domain=very_long_domain, current_label=None
    )
    assert out is None


def test_build_tag_service_keyboard_includes_clear_only_when_labelled() -> None:
    from proton_telegram_bot.bot import _build_tag_service_keyboard

    no_clear = _build_tag_service_keyboard(
        alias_id=1, sender_domain="cognition.ai", current_label=None
    )
    assert no_clear is not None
    assert len(no_clear.inline_keyboard) == 1

    with_clear = _build_tag_service_keyboard(
        alias_id=1, sender_domain="cognition.ai", current_label="Devin"
    )
    assert with_clear is not None
    assert len(with_clear.inline_keyboard) == 2


def test_build_tag_service_keyboard_pending_mark_read_mode() -> None:
    """First-time forwards must lead with "✓ Tandai sudah dibaca" and
    suppress "🗑 Hapus label" — labeling is gated on the user's explicit
    confirmation.
    """
    from proton_telegram_bot.bot import (
        CB_MARK_READ,
        CB_TAG_SERVICE,
        _build_tag_service_keyboard,
    )

    pending = _build_tag_service_keyboard(
        alias_id=42,
        sender_domain="cognition.ai",
        current_label="Devin",
        pending_mark_read=True,
    )
    assert pending is not None
    rows = pending.inline_keyboard
    # Top row is the mark-read CTA.
    assert "✓ Tandai sudah dibaca" in rows[0][0].text
    assert rows[0][0].callback_data == f"{CB_MARK_READ}:42:cognition.ai"
    # Followed by tag-ulang. No "Hapus label" until the sender row exists.
    assert "Tag ulang" in rows[1][0].text
    assert rows[1][0].callback_data == f"{CB_TAG_SERVICE}:42:cognition.ai"
    assert all("Hapus label" not in btn.text for row in rows for btn in row)

    # Standard mode (already recorded): top row is tag-ulang, then
    # "Hapus label" when a label exists.
    standard = _build_tag_service_keyboard(
        alias_id=42,
        sender_domain="cognition.ai",
        current_label="Devin",
        pending_mark_read=False,
    )
    assert standard is not None
    standard_rows = standard.inline_keyboard
    assert "Tag ulang" in standard_rows[0][0].text
    assert "Hapus label" in standard_rows[1][0].text


def test_services_keyboard_renders_suppression_distinctly() -> None:
    """A row with ``label = ''`` is the 🗑 Hapus label sentinel — the
    keyboard distinguishes it from named mappings using the 🚫 icon and
    a "(disembunyikan)" placeholder so the user can audit suppressions
    in /services."""
    from proton_telegram_bot.bot import _build_services_keyboard

    keyboard = _build_services_keyboard(
        [("cognition.ai", ""), ("github.com", "GitHub")]
    )
    assert keyboard is not None
    rows = keyboard.inline_keyboard
    assert len(rows) == 2
    suppressed_btn = rows[0][0]
    named_btn = rows[1][0]
    assert "🚫" in suppressed_btn.text
    assert "(disembunyikan)" in suppressed_btn.text
    assert "🗑" in named_btn.text
    assert "GitHub" in named_btn.text


@pytest.mark.asyncio
async def test_purge_alias_email_messages_calls_bot_delete(tmp_path) -> None:
    """`_purge_alias_email_messages` deletes every recorded message id for
    the alias and pops them from the DB. Failures (>48h, message gone)
    are swallowed silently so a single bad id doesn't abort the bulk
    delete — the rows are removed regardless to avoid retry forever."""
    from proton_telegram_bot.bot import _purge_alias_email_messages
    from proton_telegram_bot.db import Database

    db = Database(tmp_path / "purge.sqlite")
    await db.connect()
    try:
        await db.upsert_user(1)
        await db.add_aliases(1, ["a@proton.me"], primary_id=None)
        alias = await db.find_alias(1, "a@proton.me")
        assert alias is not None
        await db.record_forwarded_email(1, alias.id, 11)
        await db.record_forwarded_email(1, alias.id, 22)
        await db.record_forwarded_email(1, alias.id, 33)

        deleted_ids: list[int] = []

        class _Bot:
            async def delete_message(self, *, chat_id, message_id):
                if message_id == 22:
                    raise RuntimeError("simulate vanished message")
                deleted_ids.append(message_id)

        deleted = await _purge_alias_email_messages(
            _Bot(), db, 1, alias.id
        )
        assert deleted == 2  # 11 and 33 succeeded; 22 raised and was skipped
        assert sorted(deleted_ids) == [11, 33]
        # All three rows are still popped from the DB regardless of the
        # delete_message outcome.
        assert await db.pop_forwarded_email_message_ids(1, alias.id) == []
    finally:
        await db.close()
