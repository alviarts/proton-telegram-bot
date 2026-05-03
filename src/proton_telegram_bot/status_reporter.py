"""Live activity indicator for long-running bot tasks.

Each long-running task (``/cekimap``, ``/genaddr``, ``/connect``, the
per-primary "Sync alias" button, …) carries one :class:`StatusReporter`
attached to a Telegram message via an inline-keyboard button. The
button's label is updated as the task progresses through its phases —
"🔓 Login Proton…", "📋 Baca daftar alamat…", "💾 Simpan ke DB…",
"✅ Selesai" — so the user sees something move instead of staring at
a static message for 30+ seconds.

Design notes:

* Updates go through ``editMessageReplyMarkup``, NOT
  ``editMessageText``. That means the parent message's text and parse
  mode stay untouched — important for ``/cekimap`` which is also
  editing the rolling progress text every couple of seconds. Telegram
  treats reply-markup edits as a separate rate-limit bucket.
* Every update is throttled to at most one per
  :data:`STATUS_UPDATE_INTERVAL_S` (default 1.5s). Identical
  back-to-back labels are skipped so we don't burn ratelimit churn.
* The reporter swallows ``BadRequest("message is not modified")`` and
  ``RetryAfter`` errors so a short-term Telegram hiccup doesn't bring
  down the whole task.
* The button has a no-op callback. The user CAN tap it, but the bot
  just acknowledges silently — the button is purely informational.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:  # pragma: no cover - import only for typing
    from telegram import Bot
    from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)

# Callback data used by the no-op status button. Handlers should match
# the exact string when registering the no-op handler so user taps
# don't surface as "command not found" errors.
STATUS_BUTTON_CALLBACK = "status:noop"

# Hard cap from Telegram for inline-keyboard button labels. The Bot
# API truncates anything longer at the server side (sometimes silently)
# so we stay well under it.
_TELEGRAM_BUTTON_TEXT_LIMIT = 64

# Minimum gap between successive ``editMessageReplyMarkup`` calls. The
# Bot API will throttle aggressive edits with ``Too Many Requests``;
# 1.5s keeps us comfortably under the per-chat limit even when several
# tasks run in parallel.
STATUS_UPDATE_INTERVAL_S = 1.5

DEFAULT_IDLE_LABEL = "✅ Selesai"


def build_status_keyboard(
    label: str,
    *,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Wrap ``label`` in an inline keyboard.

    ``extra_rows`` are appended below the status row. This is how
    ``/genaddr`` keeps its ❌ Batalkan button visible while the live
    status row above it narrates the current phase.
    """
    truncated = _truncate_label(label)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=truncated, callback_data=STATUS_BUTTON_CALLBACK)]
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def _truncate_label(label: str) -> str:
    if len(label) <= _TELEGRAM_BUTTON_TEXT_LIMIT:
        return label
    # Reserve one char for the ellipsis.
    return label[: _TELEGRAM_BUTTON_TEXT_LIMIT - 1] + "…"


class StatusReporter:
    """Manage the lifecycle of a single status button.

    Usage::

        msg = await bot.send_message(
            chat_id=chat_id,
            text="…",
            reply_markup=build_status_keyboard("🚀 Mulai…"),
        )
        async with StatusReporter(bot, chat_id, msg.message_id) as status:
            await status.update("🔓 Login Proton…")
            ...
            await status.update("📋 Baca daftar alamat…")

    On context-manager exit the button transitions to
    :data:`DEFAULT_IDLE_LABEL` so the user has a clear "task ended"
    signal even if the parent text isn't touched again.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        *,
        idle_label: str = DEFAULT_IDLE_LABEL,
        initial_label: str | None = None,
        update_interval_s: float = STATUS_UPDATE_INTERVAL_S,
        extra_rows: list[list[InlineKeyboardButton]] | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._idle_label = idle_label
        self._update_interval_s = update_interval_s
        # Persisted across edits so a "Batalkan" / similar control row
        # stays visible alongside the rotating status label.
        self._extra_rows: list[list[InlineKeyboardButton]] = (
            [list(row) for row in extra_rows] if extra_rows else []
        )
        # ``_last_label`` seeds from ``initial_label`` when the caller
        # already rendered a meaningful label on the anchor message
        # (e.g. ``build_status_keyboard("⏳ Mempersiapkan…")`` was used
        # in the ``send_message`` that birthed the anchor). Without
        # this seed the first :meth:`relocate` — which can fire before
        # any :meth:`update` if the very first thing the handler does
        # is send a log line — would fall back to ``idle_label`` and
        # show ``"✅ Selesai"`` on a button that's actually still
        # mid-flow. The user reported exactly this: a freshly-started
        # ``/connect`` showed ``✅ Selesai`` on the keyboard while the
        # bot was waiting for the user to type their email.
        self._last_label: str | None = (
            _truncate_label(initial_label) if initial_label else None
        )
        self._last_edit_at = 0.0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def message_id(self) -> int:
        return self._message_id

    async def __aenter__(self) -> StatusReporter:
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.done()

    async def update(self, label: str, *, force: bool = False) -> None:
        """Re-label the status button.

        ``force=True`` bypasses both the same-label dedupe and the
        rate-limit window — use it for terminal transitions (the final
        "Selesai" or an error label) so the user always sees them.
        """
        if self._closed:
            return
        truncated = _truncate_label(label)
        async with self._lock:
            now = time.monotonic()
            if not force:
                if truncated == self._last_label:
                    return
                if now - self._last_edit_at < self._update_interval_s:
                    return
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    reply_markup=build_status_keyboard(
                        truncated, extra_rows=self._extra_rows or None
                    ),
                )
            except Exception as exc:
                # The most common case is "message is not modified",
                # which is just a benign duplicate. RetryAfter / 429
                # also reach us here. None of these should bring the
                # task down — the next update() call will retry.
                msg = str(exc).lower()
                if "not modified" in msg:
                    self._last_label = truncated
                    return
                LOGGER.debug(
                    "status reporter edit failed for chat=%s msg=%s: %s",
                    self._chat_id,
                    self._message_id,
                    exc,
                )
                return
            self._last_label = truncated
            self._last_edit_at = now

    async def done(self, label: str | None = None) -> None:
        """Transition the button to its terminal state and close the reporter.

        After ``done()`` further ``update()`` calls are no-ops, which
        makes it safe to call from a ``finally`` block on top of
        per-phase ``await status.update(...)`` lines.
        """
        if self._closed:
            return
        final = label if label is not None else self._idle_label
        await self.update(final, force=True)
        self._closed = True

    async def clear(self) -> None:
        """Remove the status button entirely (no keyboard at all)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=self._chat_id,
                message_id=self._message_id,
                reply_markup=None,
            )
        except Exception:
            LOGGER.debug(
                "status reporter clear failed for chat=%s msg=%s",
                self._chat_id,
                self._message_id,
                exc_info=True,
            )

    async def relocate(self, new_message_id: int) -> None:
        """Move the status keyboard to ``new_message_id``.

        Why this exists:

        The user complained that during /connect the status anchor
        stays at the *top* of the chat (where it was first sent by
        ``_ensure_connect_progress``) while subsequent log messages
        — "Tambah akun Proton baru…", "Step 1/2", "Auto-isi…",
        "Step 2/2", "Bridge add-account…" — pile up *underneath* it.
        That forced the user to scroll up to see the live progress
        button, defeating the whole point of the live indicator
        ("lho ko disitu selalu berada di paling baru tombol bridge
        account"). Re-anchoring after every new log keeps the
        button at the chat tail, right above the input field, where
        the user is already looking.

        Implementation:

        * Attach the keyboard to the new message *first*. If
          Telegram rejects that edit (rare — message gone, network
          hiccup), the old anchor stays intact so we don't lose
          progress visibility.
        * Then strip the keyboard from the old anchor so we never
          have two "🔧 Bridge add-account…" buttons in the chat at
          the same time.
        * Finally update ``self._message_id`` so future ``update()``
          / ``done()`` calls edit the *new* anchor.

        Caller is responsible for ``new_message_id`` referring to a
        message that was sent to the same ``chat_id`` and is still
        live (Telegram rejects edits on messages older than 48h,
        but for typical /connect flows that's never an issue).
        """
        if self._closed:
            return
        async with self._lock:
            old_message_id = self._message_id
            if old_message_id == new_message_id:
                return
            label = self._last_label or self._idle_label
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._chat_id,
                    message_id=new_message_id,
                    reply_markup=build_status_keyboard(
                        label, extra_rows=self._extra_rows or None
                    ),
                )
            except Exception:
                LOGGER.debug(
                    "status reporter relocate-attach failed for "
                    "chat=%s msg=%s",
                    self._chat_id,
                    new_message_id,
                    exc_info=True,
                )
                return  # leave old anchor intact
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._chat_id,
                    message_id=old_message_id,
                    reply_markup=None,
                )
            except Exception:
                LOGGER.debug(
                    "status reporter relocate-detach failed for "
                    "chat=%s msg=%s",
                    self._chat_id,
                    old_message_id,
                    exc_info=True,
                )
            self._message_id = new_message_id
            # Reset the throttle window so the *very next* :meth:`update`
            # call goes through immediately. Without this reset the
            # caller pattern "send a new log line → relocate → set new
            # phase label" silently dropped the phase label whenever
            # the relocate's two ``editMessageReplyMarkup`` calls were
            # less than ``update_interval_s`` apart, leaving the
            # keyboard showing the previous phase (or worse,
            # ``✅ Selesai`` from the seed when ``_last_label`` had
            # never been set). The reset is safe — relocate already
            # paid the rate-limit cost for this chat, and the next
            # update is the user-visible label they actually expect.
            self._last_edit_at = 0.0


async def on_status_button_noop(
    update: Any, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge taps on the live-status button.

    The button is purely informational, but Telegram clients show a
    spinner until the bot answers ``answerCallbackQuery``. Without this
    handler users see a permanently spinning icon and conclude
    something is wrong.
    """
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    try:
        await query.answer()
    except Exception:
        LOGGER.debug("status button: callback ack failed", exc_info=True)
