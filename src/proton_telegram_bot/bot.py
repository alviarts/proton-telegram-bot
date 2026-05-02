"""Telegram bot wiring: handlers, menus, and notifier implementation."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import secrets
import smtplib
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

# Conversation states for /sync
SYNC_CAPTCHA = 10

# Conversation states for /setprotonpw
SETPW_PICK_PRIMARY, SETPW_PASSWORD = 20, 21

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

# Limits applied at the bot layer (the alias generator has its own MAX_BATCH).
GENADDR_DEFAULT_DOMAIN = "proton.me"
GENADDR_MAX_COUNT = 200

CB_PICK = "pick"
CB_REFRESH = "refresh"
CB_RESET = "reset"
CB_DELETE = "delete"
CB_NOOP = "noop"
CB_POLL_NOW = "poll_now"
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

# Soft target for total aliases per primary. After /cekimap or after
# the per-primary "🔄 Sync" button finishes, if the alias count for
# that primary is below this number the bot offers a one-tap "✨
# Tambah N alamat lagi" button that drives /genaddr in random-suffix
# mode to top the account up. 21 matches the typical "20 alias + 1
# primary" shape we've been seeing across the test accounts.
ALIAS_TARGET_PER_PRIMARY = 21


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


def _bot_bridge_admin(
    context: ContextTypes.DEFAULT_TYPE,
) -> BridgeAdmin | None:
    """Return the configured BridgeAdmin, or ``None`` if auto-add is off."""
    admin = context.application.bot_data.get("bridge_admin")
    if admin is None or not getattr(admin, "enabled", False):
        return None
    return cast(BridgeAdmin, admin)


def _build_primary_keyboard(
    primaries: list[PrimaryAccount],
    alias_counts: dict[int, int] | None = None,
    active_primary_id: int | None = None,
    healthcheck_stats: dict[int, tuple[int, int]] | None = None,
) -> InlineKeyboardMarkup:
    """Top-level keyboard listing every Proton account a user owns.

    Each row drills into the alias list of that primary.
    ``alias_counts`` annotates each label with the total alias count.
    ``healthcheck_stats`` (from the last /cekimap run, keyed by
    ``primary.id``) takes priority and renders as ``ok/total`` so the
    user can spot a primary whose aliases have started failing.
    ``active_primary_id`` flags the primary whose alias is currently
    locked (purely visual).
    """
    rows: list[list[InlineKeyboardButton]] = []
    if not primaries:
        rows.append(
            [InlineKeyboardButton("(belum ada email utama)", callback_data=CB_NOOP)]
        )
    else:
        for primary in primaries:
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
) -> InlineKeyboardMarkup:
    """Drill-down keyboard showing aliases owned by a single primary."""
    rows: list[list[InlineKeyboardButton]] = []
    if not aliases:
        rows.append(
            [InlineKeyboardButton("(belum ada alias)", callback_data=CB_NOOP)]
        )
    else:
        for alias in aliases:
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
    return InlineKeyboardMarkup(rows)


def _build_poll_now_keyboard() -> InlineKeyboardMarkup:
    """Standalone 'check email now' button used in the lock-confirmation message."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📥 Cek email sekarang", callback_data=CB_POLL_NOW)]]
    )


async def _alias_count_per_primary(
    db: Database, chat_id: int, primaries: list[PrimaryAccount]
) -> dict[int, int]:
    counts: dict[int, int] = {}
    for primary in primaries:
        aliases = await db.list_aliases(chat_id, primary_id=primary.id)
        counts[primary.id] = len(aliases)
    return counts


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
    return await bot.send_message(
        chat_id=chat_id,
        text=(
            f"📧 Alias di <b>{html.escape(primary.email)}</b> "
            f"({len(aliases)} alias):"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_alias_keyboard_for_primary(
            primary, aliases, active_alias_id
        ),
    )


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
        header += (
            f"\n🔒 Alias aktif: <b>{html.escape(active.email)}</b>"
            " — kirim /unlock untuk lepas."
        )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        header,
        reply_markup=_build_primary_keyboard(
            primaries,
            counts,
            active_primary_id,
            healthcheck_stats=healthcheck_stats,
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


@_gate
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    consumed = await db.list_aliases(chat.id, status=AliasStatus.CONSUMED)
    if not consumed:
        await update.effective_message.reply_text("Belum ada alias yang sudah terpakai.")  # type: ignore[union-attr]
        return
    lines = ["Alias yang sudah terpakai:"]
    for alias in consumed:
        when = alias.consumed_at or "?"
        lines.append(f"• {alias.email} ({when})")
    lines.append("\nUntuk mengaktifkan kembali: /reset <email>")
    await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]


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
    await update.effective_message.reply_text("Menghubungi Proton API...")  # type: ignore[union-attr]
    try:
        from .proton_api import CaptchaChallenge, start_auth

        result = await asyncio.to_thread(start_auth, username, password)
    except Exception as exc:
        LOGGER.exception("proton API sync failed")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal mengambil alamat dari Proton: {exc}"
        )
        return ConversationHandler.END

    if isinstance(result, CaptchaChallenge):
        user_data = cast(dict, context.user_data)
        user_data["sync_challenge"] = result
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
    if challenge is None:
        await update.effective_message.reply_text("Sesi sync sudah kedaluwarsa. Coba /sync lagi.")  # type: ignore[union-attr]
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip().lower()  # type: ignore[union-attr]
    if text == "done":
        # User solved CAPTCHA on the web page; retry with the original token
        await update.effective_message.reply_text("Mencoba ulang autentikasi...")  # type: ignore[union-attr]
        try:
            from .proton_api import complete_auth_with_captcha

            session = await asyncio.to_thread(
                complete_auth_with_captcha, challenge, challenge.token
            )
        except Exception as exc:
            LOGGER.exception("CAPTCHA auth retry failed")
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                f"Gagal setelah CAPTCHA: {exc}\nCoba /sync lagi."
            )
            return ConversationHandler.END
        return await _sync_complete(update, context, session)

    # User sent a captcha response token directly
    await update.effective_message.reply_text("Memverifikasi token CAPTCHA...")  # type: ignore[union-attr]
    try:
        from .proton_api import complete_auth_with_captcha

        session = await asyncio.to_thread(complete_auth_with_captcha, challenge, text)
    except Exception as exc:
        LOGGER.exception("CAPTCHA token auth failed")
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
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        intro,
        parse_mode=ParseMode.HTML,
    )
    # Allow `/connect <email>` as a one-shot entry to skip the email prompt.
    args = list(context.args or [])
    if args and "@" in args[0]:
        cast(dict, context.user_data)["primary_email"] = args[0].strip().lower()
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            _connect_password_prompt(bridge_admin_on),
            parse_mode=ParseMode.HTML,
        )
        return CONNECT_PASSWORD
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Step 1/2 — kirim alamat email Proton-nya (mis. "
        "<code>vielz43@proton.me</code>):",
        parse_mode=ParseMode.HTML,
    )
    return CONNECT_EMAIL


async def connect_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if "@" not in text:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Itu bukan alamat email yang valid. Masukkan email Proton-nya:"
        )
        return CONNECT_EMAIL
    cast(dict, context.user_data)["primary_email"] = text.lower()
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        _connect_password_prompt(_bot_bridge_admin(context) is not None),
        parse_mode=ParseMode.HTML,
    )
    return CONNECT_PASSWORD


def _build_post_connect_keyboard(primary_id: int) -> InlineKeyboardMarkup:
    """Quick-action buttons for an empty newly-connected primary.

    Used when the just-connected account has no real aliases yet (the
    user just made a fresh Proton account). Walks them through the
    setprotonpw → genaddr workflow without re-typing the email address.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔐 Simpan password Proton",
                    callback_data=f"{CB_QUICK_SETPW}:{primary_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✨ Generate 20 alamat sekarang",
                    callback_data=f"{CB_QUICK_GENADDR}:{primary_id}:20",
                )
            ],
            [
                InlineKeyboardButton(
                    "🩺 Cek IMAP listener (background)",
                    callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary_id}",
                )
            ],
        ]
    )


def _build_post_connect_keyboard_with_aliases(
    primary_id: int, alias_count: int
) -> InlineKeyboardMarkup:
    """Quick-action buttons for an existing-aliases newly-connected primary.

    Used when /connect lands on an account that already has aliases
    (auto-sync just imported them, or they were already in the DB from
    an earlier session). Surfaces three one-tap actions:

    1. Generate 20 more random-suffix aliases — same callback the
       fresh-account onboarding uses, so the user can extend the pool
       without re-typing the email address. ``/genaddr`` and
       ``/cekimap`` use independent ``chat_data`` locks
       (``genaddr_running`` vs ``health_check_running``) so this is
       safe to click while a background health check is running.
    2. Run the end-to-end IMAP listener health check.
    3. Open ``/list`` for this primary.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✨ Generate 20 alamat sekarang",
                    callback_data=f"{CB_QUICK_GENADDR}:{primary_id}:20",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🩺 Cek IMAP listener semua {alias_count} alias",
                    callback_data=f"{CB_QUICK_HEALTHCHECK}:{primary_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Buka /list", callback_data=f"{CB_PICK_PRIMARY}:{primary_id}"
                )
            ],
        ]
    )


SMTP_SMOKE_TIMEOUT_SECONDS = 60
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


async def _finalize_connect(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    email: str,
    imap_username: str,
    imap_password: str,
    smoke_test_tempmail: TempMailbox | None = None,
    pre_probe_settle_seconds: float = 0.0,
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
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"⏳ Menunggu Bridge selesai inisialisasi "
            f"<b>{html.escape(email)}</b> "
            f"({int(pre_probe_settle_seconds)}s)...",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(pre_probe_settle_seconds)

    await update.effective_message.reply_text(  # type: ignore[union-attr]
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
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Bridge menolak login. Pastikan password yang kamu kirim "
            "adalah <b>password IMAP yang di-generate Bridge</b> (bukan "
            "password akun Proton kamu).\n\n"
            f"Detail: <code>{html.escape(detail)}</code>\n\n"
            "Kirim password yang benar lagi, atau /cancel untuk batal:",
            parse_mode=ParseMode.HTML,
        )
        return CONNECT_PASSWORD

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

    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"✅ Tersambung ke <b>{html.escape(email)}</b> — kredensial "
        "disimpan terenkripsi & jadi akun aktif.\n"
        "Listener IMAP otomatis menyala. Email belum diteruskan otomatis: "
        "buka <b>/list</b> dan pilih alias yang mau dipakai dulu.\n\n"
        "💡 <i>Hapus pesan password-mu di atas sekarang.</i>",
        parse_mode=ParseMode.HTML,
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
        return ConversationHandler.END

    if smoke_test_tempmail is not None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "🧪 Smoke test IMAP/SMTP: kirim email uji ke temp mail "
            f"(<code>{html.escape(smoke_test_tempmail.address)}</code>)...",
            parse_mode=ParseMode.HTML,
        )
        smoke_ok = await _smoke_test_via_tempmail(
            email=email,
            imap_username=imap_username,
            imap_password=imap_password,
            tempmail=smoke_test_tempmail,
        )
        if smoke_ok:
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "✅ <b>IMAP/SMTP berjalan sempurna</b> — email uji "
                "diterima di temp mail.",
                parse_mode=ParseMode.HTML,
            )
        else:
            LOGGER.warning(
                "smoke test failed for %s; rolling back primary %d",
                email,
                primary_id,
            )
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "❌ <b>Smoke test gagal</b> — email uji tidak sampai ke "
                "temp mail dalam 60 detik. Bridge mungkin belum benar-benar "
                "siap atau IMAP/SMTP tidak jalan.\n\n"
                "Sesi ini di-rollback. Silakan <b>/connect</b> lagi.",
                parse_mode=ParseMode.HTML,
            )
            with contextlib.suppress(Exception):
                await manager.stop_for_primary(primary_id)
            with contextlib.suppress(Exception):
                await db.delete_primary_account(chat.id, primary_id)
            bridge_admin = _bot_bridge_admin(context)
            if bridge_admin is not None:
                with contextlib.suppress(Exception):
                    await bridge_admin.remove_account(email)
            return ConversationHandler.END

    # Auto-sync addresses from Proton account API if we have them.
    # The recovery-email Playwright flow stashes the full address list
    # (pulled from /api/core/v4/addresses with the still-logged-in
    # browser session) into ``user_data["proton_account_addresses"]``.
    # We persist them as aliases here, after the primary row exists,
    # so the user doesn't have to run /sync separately to populate
    # the 18+ existing addresses on a Business account.
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

            await update.effective_message.reply_text(  # type: ignore[union-attr]
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
                await update.effective_message.reply_text(  # type: ignore[union-attr]
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
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"ℹ️ Akun <b>{html.escape(email)}</b> belum punya alias.\n\n"  # noqa: RUF001
            "<b>Cara cepat bikin alias:</b>\n"
            "1️⃣  Klik <b>🔐 Simpan password Proton</b> — sekali aja, "
            "buat akun ini.\n"
            "2️⃣  Klik <b>✨ Generate 20 alamat sekarang</b> — bot bikin "
            "20 alias <code>vielz001..vielz020</code> otomatis di background.\n"
            "   Bot kirim update tiap 5 alias (5/20, 10/20, ...) dan kamu "
            "tetap bisa pakai perintah lain sambil generate jalan.\n"
            "3️⃣  Pakai <b>🩺 Cek IMAP listener</b> kapan aja buat "
            "validasi semua alias bisa terima email.",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_post_connect_keyboard(primary_id),
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
    return ConversationHandler.END


async def _setup_tempmail_recovery(
    update: Update,
    email: str,
    proton_password: str,
) -> tuple[TempMailbox | None, bool, list[str] | None]:
    """Create a temp mail and set it as recovery email in Proton settings.

    Logs into the Proton web UI, navigates to recovery settings, and
    replaces the current recovery email with a fresh Mail.tm address.
    Sends the recovery-email verification link to the Telegram user
    for manual click.

    Returns ``(tempmail, ok, addresses)`` where:
      * ``tempmail`` is the disposable mailbox (or ``None`` on early failure).
      * ``ok`` is ``True`` only if the verification link was sent to the
        chat. When ``ok`` is ``False`` the caller MUST NOT proceed to
        Bridge add-account: Proton login or recovery email change failed
        and the user needs to retry ``/connect``.
      * ``addresses`` is the full list of email addresses Proton's
        ``/api/core/v4/addresses`` endpoint returned for this user
        (extracted from the still-logged-in browser session before
        teardown), so the caller can persist them as aliases. ``None``
        when the API call failed; an empty list is also possible
        (very rare, single-address account).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        LOGGER.warning("playwright not installed; skipping recovery email setup")
        return None, False, None

    async with httpx.AsyncClient() as client:
        try:
            tempmail = await TempMailbox.create(client)
        except TempMailError as exc:
            LOGGER.warning("failed to create temp mailbox: %s", exc)
            return None, False, None

        await update.effective_message.reply_text(  # type: ignore[union-attr]
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
                return tempmail, False, None

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
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    f"📧 Recovery email diubah ke <code>{html.escape(tempmail.address)}</code>\n\n"
                    "Klik link berikut untuk verifikasi recovery email:\n"
                    f"{html.escape(verify_link)}",
                    parse_mode=ParseMode.HTML,
                )
                return tempmail, True, addresses

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
            return tempmail, False, None
        except Exception:
            LOGGER.exception("recovery email setup failed")
            return tempmail, False, None
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
    tempmail, recovery_ok, proton_addresses = await _setup_tempmail_recovery(
        update, email, proton_password
    )
    if not recovery_ok:
        # Recovery email step failed: do NOT attempt Bridge add-account.
        # Bridge uses the Proton account password for login (not the
        # IMAP password it later generates), and without a verified
        # recovery email Proton will demand human verification we cannot
        # automate.  Better to abort cleanly than leave the user with a
        # confusing TimeoutError after a broken recovery flow.
        return ConversationHandler.END

    # Hand off to the recovery-verify wait state. The verification link
    # has already been DM'd from inside ``_setup_tempmail_recovery``;
    # we just need the user to click it and reply "ok" before we
    # proceed to bridge add-account.
    user_data["bridge_recovery_email"] = email
    user_data["bridge_recovery_proton_password"] = proton_password
    user_data["bridge_recovery_tempmail"] = tempmail
    # Stash the list pulled from /api/core/v4/addresses so _finalize_connect
    # can persist them as aliases once IMAP comes up.
    user_data["proton_account_addresses"] = proton_addresses
    await update.effective_message.reply_text(  # type: ignore[union-attr]
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
) -> int:
    """Run ``bridge add_account`` and the smoke test for ``email``.

    Extracted from ``_drive_bridge_login`` so it can be invoked **after**
    the user has confirmed they clicked the recovery-email verification
    link, without duplicating the iterator/CAPTCHA bookkeeping.
    """
    user_data = cast(dict, context.user_data)
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
                auto_ok = await _try_auto_verify(
                    update, event.url, bridge_admin, tempmail
                )
                if auto_ok:
                    # Continue consuming events — Bridge should proceed
                    continue

                # Auto-verify failed: fall back to manual flow
                user_data["bridge_captcha_iterator"] = iterator
                user_data["bridge_email"] = email
                user_data["bridge_smoke_tempmail"] = tempmail
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
        return ConversationHandler.END
    if creds is None:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "❌ Login Bridge sukses tapi password IMAP tidak ditemukan "
            "di vault. Coba /connect lagi atau cek konfigurasi Bridge."
        )
        return ConversationHandler.END

    return await _finalize_connect(
        update,
        context,
        email=creds.email,
        imap_username=creds.imap_username,
        imap_password=creds.imap_password,
        smoke_test_tempmail=tempmail,
        pre_probe_settle_seconds=30.0,
    )


async def connect_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        await update.effective_message.reply_text("Password tidak boleh kosong:")  # type: ignore[union-attr]
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
            )
        LOGGER.info(
            "vault has %s but Bridge IMAP rejected (%s); running full re-add",
            email,
            detail,
        )

    return await _drive_bridge_login(
        update,
        context,
        bridge_admin=bridge_admin,
        email=email,
        proton_password=text,
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
        return ConversationHandler.END

    return await _perform_bridge_add_account(
        update,
        context,
        bridge_admin=bridge_admin,
        email=email,
        proton_password=proton_password,
        tempmail=tempmail,
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
            )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = cast(dict, context.user_data)
    user_data.pop("imap_password", None)
    user_data.pop("bridge_captcha_iterator", None)
    user_data.pop("bridge_email", None)
    user_data.pop("bridge_smoke_tempmail", None)
    user_data.pop("proton_password", None)
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
    await db.set_active_alias(chat.id, None)
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
        return
    counts = await _alias_count_per_primary(db, chat.id, primaries)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "🩺 Pilih email utama yang mau di-cek IMAP listener-nya:",
        reply_markup=_build_cekimap_picker_keyboard(primaries, counts),
    )


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
    if len(primaries) > 1:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Akun yang dipakai: <b>{html.escape(primary.email)}</b> "
            f"({primary_pick_reason}). Ganti dengan /setprotonpw atau "
            "klik alias di /list.",
            parse_mode=ParseMode.HTML,
        )

    if context.chat_data.get("genaddr_running"):
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "⚠️ Masih ada /genaddr lain yang berjalan di chat ini. "
            "Tunggu selesai atau klik tombol Batalkan di pesan progressnya.",
        )
        return

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
    # the live-status anchor (button row above ❌ Batalkan narrates the
    # current phase, mirroring /cekimap). The background task edits
    # this message's reply_markup via StatusReporter; the body stays
    # as the contextual header (count, pattern, proxy note).
    starter_message = await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"🚀 Mulai generate <b>{count}</b> alamat di background untuk "
        f"<b>{html.escape(primary.email)}</b>{proxy_note}.\n"
        f"Pola: {pattern}\n"
        f"Bot tetap responsif — kamu bisa kirim /list, /cekimap, atau "
        f"perintah lain sambil generate jalan. Update tiap "
        f"<b>{GENADDR_NOTIFY_EVERY}</b> alamat sukses.",
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
    )


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

    async def _on_progress(success_count: int, target: int, result) -> None:
        nonlocal last_notified_count
        if result.status is CreationStatus.SUCCESS:
            successes.append(result.email)
        else:
            failures.append((result.email, result.status.value))

        # Live status: narrate every attempt so the user always sees
        # the bot working. The 1.5s throttle inside StatusReporter
        # absorbs bursts without rate-limiting Telegram.
        if result.status is CreationStatus.SUCCESS:
            label = (
                f"🪄 {success_count}/{target} sukses · "
                f"{result.email}"
            )
        else:
            label = (
                f"⚠️ {len(failures)} gagal/duplikat · "
                f"{result.email}"
            )
        await _status(label)

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
    try:
        try:
            await _status("🌐 Buka browser proxy & login Proton…", force=True)
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
        chat_data = context.application.chat_data.get(chat_id)
        if chat_data is not None:
            chat_data.pop("genaddr_running", None)
            chat_data.pop("genaddr_cancel_event", None)
            chat_data.pop("genaddr_browser_handle", None)
            chat_data.pop("genaddr_force_close_task", None)
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
    if data == CB_GENADDR_CANCEL:
        cancel_event = context.chat_data.get("genaddr_cancel_event")
        if cancel_event is None:
            await query.answer(
                "Tidak ada /genaddr aktif untuk dibatalkan.", show_alert=False
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        cancel_event.set()
        # Force-close the live browser so any in-flight Playwright await
        # (``page.click``, ``wait_for_url``, …) raises ``TargetClosedError``
        # right now instead of running the rest of its 60s timeout.
        # ``force_close`` itself is best-effort + bounded, so we fire it
        # off in a background task to keep this callback snappy.
        browser_handle = context.chat_data.get("genaddr_browser_handle")
        browser = (
            browser_handle.get("browser")
            if isinstance(browser_handle, dict)
            else None
        )
        force_close = getattr(browser, "force_close", None) if browser else None
        if callable(force_close):
            # Hold a reference on chat_data so the GC doesn't collect the
            # task before it finishes (RUF006). The handler's ``finally``
            # clears chat_data, which is fine — by then the task is done
            # or the next /genaddr will overwrite the slot.
            context.chat_data["genaddr_force_close_task"] = asyncio.create_task(
                _safe_force_close(force_close)
            )
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
                )
            )
        except Exception:
            pass
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
            await query.edit_message_text(
                f"📧 Alias di <b>{html.escape(primary.email)}</b> "
                f"({len(aliases)} alias):",
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
        manager = _bot_manager(context)
        await manager.stop_for_primary(primary_id)
        # Best-effort logout from the host Proton Bridge so a future
        # /connect for the same email is a clean slate (no cached
        # credentials, no stale message UID baseline). DB cleanup runs
        # regardless of the Bridge-side outcome.
        bridge_admin = _bot_bridge_admin(context)
        bridge_removed = False
        if bridge_admin is not None:
            try:
                bridge_removed = await bridge_admin.remove_account(primary.email)
            except Exception:
                LOGGER.exception(
                    "bridge_admin.remove_account failed for %s",
                    primary.email,
                )
        await db.delete_primary_account(chat_id, primary_id)
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
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
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
            f"🔒 Aktif: <b>{html.escape(alias.email)}</b>\n"
            "Bot sekarang <b>terkunci</b> ke alias ini — hanya email yang "
            "dikirim ke alamat di atas yang akan diteruskan ke chat ini. "
            "Alias tetap di /list dan terus terima email sampai kamu pilih "
            "alias lain atau kirim /unlock.\n\n"
            "Klik tombol di bawah kalau email kamu belum sampai dan kamu "
            "ingin cek manual (tanpa nunggu polling 5 detik).",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_poll_now_keyboard(),
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
    if data.startswith(f"{CB_QUICK_GENADDR}:"):
        # Quick-action button shown after /connect. Format:
        # ``qgenaddr:<primary_id>:<count>``. Bridge to /genaddr by
        # synthesising the right context.args + delegating to the real
        # handler so we keep a single code path for the actual generation.
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
        context.user_data["genaddr_random_suffix"] = True
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
    ) -> None:
        await self._application.bot.send_message(
            chat_id=chat_id,
            text=_render_email_message(alias_email, summary),
            parse_mode=ParseMode.HTML,
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


def _render_email_message(alias_email: str, summary: dict[str, str]) -> str:
    """Render the email-received Telegram message safely under the 4096-byte limit.

    The body is truncated *before* HTML escaping/assembly so we never split an HTML
    tag (e.g. ``<pre>``) or an entity (e.g. ``&amp;``) at the byte boundary, which
    would cause Telegram's HTML parser to reject the message.
    """
    body = summary.get("body") or "(tidak ada isi text)"
    header = (
        f"<b>Email masuk untuk</b> <code>{html.escape(alias_email)}</code>\n"
        f"<b>Dari:</b> {html.escape(summary.get('from', '?'))}\n"
        f"<b>Subjek:</b> {html.escape(summary.get('subject', ''))}\n"
        f"<b>Tanggal:</b> {html.escape(summary.get('date', ''))}\n\n"
    )
    footer = (
        "\n\n🔒 Alias masih aktif — email berikutnya ke alamat ini akan "
        "diteruskan juga. Kirim /unlock untuk lepas kunci, atau pilih "
        "alias lain di /list."
    )
    overhead = len(header) + len("<pre></pre>") + len(footer)
    available = _TELEGRAM_MESSAGE_LIMIT - overhead
    truncated = False
    if available <= 0:
        # Pathological case where the headers themselves exceed the budget.
        body_rendered = ""
        truncated = True
    else:
        escaped = html.escape(body)
        if len(escaped) <= available:
            body_rendered = escaped
        else:
            # Re-escape only the portion of the raw body that fits, leaving room
            # for the truncation marker on its own line.
            marker_budget = len(_TRUNCATION_MARKER)
            target = max(available - marker_budget, 0)
            shrunk = body
            while shrunk and len(html.escape(shrunk)) > target:
                # Drop characters from the end until the escaped form fits.
                shrunk = shrunk[: max(len(shrunk) - 32, 0)]
            body_rendered = html.escape(shrunk)
            truncated = True
    text = f"{header}<pre>{body_rendered}</pre>"
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
        entry_points=[CommandHandler("connect", cmd_connect)],
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
        connect_conv,
        sync_conv,
        setpw_conv,
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
