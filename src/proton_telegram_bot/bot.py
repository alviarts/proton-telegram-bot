"""Telegram bot wiring: handlers, menus, and notifier implementation."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import secrets
import smtplib
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import Any, cast

import aioimaplib
import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import address_generator
from .bridge_admin import (
    BridgeAdmin,
    BridgeAdminError,
    BridgeImapCredentials,
    CaptchaRequired,
    LoginFailed,
    LoginSucceeded,
)
from .config import Settings
from .crypto import CredentialCipher
from .db import Database
from .email_parser import format_body_html
from .health_check import run_health_check
from .manager import ListenerManager, Notifier
from .models import AliasRecord, AliasStatus, PrimaryAccount
from .proton_browser import CreationStatus
from .proton_verify import solve_email_verification
from .status_reporter import (
    STATUS_BUTTON_CALLBACK,
    StatusReporter,
    build_status_keyboard,
    on_status_button_noop,
)
from .task_message_tracker import TaskMessageTracker
from .tempmail import TempMailbox, TempMailError

LOGGER = logging.getLogger(__name__)


async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Global error handler registered with ``Application.add_error_handler``.

    Without this, an exception raised inside any handler bubbles up to
    PTB's dispatcher which logs it at ERROR level and moves on — but
    the user who triggered the broken handler gets *no reply at all*.
    From their side the bot looks dead. The user explicitly asked for
    "antisipasi error mati" / "lebih baik mengulang flow daripada bot
    tidak merespon": this handler is the catch-all that guarantees a
    user-visible reply for every uncaught exception, plus a structured
    log line for debugging.

    Three behaviours:

    1. Always log the exception at ERROR with full traceback so
       journalctl/log aggregators can surface it.
    2. Best-effort reply to the user with a generic "ada error, coba
       lagi" message anchored to the chat the update came from. We
       wrap the reply in ``contextlib.suppress`` because the original
       failure may have been *caused* by Telegram being unreachable —
       no point bubbling another exception out of the error handler.
    3. Best-effort end any conversation that was active for the user
       so they're not stuck mid-flow with stale ``user_data`` waiting
       for a state-machine transition that will never come. PTB
       doesn't expose a clean "abort conversation for this user" API,
       so we just clear the per-user data dict — subsequent /connect
       /sync /setprotonpw entries restart cleanly thanks to
       ``allow_reentry=True``.
    """
    err = context.error
    LOGGER.exception("uncaught exception in handler", exc_info=err)

    # Best-effort: clear stale ``user_data`` so the next command starts
    # from a clean slate instead of inheriting half-set keys
    # (``imap_password``, ``bridge_captcha_iterator``, etc.) from the
    # broken flow. We never re-raise from inside an error handler.
    with contextlib.suppress(Exception):
        if isinstance(context.user_data, dict):
            context.user_data.clear()

    # Try to reply to whoever triggered this. The update may not be a
    # ``telegram.Update`` (e.g. ``JobQueue`` errors get a string), so
    # we duck-type to find ``effective_message`` / ``effective_chat``.
    chat_id: int | None = None
    if isinstance(update, Update):
        if update.effective_chat is not None:
            chat_id = update.effective_chat.id
    if chat_id is None:
        return

    text = (
        "⚠️ Maaf, ada error tidak terduga saat memproses perintah barusan. "
        "Bot tetap jalan — coba ulangi perintah, atau /start untuk lihat "
        "menu utama."
    )
    with contextlib.suppress(Exception):
        await context.bot.send_message(chat_id=chat_id, text=text)


# Conversation states for /connect.
#
# We deliberately keep this short: the older flow asked for host/port/SSL/
# username separately, but the answer is *always* 127.0.0.1:1143 + STARTTLS
# + username==email when the user is running the standard Proton Bridge
# locally (which is the only supported setup). Asking those four extra
# questions was pure friction. The constants below are the ones we now
# hard-code; if a future setup ever needs different values we can wire a
# dedicated /connect_advanced command instead of bringing the questions
# back into the default path.
(
    CONNECT_EMAIL,
    CONNECT_PASSWORD,
    CONNECT_BRIDGE_CAPTCHA,
    CONNECT_RECOVERY_VERIFY,
) = range(4)
CONNECT_DEFAULT_HOST = "127.0.0.1"
CONNECT_DEFAULT_PORT = 1143
CONNECT_DEFAULT_SSL = False

# Strong refs to fire-and-forget cleanup tasks scheduled at the end
# of /connect (request #8). asyncio garbage-collects tasks whose only
# reference is on the call stack, so without keeping a strong ref the
# 3-second cleanup may never run on a busy event loop. Done callbacks
# remove the entry so the set doesn't grow unbounded.
_CONNECT_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()

# Domain auto-suffix for /connect: when the user types just a username
# (no ``@`` in the input) the bot appends ``@proton.me`` automatically
# so they don't have to type it every time. The two fallback domains
# are surfaced in the hint message so the user knows what to do if
# the auto-suffix landed on the wrong one — they can resend with
# ``user@protonmail.com`` or ``user@pm.me`` explicitly.
CONNECT_DEFAULT_DOMAIN = "proton.me"
CONNECT_FALLBACK_DOMAINS: tuple[str, ...] = ("protonmail.com", "pm.me")


def _resolve_connect_email(raw: str) -> tuple[str, bool]:
    """Normalise a /connect email input.

    Returns ``(email, was_auto_suffixed)``. When the user typed just a
    username (e.g. ``vielz883``) we append ``@proton.me`` and set the
    flag so the caller can show a one-line hint pointing at the
    fallback domains. Whitespace is stripped and the result is
    lower-cased to match the rest of the codebase's address handling.
    """
    text = raw.strip().lower()
    if "@" in text:
        return text, False
    return f"{text}@{CONNECT_DEFAULT_DOMAIN}", True


def _connect_domain_hint_html(email: str) -> str:
    """One-line HTML hint shown when ``/connect`` auto-suffixed a domain.

    Tells the user which domain we picked and how to override it
    cheaply (just resend with the explicit domain).
    """
    fallback_list = ", ".join(
        f"<code>@{html.escape(d)}</code>" for d in CONNECT_FALLBACK_DOMAINS
    )
    return (
        f"📧 Auto-isi: <code>{html.escape(email)}</code>\n"
        f"<i>Kalau salah domain, kirim ulang dengan {fallback_list}.</i>"
    )

# Conversation states for /sync
SYNC_CAPTCHA = 10

# Conversation states for /setprotonpw
SETPW_PICK_PRIMARY, SETPW_PASSWORD = 20, 21

# Conversation states for the "🏷️ Tag ulang" button on forwarded emails.
# Single-state flow: we kick off when the inline button is tapped, ask
# for free-text label, persist it via ``set_service_label``, and end.
TAG_AWAIT_LABEL = 30

# Throttle: at most one progress edit every N addresses to stay well under
# Telegram's edit_message rate limit during long /genaddr runs.
# Kept for backwards-compat callers; the new background flow uses
# ``GENADDR_NOTIFY_EVERY`` for fresh-message progress instead.
GENADDR_PROGRESS_EVERY = 1
# Per how many newly-created addresses the background /genaddr task should
# post a fresh progress message into the chat. The user's UX request was
# "laporan text per 5 alias sudah selesai", so the default is 5; users with
# big batches still get a steady drip of updates without spamming the chat.
GENADDR_NOTIFY_EVERY = 5
# Minimum gap between successive ``editMessageText`` calls on the
# /genaddr starter message. Telegram's bot rate limit is ~1 edit per
# second per chat; 2s gives us comfortable headroom even when the
# browser bursts through several addresses inside the same second.
# Force-edits on milestones bypass this throttle so the user never
# misses an "every 5 sukses" tick.
GENADDR_BODY_EDIT_INTERVAL_S = 2.0
# How often the background heartbeat task force-refreshes the starter
# message body even when no new address has been created yet (e.g.
# during browser startup, login, captcha solve). The user explicitly
# asked for a "sedang membuat address X/Y" tick every 5 seconds so they
# always see something move regardless of whether a new alias landed.
GENADDR_BODY_REFRESH_INTERVAL_S = 5.0
# Cap how many of the most-recent successful aliases we list in the
# rolling body update. Five is the sweet spot the user asked for in
# their handoff: enough to verify progress at a glance, short enough to
# stay on a single line in the Telegram client.
GENADDR_BODY_RECENT_COUNT = 5

# Limits applied at the bot layer (the alias generator has its own MAX_BATCH).
GENADDR_DEFAULT_DOMAIN = "proton.me"
GENADDR_MAX_COUNT = 200

CB_PICK = "pick"
CB_REFRESH = "refresh"
CB_RESET = "reset"
CB_DELETE = "delete"
CB_NOOP = "noop"
CB_POLL_NOW = "poll_now"
# Tap-to-copy convenience: posts a fresh single-line ``<code>email</code>``
# message containing the user's currently locked alias so desktop users
# can one-click copy without scrolling back through chat history. The
# alias header itself is also wrapped in ``<code>`` for mobile tap-copy.
CB_COPY_ACTIVE_EMAIL = "copyactive"
# Pagination across the two-level keyboard (primary list and alias
# drill-down). Format:
#   * primary list page:  ``ppg:<page>``
#   * alias drill-down:   ``apg:<primary_id>:<page>``
# Page indices are 0-based. Both stay well under Telegram's 64-byte
# callback_data limit since they're just small ints.
CB_PRIMARY_PAGE = "ppg"
CB_ALIAS_PAGE = "apg"
# Rows per page on both keyboards. The user asked for "4 baris per
# halaman" so /list scrolls smoothly on phones without becoming a wall
# of buttons. Tweak in one place if the UX team ever wants a different
# default.
LIST_PAGE_SIZE = 4
# Two-level primary/alias UI:
CB_PICK_PRIMARY = "pickp"
CB_BACK_TO_PRIMARIES = "backp"
CB_DEL_PRIMARY = "delp"
CB_GENADDR_CANCEL = "genaddr_cancel"
# Quick-action buttons shown after /connect when the new primary has zero
# aliases. They short-circuit the user back into the right /setprotonpw or
# /genaddr flow without making them re-type the email address.
CB_QUICK_SETPW = "qsetpw"
CB_QUICK_GENADDR = "qgenaddr"
# Post-disconnect re-entry shortcut: the "🔌 Connect lagi" button below
# the disconnect-done message routes through the existing /connect
# conversation so the user doesn't have to retype the command.
CB_CONNECT_AGAIN = "qconnect"
# PR-F lock-active reminder: a transient bubble posted at the end of
# common commands while an alias is locked, with two actions —
# ``CB_LOCK_REMINDER_UNLOCK`` releases the lock, and
# ``CB_LOCK_REMINDER_PICK_NEW`` re-renders the primary list so the
# user can switch alias.
CB_LOCK_REMINDER_UNLOCK = "lockunlock"
CB_LOCK_REMINDER_PICK_NEW = "lockpicknew"
# /cekimap entry: trigger an end-to-end health check (Bridge SMTP →
# tempmail) for every alias of a chosen primary. Two callback flavours:
# ``hcpick:<primary_id>``  — picker entry (from /cekimap menu and /list).
# ``qhc:<primary_id>``      — direct trigger (from after-connect message).
CB_HEALTHCHECK_PICK = "hcpick"
CB_QUICK_HEALTHCHECK = "qhc"
# /list per-primary "Sync alias" button. Drives a Playwright session
# back into ``account.proton.me`` for the picked primary, fetches the
# full address list, and persists any new aliases to the DB. Surfaces
# progress through a :class:`StatusReporter` button that updates as
# the scrape moves through its phases.
CB_SYNC_PRIMARY = "syncp"
# Tag-service flow: button attached to forwarded emails so the user can
# label / re-label the sender's domain without using /services. Format:
# ``tagsvc:<alias_id>:<sender_domain>``. The conversation handler asks
# for the new label via free text. ``tagsvc_clear:<alias_id>:<domain>``
# wipes the label so the alias falls back to the raw domain.
CB_TAG_SERVICE = "tagsvc"
CB_TAG_SERVICE_CLEAR = "tagsvc_clear"
# /services keyboard: rows of ``svcdel:<domain>`` entries to one-tap
# delete a chat-level mapping.
CB_SVC_DELETE = "svcdel"

# Soft target for total aliases per primary. After /cekimap or after
# the per-primary "🔄 Sync" button finishes, if the alias count for
# that primary is below this number the bot offers a one-tap "✨
# Tambah N alamat lagi" button that drives /genaddr in random-suffix
# mode to top the account up. 20 = the alias-only target the user
# specified ("button generate pointnya 20") — primary itself is not
# counted because the user can't /genaddr a primary, only aliases.
# Top-up math is therefore a clean ``missing = TARGET - alias_count``
# that matches what the user sees in /list.
ALIAS_TARGET_PER_PRIMARY = 20


def _is_allowed(settings: Settings, user_id: int | None) -> bool:
    if not settings.allowed_user_ids:
        return True
    if user_id is None:
        return False
    return user_id in settings.allowed_user_ids


def _gate(handler):  # type: ignore[no-untyped-def]
    """Decorator-like wrapper: reject calls from non-allowlisted Telegram users."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings: Settings = context.application.bot_data["settings"]
        user_id = update.effective_user.id if update.effective_user else None
        if not _is_allowed(settings, user_id):
            if update.effective_message is not None:
                await update.effective_message.reply_text(
                    "Maaf, kamu tidak terdaftar untuk memakai bot ini."
                )
            return ConversationHandler.END
        return await handler(update, context)

    wrapper.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapper


# --------------------------------------------------------------- helpers


def _bot_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return cast(Database, context.application.bot_data["db"])


def _bot_cipher(context: ContextTypes.DEFAULT_TYPE) -> CredentialCipher:
    return cast(CredentialCipher, context.application.bot_data["cipher"])


def _bot_manager(context: ContextTypes.DEFAULT_TYPE) -> ListenerManager:
    return cast(ListenerManager, context.application.bot_data["manager"])


async def _purge_alias_email_messages(
    bot, db: Database, chat_id: int, alias_id: int
) -> int:
    """Delete every previously-forwarded email message for ``alias_id``.

    Called on the three "active alias just changed" boundaries
    (``/list`` pick, ``/unlock``, lock-reminder Lepas) so the chat stops
    accumulating stale email forwards from an alias the user is no
    longer using. Returns the count actually deleted from Telegram —
    rows are popped from the DB regardless so we don't retry forever
    on messages older than Telegram's 48-hour deletion window.
    """
    message_ids = await db.pop_forwarded_email_message_ids(chat_id, alias_id)
    if not message_ids:
        return 0
    deleted = 0
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            # Message may already be gone (user deleted it, >48h old,
            # bot lost permission, etc.). Silently skip — the DB row
            # was already popped so we won't retry it forever.
            LOGGER.debug(
                "delete_message failed for chat=%s mid=%s",
                chat_id,
                mid,
                exc_info=True,
            )
    return deleted


def _bot_bridge_admin(
    context: ContextTypes.DEFAULT_TYPE,
) -> BridgeAdmin | None:
    """Return the configured BridgeAdmin, or ``None`` if auto-add is off."""
    admin = context.application.bot_data.get("bridge_admin")
    if admin is None or not getattr(admin, "enabled", False):
        return None
    return cast(BridgeAdmin, admin)


def _paginate_slice(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    """Slice ``items`` for the requested ``page`` and return navigation context.

    Returns ``(window, page, total_pages)`` where ``window`` is the
    sublist for the page (clamped into bounds), ``page`` is the
    normalised page index (negative or out-of-range pages collapse to
    the nearest valid edge), and ``total_pages`` is at least 1 even for
    an empty list (so the caller can still render a stable "Page 1/1"
    label without special-casing).
    """
    if page_size <= 0:
        return items, 0, 1
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start : start + page_size], page, total_pages


def _build_pagination_row(
    callback_prefix: str,
    page: int,
    total_pages: int,
) -> list[InlineKeyboardButton] | None:
    """Build a single ``◀ Prev | Page X/Y | Next ▶`` row, or ``None`` if
    there's only one page.

    ``callback_prefix`` is the full prefix the row's Prev/Next buttons
    should encode. For the primary list it's ``"ppg"``; for an alias
    drill-down it's ``f"apg:{primary_id}"``. Both stay under Telegram's
    64-byte callback_data ceiling because the suffix is just a small
    integer.

    The Page-N/M label is wired to ``CB_NOOP`` so tapping it doesn't
    do anything (the swallow-and-toast path in ``on_callback`` handles
    the dismissal).
    """
    if total_pages <= 1:
        return None
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "◀ Prev", callback_data=f"{callback_prefix}:{page - 1}"
            )
        )
    row.append(
        InlineKeyboardButton(
            f"Page {page + 1}/{total_pages}", callback_data=CB_NOOP
        )
    )
    if page < total_pages - 1:
        row.append(
            InlineKeyboardButton(
                "Next ▶", callback_data=f"{callback_prefix}:{page + 1}"
            )
        )
    return row


def _build_primary_keyboard(
    primaries: list[PrimaryAccount],
    alias_counts: dict[int, int] | None = None,
    active_primary_id: int | None = None,
    healthcheck_stats: dict[int, tuple[int, int]] | None = None,
    *,
    with_copy_active_button: bool = False,
    page: int = 0,
    page_size: int = LIST_PAGE_SIZE,
) -> InlineKeyboardMarkup:
    """Top-level keyboard listing every Proton account a user owns.

    Each row drills into the alias list of that primary.
    ``alias_counts`` annotates each label with the total alias count.
    ``healthcheck_stats`` (from the last /cekimap run, keyed by
    ``primary.id``) takes priority and renders as ``ok/total`` so the
    user can spot a primary whose aliases have started failing.
    ``active_primary_id`` flags the primary whose alias is currently
    locked (purely visual). ``with_copy_active_button`` adds a "📋 Copy
    email aktif" row when an alias is locked, so desktop users have a
    one-click way to grab the address without selecting text out of the
    rendered HTML message.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not primaries:
        rows.append(
            [InlineKeyboardButton("(belum ada email utama)", callback_data=CB_NOOP)]
        )
    else:
        window, page, total_pages = _paginate_slice(primaries, page, page_size)
        for primary in window:
            count = (alias_counts or {}).get(primary.id, 0)
            marker = "🔒 " if active_primary_id == primary.id else "📧 "
            stats = (healthcheck_stats or {}).get(primary.id)
            if stats is not None:
                # ``ok/total`` from the most recent /cekimap. Slash
                # notation is compact enough to fit on one row even
                # next to the icon-only Sync button.
                ok, total = stats
                count_label = f"{ok}/{total}"
            else:
                count_label = f"{count}"
            label = f"{marker}{primary.email} · {count_label}"
            # Email + Sync stay on the same row. The Sync button is
            # icon-only ("🔄") so a long primary email + alias count
            # has room to render without the client clipping the
            # label on narrow viewports.
            rows.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"{CB_PICK_PRIMARY}:{primary.id}",
                    ),
                    InlineKeyboardButton(
                        "🔄",
                        callback_data=f"{CB_SYNC_PRIMARY}:{primary.id}",
                    ),
                ]
            )
        # Pagination row sits directly under the per-primary rows so the
        # Prev/Next controls feel attached to the list they navigate.
        nav = _build_pagination_row(CB_PRIMARY_PAGE, page, total_pages)
        if nav is not None:
            rows.append(nav)
    if with_copy_active_button:
        rows.append(
            [
                InlineKeyboardButton(
                    "📋 Copy email aktif", callback_data=CB_COPY_ACTIVE_EMAIL
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "📥 Cek email sekarang", callback_data=CB_POLL_NOW
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton("🔄 Refresh daftar", callback_data=CB_REFRESH)]
    )
    return InlineKeyboardMarkup(rows)


def _build_alias_keyboard_for_primary(
    primary: PrimaryAccount,
    aliases: list[AliasRecord],
    active_alias_id: int | None = None,
    *,
    page: int = 0,
    page_size: int = LIST_PAGE_SIZE,
    alias_count: int | None = None,
) -> InlineKeyboardMarkup:
    """Drill-down keyboard showing aliases owned by a single primary.

    ``page`` / ``page_size`` slice the visible aliases so a primary
    with hundreds of generated addresses doesn't overflow the
    keyboard. Both default to the standard ``LIST_PAGE_SIZE`` (4 rows
    per page). The Prev/Next callback embeds the primary id so the
    callback handler can re-render the same drill-down without
    needing chat-state.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not aliases:
        rows.append(
            [InlineKeyboardButton("(belum ada alias)", callback_data=CB_NOOP)]
        )
    else:
        window, page, total_pages = _paginate_slice(aliases, page, page_size)
        for alias in window:
            # callback_data must be ≤ 64 bytes (Telegram API), so we use the
            # alias's numeric id rather than the email address itself.
            label = alias.email
            if active_alias_id is not None and alias.id == active_alias_id:
                label = f"🔒 {label}"
            rows.append(
                [
                    InlineKeyboardButton(
                        label, callback_data=f"{CB_PICK}:{alias.id}"
                    )
                ]
            )
        nav = _build_pagination_row(
            f"{CB_ALIAS_PAGE}:{primary.id}", page, total_pages
        )
        if nav is not None:
            rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "📥 Cek email sekarang", callback_data=CB_POLL_NOW
            ),
            InlineKeyboardButton(
                "← Email utama", callback_data=CB_BACK_TO_PRIMARIES
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🩺 Cek IMAP listener (background)",
                callback_data=f"{CB_HEALTHCHECK_PICK}:{primary.id}",
            )
        ]
    )
    # Smart top-up shortcut: render only when the primary is below
    # :data:`ALIAS_TARGET_PER_PRIMARY` so a fully-loaded account never
    # sees a stale top-up button. ``alias_count`` defaults to
    # ``len(aliases)`` (which is exactly the "(N alias)" header
    # rendered above this keyboard, e.g. "12 alias"). Wording mirrors
    # the post-healthcheck prompt — "Tambah N alamat lagi" — so the
    # user sees a consistent vocabulary across the bot.
    if alias_count is None:
        alias_count = len(aliases)
    topup_missing = max(0, ALIAS_TARGET_PER_PRIMARY - alias_count)
    if topup_missing > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✨ Tambah {topup_missing} alamat lagi",
                    callback_data=(
                        f"{CB_QUICK_GENADDR}:{primary.id}:{topup_missing}"
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _build_poll_now_keyboard(
    *, with_copy_active: bool = False
) -> InlineKeyboardMarkup:
    """Standalone 'check email now' button used in the lock-confirmation message.

    When ``with_copy_active`` is ``True`` an extra "📋 Copy email aktif"
    row is added so desktop users can post a single-line ``<code>email</code>``
    message into the chat for one-click copy.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if with_copy_active:
        rows.append(
            [
                InlineKeyboardButton(
                    "📋 Copy email aktif", callback_data=CB_COPY_ACTIVE_EMAIL
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("📥 Cek email sekarang", callback_data=CB_POLL_NOW)]
    )
    return InlineKeyboardMarkup(rows)


# Telegram caps callback_data at 64 bytes. ``tagsvc:<alias_id>:<domain>``
# fits comfortably for typical domains (alias_id ≤ 6 digits + colons +
# domain ≤ 50 chars = well under 64). For pathologically long domains
# we silently skip the button rather than truncating the domain (which
# would break the round-trip to ``set_service_label``).
_TG_CALLBACK_DATA_LIMIT = 64


def _build_tag_service_keyboard(
    *,
    alias_id: int,
    sender_domain: str,
    current_label: str | None,
) -> InlineKeyboardMarkup | None:
    """Single-row keyboard attached to forwarded emails.

    Lets the user (re)label the sender's domain (chat-wide mapping) or
    clear an existing label. Returns ``None`` when the callback_data
    payload would overflow Telegram's 64-byte cap so the email is
    forwarded without buttons rather than crashing the send.
    """
    domain = sender_domain.strip().lower()
    if not domain:
        return None
    tag_data = f"{CB_TAG_SERVICE}:{alias_id}:{domain}"
    clear_data = f"{CB_TAG_SERVICE_CLEAR}:{alias_id}:{domain}"
    if len(tag_data.encode()) > _TG_CALLBACK_DATA_LIMIT:
        return None
    label_for_button = current_label or domain
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"🏷️ Tag ulang ({label_for_button})", callback_data=tag_data
            )
        ]
    ]
    if current_label is not None:
        # Only show "Hapus label" when there's actually a label to wipe.
        if len(clear_data.encode()) <= _TG_CALLBACK_DATA_LIMIT:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🗑️ Hapus label", callback_data=clear_data
                    )
                ]
            )
    return InlineKeyboardMarkup(rows)


async def _alias_count_per_primary(
    db: Database, chat_id: int, primaries: list[PrimaryAccount]
) -> dict[int, int]:
    counts: dict[int, int] = {}
    for primary in primaries:
        aliases = await db.list_aliases(chat_id, primary_id=primary.id)
        counts[primary.id] = len(aliases)
    return counts


def _format_alias_with_labels(
    alias_email: str,
    labels: list[str],
    *,
    is_active: bool = False,
    max_labels: int = 3,
) -> str:
    """Render a single ``"vielz008 · 📧 Devin, GitHub"`` line for /list.

    Caps at ``max_labels`` visible labels with a ``+N`` overflow tail
    so an alias used by many services doesn't blow out the message
    width on mobile. The active alias gets a leading 🔒 to mirror the
    keyboard button.
    """
    prefix = "🔒 " if is_active else "• "
    base = f"{prefix}<code>{html.escape(alias_email)}</code>"
    if not labels:
        return base
    visible = labels[:max_labels]
    extra = len(labels) - len(visible)
    rendered_labels = ", ".join(html.escape(label) for label in visible)
    if extra > 0:
        rendered_labels = f"{rendered_labels} +{extra}"
    return f"{base}  ·  📧 {rendered_labels}"


def _build_alias_list_text(
    primary: PrimaryAccount,
    aliases: list[AliasRecord],
    labels_by_alias: dict[int, list[str]],
    active_alias_id: int | None,
) -> str:
    """Compose the message body for ``/list`` per-primary view.

    Header line is the primary's email + alias count. Each alias gets
    one line with its service labels (when known). When *no* alias has
    been tagged yet the body collapses back to the original single-
    line header so the message stays short for fresh accounts.
    """
    header = (
        f"📧 Alias di <b>{html.escape(primary.email)}</b> "
        f"({len(aliases)} alias):"
    )
    has_any_label = any(labels_by_alias.get(a.id) for a in aliases)
    if not has_any_label:
        return header
    lines = [header, ""]
    for alias in aliases:
        labels = labels_by_alias.get(alias.id, [])
        lines.append(
            _format_alias_with_labels(
                alias.email,
                labels,
                is_active=(alias.id == active_alias_id),
            )
        )
    return "\n".join(lines)


async def _render_primary_alias_list(
    bot: Any, db: Database, chat_id: int, primary: PrimaryAccount
) -> Any:
    """Send a fresh per-primary alias keyboard.

    Used as the ``after`` callback of :class:`TaskMessageTracker` so
    long-running tasks (``/cekimap``, ``/genaddr``, sync) collapse all
    their transient progress messages into a single canonical "back to
    /list" view for the primary that just finished. The message
    intentionally mirrors the inline keyboard rendered by the
    per-primary callback in :func:`on_callback` (drill-down view) so
    the user lands somewhere they already recognise.
    """
    try:
        aliases = await db.list_aliases(chat_id, primary_id=primary.id)
    except Exception:
        LOGGER.debug(
            "render_primary_alias_list: list_aliases failed", exc_info=True
        )
        return None
    active = await db.get_active_alias(chat_id)
    active_alias_id = active.id if active and active.primary_id == primary.id else None
    try:
        labels_by_alias = await db.get_alias_service_labels(
            chat_id, primary_id=primary.id
        )
    except Exception:
        LOGGER.debug(
            "get_alias_service_labels failed; falling back to empty",
            exc_info=True,
        )
        labels_by_alias = {}
    return await bot.send_message(
        chat_id=chat_id,
        text=_build_alias_list_text(
            primary, aliases, labels_by_alias, active_alias_id
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_alias_keyboard_for_primary(
            primary, aliases, active_alias_id
        ),
    )


async def _send_connect_log(
    update: Update,
    tracker_or_context: TaskMessageTracker | ContextTypes.DEFAULT_TYPE | None,
    text: str,
    **kwargs: Any,
) -> Any:
    """Send a /connect status line and (optionally) register it for cleanup.

    The second positional argument accepts either a
    :class:`TaskMessageTracker` (legacy) *or* the handler's
    ``ContextTypes.DEFAULT_TYPE`` (preferred). With the context we
    can also:

    * **Dedupe duplicate log lines** — some state-machine paths
      (e.g. user mistypes the email, gets re-prompted, then types
      it correctly) previously caused identical "Auto-isi: …" /
      "Step 1/2 …" messages to land in the chat twice. We compare
      against the *previous* message's text on a per-flow basis; an
      immediate retransmission with the same body is suppressed and
      the already-posted message is returned instead.
    * **Re-anchor the status keyboard** — the ``StatusReporter`` was
      originally attached to the dedicated anchor message that
      ``_ensure_connect_progress`` sends first. As more log messages
      piled up below it the user had to scroll up to see the live
      button ("lho ko disitu selalu berada di paling baru tombol
      bridge account"). After every new log line we now
      ``relocate()`` the keyboard to the freshly-sent message so the
      live button always sits right above the input field.

    When called with ``None`` or a tracker-only argument (legacy
    error-only branches or test helpers without a context) the
    helper degrades to a plain ``reply_text`` so persistent error
    messages don't accidentally get scheduled for deletion.
    """
    tracker: TaskMessageTracker | None = None
    user_data: dict[str, Any] | None = None
    if isinstance(tracker_or_context, TaskMessageTracker):
        tracker = tracker_or_context
    elif tracker_or_context is not None:
        ud = getattr(tracker_or_context, "user_data", None)
        if isinstance(ud, dict):
            user_data = ud
            maybe_tracker = ud.get("connect_log_tracker")
            if isinstance(maybe_tracker, TaskMessageTracker):
                tracker = maybe_tracker

    if user_data is not None:
        last_text: str | None = user_data.get("connect_last_log_text")
        last_msg = user_data.get("connect_last_log_msg")
        if last_text == text and last_msg is not None:
            # Identical body just sent — return the existing message
            # so the caller's tracker/relocate logic is a no-op.
            return last_msg

    msg = await update.effective_message.reply_text(  # type: ignore[union-attr]
        text, **kwargs
    )
    if tracker is not None:
        tracker.track(msg)

    if user_data is not None:
        user_data["connect_last_log_text"] = text
        user_data["connect_last_log_msg"] = msg
        status: StatusReporter | None = user_data.get("connect_status_reporter")
        if status is not None and msg is not None:
            new_id = getattr(msg, "message_id", None)
            if new_id is not None:
                with contextlib.suppress(Exception):
                    await status.relocate(new_id)

    return msg


async def _ensure_connect_progress(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[TaskMessageTracker | None, StatusReporter | None]:
    """Get or create the /connect-flow tracker + live status reporter.

    The /connect flow spans 4-6 handlers (``cmd_connect`` → ``connect_email``
    → ``connect_password`` → maybe ``connect_recovery_verify`` /
    ``connect_bridge_captcha`` → ``_finalize_connect``) and can take 60-180s
    end-to-end. The user complained that all the prompt messages
    ("Tambah akun Proton baru…", "Step 1/2…", "Auto-isi…", "Step 2/2…")
    pile up in the chat after success ("hapus yang saya pilih ketika
    sudah berhasil"), and that without a live indicator they cannot
    tell whether the bot is still working or has silently died ("berikan
    progress bar dibawah sedang melakukan apa bot nya jadi user tidak
    mengira bot sudah selesai").

    Both problems share the same lifecycle: a single tracker eagerly
    created at the top of ``cmd_connect`` collects every transient
    prompt for cleanup at the end of ``_finalize_connect``, while a
    status anchor message — sent right after the tracker — exposes a
    no-op inline button whose label rotates through the current
    phase ("⏳ Tunggu input email", "🔐 Login Bridge…", "🧪 Smoke
    test 1/3…", "✅ Selesai"). The status anchor itself is registered
    with the tracker so it disappears with the rest of the log.

    Idempotent: calling this from a later handler in the same flow
    returns the same pair stored in ``user_data`` so progress updates
    in ``connect_email`` / ``_finalize_connect`` edit the same anchor
    instead of starting a fresh series.
    """
    user_data = cast(dict, context.user_data)
    tracker = user_data.get("connect_log_tracker")
    status = user_data.get("connect_status_reporter")
    chat = update.effective_chat
    bot = getattr(context, "bot", None)
    if chat is None or bot is None:
        return tracker, status

    if tracker is None:
        tracker = TaskMessageTracker(bot, chat.id)
        user_data["connect_log_tracker"] = tracker

    if status is None:
        initial_label = "⏳ Mempersiapkan flow /connect…"
        try:
            anchor = await update.effective_message.reply_text(  # type: ignore[union-attr]
                "🔌 <b>Proses /connect berjalan</b>\n"
                "<i>Tombol di bawah memperlihatkan tahap yang sedang "
                "dikerjakan bot. Selama tombol berputar (label berubah-"
                "ubah), jangan kira bot mati — tunggu sampai akhir flow "
                "(setelah smoke test sukses) baru tombol berubah jadi "
                "✅ Selesai.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=build_status_keyboard(initial_label),
            )
        except Exception:
            LOGGER.debug("connect: failed to send status anchor", exc_info=True)
            return tracker, status
        tracker.track(anchor)
        # ``initial_label`` seeds StatusReporter._last_label so the very
        # first :meth:`relocate` (fired by ``_send_connect_log`` when
        # the next message lands in the chat) doesn't fall back to the
        # idle "✅ Selesai" label and confuse the user mid-flow.
        status = StatusReporter(
            bot,
            chat.id,
            anchor.message_id,
            initial_label=initial_label,
        )
        user_data["connect_status_reporter"] = status

    return tracker, status


async def _set_connect_status(
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
    *,
    force: bool = False,
) -> None:
    """Update the /connect status button if a reporter is active.

    Best-effort: missing reporter (legacy path, test harness without a
    live ``Bot``) is silently ignored. Telegram rate-limits and
    "message not modified" errors are already swallowed inside
    :class:`StatusReporter`.
    """
    user_data = cast(dict, getattr(context, "user_data", None) or {})
    status: StatusReporter | None = user_data.get("connect_status_reporter")
    if status is None:
        return
    with contextlib.suppress(Exception):
        await status.update(label, force=force)


async def _close_connect_status(
    context: ContextTypes.DEFAULT_TYPE,
    label: str | None = None,
) -> None:
    """Mark the /connect status reporter done and drop it from user_data.

    Called from the cleanup paths in ``_finalize_connect`` and the
    error branches that return ``ConversationHandler.END`` early. The
    reporter's anchor message is also tracked by the cleanup tracker,
    so it gets deleted by ``tracker.cleanup`` shortly after — but
    transitioning the button to a terminal label first means the user
    sees "✅ Selesai" / "⚠️ Soft-fail" briefly before the message
    vanishes, instead of a stale "🔌 Login…" label.
    """
    user_data = cast(dict, getattr(context, "user_data", None) or {})
    status: StatusReporter | None = user_data.pop("connect_status_reporter", None)
    if status is None:
        return
    with contextlib.suppress(Exception):
        await status.done(label)


async def _maybe_send_lock_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Post a transient lock-active reminder if the chat has an active alias.

    Hooked into the tail of the main commands (``/list``, ``/cekimap``,
    ``/genaddr``, ``/history``, ``/accounts``) so the user is gently
    nudged about the still-locked alias every time they interact with
    the bot. The reminder carries two action buttons:

      * ``🔓 Unlock`` (``CB_LOCK_REMINDER_UNLOCK``) — release the lock.
      * ``🔄 Ganti alias`` (``CB_LOCK_REMINDER_PICK_NEW``) — re-render
        the primary list so they can pick a different alias.

    To avoid stacking, we delete the previous reminder for this chat
    (tracked in ``chat_data['lock_reminder_msg_id']``) before posting a
    new one. Failures to delete the prior reminder are silently
    swallowed because the previous message may have already aged out
    of Telegram's 48 h delete window.

    Best-effort: every Telegram call is wrapped so a transient API
    failure does NOT propagate up and break the host command's normal
    completion path.
    """
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    try:
        active = await db.get_active_alias(chat.id)
    except Exception:
        LOGGER.exception("lock reminder: failed to read active alias")
        return
    if active is None:
        return

    chat_data = cast(dict, context.chat_data)
    prev_id = chat_data.pop("lock_reminder_msg_id", None)
    if prev_id is not None:
        with contextlib.suppress(Exception):
            await context.bot.delete_message(
                chat_id=chat.id, message_id=prev_id
            )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔓 Unlock", callback_data=CB_LOCK_REMINDER_UNLOCK
                ),
                InlineKeyboardButton(
                    "🔄 Ganti alias",
                    callback_data=CB_LOCK_REMINDER_PICK_NEW,
                ),
            ]
        ]
    )
    text = (
        f"🔒 Alias <code>{html.escape(active.email)}</code> masih aktif. "
        "Email yang masuk akan diforward ke sini sampai kamu unlock."
    )
    try:
        msg = await update.effective_message.reply_text(  # type: ignore[union-attr]
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        LOGGER.exception("lock reminder: failed to send")
        return
    msg_id = getattr(msg, "message_id", None)
    if isinstance(msg_id, int):
        chat_data["lock_reminder_msg_id"] = msg_id


async def _show_primary_list(
    update: Update, db: Database, chat_id: int
) -> None:
    """Render the top-level primary keyboard. Used by /list and /start."""
    primaries = await db.list_primary_accounts(chat_id)
    counts = await _alias_count_per_primary(db, chat_id, primaries)
    healthcheck_stats = await db.get_last_healthcheck_stats(chat_id)
    active = await db.get_active_alias(chat_id)
    active_primary_id = active.primary_id if active else None
    if primaries:
        header = f"📧 Email utama kamu ({len(primaries)}):"
    else:
        header = (
            "Belum ada email utama yang terdaftar. "
            "Kirim /connect untuk menambahkan akun Proton + Bridge."
        )
    if active is not None:
        # Render the active alias inside ``<code>`` so Telegram mobile clients
        # treat it as tap-to-copy — same UX as the per-message email forward
        # header. The "📋 Copy email aktif" button below is the desktop
        # equivalent (since desktop clients don't expose tap-to-copy on
        # ``<code>`` blocks).
        header += (
            f"\n🔒 Alias aktif: <code>{html.escape(active.email)}</code>"
            " — kirim /unlock untuk lepas."
        )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        header,
        reply_markup=_build_primary_keyboard(
            primaries,
            counts,
            active_primary_id,
            healthcheck_stats=healthcheck_stats,
            with_copy_active_button=active is not None,
        ),
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------- /start


def _greeting(update: Update) -> str:
    """Build a personalised opening line for /start.

    Prefers the Telegram first name (more conversational), falls back to
    @username, then a plain ``Halo!``. Keeping this in one place so we
    don't drift between handlers.
    """
    user = update.effective_user
    if user is not None:
        if user.first_name:
            return f"Halo, <b>{html.escape(user.first_name)}</b>!"
        if user.username:
            return f"Halo, <b>@{html.escape(user.username)}</b>!"
    return "Halo!"


@_gate
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    await db.upsert_user(chat.id)
    quickstart = (
        "\n\n<b>Cara pakai (4 langkah):</b>\n"
        "1️⃣  Buka Proton Bridge di server &amp; login akun Proton-mu.\n"
        "2️⃣  Kirim <code>/connect</code> — masukkan email + password "
        "Bridge, sisanya otomatis.\n"
        "3️⃣  Kirim <code>/setprotonpw</code> — simpan password master "
        "Proton (sekali aja, dipakai /genaddr).\n"
        "4️⃣  Kirim <code>/genaddr vielz 10</code> — buat 10 alamat "
        "<code>vielz001..vielz010</code> otomatis."
    )
    commands_help = (
        "\n\n<b>Perintah yang tersedia:</b>\n"
        "/start — Tampilkan pesan ini\n"
        "/connect — Setup kredensial IMAP Proton Bridge\n"
        "/disconnect — Hapus kredensial dan stop listener\n"
        "/sync &lt;user&gt; &lt;pass&gt; — Auto-sync alias dari akun Proton\n"
        "/accounts — Daftar akun Proton (sama dgn /list)\n"
        "/setprotonpw — Simpan password master Proton untuk /genaddr\n"
        "/genaddr &lt;base&gt; &lt;count&gt; [@domain] — Otomatis buat N alamat lewat browser\n"
        "/addalias — Tambah alias secara manual\n"
        "/removealias — Hapus alias\n"
        "/list — Lihat semua alias dan pilih yang aktif\n"
        "/unlock — Lepas kunci alias yang sedang aktif\n"
        "/history — Lihat alias yang sudah terpakai\n"
        "/reset — Kembalikan alias ke daftar tersedia\n"
        "/cekimap — Cek IMAP listener semua alias (jalan di background)\n"
        "/services — Atur label service (mis. cognition.ai → Devin)\n"
        "/aliasinfo &lt;email&gt; — Lihat history pengirim alias tertentu\n"
        "/cancel — Batalkan dialog /connect"
    )
    greeting = _greeting(update)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"{greeting} Aku akan memberitahumu kalau ada email masuk "
            "ke alias Proton-mu."
            + quickstart
            + commands_help,
            parse_mode=ParseMode.HTML,
        )
        return
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"{greeting} Berikut akun Proton-mu yang sudah terdaftar."
        + quickstart
        + commands_help,
        parse_mode=ParseMode.HTML,
    )
    await _show_primary_list(update, db, chat.id)


# --------------------------------------------------------------- /list


@_gate
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    await _show_primary_list(update, db, chat.id)
    await _maybe_send_lock_reminder(update, context)


@_gate
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    consumed = await db.list_aliases(chat.id, status=AliasStatus.CONSUMED)
    if not consumed:
        await update.effective_message.reply_text("Belum ada alias yang sudah terpakai.")  # type: ignore[union-attr]
        await _maybe_send_lock_reminder(update, context)
        return
    lines = ["Alias yang sudah terpakai:"]
    for alias in consumed:
        when = alias.consumed_at or "?"
        lines.append(f"• {alias.email} ({when})")
    lines.append("\nUntuk mengaktifkan kembali: /reset <email>")
    await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]
    await _maybe_send_lock_reminder(update, context)


# --------------------------------------------------------------- /services


def _build_services_keyboard(
    user_labels: list[tuple[str, str]],
) -> InlineKeyboardMarkup | None:
    """One row per user-defined label with a 🗑 button to delete it.

    Defaults from :data:`db.DEFAULT_SERVICE_LABELS` are NOT listed here
    — they're an implicit fallback the user can override by saving
    their own ``domain → label`` row. Rows with an *empty* label
    represent an explicit suppression (the user tapped "🗑 Hapus label"
    on a forwarded email) and are rendered with a 🚫 icon so the user
    can clearly tell them apart from named mappings. ``None`` means the
    user has no custom mappings yet.
    """
    if not user_labels:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for domain, label in user_labels:
        is_suppressed = not (label or "").strip()
        display = "(disembunyikan)" if is_suppressed else label
        icon = "🚫" if is_suppressed else "🗑"
        callback = f"{CB_SVC_DELETE}:{domain}"
        if len(callback.encode()) > _TG_CALLBACK_DATA_LIMIT:
            # Domain too long to round-trip safely; show as informational.
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{display}  ·  {domain}", callback_data=CB_NOOP
                    )
                ]
            )
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {display}  ·  {domain}", callback_data=callback
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


@_gate
async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List/edit chat-level domain → service-label mappings.

    Forms:
      /services                     — list current mappings
      /services <domain> = <label>  — upsert a mapping
      /services del <domain>        — remove a mapping
    """
    chat = update.effective_chat
    if chat is None or update.effective_message is None:
        return
    db = _bot_db(context)
    args_text = " ".join(context.args or []).strip() if context.args else ""

    if args_text:
        lower = args_text.lower()
        if lower.startswith("del ") or lower.startswith("delete "):
            target = args_text.split(None, 1)[1].strip().lower()
            removed = await db.remove_service_label(chat.id, target)
            if removed:
                await update.effective_message.reply_text(
                    f"🗑 Mapping untuk <code>{html.escape(target)}</code> dihapus.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.effective_message.reply_text(
                    f"Mapping untuk <code>{html.escape(target)}</code> tidak ada.",
                    parse_mode=ParseMode.HTML,
                )
            return
        if "=" in args_text:
            domain_part, label_part = args_text.split("=", 1)
            domain = domain_part.strip().lower()
            label = label_part.strip()
            if not domain or not label:
                await update.effective_message.reply_text(
                    "Format: <code>/services domain = label</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await db.set_service_label(chat.id, domain, label)
            await update.effective_message.reply_text(
                f"✅ <code>{html.escape(domain)}</code> → "
                f"<b>{html.escape(label)}</b> tersimpan.",
                parse_mode=ParseMode.HTML,
            )
            return
        await update.effective_message.reply_text(
            "Format yang dikenali:\n"
            "• <code>/services</code> — lihat semua mapping\n"
            "• <code>/services domain = Label</code> — simpan / ubah\n"
            "• <code>/services del domain</code> — hapus",
            parse_mode=ParseMode.HTML,
        )
        return

    user_labels = await db.list_service_labels(chat.id)
    intro = (
        "🏷️ <b>Mapping domain → service label</b>\n"
        "Setiap email yang masuk ke alias akan diberi label sesuai "
        "domain pengirimnya. Default sudah berisi <i>cognition.ai → "
        "Devin</i>, <i>github.com → GitHub</i>, dst (lihat dokumentasi).\n\n"
    )
    if user_labels:
        intro += "<b>Mapping kamu:</b>\n"
        rendered_lines: list[str] = []
        for d, label in user_labels:
            stripped = (label or "").strip()
            if stripped:
                rendered_lines.append(
                    f"• <code>{html.escape(d)}</code> → "
                    f"<b>{html.escape(stripped)}</b>"
                )
            else:
                # Empty-string row → explicit suppression via 🗑 button.
                rendered_lines.append(
                    f"• <code>{html.escape(d)}</code> → "
                    f"<i>🚫 disembunyikan</i>"
                )
        intro += "\n".join(rendered_lines)
        intro += (
            "\n\nTap tombol di bawah untuk menghapus. "
            "Untuk menambah/ubah: <code>/services domain = Label</code>."
        )
    else:
        intro += (
            "<i>Belum ada mapping kustom.</i>\n\n"
            "Tambah dengan: <code>/services domain = Label</code>\n"
            "Contoh: <code>/services roboneo.com = Roboneo</code>"
        )
    await update.effective_message.reply_text(
        intro,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_services_keyboard(user_labels),
    )


# --------------------------------------------------------------- /aliasinfo


@_gate
async def cmd_aliasinfo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the sender history for one alias.

    Usage: ``/aliasinfo <alias_email>``. Lists every domain that has
    sent mail to this alias, with the resolved service label, first/
    last-seen timestamps, and the number of messages.
    """
    chat = update.effective_chat
    if chat is None or update.effective_message is None:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Format: <code>/aliasinfo &lt;email_alias&gt;</code>\n"
            "Contoh: <code>/aliasinfo vielz008@proton.me</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    db = _bot_db(context)
    target = context.args[0].strip().lower()
    alias = await db.find_alias(chat.id, target)
    if alias is None:
        await update.effective_message.reply_text(
            f"Alias <code>{html.escape(target)}</code> tidak ditemukan.",
            parse_mode=ParseMode.HTML,
        )
        return
    senders = await db.list_alias_senders(chat.id, alias.id)
    if not senders:
        await update.effective_message.reply_text(
            f"Alias <code>{html.escape(alias.email)}</code> belum pernah "
            "menerima email (atau email-nya datang sebelum fitur tracking aktif).",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = [
        f"📧 <b>{html.escape(alias.email)}</b>",
        f"Total <b>{len(senders)}</b> domain pengirim:",
        "",
    ]
    for sender in senders:
        # Re-resolve so a /services edit reflects without a backfill.
        label = (
            await db.resolve_service_label(chat.id, sender.sender_domain)
            or sender.sender_domain
        )
        line = (
            f"• 🏷️ <b>{html.escape(label)}</b>  ·  "
            f"<code>{html.escape(sender.sender_domain)}</code>\n"
            f"  {sender.seen_count}× — terakhir {sender.last_seen_at}"  # noqa: RUF001
        )
        lines.append(line)
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------- /cleanmail


@_gate
async def cmd_cleanmail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """One-shot cleanup of legacy email forwards in the chat.

    The auto-purge on alias-switch only knows about messages forwarded
    *after* the feature was deployed (the bot writes to
    ``forwarded_emails`` then). For chats that already accumulated lots
    of stale forwards before the feature shipped — or for any other
    bot-sent message we want to wipe — this command walks message ids
    backward from the ``/cleanmail`` invocation itself and best-effort
    calls ``delete_message`` on each. Telegram silently rejects deletes
    on user-sent messages and on messages older than 48h, so the worst
    case is "did nothing" rather than data loss.

    Usage:
      /cleanmail            — try the last 100 message ids
      /cleanmail <count>    — try the last <count> ids (capped at 500)
    """
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return
    requested = 100
    if context.args:
        try:
            requested = int(context.args[0])
        except (TypeError, ValueError):
            await msg.reply_text(
                "Format: <code>/cleanmail [jumlah]</code>\n"
                "Contoh: <code>/cleanmail 200</code>",
                parse_mode=ParseMode.HTML,
            )
            return
    # Cap so we don't accidentally hammer the Bot API. 500 ids ≈ ~17s
    # of API calls under the default rate limit of ~30/sec.
    requested = max(1, min(requested, 500))
    bot = context.application.bot
    db = _bot_db(context)
    cleanmail_msg_id = msg.message_id
    progress = await msg.reply_text(
        f"🧹 Membersihkan {requested} pesan terakhir… (best-effort)"
    )
    deleted = 0
    failed = 0
    # Iterate from the most recent id backward (skip the cleanmail
    # command and progress reply themselves so the user keeps a
    # confirmation in chat).
    skip_ids = {cleanmail_msg_id, progress.message_id}
    for offset in range(1, requested + 1):
        candidate = cleanmail_msg_id - offset
        if candidate <= 0 or candidate in skip_ids:
            continue
        try:
            await bot.delete_message(chat_id=chat.id, message_id=candidate)
            deleted += 1
        except Exception:
            failed += 1
    # Pop any remaining forwarded_emails rows for this chat too — those
    # message ids were either inside the deletion window above or they
    # were already wiped by an earlier alias-switch. Either way the
    # bookkeeping should match the chat state.
    await db.conn.execute(
        "DELETE FROM forwarded_emails WHERE chat_id = ?", (chat.id,)
    )
    await db.conn.commit()
    try:
        await progress.edit_text(
            f"🧹 Selesai: dihapus <b>{deleted}</b>, dilewati <b>{failed}</b> "
            f"(milik user, &gt;48 jam, atau bukan dari bot).\n"
            f"Tabel <code>forwarded_emails</code> juga di-reset untuk chat ini.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # If the progress message itself was caught in the sweep, just
        # send a fresh one.
        await msg.reply_text(
            f"🧹 Selesai: dihapus {deleted}, dilewati {failed}."
        )


# ----------------------------------------------- /tag-service conversation


async def tag_service_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point for the ``🏷️ Tag ulang`` button.

    Stashes ``(alias_id, sender_domain)`` in ``chat_data`` so the
    follow-up text reply can persist the new label without re-parsing
    the original callback payload (already consumed by Telegram).
    """
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Data tag tidak valid.", show_alert=True)
        return ConversationHandler.END
    try:
        alias_id = int(parts[1])
    except ValueError:
        await query.answer("Alias id tidak valid.", show_alert=True)
        return ConversationHandler.END
    domain = parts[2].strip().lower()
    if not domain:
        await query.answer("Domain pengirim kosong.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return ConversationHandler.END
    db = _bot_db(context)
    current = await db.resolve_service_label(chat_id, domain)
    context.chat_data["pending_service_tag"] = {
        "alias_id": alias_id,
        "domain": domain,
    }
    prompt = (
        f"🏷️ Set label untuk domain <code>{html.escape(domain)}</code>.\n"
        f"Saat ini: <b>{html.escape(current or '(belum ada)')}</b>.\n\n"
        "Balas dengan label baru (mis. <code>Devin</code>, <code>Roboneo</code>, "
        "<code>Skip — work</code>). Kirim /cancel untuk batal."
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        parse_mode=ParseMode.HTML,
    )
    return TAG_AWAIT_LABEL


async def tag_service_label_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Persist the user's chosen label and end the conversation."""
    if update.effective_message is None or update.effective_chat is None:
        return ConversationHandler.END
    pending = context.chat_data.pop("pending_service_tag", None)
    if not isinstance(pending, dict):
        return ConversationHandler.END
    label = (update.effective_message.text or "").strip()
    if not label:
        await update.effective_message.reply_text(
            "Label kosong, batal. Coba lagi via tombol 🏷️ di email."
        )
        return ConversationHandler.END
    domain = pending["domain"]
    db = _bot_db(context)
    await db.set_service_label(update.effective_chat.id, domain, label)
    await update.effective_message.reply_text(
        f"✅ <code>{html.escape(domain)}</code> → "
        f"<b>{html.escape(label)}</b> tersimpan. Email berikutnya dari "
        "domain ini akan otomatis pakai label ini.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# --------------------------------------------------------------- /addalias


@_gate
async def cmd_addalias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    args_text = " ".join(context.args or []) if context.args else ""
    if not args_text and update.effective_message and update.effective_message.text:
        # Allow newline-separated input following the command on subsequent lines.
        text = update.effective_message.text.split(None, 1)
        args_text = text[1] if len(text) > 1 else ""
    if not args_text:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Pakai: /addalias email1@domain.com email2@domain.com\n"
            "Boleh juga dipisah newline."
        )
        return
    candidates = [
        part.strip()
        for chunk in args_text.replace(",", " ").splitlines()
        for part in chunk.split()
        if part.strip()
    ]
    valid = [c for c in candidates if "@" in c and "." in c.split("@", 1)[1]]
    if not valid:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Tidak ada email yang valid dikenali."
        )
        return
    db = _bot_db(context)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Belum ada email utama. Kirim /connect dulu untuk tambah akun Proton."
        )
        return
    target_primary = primaries[0]
    if len(primaries) > 1:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Ada {len(primaries)} email utama — alias ini akan ditambahkan ke "
            f"<b>{html.escape(target_primary.email)}</b> (akun pertama). "
            "Untuk pindah ke akun lain pakai /list dulu, atau hapus + tambah "
            "ulang via akun yang dimaksud.",
            parse_mode=ParseMode.HTML,
        )
    inserted = await db.add_aliases(chat.id, valid, primary_id=target_primary.id)
    skipped = len(valid) - inserted
    msg = (
        f"Ditambahkan: {inserted} alias ke "
        f"<b>{html.escape(target_primary.email)}</b>."
    )
    if skipped:
        msg += f" Sudah ada sebelumnya: {skipped}."
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        msg,
        parse_mode=ParseMode.HTML,
    )
    await _show_primary_list(update, db, chat.id)


@_gate
async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fetch addresses from the Proton API and add them as aliases."""
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Pakai: /sync <username_proton> <password_proton>\n"
            "Contoh: /sync vielz43 passwordku\n\n"
            "Username dan password akun Proton (bukan Bridge)."
        )
        return ConversationHandler.END
    username, password = args[0], " ".join(args[1:])
    # Anchor a live status button: legacy /sync hits Proton SRP +
    # CAPTCHA + address fetch and routinely takes 5-30s, so a static
    # "Menghubungi Proton API..." line leaves the user wondering
    # whether the bot is still working. We stash the reporter on
    # ``user_data`` so the CAPTCHA continuation handler
    # (sync_captcha_done) and the shared finaliser (_sync_complete)
    # can keep editing the same anchor instead of spawning duplicates.
    user_data = cast(dict, context.user_data)
    sync_status: StatusReporter | None = None
    sync_initial_label = "⏳ Mempersiapkan sync Proton…"
    try:
        anchor = await update.effective_message.reply_text(  # type: ignore[union-attr]
            "🔄 <b>Sync alamat Proton</b>\n"
            "<i>Tombol di bawah memperlihatkan tahap yang sedang "
            "dikerjakan bot.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_status_keyboard(sync_initial_label),
        )
    except Exception:
        anchor = None
    if anchor is not None:
        sync_status = StatusReporter(
            context.application.bot,
            chat.id,
            anchor.message_id,
            initial_label=sync_initial_label,
        )
        user_data["legacy_sync_status_reporter"] = sync_status

    if sync_status is not None:
        with contextlib.suppress(Exception):
            await sync_status.update("🌐 Login Proton API (SRP)…")
    try:
        from .proton_api import CaptchaChallenge, start_auth

        result = await asyncio.to_thread(start_auth, username, password)
    except Exception as exc:
        LOGGER.exception("proton API sync failed")
        if sync_status is not None:
            with contextlib.suppress(Exception):
                await sync_status.done("❌ Login Proton gagal")
            user_data.pop("legacy_sync_status_reporter", None)
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal mengambil alamat dari Proton: {exc}"
        )
        return ConversationHandler.END

    if isinstance(result, CaptchaChallenge):
        user_data["sync_challenge"] = result
        if sync_status is not None:
            with contextlib.suppress(Exception):
                await sync_status.update(
                    "⏳ Tunggu user selesaikan CAPTCHA Proton…"
                )
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Proton memerlukan verifikasi CAPTCHA.\n\n"
            f"1. Buka link ini di browser:\n{result.web_url}\n\n"
            "2. Selesaikan CAPTCHA\n"
            "3. Setelah selesai, kirim 'done' di sini.\n\n"
            "Kirim /cancel untuk membatalkan.",
        )
        return SYNC_CAPTCHA

    return await _sync_complete(update, context, result)


async def sync_captcha_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user confirming CAPTCHA is solved."""
    user_data = cast(dict, context.user_data)
    challenge = user_data.pop("sync_challenge", None)
    sync_status: StatusReporter | None = user_data.get(
        "legacy_sync_status_reporter"
    )

    async def _status(label: str) -> None:
        if sync_status is None:
            return
        with contextlib.suppress(Exception):
            await sync_status.update(label)

    async def _status_done(label: str) -> None:
        if sync_status is None:
            return
        with contextlib.suppress(Exception):
            await sync_status.done(label)
        user_data.pop("legacy_sync_status_reporter", None)

    if challenge is None:
        await _status_done("❌ Sesi /sync kedaluwarsa")
        await update.effective_message.reply_text("Sesi sync sudah kedaluwarsa. Coba /sync lagi.")  # type: ignore[union-attr]
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip().lower()  # type: ignore[union-attr]
    if text == "done":
        # User solved CAPTCHA on the web page; retry with the original token
        await _status("🔁 Retry autentikasi setelah CAPTCHA…")
        await update.effective_message.reply_text("Mencoba ulang autentikasi...")  # type: ignore[union-attr]
        try:
            from .proton_api import complete_auth_with_captcha

            session = await asyncio.to_thread(
                complete_auth_with_captcha, challenge, challenge.token
            )
        except Exception as exc:
            LOGGER.exception("CAPTCHA auth retry failed")
            await _status_done("❌ Auth setelah CAPTCHA gagal")
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                f"Gagal setelah CAPTCHA: {exc}\nCoba /sync lagi."
            )
            return ConversationHandler.END
        return await _sync_complete(update, context, session)

    # User sent a captcha response token directly
    await _status("🔁 Verifikasi token CAPTCHA…")
    await update.effective_message.reply_text("Memverifikasi token CAPTCHA...")  # type: ignore[union-attr]
    try:
        from .proton_api import complete_auth_with_captcha

        session = await asyncio.to_thread(complete_auth_with_captcha, challenge, text)
    except Exception as exc:
        LOGGER.exception("CAPTCHA token auth failed")
        await _status_done("❌ Token CAPTCHA invalid")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Token CAPTCHA tidak valid: {exc}\nCoba /sync lagi."
        )
        return ConversationHandler.END
    return await _sync_complete(update, context, session)


async def _sync_complete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: object
) -> int:
    """Finish the /sync flow: fetch addresses and add as aliases."""
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    try:
        from .proton_api import fetch_addresses_from_session

        addresses = await asyncio.to_thread(fetch_addresses_from_session, session)
    except Exception as exc:
        LOGGER.exception("address fetch failed")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal mengambil alamat: {exc}"
        )
        return ConversationHandler.END
    if not addresses:
        await update.effective_message.reply_text("Tidak ada alamat aktif di akun Proton.")  # type: ignore[union-attr]
        return ConversationHandler.END
    db = _bot_db(context)
    primaries = await db.list_primary_accounts(chat.id)
    target_primary_id = primaries[0].id if primaries else None
    inserted = await db.add_aliases(
        chat.id, addresses, primary_id=target_primary_id
    )
    total = len(addresses)
    skipped = total - inserted
    msg = f"Sync selesai! Ditemukan {total} alamat.\nDitambahkan: {inserted}."
    if skipped:
        msg += f" Sudah ada: {skipped}."
    if primaries:
        msg += f"\nDilampirkan ke: <b>{html.escape(primaries[0].email)}</b>"
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        msg,
        parse_mode=ParseMode.HTML,
    )
    await _show_primary_list(update, db, chat.id)
    return ConversationHandler.END


@_gate
async def cmd_removealias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or not context.args:
        await update.effective_message.reply_text("Pakai: /removealias email@domain.com")  # type: ignore[union-attr]
        return
    db = _bot_db(context)
    removed_any = False
    for email in context.args:
        if await db.remove_alias(chat.id, email):
            removed_any = True
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Alias dihapus." if removed_any else "Alias tidak ditemukan."
    )


@_gate
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or not context.args:
        await update.effective_message.reply_text("Pakai: /reset email@domain.com")  # type: ignore[union-attr]
        return
    db = _bot_db(context)
    if await db.reset_alias(chat.id, context.args[0]):
        await update.effective_message.reply_text("Alias kembali ke daftar tersedia.")  # type: ignore[union-attr]
    else:
        await update.effective_message.reply_text("Alias tidak ditemukan.")  # type: ignore[union-attr]


# --------------------------------------------------------------- /connect (ConversationHandler)


async def _verify_bridge_login(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_ssl: bool,
    attempts: int = 1,
    backoff_seconds: float = 5.0,
    rate_limit_backoff_seconds: float = 75.0,
) -> tuple[bool, str]:
    """Try LOGIN against the user-supplied IMAP creds, with optional retries.

    Returns ``(ok, detail)``. We do this *before* persisting the row so a
    typo in the Bridge password fails loud — the previous flow happily
    saved bogus creds, then the listener would silently NONAUTH-loop
    every two minutes and the user just saw "no email arrives". Bridge
    runs locally so the round-trip cost is negligible.

    A freshly added account often needs a few seconds before its IMAP
    listener accepts logins, so callers running this immediately after
    ``bridge add_account`` should pass ``attempts > 1``. We retry on
    connection-level errors (TimeoutError, ConnectionRefused, ...),
    on Bridge's ``"too many login attempts"`` response (clears in
    ~60-75s on its own), and on Bridge's ``"no such user"`` response
    (which it emits while still loading the freshly-added user from
    vault into its in-memory IMAP user list — this can take a minute
    or so after the bridge service restart). Auth-level "Incorrect
    login credentials" responses are final and don't trigger a retry.
    """
    last_detail = ""
    transient_markers = ("too many login attempts", "no such user")
    for attempt in range(1, max(1, attempts) + 1):
        try:
            if use_ssl:
                client = aioimaplib.IMAP4_SSL(host=host, port=port, timeout=10)
            else:
                client = aioimaplib.IMAP4(host=host, port=port, timeout=10)
            try:
                await client.wait_hello_from_server()
                resp = await client.login(username, password)
                if resp.result != "OK":
                    detail = " | ".join(
                        line.decode("utf-8", "replace")
                        if isinstance(line, bytes)
                        else str(line)
                        for line in (resp.lines or [])
                    ) or resp.result
                    last_detail = detail
                    detail_lc = detail.lower()
                    is_transient = any(
                        marker in detail_lc for marker in transient_markers
                    )
                    if is_transient and attempt < attempts:
                        LOGGER.info(
                            "bridge IMAP login attempt %d/%d hit "
                            "transient error (%s); sleeping %ss before "
                            "retry",
                            attempt,
                            attempts,
                            detail,
                            rate_limit_backoff_seconds,
                        )
                        await asyncio.sleep(rate_limit_backoff_seconds)
                        continue
                    return False, detail
                try:
                    await client.logout()
                except Exception:
                    # Best-effort cleanup; LOGIN already succeeded so we don't
                    # care if LOGOUT errors out.
                    pass
                return True, ""
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        except Exception as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            LOGGER.info(
                "bridge IMAP login attempt %d/%d failed: %s",
                attempt,
                attempts,
                last_detail,
            )
            if attempt < attempts:
                await asyncio.sleep(backoff_seconds)
    return False, last_detail


def _connect_password_prompt(bridge_admin_on: bool) -> str:
    """Return the right Step 2 prompt depending on auto-add availability.

    With auto-add on, the bot accepts the user's *Proton account*
    password and handles Bridge enrolment internally — much friendlier
    than asking the user to copy a 22-character random string out of
    Bridge's GUI.
    """
    if bridge_admin_on:
        return (
            "Step 2/2 — kirim <b>password Proton akunmu</b> (yang biasa "
            "kamu pakai login di proton.me).\n"
            "Bot akan otomatis daftarkan akun ini ke Proton Bridge & "
            "ambil password IMAP-nya. Kalau ada CAPTCHA, link verifikasi "
            "akan dikirim ke chat ini.\n"
            "💡 <i>Hapus pesan password setelah bot konfirmasi sukses.</i>"
        )
    return (
        "Step 2/2 — kirim password IMAP Bridge (akan disimpan terenkripsi).\n"
        "💡 <i>Hapus pesan password setelah bot konfirmasi sukses.</i>"
    )


@_gate
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    db = _bot_db(context)
    existing = await db.list_primary_accounts(chat.id)
    bridge_admin_on = _bot_bridge_admin(context) is not None

    # Eagerly bring up the cleanup tracker + live status anchor so every
    # prompt message we emit below (intro, domain hint, Step 1/2, the
    # password prompt) is registered for the post-finalize cleanup AND
    # the user has a live "what is the bot doing" indicator from the
    # very first reply. See ``_ensure_connect_progress`` for rationale.
    _tracker, _status = await _ensure_connect_progress(update, context)

    if bridge_admin_on:
        intro = (
            "Tambah akun Proton baru.\n\n"
            "<b>Yang kamu butuhkan:</b>\n"
            "• Alamat email Proton (mis. <code>vielz43@proton.me</code>)\n"
            "• Password Proton akunmu (bukan password Bridge — bot urus "
            "Bridge-nya otomatis)\n\n"
            "Bot akan: daftarkan akun ke Bridge → ambil IMAP password "
            "otomatis → start listener. Kalau Proton minta CAPTCHA, "
            "linknya dikirim ke chat ini."
        )
    else:
        intro = (
            "Tambah akun Proton baru.\n\n"
            "<b>Yang kamu butuhkan:</b>\n"
            "• Alamat email Proton (mis. <code>vielz43@proton.me</code>)\n"
            "• Password IMAP yang di-generate Proton Bridge "
            "(<i>bukan</i> password Proton akunmu — ambil dari Proton Bridge "
            "→ akun → 'Mailbox details')\n\n"
            "Aku otomatis pakai default Bridge: "
            f"<code>{CONNECT_DEFAULT_HOST}:{CONNECT_DEFAULT_PORT}</code>, "
            "STARTTLS, username = email."
        )
    if existing:
        emails = ", ".join(p.email for p in existing)
        intro += (
            f"\n\nSudah terdaftar: <b>{html.escape(emails)}</b>. "
            "Kalau email yang sama dimasukkan ulang, kredensial-nya akan ditimpa."
        )
    await _send_connect_log(
        update,
        context,
        intro,
        parse_mode=ParseMode.HTML,
    )
    # Allow `/connect <email>` as a one-shot entry to skip the email prompt.
    # Accepts either a full address (``vielz@proton.me``) or just the
    # username (``vielz``); :func:`_resolve_connect_email` does the
    # auto-suffix and tells us whether to show the domain hint.
    args = list(context.args or [])
    if args and args[0].strip():
        email, was_suffixed = _resolve_connect_email(args[0])
        cast(dict, context.user_data)["primary_email"] = email
        if was_suffixed:
            await _send_connect_log(
                update,
                context,
                _connect_domain_hint_html(email),
                parse_mode=ParseMode.HTML,
            )
        await _send_connect_log(
            update,
            context,
            _connect_password_prompt(bridge_admin_on),
            parse_mode=ParseMode.HTML,
        )
        await _set_connect_status(context, "⏳ Tunggu input password Proton…")
        return CONNECT_PASSWORD
    await _send_connect_log(
        update,
        context,
        "Step 1/2 — kirim alamat email Proton-nya (mis. "
        "<code>vielz43@proton.me</code>):",
        parse_mode=ParseMode.HTML,
    )
    await _set_connect_status(context, "⏳ Tunggu input email Proton…")
    return CONNECT_EMAIL


async def connect_again_quick_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback entry point for the post-disconnect "🔌 Connect lagi"
    button. Acks the click, removes the keyboard from the disconnect
    confirmation message so it can't be re-fired, then delegates to
    :func:`cmd_connect` so step 1/2 lands in the chat exactly the same
    way as if the user had typed ``/connect``.

    The flag ``context.args = []`` is set explicitly so cmd_connect's
    "one-shot ``/connect <email>``" branch doesn't trip on a stale
    args list left by an earlier command.
    """
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("🔌 Mulai connect baru…", show_alert=False)
        except Exception:
            LOGGER.debug("query.answer failed for CB_CONNECT_AGAIN", exc_info=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            LOGGER.debug(
                "edit_message_reply_markup failed for CB_CONNECT_AGAIN",
                exc_info=True,
            )
    context.args = []
    return await cmd_connect(update, context)


async def connect_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # The tracker + status anchor were created in ``cmd_connect``; this
    # call retrieves them so the auto-suffix hint and Step 2/2 prompt
    # join the same cleanup batch and the live status indicator
    # transitions to "Tunggu input password" without spawning a second
    # anchor message.
    _tracker, _status = await _ensure_connect_progress(update, context)
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        await _send_connect_log(
            update,
            context,
            "Itu bukan alamat email yang valid. Masukkan email Proton-nya:",
        )
        return CONNECT_EMAIL
    # Accept bare username — :func:`_resolve_connect_email` auto-suffixes
    # ``@proton.me`` (the most common case). The domain hint message
    # below tells the user how to retry with a fallback domain
    # (``@protonmail.com`` / ``@pm.me``) if Proton rejects the login.
    email, was_suffixed = _resolve_connect_email(text)
    cast(dict, context.user_data)["primary_email"] = email
    if was_suffixed:
        await _send_connect_log(
            update,
            context,
            _connect_domain_hint_html(email),
            parse_mode=ParseMode.HTML,
        )
    await _send_connect_log(
        update,
        context,
        _connect_password_prompt(_bot_bridge_admin(context) is not None),
        parse_mode=ParseMode.HTML,
    )
    await _set_connect_status(context, "⏳ Tunggu input password Proton…")
    return CONNECT_PASSWORD


def _build_post_disconnect_keyboard() -> InlineKeyboardMarkup:
    """Single-button keyboard rendered after a successful disconnect.

    The user almost always disconnects an account because they want to
    swap to a different one — putting the "🔌 Connect lagi" button
    right below the confirmation message saves a typed ``/connect``
    command. The callback ``CB_CONNECT_AGAIN`` is wired into the
    ``connect_conv`` ConversationHandler's ``entry_points`` (alongside
    the regular ``CommandHandler("connect", …)``) so pressing it lands
    the user straight in step 1/2 of the flow.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔌 Connect lagi",
                    callback_data=CB_CONNECT_AGAIN,
                )
            ]
        ]
    )


def _topup_label_and_count(alias_count: int) -> tuple[str, int]:
    """Compute the recommended top-up size for the "✨ Generate …" button.

    User feedback: the old hardcoded "✨ Generate 20 alamat sekarang"
    button kept showing 20 even when the primary already had 11/20
    aliases — confusing because the user expected the button to
    recommend exactly *how many* more they still need ("kan kurang 9").

    Returns ``(label, missing)`` where ``missing`` is the number of
    aliases to generate to reach :data:`ALIAS_TARGET_PER_PRIMARY`.
    Caller decides whether to render a row at all — when ``missing``
    is 0 the button is hidden so the user doesn't get a no-op tap.
    """
    missing = max(0, ALIAS_TARGET_PER_PRIMARY - alias_count)
    if missing == 0:
        return ("", 0)
    if alias_count == 0:
        # Fresh account — keep the original "Generate N alamat
        # sekarang" wording so the empty-onboarding copy still reads
        # naturally for first-time users.
        return (f"✨ Generate {missing} alamat sekarang", missing)
    return (f"✨ Generate {missing} alamat lagi", missing)


def _build_post_connect_keyboard(
    primary_id: int,
    *,
    master_password_saved: bool = False,
    alias_count: int = 0,
) -> InlineKeyboardMarkup:
    """Quick-action buttons for an empty newly-connected primary.

    Used when the just-connected account has no real aliases yet (the
    user just made a fresh Proton account). Walks them through the
    setprotonpw → genaddr workflow without re-typing the email address.

    When ``master_password_saved`` is true (the auto-save in
    :func:`_finalize_connect` succeeded — request #9), the
    "🔐 Simpan password Proton" row is omitted because there's
    nothing left to save.

    ``alias_count`` parameterises the generate-button label so a
    "fresh" primary that already happened to import some aliases
    via auto-sync still gets the right top-up recommendation
    (matches the /list view).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not master_password_saved:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔐 Simpan password Proton",
                    callback_data=f"{CB_QUICK_SETPW}:{primary_id}",
                )
            ]
        )
    label, missing = _topup_label_and_count(alias_count)
    if missing > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_QUICK_GENADDR}:{primary_id}:{missing}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🩺 Cek IMAP listener (background)",
                callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _build_post_connect_keyboard_with_aliases(
    primary_id: int, alias_count: int
) -> InlineKeyboardMarkup:
    """Quick-action buttons for an existing-aliases newly-connected primary.

    Used when /connect lands on an account that already has aliases
    (auto-sync just imported them, or they were already in the DB from
    an earlier session). Surfaces three one-tap actions:

    1. Generate however-many random-suffix aliases are needed to top
       the account up to :data:`ALIAS_TARGET_PER_PRIMARY` (20).
       Examples: 11 aliases → "✨ Generate 9 alamat lagi"; 4 aliases
       → "✨ Generate 16 alamat lagi"; ≥20 aliases → row hidden so
       the user doesn't get a no-op tap. Same callback the
       fresh-account onboarding uses.
    2. Run the end-to-end IMAP listener health check.
    3. Open ``/list`` for this primary.
    """
    rows: list[list[InlineKeyboardButton]] = []
    label, missing = _topup_label_and_count(alias_count)
    if missing > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_QUICK_GENADDR}:{primary_id}:{missing}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                f"🩺 Cek IMAP listener semua {alias_count} alias",
                callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "📋 Buka /list", callback_data=f"{CB_PICK_PRIMARY}:{primary_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


SMTP_SMOKE_TIMEOUT_SECONDS = 60
# When the first smoke-test attempt times out, /connect retries a few
# times after a short settle window before giving up. Bridge is
# occasionally still warming up the SMTP listener (or DNS to the
# temp-mail provider hasn't fully propagated) the first time we hit
# it post add-account; the retries catch the common case without
# making the user re-run /connect from scratch.
SMTP_SMOKE_RETRY_DELAY_SECONDS = 30
# Bumped from 2 → 3 so we cover ~3 minutes of Bridge warm-up before
# considering the smoke test a soft failure.
SMTP_SMOKE_MAX_ATTEMPTS = 3
# Hard wall-clock cap for the entire smoke-test phase. Even with all
# retries combined, ``_finalize_connect`` can never spin in the smoke
# branch longer than this — wrapping in ``asyncio.wait_for`` makes it
# impossible for a hung SMTP socket / Mail.tm 500 to keep the
# /connect coroutine alive and freeze the user's view. Picked to be
# comfortably > MAX_ATTEMPTS * (TIMEOUT + RETRY_DELAY) so a healthy
# slow-warm-up still completes naturally.
SMTP_SMOKE_HARD_CAP_SECONDS = 360
BRIDGE_SMTP_PORT = 1025


def _smtp_send_test_message(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> None:
    """Synchronous SMTP send via Bridge. Wrapped in ``to_thread`` by callers."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)


async def _smoke_test_via_tempmail(
    *,
    email: str,
    imap_username: str,
    imap_password: str,
    tempmail: TempMailbox,
) -> bool:
    """Send a tagged test email via Bridge SMTP and confirm Mail.tm receives it.

    Bridge shares credentials between IMAP and SMTP, so a successful
    SMTP roundtrip is a strong proxy for "IMAP listener will work too".
    """
    token = secrets.token_hex(8)
    subject = f"[bot-smoke-test] {token}"
    body = f"Smoke test from {email}.\nToken: {token}\n"
    try:
        await asyncio.to_thread(
            _smtp_send_test_message,
            host=CONNECT_DEFAULT_HOST,
            port=BRIDGE_SMTP_PORT,
            username=imap_username,
            password=imap_password,
            from_addr=email,
            to_addr=tempmail.address,
            subject=subject,
            body=body,
        )
    except Exception as exc:
        LOGGER.warning("smoke-test SMTP send failed: %s", exc)
        return False

    poll_attempts = max(1, SMTP_SMOKE_TIMEOUT_SECONDS // 2)
    async with httpx.AsyncClient() as client:
        return await tempmail.wait_for_subject(
            client, token, max_attempts=poll_attempts, poll_interval=2.0
        )


async def _smoke_test_with_retry(
    *,
    email: str,
    imap_username: str,
    imap_password: str,
    tempmail: TempMailbox,
    on_retry: Callable[[int], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_attempts: int = SMTP_SMOKE_MAX_ATTEMPTS,
    retry_delay: float = SMTP_SMOKE_RETRY_DELAY_SECONDS,
) -> bool:
    """Run :func:`_smoke_test_via_tempmail` with up to ``max_attempts`` tries.

    Returns ``True`` on the first successful attempt, ``False`` after
    every attempt has timed out / errored. Between attempts we sleep
    ``retry_delay`` seconds and call ``on_retry(attempt_number)`` so
    the caller can post a user-visible "tunggu Xs lalu coba lagi"
    message — extracted from :func:`_finalize_connect` so the retry
    behaviour is unit-testable in isolation.
    """
    for attempt in range(1, max_attempts + 1):
        ok = await _smoke_test_via_tempmail(
            email=email,
            imap_username=imap_username,
            imap_password=imap_password,
            tempmail=tempmail,
        )
        if ok:
            return True
        if attempt == max_attempts:
            return False
        if on_retry is not None:
            with contextlib.suppress(Exception):
                await on_retry(attempt)
        await sleep(retry_delay)
    return False


async def _finalize_connect(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    email: str,
    imap_username: str,
    imap_password: str,
    smoke_test_tempmail: TempMailbox | None = None,
    pre_probe_settle_seconds: float = 0.0,
    tracker: TaskMessageTracker | None = None,
) -> int:
    """Common tail of /connect: verify, save, start listener, friendly reply.

    Both the legacy "user-typed-Bridge-password" path and the new
    "auto-extracted-from-Bridge-vault" path land here once we hold a
    plausible IMAP password.

    When ``smoke_test_tempmail`` is provided, run an end-to-end SMTP
    smoke test (send email -> tempmail) after the listener starts. On
    failure the freshly-added primary is rolled back so the user can
    cleanly ``/connect`` again instead of being stuck with broken creds.

    ``pre_probe_settle_seconds`` introduces a wait *before* the IMAP
    probe so a freshly-added Bridge account has time to finish its
    initial sync — Bridge accepts IMAP TCP connects immediately on
    service restart but rejects LOGINs (with ``"too many login
    attempts"`` after a few tries) until the per-user goroutine is
    fully up.
    """
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    user_data = cast(dict, context.user_data)
    host = CONNECT_DEFAULT_HOST
    port = CONNECT_DEFAULT_PORT
    use_ssl = CONNECT_DEFAULT_SSL

    if pre_probe_settle_seconds > 0:
        await _set_connect_status(
            context,
            (
                f"⏳ Tunggu listener IMAP Bridge siap (warm-up "
                f"{int(pre_probe_settle_seconds)}s)…"
            ),
        )
        await _send_connect_log(
            update,
            context,
            f"⏳ Menunggu Bridge selesai inisialisasi "
            f"<b>{html.escape(email)}</b> "
            f"({int(pre_probe_settle_seconds)}s)...",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(pre_probe_settle_seconds)

    await _set_connect_status(
        context,
        "🔌 Cek login Bridge IMAP — uji LOGIN ke 127.0.0.1:1143…",
    )
    await _send_connect_log(
        update,
        context,
        f"🔌 Cek login ke Bridge sebagai <b>{html.escape(email)}</b>...",
        parse_mode=ParseMode.HTML,
    )
    # Retry: a freshly added Bridge account often takes 5-15s before its
    # IMAP listener fully comes up. The previous one-shot probe failed
    # immediately with TimeoutError on a perfectly valid account.
    ok, detail = await _verify_bridge_login(
        host=host,
        port=port,
        username=imap_username,
        password=imap_password,
        use_ssl=use_ssl,
        attempts=4,
        backoff_seconds=5.0,
    )
    if not ok:
        await _set_connect_status(context, "⏳ Tunggu password ulang…")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Bridge menolak login. Pastikan password yang kamu kirim "
            "adalah <b>password IMAP yang di-generate Bridge</b> (bukan "
            "password akun Proton kamu).\n\n"
            f"Detail: <code>{html.escape(detail)}</code>\n\n"
            "Kirim password yang benar lagi, atau /cancel untuk batal:",
            parse_mode=ParseMode.HTML,
        )
        return CONNECT_PASSWORD

    await _set_connect_status(
        context,
        "💾 Enkripsi & simpan kredensial Bridge ke database…",
    )
    db = _bot_db(context)
    cipher = _bot_cipher(context)
    manager = _bot_manager(context)
    encrypted = cipher.encrypt(imap_password)
    primary_id = await db.add_primary_account(
        chat_id=chat.id,
        email=email,
        host=host,
        port=port,
        username=imap_username,
        encrypted_password=encrypted,
        use_ssl=use_ssl,
    )
    user_data.pop("primary_email", None)
    user_data.pop("proton_password", None)

    # Mark the new primary as the active one and clear any stale alias lock
    # left over from a previous primary. Strict lock-mode means email is
    # only forwarded once the user explicitly picks an alias from /list, so
    # we deliberately leave ``active_alias_id`` NULL until then.
    await db.set_active_primary(chat.id, primary_id)
    await db.set_active_alias(chat.id, None)

    # Auto-store the Proton account password as the master password for
    # /genaddr (request #9: "otomatis set protonpw dari awal connect").
    # Only the Bridge-admin path populates ``connect_proton_password``;
    # the legacy path uses the Bridge IMAP password (which would not
    # work for the Proton web UI) and explicitly clears the key, so
    # ``proton_master`` is None there. The post-connect CTA keyboard
    # below uses ``master_password_saved`` to skip the now-redundant
    # "🔐 Simpan password Proton" button.
    proton_master = user_data.get("connect_proton_password")
    master_password_saved = False
    if proton_master:
        try:
            await db.set_proton_password(
                chat.id, primary_id, cipher.encrypt(proton_master)
            )
            master_password_saved = True
        except Exception:  # pragma: no cover - DB write shouldn't block /connect
            LOGGER.exception(
                "auto-save proton master password failed for primary %d",
                primary_id,
            )

    await _send_connect_log(
        update,
        context,
        f"✅ Tersambung ke <b>{html.escape(email)}</b> — kredensial "
        "disimpan terenkripsi & jadi akun aktif.\n"
        "Listener IMAP otomatis menyala. Email belum diteruskan otomatis: "
        "buka <b>/list</b> dan pilih alias yang mau dipakai dulu.",
        parse_mode=ParseMode.HTML,
    )
    await _set_connect_status(
        context,
        "📡 Start listener IMAP — pantau INBOX semua alias…",
    )
    try:
        await manager.start_for_primary(primary_id)
    except Exception as exc:
        LOGGER.exception(
            "failed to start listener after /connect for primary %s", primary_id
        )
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal start listener: {exc}\n"
            "Bridge mungkin belum siap. Coba /connect ulang dalam beberapa detik."
        )
        await _close_connect_status(context, "❌ Listener gagal start")
        return ConversationHandler.END

    if smoke_test_tempmail is not None:
        # Provision a FRESH tempmail dedicated to the smoke test
        # instead of reusing the one Proton already sent the recovery
        # verification email to. The recovery mailbox often still has
        # the verification message pending in its listing, plus
        # Mail.tm sometimes delays delivery of a second message to
        # the same inbox by 60+ seconds — both blow past the 60s
        # smoke-test deadline. A separate inbox guarantees the only
        # message we'll ever see is the bot's own probe.
        smoke_tempmail = smoke_test_tempmail
        try:
            async with httpx.AsyncClient() as client:
                smoke_tempmail = await TempMailbox.create(client)
        except TempMailError as exc:
            LOGGER.warning(
                "smoke test: failed to create fresh tempmail (%s); "
                "falling back to recovery tempmail %s",
                exc,
                smoke_test_tempmail.address,
            )
        await _set_connect_status(
            context,
            f"🧪 Smoke test 1/{SMTP_SMOKE_MAX_ATTEMPTS} (kirim email uji)…",
        )
        await _send_connect_log(
            update,
            context,
            "🧪 Smoke test IMAP/SMTP: kirim email uji ke temp mail "
            f"(<code>{html.escape(smoke_tempmail.address)}</code>)...",
            parse_mode=ParseMode.HTML,
        )
        # On timeout, ``_smoke_test_with_retry`` waits
        # ``SMTP_SMOKE_RETRY_DELAY_SECONDS`` and tries once more before
        # giving up — Bridge occasionally warms up its outbound SMTP
        # listener a few seconds after IMAP login starts working, and
        # rolling the primary back on a one-off timeout is wasteful.
        async def _on_retry(attempt: int) -> None:
            LOGGER.info(
                "smoke test attempt %d/%d failed for %s; retrying in %ds",
                attempt,
                SMTP_SMOKE_MAX_ATTEMPTS,
                email,
                SMTP_SMOKE_RETRY_DELAY_SECONDS,
            )
            # Live status: tell the user we're between attempts so the
            # button doesn't look frozen on "Smoke test 1/3" while we
            # actually sleep ``RETRY_DELAY`` then re-probe.
            next_attempt = attempt + 1
            await _set_connect_status(
                context,
                f"⏳ Smoke retry: tunggu {SMTP_SMOKE_RETRY_DELAY_SECONDS}s lalu "
                f"attempt {next_attempt}/{SMTP_SMOKE_MAX_ATTEMPTS}…",
            )
            await _send_connect_log(
                update,
                context,
                "⚠️ Smoke test belum dapat email uji — Bridge mungkin "
                f"masih warm-up. Tunggu {SMTP_SMOKE_RETRY_DELAY_SECONDS}s "
                "lalu coba sekali lagi…",
                parse_mode=ParseMode.HTML,
            )

        # The smoke test runs under a hard wall-clock cap (``wait_for``)
        # so even pathological cases — Mail.tm 500ing for minutes,
        # Bridge SMTP socket accepting but never delivering — can't
        # keep this coroutine alive past ``SMTP_SMOKE_HARD_CAP_SECONDS``
        # and starve every other handler. ``concurrent_updates=True``
        # in __main__ already prevents one user's flow from blocking
        # another's, but capping the duration makes the *log experience*
        # reliable: the user always sees either ✅ or ⚠ within bounded
        # time, never an indefinite "kirim email uji..." stall.
        try:
            smoke_ok = await asyncio.wait_for(
                _smoke_test_with_retry(
                    email=email,
                    imap_username=imap_username,
                    imap_password=imap_password,
                    tempmail=smoke_tempmail,
                    on_retry=_on_retry,
                ),
                timeout=SMTP_SMOKE_HARD_CAP_SECONDS,
            )
        except TimeoutError:
            LOGGER.warning(
                "smoke test for %s exceeded hard cap of %ds; treating as soft fail",
                email,
                SMTP_SMOKE_HARD_CAP_SECONDS,
            )
            smoke_ok = False
        if smoke_ok:
            await _send_connect_log(
                update,
                context,
                "✅ <b>IMAP/SMTP berjalan sempurna</b> — email uji "
                "diterima di temp mail.",
                parse_mode=ParseMode.HTML,
            )
        else:
            # Soft-fail: smoke test couldn't verify SMTP delivery, but
            # the IMAP listener has already started successfully (a
            # previous call to ``_verify_bridge_login`` confirmed the
            # credentials, and ``manager.start_for_primary`` returned
            # without exception). Rolling the primary back here forced
            # the user to redo the entire /connect flow — including
            # the recovery-email Playwright dance — for what is often
            # a transient Mail.tm hiccup. The user pushed back hard
            # ("lebih baik mengulang flow daripada bot tidak merespon
            # atau mati"), so we now keep the primary, surface a
            # warning with concrete next steps, and let the user
            # verify with /cekimap when they want.
            LOGGER.warning(
                "smoke test failed for %s after %d attempts; KEEPING "
                "primary %d (listener already up — soft fail)",
                email,
                SMTP_SMOKE_MAX_ATTEMPTS,
                primary_id,
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🩺 Jalankan /cekimap sekarang",
                            callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📋 Buka /list",
                            callback_data=f"{CB_PICK_PRIMARY}:{primary_id}",
                        )
                    ],
                ]
            )
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "⚠️ <b>Smoke test belum dapat email uji</b> dalam "
                f"{SMTP_SMOKE_MAX_ATTEMPTS}x percobaan "
                f"({SMTP_SMOKE_TIMEOUT_SECONDS}s + retry "
                f"{SMTP_SMOKE_RETRY_DELAY_SECONDS}s). Mail.tm bisa lemot "
                "atau Bridge masih warm-up.\n\n"
                "<b>Akun TIDAK di-rollback</b> — IMAP listener sudah jalan "
                f"untuk <code>{html.escape(email)}</code>. Cek manual:\n"
                "• Tap tombol di bawah untuk jalankan health check, atau\n"
                "• Kirim email uji ke alias dari device lain dan lihat "
                "apakah bot meneruskannya.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

    # Auto-sync addresses from Proton account API if we have them.
    # The recovery-email Playwright flow stashes the full address list
    # (pulled from /api/core/v4/addresses with the still-logged-in
    # browser session) into ``user_data["proton_account_addresses"]``.
    # We persist them as aliases here, after the primary row exists,
    # so the user doesn't have to run /sync separately to populate
    # the 18+ existing addresses on a Business account.
    await _set_connect_status(
        context,
        "🔍 Buka Proton (Playwright) & ambil semua alamat → DB…",
    )
    proton_addresses = user_data.pop("proton_account_addresses", None)
    if proton_addresses is None:
        # Existing-creds path skipped the recovery Playwright flow.
        # Spawn a one-shot browser session purely to fetch the address
        # list. This adds ~30s to /connect but makes the auto-sync
        # behaviour consistent across both paths (the user explicitly
        # asked for "setiap konek otomatis db akan menambah").
        proton_password = user_data.pop("connect_proton_password", None)
        if proton_password:
            from .proton_verify import fetch_all_addresses_via_browser

            await _send_connect_log(
                update,
                context,
                "🔍 Sync semua alamat Proton ke DB...",
            )
            proton_addresses = await fetch_all_addresses_via_browser(
                email, proton_password
            )
    if proton_addresses:
        # Filter out the primary email — it's not an "alias" in the
        # /list sense (it IS the primary), and add_aliases would
        # silently dedupe but we prefer to not even store it.
        candidate_aliases = [a for a in proton_addresses if a != email.lower()]
        if candidate_aliases:
            try:
                added = await db.add_aliases(
                    chat.id, candidate_aliases, primary_id=primary_id
                )
            except Exception:  # pragma: no cover - DB failure shouldn't block
                LOGGER.exception(
                    "could not auto-sync %d Proton addresses for %s",
                    len(candidate_aliases),
                    email,
                )
                added = 0
            if added > 0:
                await _send_connect_log(
                    update,
                    context,
                    f"📥 Auto-sync: <b>{added}</b> alias dari "
                    f"akun Proton ditambahkan ke DB "
                    f"(total {len(candidate_aliases)} terdeteksi).",
                    parse_mode=ParseMode.HTML,
                )

    aliases = await db.list_aliases(chat.id, primary_id=primary_id)
    # Real aliases = anything other than the primary email itself. The
    # inbox-scan auto-add path can occasionally insert the primary as a
    # row in the alias table (because Bridge surfaces incoming mail with
    # ``To: vielzNN@proton.me`` for the primary too); from the user's
    # perspective that is *not* a real alias, so don't count it when
    # deciding whether the account is "fresh" / needs onboarding.
    primary_lower = email.lower()
    real_aliases = [a for a in aliases if a.email.lower() != primary_lower]
    if not real_aliases:
        # When the auto-save in /connect succeeded the "1️⃣ Simpan
        # password Proton" step is already done, so re-word the
        # onboarding text accordingly. The keyboard helper drops
        # the matching button via ``master_password_saved=True``.
        if master_password_saved:
            onboarding_text = (
                f"ℹ️ Akun <b>{html.escape(email)}</b> belum punya alias.\n\n"  # noqa: RUF001
                "<b>Cara cepat bikin alias:</b>\n"
                "1️⃣  Password Proton sudah otomatis disimpan dari /connect — "
                "✅ siap dipakai.\n"
                "2️⃣  Klik <b>✨ Generate 20 alamat sekarang</b> — bot bikin "
                "20 alias <code>vielz001..vielz020</code> otomatis di background.\n"
                "   Pesan progress update tiap 5 detik (live counter + alias terbaru) dan kamu "
                "tetap bisa pakai perintah lain sambil generate jalan.\n"
                "3️⃣  Pakai <b>🩺 Cek IMAP listener</b> kapan aja buat "
                "validasi semua alias bisa terima email."
            )
        else:
            onboarding_text = (
                f"ℹ️ Akun <b>{html.escape(email)}</b> belum punya alias.\n\n"  # noqa: RUF001
                "<b>Cara cepat bikin alias:</b>\n"
                "1️⃣  Klik <b>🔐 Simpan password Proton</b> — sekali aja, "
                "buat akun ini.\n"
                "2️⃣  Klik <b>✨ Generate 20 alamat sekarang</b> — bot bikin "
                "20 alias <code>vielz001..vielz020</code> otomatis di background.\n"
                "   Pesan progress update tiap 5 detik (live counter + alias terbaru) dan kamu "
                "tetap bisa pakai perintah lain sambil generate jalan.\n"
                "3️⃣  Pakai <b>🩺 Cek IMAP listener</b> kapan aja buat "
                "validasi semua alias bisa terima email."
            )
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            onboarding_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_build_post_connect_keyboard(
                primary_id,
                master_password_saved=master_password_saved,
                alias_count=len(real_aliases),
            ),
        )
    else:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"📥 Akun <b>{html.escape(email)}</b> sudah punya "
            f"<b>{len(real_aliases)}</b> alias.\n"
            "Kirim /list untuk lihat semuanya, atau klik tombol di "
            "bawah buat validasi semua alias bisa terima email "
            "(jalan di background, bot tetap bisa dipakai).",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_post_connect_keyboard_with_aliases(
                primary_id, len(real_aliases)
            ),
        )

    # Flip the live status anchor to its terminal label so the user
    # sees "✅ Selesai" briefly before the cleanup tracker deletes the
    # whole log series. Doing this BEFORE scheduling the cleanup task
    # ensures the final transition is visible even on slow Telegram
    # networks where the delete races the edit.
    await _close_connect_status(context, "✅ Selesai — siap dipakai")

    # Request #8 — auto-cleanup the verbose log messages 3 seconds
    # after success. The CTA messages above are intentionally NOT
    # tracked because they're the user's interaction surface for the
    # next step (Simpan password / Generate / Cek IMAP). The cleanup
    # task is fire-and-forget so the conversation handler can return
    # immediately; PTB's main event loop keeps running it. The task
    # ref is parked in :data:`_CONNECT_CLEANUP_TASKS` to keep it alive
    # against asyncio's "task that has never been awaited gets GC'd"
    # rule (RUF006).
    if tracker is not None and len(tracker) > 0:
        cleanup_task = asyncio.create_task(
            tracker.cleanup(),
            name=f"connect-cleanup-{chat.id}",
        )
        _CONNECT_CLEANUP_TASKS.add(cleanup_task)
        cleanup_task.add_done_callback(_CONNECT_CLEANUP_TASKS.discard)
    # Drop the stash so a follow-up /connect in the same chat starts
    # with a fresh tracker (the old one's ids are already scheduled
    # for deletion). The dedupe stash also goes — without this the
    # *next* /connect would suppress legitimately-identical opening
    # messages thinking they're a re-send.
    user_data.pop("connect_log_tracker", None)
    user_data.pop("connect_last_log_text", None)
    user_data.pop("connect_last_log_msg", None)

    return ConversationHandler.END


async def _setup_tempmail_recovery(
    update: Update,
    email: str,
    proton_password: str,
    *,
    tracker: TaskMessageTracker | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> tuple[TempMailbox | None, bool, list[str] | None, bool]:
    """Create a temp mail and set it as recovery email in Proton settings.

    Logs into the Proton web UI, navigates to recovery settings, and
    replaces the current recovery email with a fresh Mail.tm address.
    After Proton emails the verification link to the temp inbox we try
    to auto-verify it in a fresh tab of the same browser context. On
    success the user never sees the link — they get a single ``✅
    Recovery email otomatis diverifikasi`` line and the conversation
    skips the manual ``ok`` wait state. On failure we fall back to the
    pre-PR-E behaviour and DM the link.

    Returns ``(tempmail, ok, addresses, auto_verified)`` where:
      * ``tempmail`` is the disposable mailbox (or ``None`` on early failure).
      * ``ok`` is ``True`` only if the verification link was sent to the
        chat (or auto-verified). When ``ok`` is ``False`` the caller
        MUST NOT proceed to Bridge add-account: Proton login or recovery
        email change failed and the user needs to retry ``/connect``.
      * ``addresses`` is the full list of email addresses Proton's
        ``/api/core/v4/addresses`` endpoint returned for this user
        (extracted from the still-logged-in browser session before
        teardown), so the caller can persist them as aliases. ``None``
        when the API call failed; an empty list is also possible
        (very rare, single-address account).
      * ``auto_verified`` is ``True`` when ``auto_verify_recovery_link``
        confirmed Proton accepted the verification — the caller can
        skip the ``CONNECT_RECOVERY_VERIFY`` wait state and dive
        straight into Bridge add-account.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        LOGGER.warning("playwright not installed; skipping recovery email setup")
        return None, False, None, False

    async with httpx.AsyncClient() as client:
        try:
            tempmail = await TempMailbox.create(client)
        except TempMailError as exc:
            LOGGER.warning("failed to create temp mailbox: %s", exc)
            return None, False, None, False

        await _send_connect_log(
            update,
            context,
            f"📧 Temp mail dibuat: <code>{html.escape(tempmail.address)}</code>\n"
            "Mengubah recovery email di Proton...",
            parse_mode=ParseMode.HTML,
        )

        pw = None
        browser = None
        try:
            from .proton_verify import (
                _login_proton,
                change_recovery_email,
                fetch_all_addresses,
            )

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()

            # Log into Proton web and detect user_index
            user_index = await _login_proton(page, email, proton_password)
            if user_index is None:
                failure = getattr(_login_proton, "last_failure", {}) or {}
                blocker = failure.get("blocker", "unknown")
                screenshot = failure.get("screenshot")
                LOGGER.warning(
                    "Proton web login failed (blocker=%s, url=%s); skipping recovery email change",
                    blocker,
                    failure.get("url"),
                )
                blocker_msg = {
                    "2fa": (
                        "Akun Proton ini punya <b>2FA aktif</b> — bot belum "
                        "mendukung input kode 2FA. Matikan 2FA sementara di "
                        "<code>account.proton.me/u/0/account-password/two-factor-authentication</code> "
                        "atau /cancel."
                    ),
                    "captcha": (
                        "Proton menampilkan <b>CAPTCHA / human verification</b>. "
                        "Lihat screenshot di bawah, lalu coba lagi setelah "
                        "beberapa menit (Proton mungkin rate-limit IP VPS)."
                    ),
                    "bad_credentials": (
                        "Proton menolak password — pastikan ini password "
                        "akun Proton (yang kamu pakai login di proton.me), "
                        "bukan password Bridge."
                    ),
                    "unlock": (
                        "Proton meminta verifikasi tambahan untuk membuka kunci "
                        "akun. Buka akun di browser sendiri sekali, selesaikan "
                        "verifikasinya, lalu /connect lagi."
                    ),
                }.get(blocker, "Proton tidak redirect ke dashboard dalam 60 detik.")
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    f"❌ <b>Login Proton web gagal</b> (<i>{blocker}</i>). {blocker_msg}\n\n"
                    "Bridge add-account dibatalkan — silakan /connect lagi "
                    "setelah memperbaiki masalah di atas.",
                    parse_mode=ParseMode.HTML,
                )
                if screenshot:
                    try:
                        with open(screenshot, "rb") as fh:
                            await update.effective_message.reply_photo(  # type: ignore[union-attr]
                                photo=fh,
                                caption=f"Screenshot saat login gagal ({blocker})",
                            )
                    except Exception as exc:
                        LOGGER.debug("could not send login-failure screenshot: %s", exc)
                return tempmail, False, None, False

            # Change recovery email and get verification link
            verify_link = await change_recovery_email(
                page, tempmail.address, proton_password, tempmail, client,
                user_index=user_index,
            )
            if verify_link:
                # Reuse the still-logged-in browser session to pull every
                # address Proton knows about for this account; we can
                # then auto-add them as aliases once /connect finishes.
                # Doing it here (vs. spawning a second browser later)
                # saves ~30s on the happy path.
                addresses = await fetch_all_addresses(
                    page, user_index=user_index
                )
                if addresses is not None:
                    LOGGER.info(
                        "Proton account %s has %d addresses (will sync after connect)",
                        email,
                        len(addresses),
                    )

                # PR-E: try to auto-verify the recovery link in a fresh
                # tab of the same browser context. The new tab keeps
                # ``page`` (and its Proton session cookies) untouched
                # so the surrounding flow can continue without surprise
                # navigations. On success we never DM the link to the
                # user and the caller can skip the ``ok`` wait state.
                from .proton_verify import auto_verify_recovery_link

                auto_verified = False
                verify_page = None
                try:
                    verify_page = await ctx.new_page()
                    auto_verified = await auto_verify_recovery_link(
                        verify_page, verify_link
                    )
                except Exception:
                    LOGGER.exception("auto_verify_recovery_link unexpected failure")
                finally:
                    if verify_page is not None:
                        with contextlib.suppress(Exception):
                            await verify_page.close()

                if auto_verified:
                    await _send_connect_log(
                        update,
                        context,
                        f"✅ Recovery email <code>{html.escape(tempmail.address)}</code> "
                        "otomatis diverifikasi — lanjut Bridge add-account.",
                        parse_mode=ParseMode.HTML,
                    )
                    return tempmail, True, addresses, True

                # Auto-verify failed: fall back to the pre-PR-E manual
                # flow. DM the link, the conversation continues to wait
                # for the user's "ok".
                await _send_connect_log(
                    update,
                    context,
                    f"📧 Recovery email diubah ke <code>{html.escape(tempmail.address)}</code>\n\n"
                    "Auto-verify tidak bisa konfirmasi otomatis — klik link "
                    "berikut untuk verifikasi manual:\n"
                    f"{html.escape(verify_link)}",
                    parse_mode=ParseMode.HTML,
                )
                return tempmail, True, addresses, False

            failure = getattr(change_recovery_email, "last_failure", {}) or {}
            step = failure.get("step", "unknown")
            screenshot = failure.get("screenshot")
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "❌ <b>Gagal mengubah/verifikasi recovery email</b> "
                f"(step: <code>{html.escape(step)}</code>).\n"
                "Bridge add-account dibatalkan — password Proton beda dengan "
                "password Bridge IMAP, jadi tidak aman lanjut.\n\n"
                "Coba <b>/connect</b> lagi. Kalau berulang, kirim screenshot "
                "ke developer.",
                parse_mode=ParseMode.HTML,
            )
            if screenshot:
                try:
                    with open(screenshot, "rb") as fh:
                        await update.effective_message.reply_photo(  # type: ignore[union-attr]
                            photo=fh,
                            caption=f"Screenshot saat recovery email gagal (step={step})",
                        )
                except Exception as exc:
                    LOGGER.debug("could not send recovery-failure screenshot: %s", exc)
            return tempmail, False, None, False
        except Exception:
            LOGGER.exception("recovery email setup failed")
            return tempmail, False, None, False
        finally:
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()
            if pw:
                with contextlib.suppress(Exception):
                    await pw.stop()


async def _try_auto_verify(
    update: Update,
    verify_url: str,
    bridge_admin: BridgeAdmin,
    tempmail: TempMailbox | None,
) -> bool:
    """Attempt to auto-solve a Proton email verification via temp mail.

    Opens the verification URL in a headless browser, triggers the code
    send (to the recovery email = our temp mail), polls the temp inbox
    for the 6-digit code, and submits it.  Returns True on success,
    False if any step fails (caller should fall back to manual flow).
    """
    if "ownership-email" not in verify_url:
        return False
    if tempmail is None:
        # No temp mail available; create one on-the-fly
        try:
            async with httpx.AsyncClient() as client:
                tempmail = await TempMailbox.create(client)
        except TempMailError as exc:
            LOGGER.warning("failed to create temp mailbox: %s", exc)
            return False

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        LOGGER.warning("playwright not installed; skipping auto-verify")
        return False

    async with httpx.AsyncClient() as client:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"🤖 Auto-verify via <code>{html.escape(tempmail.address)}</code>...",
            parse_mode=ParseMode.HTML,
        )

        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()

            solved = await solve_email_verification(
                page, verify_url, tempmail, client
            )
            if solved:
                await bridge_admin.acknowledge_captcha()
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    "✅ Verifikasi email otomatis berhasil!",
                )
                return True
            LOGGER.warning("auto-verify returned False")
            return False
        except Exception:
            LOGGER.exception("auto-verify failed")
            return False
        finally:
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()
            if pw:
                with contextlib.suppress(Exception):
                    await pw.stop()


async def _drive_bridge_login(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    bridge_admin: BridgeAdmin,
    email: str,
    proton_password: str,
    tracker: TaskMessageTracker | None = None,
) -> int:
    """Walk Bridge through ``add_account`` end-to-end on behalf of /connect.

    When Proton requests email-based human verification, the bot first
    tries to solve it automatically via a disposable Mail.tm inbox.  If
    auto-verification fails it falls back to the manual flow (sending
    the verification URL to the chat).

    After ``_setup_tempmail_recovery`` posts the verification link to the
    chat we **wait for the user to confirm they actually clicked it**
    before kicking off ``bridge add_account``. Firing add-account in
    parallel with the click race-conditioned with Proton: the new
    recovery email was still flagged "Belum diverifikasi", so Proton
    treated the freshly added Bridge session as risky and Bridge IMAP
    came up half-initialised — leading to the ``TimeoutError`` we kept
    seeing on the post-add IMAP login probe.
    """
    user_data = cast(dict, context.user_data)

    # Step 0: set up temp mail + change recovery email BEFORE Bridge login.
    # This must happen before Bridge login because the Bridge CLI stops
    # the service (and thus the Proton web session is separate).
    await _set_connect_status(
        context,
        "🌐 Buka Proton (Playwright) untuk pasang recovery email…",
    )
    (
        tempmail,
        recovery_ok,
        proton_addresses,
        auto_verified,
    ) = await _setup_tempmail_recovery(
        update, email, proton_password, tracker=tracker, context=context
    )
    if not recovery_ok:
        # Recovery email step failed: do NOT attempt Bridge add-account.
        # Bridge uses the Proton account password for login (not the
        # IMAP password it later generates), and without a verified
        # recovery email Proton will demand human verification we cannot
        # automate.  Better to abort cleanly than leave the user with a
        # confusing TimeoutError after a broken recovery flow.
        await _close_connect_status(context, "❌ Recovery email gagal")
        return ConversationHandler.END

    # Persist context for either the auto-verified fast path OR the
    # manual ``ok`` wait state, so both paths can hand off the same
    # bookkeeping to ``_perform_bridge_add_account`` and ``_finalize_connect``.
    user_data["bridge_recovery_email"] = email
    user_data["bridge_recovery_proton_password"] = proton_password
    user_data["bridge_recovery_tempmail"] = tempmail
    # Stash the list pulled from /api/core/v4/addresses so _finalize_connect
    # can persist them as aliases once IMAP comes up.
    user_data["proton_account_addresses"] = proton_addresses

    if auto_verified:
        # PR-E fast path: auto_verify_recovery_link already confirmed
        # Proton accepted the link, so we skip the
        # ``CONNECT_RECOVERY_VERIFY`` wait state entirely and dive
        # straight into Bridge add-account.
        bridge_admin = _bot_bridge_admin(context)
        if bridge_admin is None:
            # ``_bot_bridge_admin`` only returns ``None`` when bot wiring
            # was set up without a Bridge admin (test harness, etc.). In
            # production this branch is unreachable; degrade gracefully.
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "Sesi /connect kedaluwarsa. Mulai lagi dengan /connect."
            )
            return ConversationHandler.END
        return await _perform_bridge_add_account(
            update,
            context,
            bridge_admin=bridge_admin,
            email=email,
            proton_password=proton_password,
            tempmail=tempmail,
            tracker=tracker,
        )

    # Auto-verify failed (or wasn't attempted): fall back to the manual
    # wait state. The verification link has already been DM'd from
    # inside ``_setup_tempmail_recovery`` — we just need the user to
    # click it and reply "ok" before we proceed to bridge add-account.
    # Track the "👆 Klik link" prompt: cleanup only fires AFTER the
    # user has confirmed (sent "ok") and _finalize_connect ran, so
    # at that point the prompt has served its purpose and is safe
    # to delete alongside the rest of the success log.
    await _set_connect_status(
        context, "⏳ Tunggu user klik link recovery email…"
    )
    await _send_connect_log(
        update,
        context,
        "👆 Klik link verifikasi di atas dan selesaikan di browser "
        "(buka link, tekan tombol verifikasi di halaman Proton).\n\n"
        "Begitu Proton mengonfirmasi recovery email <b>terverifikasi</b>, "
        "kirim <code>ok</code> di sini supaya bot lanjut daftar ke Bridge.\n"
        "Kirim /cancel untuk batal.",
        parse_mode=ParseMode.HTML,
    )
    return CONNECT_RECOVERY_VERIFY


async def _perform_bridge_add_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    bridge_admin: BridgeAdmin,
    email: str,
    proton_password: str,
    tempmail: TempMailbox | None,
    tracker: TaskMessageTracker | None = None,
) -> int:
    """Run ``bridge add_account`` and the smoke test for ``email``.

    Extracted from ``_drive_bridge_login`` so it can be invoked **after**
    the user has confirmed they clicked the recovery-email verification
    link, without duplicating the iterator/CAPTCHA bookkeeping.
    """
    user_data = cast(dict, context.user_data)
    await _set_connect_status(
        context,
        "🔧 Daftar akun ke Bridge — stop service, CLI login, restart…",
    )
    progress = await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"🔧 Mendaftarkan <b>{html.escape(email)}</b> ke Proton Bridge "
        "(stop service → cli login → restart)...",
        parse_mode=ParseMode.HTML,
    )

    iterator = bridge_admin.add_account(email, proton_password).__aiter__()

    async def consume() -> tuple[BridgeImapCredentials | None, str | None]:
        captcha_count = 0
        while True:
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                return None, "Bridge selesai tanpa hasil."
            if isinstance(event, CaptchaRequired):
                captcha_count += 1

                # Try auto-verification for email-based challenges
                await _set_connect_status(
                    context, "🤖 Auto-verifikasi CAPTCHA email…"
                )
                auto_ok = await _try_auto_verify(
                    update, event.url, bridge_admin, tempmail
                )
                if auto_ok:
                    # Continue consuming events — Bridge should proceed
                    await _set_connect_status(
                        context, "✅ CAPTCHA auto-verified — lanjut…"
                    )
                    continue

                # Auto-verify failed: fall back to manual flow
                user_data["bridge_captcha_iterator"] = iterator
                user_data["bridge_email"] = email
                user_data["bridge_smoke_tempmail"] = tempmail
                await _set_connect_status(
                    context, "⏳ Tunggu user selesaikan CAPTCHA manual…"
                )
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    "🔒 Proton minta verifikasi manusia.\n\n"
                    "Auto-verify gagal. Selesaikan manual:\n"
                    f"Klik link berikut, selesaikan CAPTCHA / kode email, "
                    f"lalu kirim <code>ok</code> di sini:\n\n"
                    f"{event.url}",
                    parse_mode=ParseMode.HTML,
                )
                return None, "__CAPTCHA__"
            if isinstance(event, LoginFailed):
                return None, event.reason or "login gagal"
            if isinstance(event, LoginSucceeded):
                creds = await bridge_admin.fetch_imap_credentials(email)
                return creds, None

    try:
        creds, err = await consume()
    except BridgeAdminError as exc:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"❌ BridgeAdmin error: <code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        await _close_connect_status(context, "❌ Bridge admin error")
        return ConversationHandler.END
    finally:
        try:
            await progress.delete()
        except Exception:
            pass

    if err == "__CAPTCHA__":
        return CONNECT_BRIDGE_CAPTCHA
    if err is not None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Bridge tidak menerima login.\n"
            f"Detail: <code>{html.escape(err)}</code>\n\n"
            "Coba /connect lagi dengan password Proton yang benar, "
            "atau /cancel untuk batal.",
            parse_mode=ParseMode.HTML,
        )
        await _close_connect_status(context, "❌ Bridge tolak login")
        return ConversationHandler.END
    if creds is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Login Bridge sukses tapi password IMAP tidak ditemukan "
            "di vault. Coba /connect lagi atau cek konfigurasi Bridge."
        )
        await _close_connect_status(context, "❌ IMAP password hilang")
        return ConversationHandler.END

    return await _finalize_connect(
        update,
        context,
        email=creds.email,
        imap_username=creds.imap_username,
        imap_password=creds.imap_password,
        smoke_test_tempmail=tempmail,
        pre_probe_settle_seconds=30.0,
        tracker=tracker,
    )


async def connect_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Pull the same tracker + status anchor that ``cmd_connect`` set
    # up. Calling ``_ensure_connect_progress`` here guarantees that
    # entry points which skipped ``cmd_connect`` (e.g. an unusual
    # re-entry path that lands directly in CONNECT_PASSWORD) still
    # get a tracker; in the normal flow this just returns the
    # already-stashed pair.
    tracker, _status = await _ensure_connect_progress(update, context)
    await _set_connect_status(
        context,
        "🔍 Validasi format password Proton…",
    )
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    # Security: scrub the user's password message from the chat as
    # soon as we've read it. Telegram bots can delete user messages
    # in private chats up to 48h old, which is exactly the window we
    # need. We do this BEFORE any other side effect so that a transient
    # bot crash mid-handler still gets the password off-screen.
    with contextlib.suppress(Exception):
        await update.effective_message.delete()  # type: ignore[union-attr]
    if not text:
        await _send_connect_log(
            update, context, "Password tidak boleh kosong:"
        )
        await _set_connect_status(context, "⏳ Tunggu input password Proton…")
        return CONNECT_PASSWORD
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    user_data = cast(dict, context.user_data)
    email = user_data.get("primary_email")
    if not email:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Sesi /connect kedaluwarsa. Mulai lagi dengan /connect."
        )
        await _close_connect_status(context, "❌ Sesi kedaluwarsa")
        return ConversationHandler.END

    # Stash the Proton account password so _finalize_connect can pull
    # the full address list via Playwright in the existing-creds short
    # path (where _setup_tempmail_recovery is skipped).
    user_data["connect_proton_password"] = text

    bridge_admin = _bot_bridge_admin(context)
    if bridge_admin is None:
        # Legacy path: user typed the Bridge IMAP password directly.
        # That's the Bridge IMAP password, not the Proton account
        # password, so address sync wouldn't work — clear the key.
        # No tracker on this path: the legacy flow emits only ~2-3
        # log lines so the cleanup churn isn't worth the complexity.
        user_data.pop("connect_proton_password", None)
        return await _finalize_connect(
            update,
            context,
            email=email,
            imap_username=email,
            imap_password=text,
        )

    # Auto-add path: text is the Proton account password. If the account
    # is already in Bridge AND the cached IMAP creds actually work, skip
    # the cli login and go straight to vault extraction. Otherwise (no
    # vault entry, OR vault entry is stale because Bridge forgot the
    # user but we never rewrote the vault), drive the full flow:
    # proton-login + recovery-email setup + ``bridge --cli login``.
    await _set_connect_status(
        context,
        "🗄️ Cek vault Bridge — apakah akun sudah terdaftar?",
    )
    try:
        existing = await bridge_admin.fetch_imap_credentials(email)
    except BridgeAdminError as exc:
        LOGGER.warning("vault probe failed: %s", exc)
        existing = None
    if existing is not None:
        # When existing vault creds are present, the Bridge IMAP server
        # may still be in a temporary "too many login attempts" lockout
        # from a previous failed run. Pass attempts=2 so we retry once
        # after the rate-limit backoff before deciding to re-add the
        # whole account (which would needlessly redo the recovery-email
        # flow).
        await _set_connect_status(
            context,
            "🔐 Verifikasi kredensial IMAP — LOGIN test ke Bridge",
        )
        ok, detail = await _verify_bridge_login(
            host=CONNECT_DEFAULT_HOST,
            port=CONNECT_DEFAULT_PORT,
            username=existing.imap_username,
            password=existing.imap_password,
            use_ssl=CONNECT_DEFAULT_SSL,
            attempts=2,
            backoff_seconds=5.0,
        )
        if ok:
            return await _finalize_connect(
                update,
                context,
                email=existing.email,
                imap_username=existing.imap_username,
                imap_password=existing.imap_password,
                tracker=tracker,
            )
        LOGGER.info(
            "vault has %s but Bridge IMAP rejected (%s); running full re-add",
            email,
            detail,
        )

    await _set_connect_status(
        context,
        "🛠️ Bridge CLI: stop service → login Proton → restart…",
    )
    return await _drive_bridge_login(
        update,
        context,
        bridge_admin=bridge_admin,
        email=email,
        proton_password=text,
        tracker=tracker,
    )


_RECOVERY_VERIFY_OK_TOKENS = {
    "ok", "oke", "okay", "selesai", "done", "sudah", "udah", "yes", "ya",
}


async def connect_recovery_verify(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Wait for the user to confirm they clicked the recovery-email
    verification link before kicking off ``bridge add_account``."""
    text = (update.effective_message.text or "").strip().lower()  # type: ignore[union-attr]
    if text not in _RECOVERY_VERIFY_OK_TOKENS:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Setelah klik link verifikasi & Proton bilang sukses, kirim "
            "<code>ok</code> di sini. /cancel untuk batal.",
            parse_mode=ParseMode.HTML,
        )
        return CONNECT_RECOVERY_VERIFY

    user_data = cast(dict, context.user_data)
    email = user_data.get("bridge_recovery_email")
    proton_password = user_data.get("bridge_recovery_proton_password")
    tempmail = user_data.get("bridge_recovery_tempmail")
    bridge_admin = _bot_bridge_admin(context)
    if bridge_admin is None or not email or not proton_password:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Sesi /connect kedaluwarsa. Mulai lagi dengan /connect."
        )
        await _close_connect_status(context, "❌ Sesi kedaluwarsa")
        return ConversationHandler.END

    # Restore the tracker stashed at the start of ``connect_password``
    # so the success-log cleanup in :func:`_finalize_connect` includes
    # everything emitted before the recovery-verify wait state.
    tracker = user_data.get("connect_log_tracker")
    await _set_connect_status(
        context,
        "🛠️ Bridge CLI: stop service → login Proton → restart…",
    )
    return await _perform_bridge_add_account(
        update,
        context,
        bridge_admin=bridge_admin,
        email=email,
        proton_password=proton_password,
        tempmail=tempmail,
        tracker=tracker,
    )


async def connect_bridge_captcha(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = (update.effective_message.text or "").strip().lower()  # type: ignore[union-attr]
    if text not in {"ok", "oke", "okay", "selesai", "done"}:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Setelah selesai CAPTCHA, kirim <code>ok</code>. "
            "Kalau mau batal, kirim /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return CONNECT_BRIDGE_CAPTCHA

    bridge_admin = _bot_bridge_admin(context)
    if bridge_admin is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Sesi /connect kedaluwarsa. Mulai lagi dengan /connect."
        )
        return ConversationHandler.END

    user_data = cast(dict, context.user_data)
    iterator = user_data.get("bridge_captcha_iterator")
    email = user_data.get("bridge_email") or user_data.get("primary_email")
    if iterator is None or email is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Sesi /connect kedaluwarsa. Mulai lagi dengan /connect."
        )
        return ConversationHandler.END

    await bridge_admin.acknowledge_captcha()
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "▶️ Lanjut login Bridge..."
    )
    while True:
        try:
            event = await iterator.__anext__()
        except StopAsyncIteration:
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "Bridge selesai tanpa hasil. Coba /connect lagi."
            )
            return ConversationHandler.END
        if isinstance(event, CaptchaRequired):
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "🔒 Proton minta verifikasi manusia (lagi).\n\n"
                f"Klik link berikut, selesaikan, lalu kirim <code>ok</code>:\n\n"
                f"{event.url}",
                parse_mode=ParseMode.HTML,
            )
            return CONNECT_BRIDGE_CAPTCHA
        if isinstance(event, LoginFailed):
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "❌ Bridge tidak menerima login.\n"
                f"Detail: <code>{html.escape(event.reason or 'unknown')}</code>",
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END
        if isinstance(event, LoginSucceeded):
            creds = await bridge_admin.fetch_imap_credentials(email)
            if creds is None:
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    "❌ Login Bridge sukses tapi password IMAP tidak "
                    "ditemukan di vault. Coba /connect lagi."
                )
                return ConversationHandler.END
            return await _finalize_connect(
                update,
                context,
                email=creds.email,
                imap_username=creds.imap_username,
                imap_password=creds.imap_password,
                smoke_test_tempmail=user_data.get("bridge_smoke_tempmail"),
                pre_probe_settle_seconds=30.0,
                tracker=user_data.get("connect_log_tracker"),
            )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = cast(dict, context.user_data)
    user_data.pop("imap_password", None)
    user_data.pop("bridge_captcha_iterator", None)
    user_data.pop("bridge_email", None)
    user_data.pop("bridge_smoke_tempmail", None)
    user_data.pop("proton_password", None)
    # Flip the live status anchor (if any) to a terminal "Dibatalkan"
    # label first so the indicator doesn't keep displaying a stale
    # "Tunggu input password…" after the user has clearly chosen to
    # abort. The anchor message itself stays in chat (no tracker
    # cleanup on cancel — see comment below).
    await _close_connect_status(context, "🛑 Dibatalkan oleh user")
    # Drop the success-log tracker stash (request #8). On cancel we
    # deliberately do NOT cleanup the chat: the user might want to
    # see the partial log to understand what failed.
    user_data.pop("connect_log_tracker", None)
    user_data.pop("connect_last_log_text", None)
    user_data.pop("connect_last_log_msg", None)
    user_data.pop("connect_proton_password", None)
    bridge_admin = _bot_bridge_admin(context)
    if bridge_admin is not None:
        try:
            await bridge_admin.cancel_captcha()
        except Exception:
            LOGGER.exception("failed to clean up bridge captcha state")
    # Restore IMAP listeners that may have been disrupted while Bridge was
    # stopped during the /connect flow.  Without this, a /cancel leaves the
    # listeners dead because Bridge was killed mid-flight.
    chat = update.effective_chat
    if chat is not None:
        manager = _bot_manager(context)
        try:
            db = _bot_db(context)
            primaries = await db.list_primary_accounts(chat.id)
            for primary in primaries:
                await manager.start_for_primary(primary.id)
        except Exception:
            LOGGER.exception("failed to restore listeners after /cancel")
    if update.effective_message is not None:
        await update.effective_message.reply_text("Dibatalkan. Listener IMAP dipulihkan.")
    return ConversationHandler.END


@_gate
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Release the active-alias lock so no email is forwarded until next pick."""
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    active = await db.get_active_alias(chat.id)
    if active is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Tidak ada alias yang sedang aktif. Pilih satu di /list."
        )
        return
    # Tear down the lock first so any in-flight email-forward racing
    # against this command is rejected by strict lock-mode, then bulk
    # delete every message we previously forwarded for this alias so
    # the chat doesn't stay polluted with stale emails.
    await db.set_active_alias(chat.id, None)
    await _purge_alias_email_messages(
        context.application.bot, db, chat.id, active.id
    )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"🔓 Kunci dilepas dari <b>{html.escape(active.email)}</b>. "
        "Pilih alias di /list saat siap menerima email lagi.",
        parse_mode=ParseMode.HTML,
    )


@_gate
async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Belum ada email utama yang terdaftar."
        )
        return
    rows: list[list[InlineKeyboardButton]] = []
    for primary in primaries:
        rows.append(
            [
                InlineKeyboardButton(
                    f"❌ Hapus {primary.email}",
                    callback_data=f"{CB_DEL_PRIMARY}:{primary.id}",
                )
            ]
        )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Pilih akun yang mau dihapus. Saya akan:\n"
        "• stop listener IMAP\n"
        "• hapus kredensial + semua alias-nya\n"
        "• logout akun dari Proton Bridge (cache & keychain di-purge)\n"
        "Jadi kalau /connect lagi nanti, mulai dari nol.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# --------------------------------------------------------------- /accounts


@_gate
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut for /list — same UI, kept as a separate command so users
    discover the multi-account feature more naturally."""
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    await _show_primary_list(update, db, chat.id)
    await _maybe_send_lock_reminder(update, context)


# --------------------------------------------------------------- /cekimap


def _build_cekimap_picker_keyboard(
    primaries: list[PrimaryAccount],
    counts: dict[int, int],
) -> InlineKeyboardMarkup:
    """Picker for /cekimap: one row per primary, label shows alias count."""
    rows: list[list[InlineKeyboardButton]] = []
    for primary in primaries:
        count = counts.get(primary.id, 0)
        label = f"📧 {primary.email} ({count} alias)"
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_HEALTHCHECK_PICK}:{primary.id}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton("(belum ada email utama)", callback_data=CB_NOOP)]
        )
    return InlineKeyboardMarkup(rows)


def _launch_health_check_task(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    primary: PrimaryAccount,
    targets: list[str],
) -> None:
    """Spawn run_health_check as a tracked background task.

    Keeps a strong reference on ``application.bot_data['health_check_tasks']``
    so the asyncio task isn't GC'd before it finishes (RUF006), and
    sweeps completed tasks out of the list when the next one launches
    so it doesn't grow unbounded.
    """
    db = _bot_db(context)
    bridge_admin = _bot_bridge_admin(context)
    bot = context.application.bot

    task_list = context.application.bot_data.setdefault(
        "health_check_tasks", []
    )
    # Drop already-finished tasks to keep the list bounded.
    task_list[:] = [t for t in task_list if not t.done()]

    async def _run_then_offer_topup() -> None:
        """Run /cekimap and, when it finishes, offer to top the
        primary up to the soft alias target if it's still under.

        All transient health-check messages (started header, rolling
        progress, final summary) are tracked via
        :class:`TaskMessageTracker` and auto-deleted after a short
        delay. After cleanup we re-render the per-primary alias list
        so the chat lands back at the canonical /list view.

        The topup-offer CTA is intentionally **not** tracked: it
        persists below the new /list as a call-to-action button.
        """
        tracker = TaskMessageTracker(bot, chat_id)
        try:
            try:
                await run_health_check(
                    bot=bot,
                    chat_id=chat_id,
                    db=db,
                    bridge_admin=bridge_admin,
                    primary=primary,
                    targets=targets,
                    tracker=tracker,
                )
            finally:
                # Topup offer runs even on failure: the user might
                # still want to add aliases despite a flaky run.
                try:
                    aliases = await db.list_aliases(
                        chat_id, primary_id=primary.id
                    )
                    await _maybe_offer_alias_topup(
                        bot,
                        chat_id=chat_id,
                        primary=primary,
                        current_count=len(aliases),
                    )
                except Exception:
                    LOGGER.debug(
                        "post-cekimap topup offer failed", exc_info=True
                    )
        finally:
            await tracker.cleanup(
                after=lambda: _render_primary_alias_list(
                    bot, db, chat_id, primary
                )
            )

    task = asyncio.create_task(
        _run_then_offer_topup(),
        name=f"healthcheck-{primary.id}",
    )
    task_list.append(task)


async def _maybe_offer_alias_topup(
    bot: Any,
    *,
    chat_id: int,
    primary: PrimaryAccount,
    current_count: int,
    target: int = ALIAS_TARGET_PER_PRIMARY,
) -> None:
    """If the primary has fewer than ``target`` aliases, post a
    follow-up message with a one-tap "✨ Tambah N alamat lagi"
    button that fires /genaddr in random-suffix mode to top up.

    Best-effort: any send/encode failure is swallowed because this
    is a UX nicety on top of the real summary message — losing it
    must never make the underlying flow look broken.
    """
    missing = target - current_count
    if missing <= 0:
        return
    try:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✨ Tambah {missing} alamat lagi",
                        callback_data=(
                            f"{CB_QUICK_GENADDR}:{primary.id}:{missing}"
                        ),
                    )
                ]
            ]
        )
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"💡 Akun <b>{html.escape(primary.email)}</b> baru "
                f"punya <b>{current_count}</b> alias dari target "
                f"<b>{target}</b>. Mau langsung tambah "
                f"<b>{missing}</b> alamat sekaligus?"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        LOGGER.debug(
            "topup suggestion send failed (chat=%s primary=%s)",
            chat_id,
            primary.id,
            exc_info=True,
        )


async def _start_sync_for_primary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    primary_id: int,
) -> None:
    """Re-fetch the canonical address list for a primary from
    ``account.proton.me`` and persist any new aliases.

    Drives a fresh Playwright session (using the saved Proton password)
    rather than calling the Bridge or trusting the local DB — the
    Proton settings page is the source of truth, and a saved password
    means we don't need any user interaction. Live progress is shown
    via a :class:`StatusReporter` button on the kickoff message so the
    chat doesn't sit idle for 30+ seconds while the browser logs in.
    """
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    primary = await db.get_primary_account(chat.id, primary_id)
    if primary is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Email utama tidak ditemukan."
        )
        return

    # Pre-flight: bail loudly if no saved Proton password. Without it
    # we can't drive the browser back into the settings page, and a
    # silent skip would just leave the user staring at a button that
    # does nothing.
    encrypted_pw = await db.get_proton_password_encrypted(primary_id)
    if encrypted_pw is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"❌ Password Proton untuk <code>{html.escape(primary.email)}</code> "
            "belum tersimpan. Klik tombol <b>🔐 Simpan password Proton</b> "
            "(atau /setprotonpw) dulu sebelum sync.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Spawn the actual scrape as a background task so the bot stays
    # responsive while Playwright spins up + logs in (~10-30s).
    context.application.create_task(
        _run_sync_for_primary_background(
            context,
            chat_id=chat.id,
            primary=primary,
            encrypted_password=encrypted_pw,
        )
    )


async def _run_sync_for_primary_background(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    primary: PrimaryAccount,
    encrypted_password: str,
) -> None:
    """Background body for the per-primary "Sync alias" button.

    Posts a status message with a live-update button, then walks
    through: decrypt password → Playwright login → scrape addresses
    → persist new aliases → final summary. All errors get reported
    back to the chat as edits to the same status message rather than
    spamming new ones.
    """
    bot = context.application.bot
    db = _bot_db(context)
    cipher = _bot_cipher(context)

    header = (
        f"🔄 Sync alias <b>{html.escape(primary.email)}</b>\n"
        f"Mengambil daftar alamat dari halaman pengaturan Proton…"
    )
    msg = await bot.send_message(
        chat_id=chat_id,
        text=header,
        parse_mode=ParseMode.HTML,
        reply_markup=build_status_keyboard("🚀 Mulai sync…"),
    )
    msg_id = getattr(msg, "message_id", None)
    if msg_id is None:
        # Couldn't anchor a status reporter — drop straight to a
        # bare-bones flow that just posts a final message at the end.
        msg_id = 0
    status = StatusReporter(bot, chat_id, msg_id)
    try:
        await status.update("🔓 Decrypt password Proton…")
        try:
            proton_password = cipher.decrypt(encrypted_password)
        except Exception:
            LOGGER.exception("sync: failed to decrypt stored Proton password")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Gagal decrypt password Proton. "
                    "Mungkin master key berubah — coba /setprotonpw lagi."
                ),
            )
            await status.done("❌ Gagal decrypt password")
            return

        await status.update("🌐 Buka browser & login Proton…")
        from .proton_verify import fetch_all_addresses_via_browser

        addresses = await fetch_all_addresses_via_browser(
            primary.email, proton_password
        )
        if addresses is None:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Gagal ambil daftar alamat untuk "
                    f"<code>{html.escape(primary.email)}</code>. "
                    "Login Proton kemungkinan diblok (CAPTCHA / 2FA / "
                    "password salah). Coba /setprotonpw untuk update "
                    "password, atau buka browser di account.proton.me "
                    "untuk klear blokir CAPTCHA."
                ),
                parse_mode=ParseMode.HTML,
            )
            await status.done("❌ Login Proton gagal")
            return

        await status.update(
            f"📋 {len(addresses)} alamat ditemukan, simpan ke DB…"
        )
        # Drop the primary's own address from the alias list — it's
        # already represented by ``primary``. Same dedupe logic as
        # ``_finalize_connect``.
        primary_norm = primary.email.lower()
        alias_addresses = [a for a in addresses if a.lower() != primary_norm]
        inserted = await db.add_aliases(
            chat_id, alias_addresses, primary_id=primary.id
        )
        skipped = len(alias_addresses) - inserted

        await status.update(f"✅ {inserted} alias baru, {skipped} sudah ada")

        # Final summary as a fresh message — the original kickoff
        # message stays as a "this is what kicked it off" anchor.
        summary_lines = [
            f"✅ Sync <b>{html.escape(primary.email)}</b> selesai.",
            f"Total alamat di Proton: <b>{len(addresses)}</b>",
            f"Alias baru ditambahkan: <b>{inserted}</b>",
            f"Sudah ada di DB: <b>{skipped}</b>",
        ]
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(summary_lines),
            parse_mode=ParseMode.HTML,
        )
        await status.done(f"✅ {inserted} alias baru ditambahkan")

        # Offer to top the primary up to the soft target so the user
        # doesn't have to compute "current vs target" manually. The
        # button delegates to the existing CB_QUICK_GENADDR path so
        # /genaddr's random-suffix mode + concurrency guard apply.
        # ``alias_addresses`` already excludes the primary's own
        # email, so after the UPSERT it equals the alias count we
        # have on file for this primary.
        await _maybe_offer_alias_topup(
            bot,
            chat_id=chat_id,
            primary=primary,
            current_count=len(alias_addresses),
        )
    except Exception as exc:
        LOGGER.exception("sync: background task crashed")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Sync error untuk "
                    f"<code>{html.escape(primary.email)}</code>: "
                    f"<code>{html.escape(str(exc))}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        finally:
            await status.done("❌ Error")


async def _start_health_check_for_primary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    primary_id: int,
) -> None:
    """Resolve targets for ``primary_id`` and kick off a background check."""
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    primary = await db.get_primary_account(chat.id, primary_id)
    if primary is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Email utama tidak ditemukan."
        )
        return
    aliases = await db.list_aliases(chat.id, primary_id=primary_id)
    # Validate the primary itself + every alias under it. The primary is
    # always included even when it doesn't appear in the alias table:
    # the user expects "email utama" to be checked too (per their
    # description: "berikan tombol ceklis pada email utama").
    targets = [primary.email] + [a.email for a in aliases]
    _launch_health_check_task(
        context, chat_id=chat.id, primary=primary, targets=targets
    )


@_gate
async def cmd_cekimap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run an end-to-end health check: send a tagged test email from
    every alias of a chosen primary to a temp mailbox, then report
    which ones arrived.

    Lets the user validate that all aliases are still routing mail
    correctly through Bridge — useful for spotting an alias that
    silently stopped delivering after a Bridge restart, vault rewrite,
    or upstream Proton config change.

    Runs as a background task so the user can keep using other bot
    features (read mail, /list, /genaddr, …) while it executes; live
    progress is posted to the chat as each alias confirms (or times
    out).
    """
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Belum ada email utama. Kirim /connect dulu sebelum /cekimap."
        )
        return
    if len(primaries) == 1:
        # Single primary → skip the picker, run immediately.
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"🩺 Memulai health check untuk <b>{html.escape(primaries[0].email)}</b>"
            "...",
            parse_mode=ParseMode.HTML,
        )
        await _start_health_check_for_primary(
            update, context, primary_id=primaries[0].id
        )
        await _maybe_send_lock_reminder(update, context)
        return
    counts = await _alias_count_per_primary(db, chat.id, primaries)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "🩺 Pilih email utama yang mau di-cek IMAP listener-nya:",
        reply_markup=_build_cekimap_picker_keyboard(primaries, counts),
    )
    await _maybe_send_lock_reminder(update, context)


# --------------------------------------------------------------- /setprotonpw


@_gate
async def cmd_setprotonpw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the Proton master password used by /genaddr to drive the web UI.

    Single-primary chats skip the picker; multi-primary chats list every
    account so the user can pick which one to attach the password to.
    """
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    db = _bot_db(context)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Belum ada akun Proton. Kirim /connect dulu sebelum /setprotonpw."
        )
        return ConversationHandler.END

    if len(primaries) == 1:
        target = primaries[0]
        cast(dict, context.user_data)["setpw_primary_id"] = target.id
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Akan menyimpan password Proton untuk <b>{html.escape(target.email)}</b>.\n\n"
            "Kirim password Proton (master password) sekarang. "
            "<b>Hapus pesan password setelah aku konfirmasi</b> untuk mengurangi "
            "risiko kalau riwayat chat bocor.",
            parse_mode=ParseMode.HTML,
        )
        return SETPW_PASSWORD

    rows: list[list[InlineKeyboardButton]] = []
    for primary in primaries:
        rows.append(
            [
                InlineKeyboardButton(
                    f"📧 {primary.email}",
                    callback_data=f"setpw:{primary.id}",
                )
            ]
        )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Pilih akun Proton yang mau di-set passwordnya:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return SETPW_PICK_PRIMARY


async def setpw_quick_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point for the inline-button shortcut into /setprotonpw.

    Triggered by ``qsetpw:<primary_id>`` callback data emitted by the
    "Simpan password Proton" button shown after /connect. Skips the
    picker (we already know which primary the user is configuring) and
    drops straight to the password-entry state.
    """
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END
    await query.answer()
    try:
        primary_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return ConversationHandler.END
    db = _bot_db(context)
    primary = await db.get_primary_account(chat_id, primary_id)
    if primary is None:
        if query.message is not None:
            await query.message.reply_text("Akun tidak ditemukan.")
        return ConversationHandler.END
    cast(dict, context.user_data)["setpw_primary_id"] = primary.id
    if query.message is not None:
        await query.message.reply_text(
            f"Akan menyimpan password Proton untuk <b>{html.escape(primary.email)}</b>.\n\n"
            "Kirim password Proton (master password) sekarang. "
            "<b>Hapus pesan password setelah aku konfirmasi</b>.",
            parse_mode=ParseMode.HTML,
        )
    return SETPW_PASSWORD


async def setpw_pick_primary(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END
    await query.answer()
    if not query.data.startswith("setpw:"):
        return ConversationHandler.END
    try:
        primary_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return ConversationHandler.END
    db = _bot_db(context)
    primary = await db.get_primary_account_by_id(primary_id)
    if primary is None or primary.chat_id != chat_id:
        if query.message is not None:
            await query.message.reply_text("Akun tidak ditemukan.")
        return ConversationHandler.END
    cast(dict, context.user_data)["setpw_primary_id"] = primary.id
    if query.message is not None:
        await query.message.reply_text(
            f"Akan menyimpan password Proton untuk <b>{html.escape(primary.email)}</b>.\n\n"
            "Kirim password Proton (master password) sekarang. "
            "<b>Hapus pesan password setelah aku konfirmasi</b>.",
            parse_mode=ParseMode.HTML,
        )
    return SETPW_PASSWORD


async def setpw_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        await update.effective_message.reply_text("Password tidak boleh kosong.")  # type: ignore[union-attr]
        return SETPW_PASSWORD
    user_data = cast(dict, context.user_data)
    primary_id = user_data.pop("setpw_primary_id", None)
    if primary_id is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Sesi /setprotonpw kedaluwarsa. Mulai lagi."
        )
        return ConversationHandler.END

    db = _bot_db(context)
    cipher = _bot_cipher(context)
    encrypted = cipher.encrypt(text)
    ok = await db.set_proton_password(chat.id, primary_id, encrypted)
    if not ok:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Gagal menyimpan — akun mungkin sudah dihapus."
        )
        return ConversationHandler.END
    # Mark the chosen primary as active so the next /genaddr defaults to it
    # without the user having to also click an alias in /list. This is the
    # "least surprising" behaviour: the account you just configured is the
    # one we'll use.
    await db.set_active_primary(chat.id, primary_id)
    primary_obj = await db.get_primary_account(chat.id, primary_id)
    active_label = (
        f"<b>{html.escape(primary_obj.email)}</b>"
        if primary_obj is not None
        else "akun ini"
    )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"Password Proton tersimpan terenkripsi untuk {active_label}.\n"
        "<b>Sekarang hapus pesan passwordmu</b> dari chat ini.\n"
        f"Akun aktif sekarang: {active_label}. Pakai /genaddr untuk "
        "generate alamat.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# --------------------------------------------------------------- /genaddr


def _render_genaddr_body(
    *,
    count: int,
    primary_email: str,
    proxy_note: str,
    pattern: str,
    success_count: int,
    fail_count: int,
    recent: list[str],
    elapsed_seconds: int = 0,
    finished: bool = False,
) -> str:
    """Render the rolling body text on the /genaddr starter message.

    The user wanted the starter message itself to keep showing fresh
    counts so they don't have to scroll through the chat to see how
    many addresses have landed. Format mirrors the spec from the PR-C
    handoff with the time-based heartbeat refresh:

        🚀 Generate <count> alamat di background — <primary>
        📊 Sudah berhasil: <ok>/<count> sukses
        ⚠️ Gagal/duplikat: <fail>
        🪄 Terbaru: <recent_5>
        🔄 Sedang membuat alamat <ok+1>/<count> · live <mm:ss>
        Bot tetap responsif — kirim /list, /cekimap, atau perintah lain.

    The heartbeat line embeds the elapsed-time counter so the body
    text always changes between refreshes — that way the periodic
    task's ``editMessageText`` doesn't bounce off Telegram's
    "message is not modified" guard when no new alias landed in
    the last 5 seconds.

    Lines are kept compact (max one blank line) so the message doesn't
    push other chat content off the screen while it's still ticking.
    """
    recent_html = ", ".join(
        html.escape(e) for e in recent[-GENADDR_BODY_RECENT_COUNT:]
    )
    if not recent_html:
        recent_line = "🪄 Terbaru: <i>(belum ada)</i>"
    else:
        recent_line = f"🪄 Terbaru: <code>{recent_html}</code>"
    pattern_line = f"Pola: {pattern}\n" if pattern else ""
    mm, ss = divmod(max(0, elapsed_seconds), 60)
    elapsed_str = f"{mm}:{ss:02d}"
    if finished:
        heartbeat_line = f"✅ Selesai · live {elapsed_str}"
    elif success_count >= count:
        heartbeat_line = f"⏳ Menutup browser… · live {elapsed_str}"
    else:
        next_idx = success_count + 1
        heartbeat_line = (
            f"🔄 Sedang membuat alamat <b>{next_idx}/{count}</b> · "
            f"live {elapsed_str}"
        )
    return (
        f"🚀 Generate <b>{count}</b> alamat di background — "
        f"<b>{html.escape(primary_email)}</b>{proxy_note}\n"
        f"{pattern_line}"
        f"📊 Sudah berhasil: <b>{success_count}/{count}</b> sukses\n"
        f"⚠️ Gagal/duplikat: <b>{fail_count}</b>\n"
        f"{recent_line}\n"
        f"{heartbeat_line}\n"
        f"\n"
        f"Bot tetap responsif — kirim /list, /cekimap, atau perintah lain "
        f"sambil generate jalan. Update tiap <b>5 detik</b>."
    )


async def _pick_primary_for_genaddr(
    *,
    db: Database,
    chat_id: int,
    primaries: list,
    base: str,
) -> tuple[Any, str]:
    """Pick which primary account ``/genaddr`` should run against.

    Selection priority (most specific wins):

    1. Exact local-part match against ``base`` (e.g. ``/genaddr vielz64`` on
       a chat that owns ``vielz64@proton.me``). Most natural UX: the user's
       first arg already names the account.
    2. Persisted active primary (set by ``/setprotonpw`` or a previous
       ``/genaddr``).
    3. Active alias's primary -- legacy fallback for chats that haven't
       picked an account yet.
    4. First primary -- better than crashing.

    Returns ``(primary, reason)`` where ``reason`` is a short Indonesian
    label suitable for showing the user so they understand which account
    we picked and why.
    """
    base_lc = base.strip().lower()
    matches = [p for p in primaries if p.email.split("@", 1)[0].lower() == base_lc]
    if len(matches) == 1:
        return matches[0], "cocok dengan base"

    active_primary_id = await db.get_active_primary_id(chat_id)
    if active_primary_id is not None:
        active = next(
            (p for p in primaries if p.id == active_primary_id), None
        )
        if active is not None:
            return active, "akun aktif"

    active_alias = await db.get_active_alias(chat_id)
    if active_alias is not None and active_alias.primary_id:
        from_alias = next(
            (p for p in primaries if p.id == active_alias.primary_id), None
        )
        if from_alias is not None:
            return from_alias, "dari alias aktif"

    return primaries[0], "default (akun pertama)"


async def _safe_force_close(force_close) -> None:  # type: ignore[no-untyped-def]
    """Run ``ProtonBrowser.force_close()`` from the cancel callback.

    Wraps the call in a broad ``try/except`` because the callback fires it
    off as a fire-and-forget task — an unhandled exception there would only
    surface as a noisy "Task exception was never retrieved" warning.
    """
    try:
        await force_close()
    except Exception:
        LOGGER.exception("force_close raised while cancelling /genaddr")


def _signal_genaddr_cancel(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Best-effort cancel of any /genaddr currently running in this chat.

    Returns ``True`` if a running task was found and the cancel signal
    was delivered (cancel_event set + browser force-close scheduled).
    Returns ``False`` if no /genaddr was running for this chat.

    Why this is a separate helper:

    The user reported that ``/disconnect`` (delete primary account) did
    NOT abort an in-flight ``/genaddr`` — the Playwright browser kept
    creating addresses against the about-to-be-deleted account, with
    those new aliases dropping on the floor when the primary was wiped
    from the DB seconds later. The fix is the same logic the
    ``CB_GENADDR_CANCEL`` button runs (``cancel_event.set()`` +
    ``browser.force_close()``), so this helper centralises it for
    reuse from both the explicit cancel button and the delete-primary
    flow.
    """
    cancel_event = context.chat_data.get("genaddr_cancel_event")
    if cancel_event is None:
        return False
    cancel_event.set()
    browser_handle = context.chat_data.get("genaddr_browser_handle")
    browser = (
        browser_handle.get("browser")
        if isinstance(browser_handle, dict)
        else None
    )
    force_close = getattr(browser, "force_close", None) if browser else None
    if callable(force_close):
        # Hold a reference on chat_data so the GC doesn't collect the
        # task before it finishes (RUF006). The genaddr ``finally`` clears
        # this slot, which is fine — by then the task is done or the next
        # /genaddr will overwrite it.
        context.chat_data["genaddr_force_close_task"] = asyncio.create_task(
            _safe_force_close(force_close)
        )
    return True


@_gate
async def cmd_genaddr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate N Proton addresses by driving the account web UI.

    Usage: ``/genaddr <base> <count> [@domain]`` — e.g. ``/genaddr vielz 10``.
    Defaults to ``@proton.me``. Aborts with a clear error if the account has
    no Proton master password stored yet.
    """
    chat = update.effective_chat
    if chat is None:
        return
    args = list(context.args or [])
    if len(args) < 2:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Pakai: <code>/genaddr &lt;base&gt; &lt;count&gt; [@domain]</code>\n"
            "Contoh: <code>/genaddr vielz 10</code> → buat vielz001..vielz010 di @proton.me\n\n"
            f"Maksimal <b>{GENADDR_MAX_COUNT}</b> per perintah. Jalankan /setprotonpw "
            "dulu kalau belum.",
            parse_mode=ParseMode.HTML,
        )
        return
    base = args[0]
    try:
        count = int(args[1])
    except ValueError:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Argumen kedua harus angka (jumlah alamat)."
        )
        return
    if count < 1 or count > GENADDR_MAX_COUNT:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Jumlah harus 1..{GENADDR_MAX_COUNT}."
        )
        return
    domain = args[2] if len(args) >= 3 else GENADDR_DEFAULT_DOMAIN
    domain = domain.lstrip("@")

    # Re-entrancy check FIRST — before any other side effect (DB lookup,
    # disambiguation message). Stops the duplicate-message spam when the
    # user double-clicks a quick-action button: the second click would
    # previously print "Akun yang dipakai: …" + "Masih ada /genaddr"
    # both. Now it dedup-guards on a per-chat flag and only emits the
    # warning once per running task.
    if context.chat_data.get("genaddr_running"):
        if not context.chat_data.get("genaddr_warned_running"):
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "⚠️ Masih ada /genaddr lain yang berjalan di chat ini. "
                "Tunggu selesai atau klik tombol Batalkan di pesan progressnya.",
            )
            context.chat_data["genaddr_warned_running"] = True
        return

    db = _bot_db(context)
    # Cipher is fetched lazily inside the background task. Verify it's
    # configured here so the user gets an immediate error instead of one
    # that only surfaces after they've waited for the browser to spin up.
    _bot_cipher(context)
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Belum ada akun Proton. Kirim /connect lalu /setprotonpw lebih dulu."
        )
        return
    primary, primary_pick_reason = await _pick_primary_for_genaddr(
        db=db, chat_id=chat.id, primaries=primaries, base=base
    )
    # Persist the picked primary so subsequent commands keep using the same
    # account until the user explicitly switches via /setprotonpw or /list.
    await db.set_active_primary(chat.id, primary.id)
    # Quick-action callbacks (CB_QUICK_GENADDR) explicitly pick the
    # primary by id, so the disambiguation message just adds noise.
    # ``genaddr_silent_pick`` is consumed (popped) here so any later
    # typed ``/genaddr`` still shows the message in multi-primary chats.
    silent_pick = bool(context.user_data.pop("genaddr_silent_pick", False))
    if len(primaries) > 1 and not silent_pick:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Akun yang dipakai: <b>{html.escape(primary.email)}</b> "
            f"({primary_pick_reason}). Ganti dengan /setprotonpw atau "
            "klik alias di /list.",
            parse_mode=ParseMode.HTML,
        )

    # The cancel button must remain visible alongside the live status
    # row that narrates the current phase ("🌐 Buka browser…",
    # "🔓 Login Proton…", …). Pass it as ``extra_rows`` to the
    # StatusReporter so every editMessageReplyMarkup tick keeps the
    # button on screen.
    cancel_button_row = [
        InlineKeyboardButton("❌ Batalkan", callback_data=CB_GENADDR_CANCEL)
    ]
    proxy_provider = context.application.bot_data.get("proxy_provider")
    proxy_note = " via proxy rotasi" if proxy_provider is not None else ""
    # Quick-action button (CB_QUICK_GENADDR) sets this flag in user_data
    # before delegating to cmd_genaddr so its batch uses the random-suffix
    # naming scheme ("vielz88311 / vielz88347 / …") the user requested.
    # The plain /genaddr command keeps the legacy sequential cursor.
    random_suffix = bool(context.user_data.pop("genaddr_random_suffix", False))
    if random_suffix:
        # Match the digit width that ``random_suffix_names`` will pick for
        # this batch so the example shown to the user lines up with what
        # actually shows up in their inbox. ``max(2, len(str(count - 1)))``
        # mirrors the formula in alias_gen.random_suffix_names — kept here
        # so the start-message string can render an accurate placeholder.
        random_digits = max(2, len(str(max(count - 1, 1))))
        pattern = (
            f"<code>{html.escape(base)}{'N' * random_digits}@"
            f"{html.escape(domain)}</code> (suffix random)"
        )
    else:
        pattern = (
            f"<code>{html.escape(base)}NNN@{html.escape(domain)}</code>"
        )
    # Background mode: send a single starting message that doubles as
    # the live-status anchor. Two layers of feedback ride on this
    # message:
    #   1. The status row (StatusReporter editMessageReplyMarkup) above
    #      ❌ Batalkan narrates the current phase, mirroring /cekimap.
    #   2. The message body (editMessageText, throttled to 2s) shows a
    #      rolling "Sudah berhasil: N/T sukses · Gagal/duplikat: M ·
    #      Terbaru: …" block so the user always has a fresh count
    #      visible without scrolling the chat.
    starter_text = _render_genaddr_body(
        count=count,
        primary_email=primary.email,
        proxy_note=proxy_note,
        pattern=pattern,
        success_count=0,
        fail_count=0,
        recent=[],
    )
    starter_message = await update.effective_message.reply_text(  # type: ignore[union-attr]
        starter_text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_status_keyboard(
            "🚀 Mulai…", extra_rows=[cancel_button_row]
        ),
    )

    cancel_event = asyncio.Event()
    # ``browser_handle`` is shared between this handler and the cancel
    # callback. ``run_batch`` populates it with the live ProtonBrowser the
    # moment the session is open, so the callback can ``force_close()`` it
    # mid-iteration instead of waiting for Playwright's per-step 60s timeout.
    browser_handle: dict[str, object] = {}
    context.chat_data["genaddr_running"] = True
    context.chat_data["genaddr_cancel_event"] = cancel_event
    context.chat_data["genaddr_browser_handle"] = browser_handle

    _launch_genaddr_task(
        context,
        chat_id=chat.id,
        primary=primary,
        base=base,
        count=count,
        domain=domain,
        cancel_event=cancel_event,
        browser_handle=browser_handle,
        proxy_provider=proxy_provider,
        random_suffix=random_suffix,
        starter_message=starter_message,
        cancel_button_row=cancel_button_row,
        proxy_note=proxy_note,
        pattern=pattern,
    )
    # PR-F: nudge the user about the still-locked alias right after the
    # background task is launched. The reminder is intentionally posted
    # AFTER the starter message so it lands underneath, where the user
    # is most likely looking.
    await _maybe_send_lock_reminder(update, context)


def _launch_genaddr_task(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    primary: PrimaryAccount,
    base: str,
    count: int,
    domain: str,
    cancel_event: asyncio.Event,
    browser_handle: dict[str, object],
    proxy_provider,
    random_suffix: bool = False,
    starter_message: Any = None,
    cancel_button_row: list[InlineKeyboardButton] | None = None,
    proxy_note: str = "",
    pattern: str = "",
) -> None:
    """Spawn ``_run_genaddr_background`` as a tracked asyncio task.

    Mirrors the ``_launch_health_check_task`` pattern: keeps a strong
    reference on ``application.bot_data['genaddr_tasks']`` so the task
    isn't GC'd before completion, and sweeps already-finished tasks out
    of the list on every new launch.
    """
    task_list = context.application.bot_data.setdefault("genaddr_tasks", [])
    task_list[:] = [t for t in task_list if not t.done()]
    task = asyncio.create_task(
        _run_genaddr_background(
            context,
            chat_id=chat_id,
            primary=primary,
            base=base,
            count=count,
            domain=domain,
            cancel_event=cancel_event,
            browser_handle=browser_handle,
            proxy_provider=proxy_provider,
            random_suffix=random_suffix,
            starter_message=starter_message,
            cancel_button_row=cancel_button_row,
            proxy_note=proxy_note,
            pattern=pattern,
        ),
        name=f"genaddr-{primary.id}-{count}",
    )
    task_list.append(task)


async def _run_genaddr_background(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    primary: PrimaryAccount,
    base: str,
    count: int,
    domain: str,
    cancel_event: asyncio.Event,
    browser_handle: dict[str, object],
    proxy_provider,
    random_suffix: bool = False,
    starter_message: Any = None,
    cancel_button_row: list[InlineKeyboardButton] | None = None,
    proxy_note: str = "",
    pattern: str = "",
) -> None:
    """Run the actual address-creation batch as a background task.

    Three layers of feedback during the run:

    * **Live status button** on the starter message — narrates the
      current phase ("🌐 Buka browser…", "🪄 1/20 sukses", "🔁 Rotasi
      proxy"). Edits :attr:`starter_message.message_id` via
      ``editMessageReplyMarkup`` so the parent text isn't churned.
      The ❌ Batalkan row stays pinned underneath it.
    * **Periodic progress messages** every ``GENADDR_NOTIFY_EVERY``
      successes with the last batch of newly-created emails so the
      user can sanity-check.
    * **Final summary** with optional "🩺 Cek hasil sekarang" CTA.

    All transient messages are tracked via :class:`TaskMessageTracker`
    and auto-deleted after a short delay, after which a fresh
    per-primary ``/list`` keyboard is rendered. Cancellation runs
    through the same path so the cancelled chat ends up just as clean.
    """
    bot = context.application.bot
    db = _bot_db(context)
    cipher = _bot_cipher(context)

    tracker = TaskMessageTracker(bot, chat_id)
    tracker.track(starter_message)

    starter_msg_id = (
        getattr(starter_message, "message_id", None)
        if starter_message is not None
        else None
    )
    extra_rows = [cancel_button_row] if cancel_button_row else None
    status: StatusReporter | None = (
        StatusReporter(
            bot,
            chat_id,
            starter_msg_id,
            extra_rows=extra_rows,
            idle_label="✅ Selesai",
        )
        if starter_msg_id is not None
        else None
    )

    async def _status(label: str, *, force: bool = False) -> None:
        if status is not None:
            await status.update(label, force=force)

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    last_notified_count = 0
    # Throttle live body edits so we never blow past Telegram's
    # ~1 edit/second-per-chat limit on editMessageText, even when the
    # browser is bursting through addresses fast. Force-edit on each
    # GENADDR_NOTIFY_EVERY milestone so the user always sees a fresh
    # count at those points (which is also when a new progress message
    # was previously posted).
    last_body_edit_at = 0.0
    started_at = asyncio.get_event_loop().time()
    # Set to ``True`` by ``_run_genaddr_background`` once the batch has
    # finished (success, error, or cancellation) so the heartbeat
    # renderer can switch to the "✅ Selesai" line on the final tick.
    body_finished = False

    async def _edit_body(*, force: bool = False) -> None:
        nonlocal last_body_edit_at
        if starter_msg_id is None:
            return
        now = asyncio.get_event_loop().time()
        if not force and now - last_body_edit_at < GENADDR_BODY_EDIT_INTERVAL_S:
            return
        elapsed = max(0, int(now - started_at))
        text = _render_genaddr_body(
            count=count,
            primary_email=primary.email,
            proxy_note=proxy_note,
            pattern=pattern,
            success_count=len(successes),
            fail_count=len(failures),
            recent=successes,
            elapsed_seconds=elapsed,
            finished=body_finished,
        )
        # ``editMessageText`` raises ``BadRequest("message is not modified")``
        # when the rendered text is byte-identical to the previous edit
        # (e.g. two consecutive failures without any new success).
        # Suppressing it keeps the task running without polluting the log
        # — losing one body refresh is fine, the next progress event will
        # re-render anyway.
        with contextlib.suppress(BadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=starter_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        last_body_edit_at = now

    async def _heartbeat_loop() -> None:
        """Refresh the starter message body every 5 seconds.

        The user reported the progress message felt "static" because
        :func:`_edit_body` only fired on the success callback — during
        long browser-startup / login / captcha pauses, no new alias
        landed for tens of seconds and the message just sat there.
        This task force-edits the body on a steady cadence
        (``GENADDR_BODY_REFRESH_INTERVAL_S``) so the elapsed-time
        counter ticks even when the count is stuck. Cancelled in the
        ``finally`` block of the parent run.
        """
        while True:
            try:
                await asyncio.sleep(GENADDR_BODY_REFRESH_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            try:
                await _edit_body(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.debug(
                    "genaddr heartbeat body edit failed", exc_info=True
                )

    async def _on_progress(success_count: int, target: int, result) -> None:
        nonlocal last_notified_count
        if result.status is CreationStatus.SUCCESS:
            successes.append(result.email)
        else:
            failures.append((result.email, result.status.value))

        # The user reported the inline keyboard "muncul redup muncul
        # redup" — flickering on every address — because every progress
        # callback used to call ``_status`` which fires
        # ``editMessageReplyMarkup`` and forces Telegram clients to
        # redraw both the status row and the ❌ Batalkan row underneath
        # it. We now only update the status button on milestones (every
        # ``GENADDR_NOTIFY_EVERY`` successes) and on the very last
        # alias, which keeps the keyboard stable in between. Live
        # progress is still visible — the body refreshes every 5s via
        # the heartbeat task.
        is_milestone = (
            success_count > 0
            and (
                success_count % GENADDR_NOTIFY_EVERY == 0
                or success_count == target
            )
        )
        if is_milestone:
            if result.status is CreationStatus.SUCCESS:
                label = f"🪄 {success_count}/{target} sukses"
            else:
                label = f"⚠️ {len(failures)} gagal/duplikat"
            await _status(label)

        # Body live update — runs on every progress callback but is
        # internally throttled to the 2-second interval. We force-edit
        # on milestones (multiples of GENADDR_NOTIFY_EVERY) and on the
        # final success so the user always sees the milestone counts.
        # The 5-second heartbeat task picks up the slack between
        # milestones so the message keeps ticking even when the browser
        # is stuck on login or captcha.
        await _edit_body(force=is_milestone)

        # Only post a new message when we cross a multiple of NOTIFY_EVERY
        # (or on the very last success), so the chat doesn't get spammed
        # for every single address.
        if success_count == last_notified_count:
            return
        if (
            success_count % GENADDR_NOTIFY_EVERY != 0
            and success_count != target
        ):
            return
        last_notified_count = success_count
        # Show the last 5 created emails as a hint of what just landed,
        # so the user can immediately verify progress is real.
        recent_window = successes[
            max(0, success_count - GENADDR_NOTIFY_EVERY) : success_count
        ]
        recent_html = ", ".join(html.escape(e) for e in recent_window)
        try:
            tracker.track(
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔄 <b>{success_count}/{target}</b> alamat sukses di "
                        f"<b>{html.escape(primary.email)}</b>\n"
                        f"⚠️ Gagal/duplikat sejauh ini: <b>{len(failures)}</b>\n"
                        f"Terbaru: <code>{recent_html}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            )
        except Exception:
            # Losing a progress update is fine — the final summary is
            # what matters.
            LOGGER.debug("genaddr background progress send failed", exc_info=True)

    summary = None
    final_status_label = "✅ Selesai"
    # 5-second heartbeat: refreshes the starter message body so the
    # user sees a ticking "live" indicator even while the browser is
    # busy logging in / solving captcha (no progress callbacks fire
    # during those phases). ``starter_msg_id is None`` happens in
    # tests that drive the flow without a real anchor message — skip
    # the heartbeat there to keep the test surface stable.
    heartbeat_task: asyncio.Task | None = None
    if starter_msg_id is not None:
        heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        try:
            await _status("🌐 Buka browser proxy & login Proton…", force=True)
            # Force one initial body render so the user sees the new
            # heartbeat line immediately instead of waiting up to 5s
            # for the first periodic tick.
            await _edit_body(force=True)
            summary = await address_generator.run_batch(
                db=db,
                cipher=cipher,
                chat_id=chat_id,
                primary=primary,
                base=base,
                count=count,
                domain=domain,
                browser_factory=context.application.bot_data.get("browser_factory"),
                progress=_on_progress,
                cancel_event=cancel_event,
                browser_handle=browser_handle,
                proxy_provider=proxy_provider,
                random_suffix=random_suffix,
            )
        except address_generator.AddressGenerationError as exc:
            final_status_label = "❌ Tidak bisa mulai"
            tracker.track(
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ /genaddr untuk <b>{html.escape(primary.email)}</b> "
                        f"tidak bisa mulai: {html.escape(str(exc))}\n\n"
                        "Kalau belum, set password Proton dengan /setprotonpw."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            )
        except Exception as exc:
            # If the user already pressed ❌ Batalkan or fired
            # /disconnect, the in-flight Playwright operation raises
            # ``TargetClosedError`` (or similar) as a *side-effect* of
            # the force_close we just performed. That's not a genuine
            # crash — surface it as a clean cancellation instead so
            # the user doesn't see a scary "browser otomasi crash"
            # report after intentionally pulling the plug.
            if cancel_event.is_set():
                LOGGER.info(
                    "genaddr cancelled mid-flight (browser closed): %s", exc
                )
                final_status_label = "❌ Dibatalkan"
                tracker.track(
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"❌ /genaddr untuk <b>{html.escape(primary.email)}</b>"
                            " dibatalkan. Browser sudah ditutup."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                )
            else:
                LOGGER.exception("genaddr background crashed")
                final_status_label = "❌ Browser crash"
                tracker.track(
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"❌ /genaddr untuk <b>{html.escape(primary.email)}</b>: "
                            f"browser otomasi crash.\n"
                            f"Detail: <code>"
                            f"{html.escape(str(exc) or type(exc).__name__)}</code>\n\n"
                            "Screenshot + HTML halaman terakhir disimpan di "
                            "<code>/tmp/proton-browser-debug/</code> dalam container.\n"
                            "Ambil dengan: <code>docker compose cp "
                            "bot:/tmp/proton-browser-debug ./debug</code>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                )

        if summary is not None:
            if cancel_event.is_set():
                final_status_label = (
                    f"❌ Dibatalkan ({len(summary.created)} sudah jadi)"
                )
            elif summary.captcha_interrupted_at:
                final_status_label = "⚠️ CAPTCHA — selesai sebagian"
            else:
                final_status_label = (
                    f"✅ Selesai · {len(summary.created)}/{count} sukses"
                )
            await _post_genaddr_summary(
                bot=bot,
                chat_id=chat_id,
                primary=primary,
                summary=summary,
                tracker=tracker,
            )
    finally:
        # Stop the body heartbeat first so it doesn't race with the
        # final body edit / status.done() below. Force one last body
        # render with ``finished=True`` so the user sees ``✅ Selesai``
        # in the heartbeat line even before the tracker cleans up.
        body_finished = True
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        with contextlib.suppress(Exception):
            await _edit_body(force=True)
        chat_data = context.application.chat_data.get(chat_id)
        if chat_data is not None:
            chat_data.pop("genaddr_running", None)
            chat_data.pop("genaddr_cancel_event", None)
            chat_data.pop("genaddr_browser_handle", None)
            chat_data.pop("genaddr_force_close_task", None)
            # Reset the dedup flag so the next /genaddr's re-entrancy
            # warning (if any) prints again as a first-time event.
            chat_data.pop("genaddr_warned_running", None)
        if status is not None:
            try:
                await status.done(final_status_label)
            except Exception:
                LOGGER.debug(
                    "genaddr: status.done failed", exc_info=True
                )
        await tracker.cleanup(
            after=lambda: _render_primary_alias_list(
                bot, db, chat_id, primary
            )
        )


async def _post_genaddr_summary(
    *,
    bot: Any,
    chat_id: int,
    primary: PrimaryAccount,
    summary: Any,
    tracker: TaskMessageTracker,
) -> None:
    """Send the final ``/genaddr selesai`` summary message.

    Split out from :func:`_run_genaddr_background` so the (long)
    summary-formatting block doesn't clutter the orchestration. The
    summary is tracked too — it disappears after the cleanup delay so
    the chat returns to a clean ``/list`` view.
    """
    final_lines = [
        f"✨ <b>/genaddr selesai</b> di <b>{html.escape(primary.email)}</b>.",
        f"Sukses: <b>{len(summary.created)}</b>, "
        f"sudah ada: <b>{len(summary.already_existing)}</b>, "
        f"gagal: <b>{len(summary.failed)}</b>.",
    ]
    if summary.captcha_interrupted_at:
        final_lines.append(
            f"⚠️ Berhenti di <code>"
            f"{html.escape(summary.captcha_interrupted_at)}</code> "
            "karena CAPTCHA. Solve manual lalu jalankan ulang /genaddr."
        )
    if summary.aborted_reason and not summary.captcha_interrupted_at:
        final_lines.append(f"ℹ️ {html.escape(summary.aborted_reason)}")  # noqa: RUF001
    if summary.created:
        sample = ", ".join(r.email for r in summary.created[:5])
        more = (
            "" if len(summary.created) <= 5
            else f" (+{len(summary.created) - 5} lagi)"
        )
        final_lines.append(f"Contoh: <code>{html.escape(sample)}</code>{more}")
    final_lines.append(
        "\nKlik /list buat lihat semuanya, atau klik tombol di bawah "
        "untuk validasi alias yang baru dibuat sudah bisa terima email."
    )
    # Surface a one-tap "Cek IMAP listener" entry to /cekimap (same
    # callback the after-connect onboarding uses). Only show it when at
    # least one alias was actually created — there's nothing to verify
    # otherwise. Reuses CB_QUICK_HEALTHCHECK so we don't introduce a
    # second router branch.
    reply_markup: InlineKeyboardMarkup | None = None
    if summary.created:
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🩺 Cek hasil sekarang ({len(summary.created)} alias)",
                        callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary.id}",
                    )
                ]
            ]
        )
    try:
        tracker.track(
            await bot.send_message(
                chat_id=chat_id,
                text="\n".join(final_lines),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        )
    except Exception:
        LOGGER.exception("genaddr final summary send failed")


# --------------------------------------------------------------- callback queries


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return
    db = _bot_db(context)
    data = query.data
    if data == CB_NOOP:
        return
    if data.startswith(f"{CB_TAG_SERVICE_CLEAR}:"):
        # ``tagsvc_clear:<alias_id>:<domain>`` — explicitly suppress the
        # label so even built-in defaults (cognition.ai → Devin, …)
        # stop showing up in /list and the email header. We write an
        # empty-string chat row instead of deleting the row, so the
        # resolver knows to stop walking the parent-domain chain.
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Data tag tidak valid.", show_alert=True)
            return
        domain = parts[2].strip().lower()
        await db.set_service_label(chat_id, domain, "")
        await query.answer(f"Label untuk {domain} dihapus.")
        # Refresh the inline keyboard on the email message so the
        # "Tag ulang" button shows the new state immediately. After
        # suppression, the resolver returns None → only the
        # "Tag ulang" button remains (no "Hapus label").
        try:
            new_label = await db.resolve_service_label(chat_id, domain)
            try:
                alias_id = int(parts[1])
            except ValueError:
                alias_id = -1
            if alias_id > 0:
                await query.edit_message_reply_markup(
                    reply_markup=_build_tag_service_keyboard(
                        alias_id=alias_id,
                        sender_domain=domain,
                        current_label=new_label,
                    )
                )
        except Exception:
            pass
        return
    if data.startswith(f"{CB_SVC_DELETE}:"):
        # ``svcdel:<domain>`` from the /services keyboard.
        domain = data.split(":", 1)[1].strip().lower()
        if not domain:
            await query.answer("Domain kosong.", show_alert=True)
            return
        removed = await db.remove_service_label(chat_id, domain)
        if removed:
            await query.answer(f"🗑 Mapping {domain} dihapus.")
        else:
            await query.answer(
                "Mapping tidak ditemukan (mungkin sudah dihapus).",
                show_alert=True,
            )
        # Re-render the /services keyboard in place.
        try:
            user_labels = await db.list_service_labels(chat_id)
            await query.edit_message_reply_markup(
                reply_markup=_build_services_keyboard(user_labels)
            )
        except Exception:
            pass
        return
    if data == CB_GENADDR_CANCEL:
        # ``_signal_genaddr_cancel`` returns False when there's no
        # task to cancel — same UX as before (alert + clear keyboard).
        # When there IS a task, it sets the cancel_event and schedules
        # the Playwright force-close as a fire-and-forget task, so
        # any in-flight ``page.click`` / ``wait_for_url`` raises
        # ``TargetClosedError`` immediately instead of running out
        # its 60s timeout.
        cancelled = _signal_genaddr_cancel(context)
        if not cancelled:
            await query.answer(
                "Tidak ada /genaddr aktif untuk dibatalkan.", show_alert=False
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        await query.answer("Membatalkan & menutup browser...")
        try:
            # Disable the button immediately so the user knows their click
            # registered. The progress edits will still come in until the
            # in-flight create_address resolves.
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if data == CB_REFRESH or data == CB_BACK_TO_PRIMARIES:
        primaries = await db.list_primary_accounts(chat_id)
        counts = await _alias_count_per_primary(db, chat_id, primaries)
        healthcheck_stats = await db.get_last_healthcheck_stats(chat_id)
        active = await db.get_active_alias(chat_id)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_build_primary_keyboard(
                    primaries,
                    counts,
                    active.primary_id if active else None,
                    healthcheck_stats=healthcheck_stats,
                    with_copy_active_button=active is not None,
                )
            )
        except Exception:
            pass
        return
    if data.startswith(f"{CB_PRIMARY_PAGE}:"):
        # Top-level primary list pagination. ``ppg:<page>`` re-renders the
        # same /list keyboard at the requested page; the message text
        # itself stays as-is (page count fits in the keyboard footer).
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            return
        primaries = await db.list_primary_accounts(chat_id)
        counts = await _alias_count_per_primary(db, chat_id, primaries)
        healthcheck_stats = await db.get_last_healthcheck_stats(chat_id)
        active = await db.get_active_alias(chat_id)
        with contextlib.suppress(BadRequest, Exception):
            await query.edit_message_reply_markup(
                reply_markup=_build_primary_keyboard(
                    primaries,
                    counts,
                    active.primary_id if active else None,
                    healthcheck_stats=healthcheck_stats,
                    with_copy_active_button=active is not None,
                    page=page,
                )
            )
        return
    if data.startswith(f"{CB_ALIAS_PAGE}:"):
        # Alias drill-down pagination. ``apg:<primary_id>:<page>`` swaps
        # the visible window of aliases under a primary without changing
        # the message text (which already shows ``N alias`` total).
        rest = data.split(":", 2)
        if len(rest) != 3:
            return
        try:
            primary_id = int(rest[1])
            page = int(rest[2])
        except ValueError:
            return
        primary = await db.get_primary_account(chat_id, primary_id)
        if primary is None:
            return
        aliases = await db.list_aliases(chat_id, primary_id=primary_id)
        active = await db.get_active_alias(chat_id)
        active_alias_id = (
            active.id
            if active is not None and active.primary_id == primary_id
            else None
        )
        with contextlib.suppress(BadRequest, Exception):
            await query.edit_message_reply_markup(
                reply_markup=_build_alias_keyboard_for_primary(
                    primary, aliases, active_alias_id, page=page
                )
            )
        return
    if data.startswith(f"{CB_PICK_PRIMARY}:"):
        raw_id = data.split(":", 1)[1]
        try:
            primary_id = int(raw_id)
        except ValueError:
            await query.answer("Email utama tidak valid.", show_alert=True)
            return
        primary = await db.get_primary_account(chat_id, primary_id)
        if primary is None:
            await query.answer("Email utama tidak ditemukan.", show_alert=True)
            return
        aliases = await db.list_aliases(chat_id, primary_id=primary_id)
        active = await db.get_active_alias(chat_id)
        active_alias_id = (
            active.id
            if active is not None and active.primary_id == primary_id
            else None
        )
        try:
            labels_by_alias = await db.get_alias_service_labels(
                chat_id, primary_id=primary_id
            )
        except Exception:
            labels_by_alias = {}
        try:
            await query.edit_message_text(
                _build_alias_list_text(
                    primary, aliases, labels_by_alias, active_alias_id
                ),
                reply_markup=_build_alias_keyboard_for_primary(
                    primary, aliases, active_alias_id
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return
    if data.startswith(f"{CB_DEL_PRIMARY}:"):
        raw_id = data.split(":", 1)[1]
        try:
            primary_id = int(raw_id)
        except ValueError:
            await query.answer("Email utama tidak valid.", show_alert=True)
            return
        primary = await db.get_primary_account(chat_id, primary_id)
        if primary is None:
            await query.answer("Email utama tidak ditemukan.", show_alert=True)
            return
        # Disconnect must abort any /genaddr that is currently running
        # in this chat. The user reported the Playwright browser kept
        # creating addresses after they hit "❌ Hapus" — those new
        # aliases dropped on the floor when the primary's DB row got
        # wiped seconds later, and the cookies/cert the browser was
        # holding became invalid the moment the Bridge account was
        # logged out. Cancel BEFORE running ``stop_for_primary`` /
        # ``remove_account`` / ``delete_primary_account`` so the
        # browser tears down cleanly instead of racing the deletion.
        genaddr_was_cancelled = _signal_genaddr_cancel(context)
        # Disconnect can take 10-30s end-to-end (stop_for_primary →
        # bridge --cli delete account → bridge restart → DB delete).
        # Without a live anchor the user just stares at the unchanged
        # picker message wondering if the bot died — exactly the
        # complaint that drove the /connect progress indicator. Anchor
        # the same StatusReporter pattern here so every >3s phase is
        # narrated. Best-effort: if the anchor send fails (rare) we
        # silently fall back to the original code path.
        disconnect_status: StatusReporter | None = None
        disconnect_initial_label = (
            "🛑 Stop /genaddr yang sedang jalan…"
            if genaddr_was_cancelled
            else "⏳ Mempersiapkan hapus akun…"
        )
        try:
            anchor = await query.message.reply_text(  # type: ignore[union-attr]
                f"🗑️ Hapus akun <b>{html.escape(primary.email)}</b>…",
                parse_mode=ParseMode.HTML,
                reply_markup=build_status_keyboard(disconnect_initial_label),
            )
        except Exception:
            anchor = None
        if anchor is not None:
            disconnect_status = StatusReporter(
                context.application.bot,
                chat_id,
                anchor.message_id,
                initial_label=disconnect_initial_label,
            )
        manager = _bot_manager(context)
        if disconnect_status is not None:
            with contextlib.suppress(Exception):
                await disconnect_status.update("🛑 Stop IMAP listener…")
        await manager.stop_for_primary(primary_id)
        # Best-effort logout from the host Proton Bridge so a future
        # /connect for the same email is a clean slate (no cached
        # credentials, no stale message UID baseline). DB cleanup runs
        # regardless of the Bridge-side outcome.
        bridge_admin = _bot_bridge_admin(context)
        bridge_removed = False
        if bridge_admin is not None:
            if disconnect_status is not None:
                with contextlib.suppress(Exception):
                    await disconnect_status.update(
                        "🗑️ Hapus akun dari Proton Bridge vault…"
                    )
            try:
                bridge_removed = await bridge_admin.remove_account(primary.email)
            except Exception:
                LOGGER.exception(
                    "bridge_admin.remove_account failed for %s",
                    primary.email,
                )
        if disconnect_status is not None:
            with contextlib.suppress(Exception):
                await disconnect_status.update(
                    "🗄️ Hapus alias + kredensial dari DB…"
                )
        await db.delete_primary_account(chat_id, primary_id)
        if disconnect_status is not None:
            with contextlib.suppress(Exception):
                await disconnect_status.done(
                    "✅ Akun dihapus" if bridge_removed or bridge_admin is None
                    else "⚠️ DB dihapus, Bridge perlu cek manual"
                )
        if bridge_admin is None:
            suffix = ""
        elif bridge_removed:
            suffix = "Cache Proton Bridge juga sudah di-purge."
        else:
            suffix = (
                "Catatan: Bridge tidak sepenuhnya dibersihkan otomatis — "
                "kalau /connect berikutnya error, jalankan "
                "<code>bridge --cli</code> → <code>delete</code> manual."
            )
        try:
            text = (
                f"❌ Akun <b>{html.escape(primary.email)}</b> + alias-aliasnya "
                "dihapus, listener dihentikan."
            )
            if suffix:
                text = f"{text}\n\n{suffix}"
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_build_post_disconnect_keyboard(),
            )
        except Exception:
            pass
        return
    if data.startswith(f"{CB_PICK}:"):
        raw_id = data.split(":", 1)[1]
        try:
            alias_id = int(raw_id)
        except ValueError:
            await query.edit_message_text("Alias tidak valid.")
            return
        alias = await db.find_alias_by_id(chat_id, alias_id)
        if alias is None:
            await query.edit_message_text("Alias tidak ditemukan.")
            return
        # Idempotent: if the user clicks the alias that's already locked, just
        # acknowledge without re-sending the "Aktif" announcement (the user
        # was double-tapping otherwise — that's where the duplicate came from).
        current = await db.get_active_alias(chat_id)
        if current is not None and current.id == alias.id:
            await query.answer("Sudah aktif.", show_alert=False)
            return
        # When switching FROM another alias, purge that alias' previously
        # forwarded email messages so the chat isn't littered with stale
        # forwards. We do this BEFORE flipping the active flag so any
        # in-flight delivery to the old alias still reflects the right
        # owner in our bookkeeping.
        if current is not None and current.id != alias.id:
            await _purge_alias_email_messages(
                context.application.bot, db, chat_id, current.id
            )
        await db.set_active_alias(chat_id, alias.id)
        # Refresh the inline keyboard so the 🔒 marker moves to the new alias
        # in-place (no second list message clutter).
        if alias.primary_id is not None:
            primary = await db.get_primary_account(chat_id, alias.primary_id)
            aliases = await db.list_aliases(chat_id, primary_id=alias.primary_id)
            if primary is not None:
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_build_alias_keyboard_for_primary(
                            primary, aliases, alias.id
                        )
                    )
                except Exception:
                    pass
        await query.message.reply_text(  # type: ignore[union-attr]
            # Wrap the alias in ``<code>`` so Telegram mobile users can
            # tap-to-copy directly from the lock-confirmation header. Desktop
            # users get the "📋 Copy email aktif" button below for one-click
            # copy without text selection.
            f"🔒 Aktif: <code>{html.escape(alias.email)}</code>\n"
            "Bot sekarang <b>terkunci</b> ke alias ini — hanya email yang "
            "dikirim ke alamat di atas yang akan diteruskan ke chat ini. "
            "Alias tetap di /list dan terus terima email sampai kamu pilih "
            "alias lain atau kirim /unlock.\n\n"
            "Klik tombol di bawah kalau email kamu belum sampai dan kamu "
            "ingin cek manual (tanpa nunggu polling 5 detik).",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_poll_now_keyboard(with_copy_active=True),
        )
        return
    if data == CB_POLL_NOW:
        manager = _bot_manager(context)
        ok = manager.poke_user(chat_id)
        if ok:
            await query.answer("📥 Mengecek...", show_alert=False)
        else:
            await query.answer(
                "Listener belum jalan — kirim /connect dulu.", show_alert=True
            )
        return
    if data == CB_LOCK_REMINDER_UNLOCK:
        # PR-F lock reminder: release the active alias lock in-place.
        # Drop the cached reminder id so the next command posts a fresh
        # one (or, if the user just unlocked, no reminder at all).
        chat_data = cast(dict, context.chat_data)
        chat_data.pop("lock_reminder_msg_id", None)
        active = await db.get_active_alias(chat_id)
        if active is None:
            with contextlib.suppress(BadRequest, Exception):
                await query.edit_message_text(
                    "Tidak ada alias aktif yang perlu di-unlock."
                )
            return
        await db.set_active_alias(chat_id, None)
        # Bulk-purge the just-released alias' forwarded emails so the
        # chat doesn't accumulate stale emails between locks.
        await _purge_alias_email_messages(
            context.application.bot, db, chat_id, active.id
        )
        with contextlib.suppress(BadRequest, Exception):
            await query.edit_message_text(
                f"🔓 Kunci dilepas dari <b>{html.escape(active.email)}</b>. "
                "Pilih alias di /list saat siap menerima email lagi.",
                parse_mode=ParseMode.HTML,
            )
        return
    if data == CB_LOCK_REMINDER_PICK_NEW:
        # PR-F lock reminder: jump back into the primary list so the
        # user can pick a different alias. Delete the reminder bubble
        # itself so the chat doesn't accumulate stale prompts.
        chat_data = cast(dict, context.chat_data)
        chat_data.pop("lock_reminder_msg_id", None)
        with contextlib.suppress(BadRequest, Exception):
            await query.delete_message()
        await _show_primary_list(update, db, chat_id)
        return
    if data == CB_COPY_ACTIVE_EMAIL:
        # Emit a fresh single-line ``<code>email</code>`` message so desktop
        # Telegram users can one-click copy without selecting text out of a
        # rendered HTML message. Mobile users already have tap-to-copy on
        # the ``<code>`` block embedded in the alias-aktif header, but a
        # dedicated message keeps the address at the bottom of the chat
        # where they don't have to scroll back to find it.
        active = await db.get_active_alias(chat_id)
        if active is None:
            await context.application.bot.send_message(
                chat_id=chat_id,
                text=(
                    "ℹ️ Belum ada alias aktif. Kunci alias di /list dulu, "  # noqa: RUF001
                    "lalu klik 📋 Copy email aktif."
                ),
            )
            return
        await context.application.bot.send_message(
            chat_id=chat_id,
            text=f"<code>{html.escape(active.email)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if data.startswith(f"{CB_QUICK_GENADDR}:"):
        # Quick-action button shown after /connect. Format:
        # ``qgenaddr:<primary_id>:<count>``. Bridge to /genaddr by
        # synthesising the right context.args + delegating to the real
        # handler so we keep a single code path for the actual generation.
        #
        # Strip the inline keyboard from the source message before
        # delegating: rapid double-clicks used to spam "Akun yang
        # dipakai…" + "Masih ada /genaddr…" because every click
        # re-entered cmd_genaddr. Removing the keyboard makes a second
        # click impossible — Telegram still delivers "callback already
        # processed" toasts but no new message is emitted.
        try:
            await query.answer("⏳ Mulai generate…", show_alert=False)
        except Exception:
            LOGGER.debug("query.answer failed for CB_QUICK_GENADDR", exc_info=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            LOGGER.debug(
                "edit_message_reply_markup failed for CB_QUICK_GENADDR",
                exc_info=True,
            )
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        try:
            primary_id = int(parts[1])
            count = int(parts[2])
        except ValueError:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        primary = await db.get_primary_account(chat_id, primary_id)
        if primary is None:
            await query.answer("Akun tidak ditemukan.", show_alert=True)
            return
        # Check we have a stored Proton password — without it /genaddr will
        # bail at the browser step. Surface that early so the user can hit
        # "Simpan password Proton" first.
        proton_pw = await db.get_proton_password_encrypted(primary_id)
        if proton_pw is None:
            if query.message is not None:
                await query.message.reply_text(
                    "❌ Password Proton belum tersimpan untuk akun ini. "
                    "Klik tombol <b>🔐 Simpan password Proton</b> dulu, "
                    "atau kirim /setprotonpw.",
                    parse_mode=ParseMode.HTML,
                )
            return
        base = primary.email.split("@", 1)[0]
        # Persist the picked primary so /genaddr's own selection logic
        # routes to it without an explicit ``base`` match.
        await db.set_active_primary(chat_id, primary_id)
        # Pretend the user typed ``/genaddr <base> <count>`` and run the
        # real handler. ``context.args`` is read inside cmd_genaddr.
        # The hidden ``genaddr_random_suffix`` flag tells cmd_genaddr to
        # use random 2-digit numeric suffixes (vielz88311, vielz88347, …)
        # instead of the legacy sequential vielz001..vielzNNN cursor —
        # per the user's "yang 11 randomized" request.
        # ``genaddr_silent_pick`` suppresses the "Akun yang dipakai: …"
        # disambiguation message: this callback already specified the
        # primary by id, so the message would just be noise.
        context.user_data["genaddr_random_suffix"] = True
        context.user_data["genaddr_silent_pick"] = True
        context.args = [base, str(count)]
        await cmd_genaddr(update, context)
        return
    if data.startswith(f"{CB_SYNC_PRIMARY}:"):
        # Per-primary "🔄 Sync" button on /list. Drives a Playwright
        # session into account.proton.me to refresh the alias list.
        parts = data.split(":")
        if len(parts) != 2:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        try:
            primary_id = int(parts[1])
        except ValueError:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        await query.answer("🔄 Sync alias dimulai...", show_alert=False)
        await _start_sync_for_primary(
            update, context, primary_id=primary_id
        )
        return
    if data.startswith(f"{CB_QUICK_HEALTHCHECK}:") or data.startswith(
        f"{CB_HEALTHCHECK_PICK}:"
    ):
        # Both flavours use ``<prefix>:<primary_id>``; route them to the
        # same launcher. ``qhc`` comes from after-connect / direct
        # buttons, ``hcpick`` comes from the /cekimap multi-primary
        # picker.
        parts = data.split(":")
        if len(parts) != 2:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        try:
            primary_id = int(parts[1])
        except ValueError:
            await query.answer("Tombol tidak valid.", show_alert=True)
            return
        primary = await db.get_primary_account(chat_id, primary_id)
        if primary is None:
            await query.answer("Akun tidak ditemukan.", show_alert=True)
            return
        await query.answer("🩺 Health check dimulai...", show_alert=False)
        if query.message is not None:
            # Drop the keyboard so a second click can't double-launch
            # the same check while the first one is still running.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _start_health_check_for_primary(
            update, context, primary_id=primary_id
        )
        return


# --------------------------------------------------------------- notifier


class TelegramNotifier(Notifier):
    """Notifier implementation that posts to Telegram via the bot's Application."""

    def __init__(self, application: Application) -> None:
        self._application = application

    async def notify_email_received(
        self,
        chat_id: int,
        alias_email: str,
        summary: dict[str, str],
        *,
        alias_id: int | None = None,
        sender_email: str = "",
        sender_domain: str = "",
    ) -> None:
        # Resolve the current label so the forwarded email can show the
        # service name in its header AND the inline button can offer an
        # accurate "Tag ulang" affordance. ``resolve_service_label``
        # walks ``service_labels`` then ``DEFAULT_SERVICE_LABELS`` so a
        # /services edit reflects in real time without a backfill.
        current_label: str | None = None
        if sender_domain:
            try:
                db = self._application.bot_data.get("db")
                if db is not None:
                    current_label = await db.resolve_service_label(
                        chat_id, sender_domain
                    )
            except Exception:
                LOGGER.debug(
                    "resolve_service_label failed in notifier", exc_info=True
                )
        text = _render_email_message(
            alias_email, summary, service_label=current_label
        )
        markup: InlineKeyboardMarkup | None = None
        if alias_id is not None and sender_domain:
            markup = _build_tag_service_keyboard(
                alias_id=alias_id,
                sender_domain=sender_domain,
                current_label=current_label,
            )
        sent = await self._application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        # Remember the message id so we can purge it when the user switches
        # to another active alias (request: "ganti alias → bersihkan
        # pesan email lama biar chat tidak penuh"). We only track when we
        # actually know which alias the email belongs to; older callers
        # without ``alias_id`` simply skip the bookkeeping.
        if alias_id is not None and sent is not None:
            try:
                db = self._application.bot_data.get("db")
                if db is not None:
                    await db.record_forwarded_email(
                        chat_id, alias_id, sent.message_id
                    )
            except Exception:
                LOGGER.debug(
                    "record_forwarded_email failed", exc_info=True
                )

    async def notify_aliases_discovered(
        self,
        chat_id: int,
        aliases: list[str],
    ) -> None:
        if not aliases:
            return
        lines = [f"<b>Auto-sync:</b> {len(aliases)} alias baru ditemukan:"]
        for alias in sorted(aliases):
            lines.append(f"• <code>{html.escape(alias)}</code>")
        lines.append("\n/list untuk lihat semua alias.")
        await self._application.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )


# Telegram caps each message at 4096 characters; we leave headroom for the rendered
# trailer that the helper below appends when truncating.
_TELEGRAM_MESSAGE_LIMIT = 4000
_TRUNCATION_MARKER = "\n…(dipotong)"


def _render_email_message(
    alias_email: str,
    summary: dict[str, str],
    *,
    service_label: str | None = None,
) -> str:
    """Render the email-received Telegram message safely under the 4096-byte limit.

    The body is rendered through :func:`email_parser.format_body_html`, which:
      * collapses runs of blank lines down to at most one,
      * wraps detected OTPs (4-8 digit standalone numbers) in
        ``<code>...</code>`` so Telegram mobile users can tap-to-copy, and
      * leaves bare URLs intact so Telegram auto-links them.

    The body is truncated *before* the final ``<code>`` injection so we never
    split an HTML entity (e.g. ``&amp;``) or a ``<code>`` tag at the byte
    boundary, which would cause Telegram's HTML parser to reject the message.

    ``service_label`` is the currently-resolved friendly name for the
    sender's domain (e.g. ``"Devin"``). When supplied it's appended to
    the ``Dari:`` line so the user can spot at a glance which service
    used the alias without scrolling to the keyboard below.
    """
    body = summary.get("body") or "(tidak ada isi text)"
    from_rendered = html.escape(summary.get("from", "?"))
    if service_label:
        from_rendered = (
            f"{from_rendered}  ·  🏷️ <b>{html.escape(service_label)}</b>"
        )
    header = (
        f"<b>Email masuk untuk</b> <code>{html.escape(alias_email)}</code>\n"
        f"<b>Dari:</b> {from_rendered}\n"
        f"<b>Subjek:</b> {html.escape(summary.get('subject', ''))}\n"
        f"<b>Tanggal:</b> {html.escape(summary.get('date', ''))}\n"
    )
    footer = (
        "\n🔒 Alias masih aktif — email berikutnya ke alamat ini akan "
        "diteruskan juga. Kirim /unlock untuk lepas kunci, atau pilih "
        "alias lain di /list."
    )
    overhead = len(header) + len(footer) + len("\n")  # newline before body
    available = _TELEGRAM_MESSAGE_LIMIT - overhead
    truncated = False
    if available <= 0:
        # Pathological case where the headers themselves exceed the budget.
        body_rendered = ""
        truncated = True
    else:
        rendered = format_body_html(body)
        if len(rendered) <= available:
            body_rendered = rendered
        else:
            # Truncate the *raw* body, then re-render so we never split inside
            # an HTML entity or a ``<code>`` tag we just injected.
            marker_budget = len(_TRUNCATION_MARKER)
            target = max(available - marker_budget, 0)
            shrunk = body
            while shrunk and len(format_body_html(shrunk)) > target:
                shrunk = shrunk[: max(len(shrunk) - 32, 0)]
            body_rendered = format_body_html(shrunk)
            truncated = True
    text = f"{header}\n{body_rendered}"
    if truncated:
        text += _TRUNCATION_MARKER
    text += footer
    return text


# --------------------------------------------------------------- registration


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for unrecognized text outside of any active conversation.

    The user explicitly asked: "kalau input belum jelas munculkan 'tidak
    dikenali, kirim /start'." This handler is registered last in
    ``build_handlers`` so the conversation handlers and command handlers
    above always win first.
    """
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "🤔 Perintah tidak dikenali. Kirim /start untuk melihat semua "
        "perintah, atau /list untuk daftar email utama."
    )


def build_handlers() -> list:
    # ``allow_reentry=True``: typing /connect mid-conversation should
    # restart the flow from scratch instead of falling through to the
    # global "unknown command" handler. Same for /sync.
    connect_conv = ConversationHandler(
        entry_points=[
            CommandHandler("connect", cmd_connect),
            # Post-disconnect "🔌 Connect lagi" shortcut. Lands in the
            # same step-1/2 prompt as the typed command.
            CallbackQueryHandler(
                connect_again_quick_entry, pattern=rf"^{CB_CONNECT_AGAIN}$"
            ),
        ],
        states={
            CONNECT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_email),
            ],
            CONNECT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_password),
            ],
            CONNECT_BRIDGE_CAPTCHA: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, connect_bridge_captcha
                ),
            ],
            CONNECT_RECOVERY_VERIFY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, connect_recovery_verify
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="connect",
        persistent=False,
        allow_reentry=True,
    )

    sync_conv = ConversationHandler(
        entry_points=[CommandHandler("sync", cmd_sync)],
        states={
            SYNC_CAPTCHA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sync_captcha_done),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="sync",
        persistent=False,
        allow_reentry=True,
    )

    # /tag-service: triggered by the inline "🏷️ Tag ulang" button on
    # forwarded emails. The pattern matches ``tagsvc:<alias_id>:<domain>``
    # — domain may contain dots/hyphens but no whitespace/colons.
    tag_service_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                tag_service_entry,
                pattern=rf"^{CB_TAG_SERVICE}:\d+:[A-Za-z0-9.\-]+$",
            ),
        ],
        states={
            TAG_AWAIT_LABEL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, tag_service_label_received
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="tag_service",
        persistent=False,
        allow_reentry=True,
    )

    setpw_conv = ConversationHandler(
        entry_points=[
            CommandHandler("setprotonpw", cmd_setprotonpw),
            # Quick-action button after /connect: skip straight to entering
            # the master password without re-running the picker.
            CallbackQueryHandler(
                setpw_quick_entry, pattern=rf"^{CB_QUICK_SETPW}:\d+$"
            ),
        ],
        states={
            SETPW_PICK_PRIMARY: [
                CallbackQueryHandler(setpw_pick_primary, pattern=r"^setpw:\d+$"),
            ],
            SETPW_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setpw_password),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="setprotonpw",
        persistent=False,
        allow_reentry=True,
    )

    return [
        CommandHandler("start", cmd_start),
        CommandHandler("list", cmd_list),
        CommandHandler("accounts", cmd_accounts),
        CommandHandler("unlock", cmd_unlock),
        CommandHandler("history", cmd_history),
        CommandHandler("addalias", cmd_addalias),
        CommandHandler("removealias", cmd_removealias),
        CommandHandler("reset", cmd_reset),
        CommandHandler("disconnect", cmd_disconnect),
        CommandHandler("genaddr", cmd_genaddr),
        CommandHandler("cekimap", cmd_cekimap),
        CommandHandler("services", cmd_services),
        CommandHandler("aliasinfo", cmd_aliasinfo),
        CommandHandler("cleanmail", cmd_cleanmail),
        connect_conv,
        sync_conv,
        setpw_conv,
        # Tag-service conv MUST come before the catch-all
        # ``CallbackQueryHandler(on_callback)`` so the inline-button
        # entry point wins over the catch-all.
        tag_service_conv,
        # Live-status indicator buttons. The button is purely
        # informational; this handler just acks the tap so Telegram
        # clients drop the spinner. Must come BEFORE the catch-all
        # ``on_callback`` so the noop pattern wins.
        CallbackQueryHandler(
            on_status_button_noop,
            pattern=rf"^{re.escape(STATUS_BUTTON_CALLBACK)}$",
        ),
        CallbackQueryHandler(on_callback),
        # Catch-all: unrecognized text → "perintah tidak dikenali, kirim
        # /start". MUST be the last MessageHandler so conversation states
        # above always run first.
        MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_unknown),
        MessageHandler(filters.COMMAND, cmd_unknown),
    ]
