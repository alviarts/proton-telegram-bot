"""Tests for the live-activity status button.

The reporter is intentionally simple — its job is to throttle and
de-duplicate ``editMessageReplyMarkup`` calls without ever raising —
but the throttle logic and the "message is not modified" handling are
both load-bearing in production, so we lock them in here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from proton_telegram_bot.status_reporter import (
    STATUS_BUTTON_CALLBACK,
    StatusReporter,
    build_status_keyboard,
    on_status_button_noop,
)


@dataclass
class _Edit:
    chat_id: int
    message_id: int
    reply_markup: Any


@dataclass
class _FakeBot:
    edits: list[_Edit] = field(default_factory=list)
    raise_on_edit: Exception | None = None

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any = None,
    ) -> None:
        if self.raise_on_edit is not None:
            raise self.raise_on_edit
        self.edits.append(
            _Edit(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        )


def _button_label(reply_markup: Any) -> str:
    return reply_markup.inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_update_throttle_skips_within_interval() -> None:
    """Updates fired faster than ``update_interval_s`` are skipped, but
    the next update past the interval goes through unchanged.
    """
    bot = _FakeBot()
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=10.0,
    )
    await reporter.update("🔄 phase 1")
    await reporter.update("🔄 phase 2")  # should be throttled
    await reporter.update("🔄 phase 3")  # should be throttled
    assert [_button_label(e.reply_markup) for e in bot.edits] == ["🔄 phase 1"]


@pytest.mark.asyncio
async def test_force_bypasses_throttle_and_dedupe() -> None:
    bot = _FakeBot()
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=10.0,
    )
    await reporter.update("🔄 same label")
    await reporter.update("🔄 same label", force=True)  # bypass dedupe + throttle
    assert [_button_label(e.reply_markup) for e in bot.edits] == [
        "🔄 same label",
        "🔄 same label",
    ]


@pytest.mark.asyncio
async def test_dedupe_skips_identical_labels() -> None:
    bot = _FakeBot()
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,  # no throttle
    )
    await reporter.update("🔄 still working")
    await reporter.update("🔄 still working")
    await reporter.update("🔄 still working")
    assert len(bot.edits) == 1


@pytest.mark.asyncio
async def test_done_emits_idle_label_and_closes() -> None:
    bot = _FakeBot()
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,
    )
    await reporter.update("🔄 working")
    await reporter.done()
    assert _button_label(bot.edits[-1].reply_markup) == "✅ Selesai"
    # After done(), further updates must not reach the bot.
    await reporter.update("🔄 ignored")
    assert _button_label(bot.edits[-1].reply_markup) == "✅ Selesai"


@pytest.mark.asyncio
async def test_done_with_custom_label() -> None:
    bot = _FakeBot()
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,
    )
    await reporter.done("❌ Login gagal")
    assert _button_label(bot.edits[-1].reply_markup) == "❌ Login gagal"


@pytest.mark.asyncio
async def test_message_not_modified_is_swallowed() -> None:
    """Telegram's ``BadRequest("message is not modified")`` is the
    single most common error for status edits — it must not surface
    out of ``update()``."""

    class _NotModifiedError(Exception):
        def __str__(self) -> str:
            return "Bad Request: message is not modified"

    bot = _FakeBot(raise_on_edit=_NotModifiedError())
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,
    )
    # Must not raise.
    await reporter.update("🔄 phase 1")
    # The internal "last label" should still advance so the dedupe
    # path stays consistent with what Telegram thinks is on screen.
    bot.raise_on_edit = None
    await reporter.update("🔄 phase 1")  # dedupe → no edit
    assert bot.edits == []


@pytest.mark.asyncio
async def test_arbitrary_exception_does_not_propagate() -> None:
    """A non-"not modified" error (rate limit, network blip, etc.)
    must also be swallowed: the task body has to keep running.
    """
    bot = _FakeBot(raise_on_edit=RuntimeError("Telegram exploded"))
    reporter = StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,
    )
    await reporter.update("🔄 phase 1")  # raise + swallow
    bot.raise_on_edit = None
    await reporter.update("🔄 phase 2")  # this one succeeds
    assert [_button_label(e.reply_markup) for e in bot.edits] == ["🔄 phase 2"]


def test_button_label_truncated_to_telegram_limit() -> None:
    """Telegram caps button text at 64 chars. The reporter must
    truncate before hitting the API or the edit silently fails on
    some clients.
    """
    long_label = "A" * 100
    keyboard = build_status_keyboard(long_label)
    rendered = keyboard.inline_keyboard[0][0].text
    assert len(rendered) <= 64
    assert rendered.endswith("…")


def test_status_button_callback_uses_dedicated_data() -> None:
    """The no-op handler is registered against this exact pattern, so
    if the callback string drifts the registration breaks silently.
    """
    keyboard = build_status_keyboard("anything")
    assert keyboard.inline_keyboard[0][0].callback_data == STATUS_BUTTON_CALLBACK


@pytest.mark.asyncio
async def test_async_context_manager_finalises_on_exit() -> None:
    bot = _FakeBot()
    async with StatusReporter(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        message_id=42,
        update_interval_s=0.0,
    ) as reporter:
        await reporter.update("🔄 mid")
    assert _button_label(bot.edits[-1].reply_markup) == "✅ Selesai"


@pytest.mark.asyncio
async def test_on_status_button_noop_acks_silently() -> None:
    answered: list[bool] = []

    class _Query:
        async def answer(self) -> None:
            answered.append(True)

    class _Update:
        callback_query = _Query()

    await on_status_button_noop(_Update(), None)  # type: ignore[arg-type]
    assert answered == [True]
