"""Tests for resilience plumbing: global ``on_error`` handler and
the ``_build_application`` wiring that activates concurrent updates +
registers the error handler.

These tests guard the user-stated invariant: "jangan error stuck lagi
kedepannya" / "lebih baik mengulang flow daripada bot tidak merespon
atau mati". Concretely:

* Any uncaught exception in a handler must not silently kill the
  polling loop — the user must see *some* reply, and the next command
  must start with a clean slate (stale ``user_data`` cleared so a
  half-set ``imap_password`` from a broken /connect doesn't poison
  the next attempt).
* The ``Application`` produced by ``_build_application`` must have
  ``concurrent_updates=True`` (so one slow user can't block another)
  and exactly one error handler registered.
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from proton_telegram_bot import bot


class _FakeChat:
    def __init__(self, chat_id: int = 12345) -> None:
        self.id = chat_id


class _FakeUpdate:
    """Stand-in for ``telegram.Update`` that exposes ``effective_chat``.

    The real ``Update`` is a ``TelegramObject`` with a long
    construction signature; ``on_error`` only ever reads
    ``effective_chat`` so a duck-typed object is fine here, but
    ``isinstance(update, Update)`` in the handler means we have to
    use the real class for the chat-reply branch. Tests below use
    this fake only when exercising the non-Update branch (e.g. JobQueue
    errors get a plain string, not an Update object).
    """

    def __init__(self, chat_id: int | None = 12345) -> None:
        self.effective_chat = _FakeChat(chat_id) if chat_id is not None else None


def _make_context(error: BaseException, *, user_data: dict | None = None) -> Any:
    """Build a minimal ``ContextTypes.DEFAULT_TYPE`` substitute."""
    ctx = MagicMock()
    ctx.error = error
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


async def test_on_error_logs_exception_and_clears_user_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``on_error`` must always log the underlying exception and clear
    stale per-user state so the next command starts fresh."""
    user_data = {"imap_password": "leftover", "bridge_email": "x@proton.me"}
    ctx = _make_context(RuntimeError("boom"), user_data=user_data)

    caplog.set_level("ERROR", logger="proton_telegram_bot.bot")
    await bot.on_error("not an Update", ctx)

    assert user_data == {}
    assert any(
        "uncaught exception in handler" in rec.message
        for rec in caplog.records
    ), "on_error must log the failure at ERROR"


async def test_on_error_replies_when_update_has_chat() -> None:
    """When the broken update came from a real chat, the user must
    receive a friendly reply identifying that the bot is still alive."""
    from telegram import Chat, Update

    chat = Chat(id=42, type=Chat.PRIVATE)
    update = Update(update_id=1)
    # Update doesn't expose a public setter for effective_chat; the
    # property is computed from the message/callback_query/etc. Patch
    # the property on this single instance for the test.
    update._effective_chat = chat  # type: ignore[attr-defined]

    ctx = _make_context(ValueError("oops"))
    await bot.on_error(update, ctx)

    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 42
    # User-facing text must mention the bot is still alive so the user
    # doesn't think it's dead.
    assert "Bot tetap jalan" in kwargs["text"]


async def test_on_error_swallows_secondary_failures() -> None:
    """If sending the reply itself raises (e.g. Telegram API down),
    ``on_error`` must not propagate — the original error is the
    interesting one and we never want the error handler to crash
    the dispatcher."""
    from telegram import Chat, Update

    chat = Chat(id=99, type=Chat.PRIVATE)
    update = Update(update_id=2)
    update._effective_chat = chat  # type: ignore[attr-defined]

    ctx = _make_context(RuntimeError("primary"))
    ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

    # Must not raise.
    await bot.on_error(update, ctx)


async def test_on_error_handles_non_update_payload() -> None:
    """``Application.add_error_handler`` may pass a non-Update object
    (e.g. for ``JobQueue`` failures the update is a string). The handler
    must handle that gracefully without trying to reply."""
    ctx = _make_context(RuntimeError("from job queue"))
    # Should be a complete no-op apart from logging — no reply attempted
    # because there's no chat to reply to.
    await bot.on_error("job-queue-error", ctx)
    ctx.bot.send_message.assert_not_called()


def test_build_application_enables_concurrent_updates_and_error_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_application`` must wire concurrent_updates=True and
    register exactly one error handler. These two settings are the
    structural backbone of the "don't get stuck" guarantee — without
    them, even the best per-handler hygiene can't prevent one slow
    user from freezing the bot for everyone else."""
    from cryptography.fernet import Fernet

    from proton_telegram_bot import __main__ as main_mod

    settings = MagicMock()
    settings.telegram_bot_token = "12345:fake"
    settings.database_path = ":memory:"
    # CredentialCipher requires a real Fernet key; generate a throw-away one
    # so the construction goes through.
    settings.encryption_key = Fernet.generate_key().decode()
    settings.alias_sync_interval_minutes = 60
    settings.bridge_admin_enabled = False

    # ProxyProvider.from_env reads env vars; force the disabled branch
    # so the test doesn't need real proxy infra.
    monkeypatch.setattr(
        main_mod.ProxyProvider, "from_env", classmethod(lambda cls: None)
    )

    application = main_mod._build_application(settings)

    # PTB normalises ``concurrent_updates(True)`` to either ``True`` or a
    # positive integer (default 256), depending on the version. Both
    # mean concurrency is on; the only "off" value is ``False`` / ``0``.
    cu = application.concurrent_updates
    if isinstance(cu, bool):
        assert cu is True
    elif isinstance(cu, int):
        assert cu > 0
    else:
        # Newer PTB returns a ``BaseUpdateProcessor`` instance.
        assert getattr(cu, "max_concurrent_updates", 0) > 0

    # Exactly one error handler registered, and it's our ``on_error``.
    assert len(application.error_handlers) == 1
    handler_func = next(iter(application.error_handlers))
    assert handler_func is bot.on_error


# ---------------------------------------------------------------------
# Tests for /connect progress indicator + cleanup wiring
# ---------------------------------------------------------------------
#
# The user explicitly asked for two UX guarantees:
#
#   1. "berikan progress bar dibawah sedang melakukan apa bot nya jadi
#      user tidak mengira bot sudah selesai" — a single live status
#      anchor whose label rotates through the current /connect phase
#      so the user never confuses a 60-180s flow with a hung bot.
#
#   2. "hapus yang saya pilih ketika sudah berhasil" — every transient
#      prompt the bot emits during /connect (intro, Step 1/2, password
#      prompt, …) plus the status anchor itself must auto-delete a few
#      seconds after the conversation completes successfully.
#
# These tests pin down the structural contract that satisfies both
# requirements: ``_ensure_connect_progress`` creates exactly one
# tracker + one StatusReporter pair (idempotent on re-entry),
# ``_set_connect_status`` uses the reporter when it exists and is a
# silent no-op otherwise, and ``_close_connect_status`` removes the
# reporter from ``user_data`` so subsequent /connect runs start fresh.


class _FakeMessage:
    """Stand-in for ``telegram.Message`` used by the tracker tests."""

    _next_id = 1000

    def __init__(self) -> None:
        type(self)._next_id += 1
        self.message_id = type(self)._next_id

    async def reply_text(self, *args: Any, **kwargs: Any) -> _FakeMessage:
        return _FakeMessage()


class _FakeUpdateWithMessage:
    def __init__(self, chat_id: int = 12345) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.effective_message = _FakeMessage()


def _make_progress_context(user_data: dict | None = None) -> Any:
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.edit_message_reply_markup = AsyncMock()
    ctx.bot.delete_message = AsyncMock()
    return ctx


async def test_ensure_connect_progress_creates_tracker_and_status() -> None:
    """First call must build a fresh tracker + StatusReporter pair and
    stash them on ``user_data`` under the canonical keys so subsequent
    handlers can retrieve them."""
    update = _FakeUpdateWithMessage()
    ctx = _make_progress_context()

    tracker, status = await bot._ensure_connect_progress(update, ctx)

    assert tracker is not None
    assert status is not None
    assert ctx.user_data["connect_log_tracker"] is tracker
    assert ctx.user_data["connect_status_reporter"] is status
    # The status anchor message must be tracked for cleanup so the
    # final tracker.cleanup() deletes it alongside the rest of the log.
    assert len(tracker) == 1


async def test_ensure_connect_progress_is_idempotent() -> None:
    """Second call (e.g. from ``connect_email`` after ``cmd_connect``)
    must return the same tracker + reporter without spawning a second
    anchor message — otherwise the user would see two status buttons."""
    update = _FakeUpdateWithMessage()
    ctx = _make_progress_context()

    tracker1, status1 = await bot._ensure_connect_progress(update, ctx)
    tracker2, status2 = await bot._ensure_connect_progress(update, ctx)

    assert tracker1 is tracker2
    assert status1 is status2
    assert len(tracker1) == 1, "anchor message must not be re-tracked"


async def test_set_connect_status_no_op_when_no_reporter() -> None:
    """The label updater must silently degrade when no reporter has
    been set up (legacy /connect path or direct call from a non-flow
    handler) — never raise."""
    ctx = _make_progress_context()
    # Should be a complete no-op; if it raised we'd see it here.
    await bot._set_connect_status(ctx, "🔌 Probe Bridge IMAP login…")


async def test_set_connect_status_invokes_reporter_update() -> None:
    """When a reporter exists, ``_set_connect_status`` must forward
    the label to ``StatusReporter.update`` so the live button label
    is actually edited."""
    update = _FakeUpdateWithMessage()
    ctx = _make_progress_context()
    _, status = await bot._ensure_connect_progress(update, ctx)
    assert status is not None
    status.update = AsyncMock()  # type: ignore[method-assign]

    await bot._set_connect_status(ctx, "🧪 Smoke test 1/3…")

    status.update.assert_awaited_once_with("🧪 Smoke test 1/3…", force=False)


async def test_close_connect_status_pops_and_calls_done() -> None:
    """``_close_connect_status`` must transition the reporter to its
    terminal label and remove it from ``user_data`` so a follow-up
    /connect creates a fresh one instead of editing the deleted
    anchor."""
    update = _FakeUpdateWithMessage()
    ctx = _make_progress_context()
    _, status = await bot._ensure_connect_progress(update, ctx)
    assert status is not None
    status.done = AsyncMock()  # type: ignore[method-assign]

    await bot._close_connect_status(ctx, "✅ Selesai")

    status.done.assert_awaited_once_with("✅ Selesai")
    assert "connect_status_reporter" not in ctx.user_data


async def test_close_connect_status_no_op_without_reporter() -> None:
    """No reporter stashed = silent no-op (legacy path / already closed)."""
    ctx = _make_progress_context()
    # Must not raise even though ``connect_status_reporter`` is missing.
    await bot._close_connect_status(ctx, "anything")


# ---------------------------------------------------------------------
# Tests for ``_send_with_retry`` and the network-resilience plumbing.
# ---------------------------------------------------------------------
#
# Background: the user runs the bot on an Indonesian VPS where ping to
# api.telegram.org showed ~33% packet loss. The default 5 s connect
# timeout in python-telegram-bot meant every other ``send_message``
# call raised ``httpx.ConnectTimeout``, which:
#
#   1. Showed up as ``ERROR uncaught exception in handler`` on every
#      command (``/inbox``, ``/list``, …).
#   2. Caused the IMAP listener to silently drop forwarded emails —
#      ``imap_listener._fetch_new_messages`` advances the UID baseline
#      in ``finally`` regardless of whether the dispatch succeeded, so
#      a transient Telegram blip permanently lost the email.
#
# These tests pin down the contract: ``_send_with_retry`` retries on
# ``TimedOut``/``NetworkError``/httpx timeouts, propagates non-retry
# errors immediately, honours ``RetryAfter.retry_after``, and
# eventually gives up after the configured budget.


async def test_send_with_retry_returns_first_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: factory succeeds first try → no sleep, return value
    is forwarded to the caller unchanged."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("proton_telegram_bot.bot.asyncio.sleep", fake_sleep)

    factory = AsyncMock(return_value="sent")

    result = await bot._send_with_retry(factory, description="happy path")

    assert result == "sent"
    factory.assert_awaited_once()
    assert sleeps == [], "no sleep on first-try success"


async def test_send_with_retry_recovers_after_transient_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``TimedOut`` failures followed by a success → returns the
    success value, sleeping per the backoff schedule between attempts.

    This is the production hot path: the VPS connection blips, retry
    1 s later, blip again, retry 3 s later, third attempt succeeds.
    The dispatched email reaches Telegram ~4 s late instead of being
    permanently lost."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("proton_telegram_bot.bot.asyncio.sleep", fake_sleep)

    call_count = {"n": 0}

    async def flaky() -> str:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise bot.TimedOut("connection blip")
        return "delivered"

    result = await bot._send_with_retry(flaky, description="flaky send")

    assert result == "delivered"
    assert call_count["n"] == 3
    # First attempt: no sleep. Second/third: backoff slots [0] and [1].
    assert sleeps == [
        bot._TELEGRAM_RETRY_BACKOFF_SECONDS[0],
        bot._TELEGRAM_RETRY_BACKOFF_SECONDS[1],
    ]


async def test_send_with_retry_propagates_non_retryable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BadRequest`` (e.g. malformed HTML, kicked from chat) is NOT
    network-recoverable — retrying just hides the bug. Must propagate
    immediately so the caller sees the real error."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("proton_telegram_bot.bot.asyncio.sleep", fake_sleep)

    async def boom() -> None:
        raise bot.BadRequest("can't parse entities")

    with pytest.raises(bot.BadRequest):
        await bot._send_with_retry(boom, description="bad request")

    assert sleeps == [], "non-retryable errors must not trigger backoff"


async def test_send_with_retry_gives_up_after_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent network failure → exhaust the schedule then re-raise
    the last exception so the listener can log & advance UID. Better
    to lose one email than wedge the listener forever."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("proton_telegram_bot.bot.asyncio.sleep", fake_sleep)

    factory = AsyncMock(side_effect=bot.NetworkError("persistent outage"))

    with pytest.raises(bot.NetworkError):
        await bot._send_with_retry(factory, description="dead network")

    # 1 initial attempt + N retries from the schedule.
    expected_calls = 1 + len(bot._TELEGRAM_RETRY_BACKOFF_SECONDS)
    assert factory.await_count == expected_calls
    # The schedule should have been walked exactly once.
    assert sleeps == list(bot._TELEGRAM_RETRY_BACKOFF_SECONDS)


async def test_send_with_retry_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram flood-control: ``RetryAfter.retry_after`` overrides the
    static schedule because Telegram knows when its own rate-limit
    window closes better than we do."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("proton_telegram_bot.bot.asyncio.sleep", fake_sleep)

    call_count = {"n": 0}

    async def rate_limited() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise bot.RetryAfter(retry_after=12)
        return "ok"

    result = await bot._send_with_retry(rate_limited, description="rate-limited")

    assert result == "ok"
    # The first sleep slot is the RetryAfter hint, not the static
    # backoff value, so 12.0 must appear at least once.
    assert 12.0 in sleeps


async def test_on_error_logs_network_blip_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``TimedOut`` reaching ``on_error`` must log at WARNING (single
    line, no traceback) so the journal stays scannable. Before this
    fix, every blip dumped a 60-line traceback at ERROR."""
    ctx = _make_context(bot.TimedOut("connect timed out"))

    caplog.set_level("WARNING", logger="proton_telegram_bot.bot")
    await bot.on_error("not an Update", ctx)

    network_records = [
        rec for rec in caplog.records
        if "transient network error in handler" in rec.message
    ]
    assert len(network_records) == 1
    assert network_records[0].levelname == "WARNING"
    # Exception text should be inlined, but no traceback frames present.
    assert network_records[0].exc_info is None


async def test_on_error_still_logs_real_bugs_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-network exceptions (ValueError, KeyError, …) must keep the
    full ERROR + traceback so real bugs stay debuggable."""
    ctx = _make_context(ValueError("kaboom"))

    caplog.set_level("ERROR", logger="proton_telegram_bot.bot")
    await bot.on_error("not an Update", ctx)

    bug_records = [
        rec for rec in caplog.records
        if "uncaught exception in handler" in rec.message
    ]
    assert len(bug_records) == 1
    assert bug_records[0].levelname == "ERROR"


def test_build_application_pins_generous_telegram_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PTB ``HTTPXRequest`` defaults (5 s connect / 5 s read) are
    too tight for the high-latency VPS link this bot runs on. The
    builder must pass our larger timeouts through to PTB so they're
    actually applied to the underlying ``httpx.AsyncClient``."""
    from cryptography.fernet import Fernet

    from proton_telegram_bot import __main__ as main_mod

    settings = MagicMock()
    settings.telegram_bot_token = "12345:fake"
    settings.database_path = ":memory:"
    settings.encryption_key = Fernet.generate_key().decode()
    settings.alias_sync_interval_minutes = 60
    settings.bridge_admin_enabled = False

    monkeypatch.setattr(
        main_mod.ProxyProvider, "from_env", classmethod(lambda cls: None)
    )

    application = main_mod._build_application(settings)

    # PTB stores two HTTPXRequest objects on ``Bot._request``: index 0
    # is the long-poll ``getUpdates`` client (its read timeout has to
    # be larger than the polling ``timeout`` parameter PTB sends with
    # each call, currently 10 s) and index 1 is the regular API
    # client used by every ``send_message`` etc.
    get_updates_request, default_request = application.bot._request

    assert default_request._client.timeout.connect >= 10.0
    assert default_request._client.timeout.read >= 10.0
    assert default_request._client.timeout.write >= 10.0

    # ``getUpdates`` long-polls for ~10 s, so its read budget must be
    # noticeably larger than the bot client's normal read timeout to
    # absorb both the poll itself plus jitter on a flaky link.
    assert get_updates_request._client.timeout.read >= 30.0
    assert get_updates_request._client.timeout.connect >= 10.0


def test_configure_logging_mutes_third_party_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the operator sets ``LOG_LEVEL=DEBUG`` to debug the
    bot, ``aioimaplib`` must not be allowed to dump raw IMAP frames
    (which include full email bodies, OTPs, recovery links, …) to the
    journal. ``_configure_logging`` clamps it to WARNING."""
    from proton_telegram_bot import __main__ as main_mod

    monkeypatch.delenv("PROTON_BOT_VERBOSE_THIRDPARTY", raising=False)

    settings = MagicMock()
    settings.log_level = "DEBUG"

    main_mod._configure_logging(settings)

    for noisy_logger in ("aioimaplib", "aioimaplib.aioimaplib", "httpx", "httpcore"):
        level = logging.getLogger(noisy_logger).level
        assert level >= logging.WARNING, (
            f"{noisy_logger} must be at WARNING or stricter even when LOG_LEVEL=DEBUG; "
            f"got level={level}"
        )


def test_configure_logging_respects_verbose_third_party_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators who genuinely need third-party DEBUG output for a
    deep dive can opt back in via ``PROTON_BOT_VERBOSE_THIRDPARTY=1``.
    The escape hatch must leave the third-party loggers untouched so
    ``LOG_LEVEL=DEBUG`` reaches them through the normal root config."""
    from proton_telegram_bot import __main__ as main_mod

    # Start from a known state: pin the noisy loggers to NOTSET so we
    # can detect whether ``_configure_logging`` touched them.
    for name in ("aioimaplib", "aioimaplib.aioimaplib", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    monkeypatch.setenv("PROTON_BOT_VERBOSE_THIRDPARTY", "1")

    settings = MagicMock()
    settings.log_level = "DEBUG"

    main_mod._configure_logging(settings)

    # All four must remain NOTSET because the escape hatch is set.
    for name in ("aioimaplib", "aioimaplib.aioimaplib", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.NOTSET
