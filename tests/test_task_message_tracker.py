"""Tests for :mod:`proton_telegram_bot.task_message_tracker`.

We only verify the **public** surface — track / cleanup / after-callback
behaviour and graceful degradation when ``delete_message`` raises. The
integration that wires ``TaskMessageTracker`` into ``/genaddr`` and
``/cekimap`` is tested separately in
``test_genaddr_background.py`` and ``test_health_check.py``.
"""
from __future__ import annotations

import pytest

from proton_telegram_bot.task_message_tracker import (
    DEFAULT_CLEANUP_DELAY_S,
    TaskMessageTracker,
)

pytestmark = pytest.mark.asyncio


class _FakeMessage:
    def __init__(self, mid: int) -> None:
        self.message_id = mid


class _FakeBot:
    def __init__(self, *, fail_ids: set[int] | None = None) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.fail_ids = fail_ids or set()

    async def delete_message(
        self, *, chat_id: int, message_id: int
    ) -> None:
        if message_id in self.fail_ids:
            raise RuntimeError(f"simulated delete failure for {message_id}")
        self.deleted.append((chat_id, message_id))


async def test_track_records_message_id_from_message_object() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    msg = _FakeMessage(101)
    returned = tracker.track(msg)
    assert returned is msg, "track must return the input unchanged"
    await tracker.cleanup()
    assert bot.deleted == [(42, 101)]


async def test_track_accepts_raw_int() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    tracker.track(101)
    await tracker.cleanup()
    assert bot.deleted == [(42, 101)]


async def test_track_ignores_none_gracefully() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    # Failed sends return None — that path must not break the tracker.
    tracker.track(None)
    tracker.track(_FakeMessage(101))
    await tracker.cleanup()
    assert bot.deleted == [(42, 101)]


async def test_cleanup_deletes_in_reverse_insertion_order() -> None:
    """Newest first: matches what most chat clients do when they
    collapse multiple deletes near each other, and means the
    'started' header disappears last so the chat doesn't briefly
    show only the summary while older messages linger."""
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    for mid in (1, 2, 3, 4):
        tracker.track(_FakeMessage(mid))
    await tracker.cleanup()
    assert [m for _, m in bot.deleted] == [4, 3, 2, 1]


async def test_cleanup_swallows_delete_errors() -> None:
    """Telegram refuses to delete messages older than 48h. The tracker
    must keep going even when one delete raises."""
    bot = _FakeBot(fail_ids={2})
    tracker = TaskMessageTracker(bot, chat_id=42)
    for mid in (1, 2, 3):
        tracker.track(_FakeMessage(mid))
    await tracker.cleanup()
    # Only the non-failing ids land in ``deleted``.
    assert [m for _, m in bot.deleted] == [3, 1]


async def test_cleanup_runs_after_callback() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    tracker.track(_FakeMessage(1))

    fired: list[str] = []

    async def _after() -> None:
        fired.append("after")

    await tracker.cleanup(after=_after)
    assert fired == ["after"]


async def test_cleanup_after_callback_errors_dont_propagate() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    tracker.track(_FakeMessage(1))

    async def _broken() -> None:
        raise RuntimeError("boom")

    # Must not raise — keeps task error handling decoupled from UI cleanup.
    await tracker.cleanup(after=_broken)


async def test_cleanup_clears_pending_ids() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    tracker.track(_FakeMessage(1))
    await tracker.cleanup()
    assert bot.deleted == [(42, 1)]
    # A second cleanup is a no-op — defensive against accidental double-calls.
    bot.deleted.clear()
    await tracker.cleanup()
    assert bot.deleted == []


async def test_cleanup_is_idempotent_when_nothing_tracked() -> None:
    bot = _FakeBot()
    tracker = TaskMessageTracker(bot, chat_id=42)
    await tracker.cleanup()
    assert bot.deleted == []


async def test_default_cleanup_delay_is_three_seconds() -> None:
    """The user-approved spec is 3s. Lock the constant so a future
    edit doesn't accidentally drop / extend it without a deliberate
    change.

    The module-level value is monkeypatched to ``0.0`` by the
    autouse fixture in :mod:`tests.conftest` for speed, but the
    ``DEFAULT_CLEANUP_DELAY_S`` symbol we ``from``-imported at the
    top of this file was bound at collection time and still carries
    the original literal — that's what we lock here.
    """
    assert DEFAULT_CLEANUP_DELAY_S == 3.0
