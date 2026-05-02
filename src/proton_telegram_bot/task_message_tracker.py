"""Track and clean up transient bot messages from long-running tasks.

Long-running tasks like ``/cekimap`` and ``/genaddr`` emit a series of
chat messages — a starter, periodic progress lines, optional
:class:`StatusReporter` anchor, and a final summary. Without cleanup
those accumulate in the chat indefinitely, which the user has
explicitly asked us to avoid: "saya ingin selalu bersih … sebaiknya
auto hapus apabila sudah selesai, semua akan kembali ke list".

:class:`TaskMessageTracker` is the small bookkeeper for that. The
flow is:

1. Caller creates a tracker once at the start of the task.
2. Every ``bot.send_message(...)`` whose result is transient gets
   wrapped in :meth:`track`, which records the returned message id.
3. In a ``finally`` block the caller awaits :meth:`cleanup`, which
   sleeps a few seconds (so the user can read the final summary),
   deletes every recorded message, and optionally runs an ``after``
   coroutine — typically re-rendering ``/list`` so the chat lands
   back in a canonical state.

The tracker swallows all delete failures: messages older than 48h
can no longer be deleted by bots, and a stale id is not worth
crashing the task over.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from telegram import Bot

LOGGER = logging.getLogger(__name__)

# Default delay before the tracker starts deleting messages. Three
# seconds is enough for the user to read the final summary on a
# normal-paced phone scroll without making the chat feel sluggish.
DEFAULT_CLEANUP_DELAY_S = 3.0


class TaskMessageTracker:
    """Collect message ids emitted by a task and delete them on cleanup.

    Usage::

        tracker = TaskMessageTracker(bot, chat_id)
        try:
            tracker.track(await bot.send_message(...))   # starter
            tracker.track(await bot.send_message(...))   # progress
            ...
            tracker.track(await bot.send_message(...))   # summary
        finally:
            await tracker.cleanup(after=lambda: render_list(...))
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        delay_s: float | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        # Resolve at construction time so tests can monkeypatch the
        # module-level :data:`DEFAULT_CLEANUP_DELAY_S` to 0 and have
        # production callers pick it up without changing their call site.
        self._delay_s = (
            delay_s if delay_s is not None else DEFAULT_CLEANUP_DELAY_S
        )
        self._message_ids: list[int] = []

    @property
    def chat_id(self) -> int:
        return self._chat_id

    @property
    def delay_s(self) -> float:
        return self._delay_s

    def __len__(self) -> int:
        return len(self._message_ids)

    def track(self, message: Any) -> Any:
        """Record ``message`` (a Telegram ``Message`` or its id) for cleanup.

        The argument is returned unchanged so callers can write::

            msg = tracker.track(await bot.send_message(...))

        without an extra statement. ``None`` and objects without a
        ``message_id`` attribute are silently ignored, which keeps
        production code compatible with test fakes that return ``None``
        from ``send_message``.
        """
        if message is None:
            return None
        mid: Any = getattr(message, "message_id", None)
        if mid is None and isinstance(message, int):
            mid = message
        if isinstance(mid, int):
            self._message_ids.append(mid)
        return message

    def track_id(self, message_id: int) -> None:
        """Record a bare ``message_id``. Use when you only have the id."""
        self._message_ids.append(int(message_id))

    async def cleanup(
        self,
        *,
        after: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Sleep, delete every tracked message, then optionally run ``after``.

        Best-effort: a failed delete is logged at debug level and does
        not stop the rest of the cleanup. The ``after`` callback (if
        provided) runs only after every delete attempt is made — so
        the chat is "clean" by the time it posts a fresh ``/list``.
        """
        if self._delay_s > 0:
            try:
                await asyncio.sleep(self._delay_s)
            except asyncio.CancelledError:
                # Cleanup runs from a finally block; if the caller's
                # task is being cancelled we still want the deletes to
                # land. Re-raising would leave half-cleaned messages.
                pass
        # Reverse so the visual collapse mirrors the order the user
        # saw them appear (bottom-up).
        ids = list(reversed(self._message_ids))
        self._message_ids.clear()
        for mid in ids:
            try:
                await self._bot.delete_message(
                    chat_id=self._chat_id, message_id=mid
                )
            except Exception as exc:
                LOGGER.debug(
                    "task tracker: delete_message failed chat=%s msg=%s: %s",
                    self._chat_id,
                    mid,
                    exc,
                )
        if after is not None:
            try:
                await after()
            except Exception:
                LOGGER.debug(
                    "task tracker: after-cleanup callback raised",
                    exc_info=True,
                )
