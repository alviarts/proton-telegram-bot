"""Telegram bot wiring: handlers, menus, and notifier implementation."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, cast

import aioimaplib
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
from .manager import ListenerManager, Notifier
from .models import AliasRecord, AliasStatus, PrimaryAccount
from .proton_browser import CreationStatus

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
CONNECT_EMAIL, CONNECT_PASSWORD, CONNECT_BRIDGE_CAPTCHA = range(3)
CONNECT_DEFAULT_HOST = "127.0.0.1"
CONNECT_DEFAULT_PORT = 1143
CONNECT_DEFAULT_SSL = False

# Conversation states for /sync
SYNC_CAPTCHA = 10

# Conversation states for /setprotonpw
SETPW_PICK_PRIMARY, SETPW_PASSWORD = 20, 21

# Throttle: at most one progress edit every N addresses to stay well under
# Telegram's edit_message rate limit during long /genaddr runs.
GENADDR_PROGRESS_EVERY = 1

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
) -> InlineKeyboardMarkup:
    """Top-level keyboard listing every Proton account a user owns.

    Each row drills into the alias list of that primary. ``alias_counts``
    annotates each label with ``(N alias)``. ``active_primary_id`` flags the
    primary whose alias is currently locked (purely visual).
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
            label = f"{marker}{primary.email} ({count} alias)"
            rows.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"{CB_PICK_PRIMARY}:{primary.id}",
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


async def _show_primary_list(
    update: Update, db: Database, chat_id: int
) -> None:
    """Render the top-level primary keyboard. Used by /list and /start."""
    primaries = await db.list_primary_accounts(chat_id)
    counts = await _alias_count_per_primary(db, chat_id, primaries)
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
            primaries, counts, active_primary_id
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
    *, host: str, port: int, username: str, password: str, use_ssl: bool
) -> tuple[bool, str]:
    """Try a single LOGIN against the user-supplied IMAP creds.

    Returns ``(ok, detail)``. We do this *before* persisting the row so a
    typo in the Bridge password fails loud — the previous flow happily
    saved bogus creds, then the listener would silently NONAUTH-loop
    every two minutes and the user just saw "no email arrives". Bridge
    runs locally so the round-trip cost is negligible.
    """
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
        return False, f"{type(exc).__name__}: {exc}"


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
    """Quick-action buttons for an empty newly-connected primary."""
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
                    "✨ Generate 10 alamat sekarang",
                    callback_data=f"{CB_QUICK_GENADDR}:{primary_id}:10",
                )
            ],
        ]
    )


async def _finalize_connect(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    email: str,
    imap_username: str,
    imap_password: str,
) -> int:
    """Common tail of /connect: verify, save, start listener, friendly reply.

    Both the legacy "user-typed-Bridge-password" path and the new
    "auto-extracted-from-Bridge-vault" path land here once we hold a
    plausible IMAP password.
    """
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    user_data = cast(dict, context.user_data)
    host = CONNECT_DEFAULT_HOST
    port = CONNECT_DEFAULT_PORT
    use_ssl = CONNECT_DEFAULT_SSL

    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"🔌 Cek login ke Bridge sebagai <b>{html.escape(email)}</b>...",
        parse_mode=ParseMode.HTML,
    )
    ok, detail = await _verify_bridge_login(
        host=host,
        port=port,
        username=imap_username,
        password=imap_password,
        use_ssl=use_ssl,
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
    # left over from a previous primary. With no alias-lock pinned, the
    # routing logic in manager.py forwards email to *any* known alias of
    # this primary out of the box — which is what the user expects when
    # they say "primary baru otomatis aktif, alias-nya juga".
    await db.set_active_primary(chat.id, primary_id)
    await db.set_active_alias(chat.id, None)

    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"✅ Tersambung ke <b>{html.escape(email)}</b> — kredensial "
        "disimpan terenkripsi & jadi akun aktif.\n"
        "Listener IMAP otomatis menyala; tiap email yang masuk ke alias "
        "akun ini akan diteruskan ke chat ini.\n\n"
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

    aliases = await db.list_aliases(chat.id, primary_id=primary_id)
    if not aliases:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"ℹ️ Akun <b>{html.escape(email)}</b> belum punya alias.\n\n"  # noqa: RUF001
            "Klik tombol di bawah untuk lanjut tanpa mengetik perintah:",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_post_connect_keyboard(primary_id),
        )
    else:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"📥 Akun ini sudah punya {len(aliases)} alias. "
            "Kirim /list untuk lihat semuanya."
        )
    return ConversationHandler.END


async def _drive_bridge_login(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    bridge_admin: BridgeAdmin,
    email: str,
    proton_password: str,
) -> int:
    """Walk Bridge through ``add_account`` end-to-end on behalf of /connect.

    Yields CAPTCHA URLs back to the user via Telegram, parks the
    conversation in :data:`CONNECT_BRIDGE_CAPTCHA` until they confirm,
    and finalises with :func:`_finalize_connect` once Bridge succeeds.
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
                user_data["bridge_captcha_iterator"] = iterator
                user_data["bridge_email"] = email
                await update.effective_message.reply_text(  # type: ignore[union-attr]
                    "🔒 Proton minta verifikasi manusia.\n\n"
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

    bridge_admin = _bot_bridge_admin(context)
    if bridge_admin is None:
        # Legacy path: user typed the Bridge IMAP password directly.
        return await _finalize_connect(
            update,
            context,
            email=email,
            imap_username=email,
            imap_password=text,
        )

    # Auto-add path: text is the Proton account password. If the account
    # is already in Bridge, skip the cli login and go straight to vault
    # extraction. Otherwise drive the cli login.
    try:
        existing = await bridge_admin.fetch_imap_credentials(email)
    except BridgeAdminError as exc:
        LOGGER.warning("vault probe failed: %s", exc)
        existing = None
    if existing is not None:
        return await _finalize_connect(
            update,
            context,
            email=existing.email,
            imap_username=existing.imap_username,
            imap_password=existing.imap_password,
        )

    return await _drive_bridge_login(
        update,
        context,
        bridge_admin=bridge_admin,
        email=email,
        proton_password=text,
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
            )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = cast(dict, context.user_data)
    user_data.pop("imap_password", None)
    user_data.pop("bridge_captcha_iterator", None)
    user_data.pop("bridge_email", None)
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
        "Pilih akun yang mau dihapus (listener akan dihentikan + kredensial "
        "+ alias-aliasnya juga dihapus):",
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
    cipher = _bot_cipher(context)
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

    cancel_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Batalkan", callback_data=CB_GENADDR_CANCEL)]]
    )
    proxy_provider = context.application.bot_data.get("proxy_provider")
    proxy_note = " via proxy rotasi" if proxy_provider is not None else ""
    progress_message = await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"⏳ Menyiapkan browser & login ke <b>{html.escape(primary.email)}</b>"
        f"{proxy_note}...\n"
        f"Akan membuat <b>{count}</b> alamat dengan pola "
        f"<code>{html.escape(base)}NNN@{html.escape(domain)}</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard,
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

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    async def _on_progress(success_count: int, target: int, result) -> None:
        # ``result`` is an AddressCreationResult — deliberately untyped here
        # to avoid widening the bot.py imports; we only use a few fields.
        # ``success_count`` is the number of successful addresses so far
        # (NOT the attempt index): the orchestrator now loops until we
        # reach ``target`` successes, attempting more names if some fail.
        if result.status is CreationStatus.SUCCESS:
            successes.append(result.email)
        else:
            failures.append((result.email, result.status.value))
        attempts = len(successes) + len(failures)
        # Throttle by attempts (every Nth attempt OR when target reached)
        # so failures still drive UI updates -- otherwise the bot would
        # look frozen during a long string of duplicates.
        if (
            attempts % GENADDR_PROGRESS_EVERY != 0
            and success_count != target
            and result.status is not CreationStatus.SUCCESS
        ):
            return
        try:
            await progress_message.edit_text(
                f"✅ <b>{success_count}/{target}</b> sukses pada "
                f"<b>{html.escape(primary.email)}</b>\n"
                f"⏳ {attempts} percobaan · ⚠️ {len(failures)} gagal/duplikat\n"
                f"Terakhir: <code>{html.escape(result.email)}</code> "
                f"({html.escape(result.status.value)})",
                parse_mode=ParseMode.HTML,
                # Critical: re-pass the cancel keyboard on every edit.
                # ``edit_text`` without ``reply_markup`` *removes* the
                # inline keyboard, which is what made the Cancel button
                # disappear after the first progress update.
                reply_markup=cancel_keyboard,
            )
        except Exception:
            # Telegram occasionally rejects identical edits or rate-limits;
            # losing a progress update is fine, the final summary is what
            # matters.
            LOGGER.debug("genaddr progress edit failed", exc_info=True)

    try:
        try:
            summary = await address_generator.run_batch(
                db=db,
                cipher=cipher,
                chat_id=chat.id,
                primary=primary,
                base=base,
                count=count,
                domain=domain,
                browser_factory=context.application.bot_data.get("browser_factory"),
                progress=_on_progress,
                cancel_event=cancel_event,
                browser_handle=browser_handle,
                proxy_provider=proxy_provider,
            )
        except address_generator.AddressGenerationError as exc:
            await progress_message.edit_text(
                f"❌ Tidak bisa mulai: {html.escape(str(exc))}\n\n"
                "Kalau belum, set password Proton dengan /setprotonpw.",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as exc:
            LOGGER.exception("genaddr crashed")
            await progress_message.edit_text(
                "❌ Browser otomasi crash.\n"
                f"Detail: <code>{html.escape(str(exc) or type(exc).__name__)}</code>\n\n"
                "Screenshot + HTML halaman terakhir disimpan di "
                "<code>/tmp/proton-browser-debug/</code> dalam container.\n"
                "Ambil dengan: <code>docker compose cp bot:/tmp/proton-browser-debug ./debug</code>",
                parse_mode=ParseMode.HTML,
            )
            return
    finally:
        # Clear chat_data so the next /genaddr can run + the cancel button
        # in any later message becomes a no-op.
        context.chat_data.pop("genaddr_running", None)
        context.chat_data.pop("genaddr_cancel_event", None)
        context.chat_data.pop("genaddr_browser_handle", None)
        context.chat_data.pop("genaddr_force_close_task", None)

    final_lines = [
        f"✅ Selesai. Sukses: <b>{len(summary.created)}</b>, "
        f"sudah ada: <b>{len(summary.already_existing)}</b>, "
        f"gagal: <b>{len(summary.failed)}</b>.",
    ]
    if summary.captcha_interrupted_at:
        final_lines.append(
            f"⚠️ Berhenti di <code>{html.escape(summary.captcha_interrupted_at)}</code> "
            "karena CAPTCHA. Solve manual lalu jalankan ulang /genaddr."
        )
    if summary.aborted_reason and not summary.captcha_interrupted_at:
        final_lines.append(f"ℹ️ {html.escape(summary.aborted_reason)}")  # noqa: RUF001
    if summary.created:
        sample = ", ".join(r.email for r in summary.created[:5])
        more = "" if len(summary.created) <= 5 else f" (+{len(summary.created) - 5} lagi)"
        final_lines.append(f"Contoh: <code>{html.escape(sample)}</code>{more}")
    final_lines.append("\n/list untuk lihat semua alamat per akun.")
    await progress_message.edit_text(
        "\n".join(final_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )


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
        active = await db.get_active_alias(chat_id)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_build_primary_keyboard(
                    primaries,
                    counts,
                    active.primary_id if active else None,
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
        await db.delete_primary_account(chat_id, primary_id)
        try:
            await query.edit_message_text(
                f"❌ Akun <b>{html.escape(primary.email)}</b> + alias-aliasnya "
                "dihapus, listener dihentikan.",
                parse_mode=ParseMode.HTML,
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
        context.args = [base, str(count)]
        await cmd_genaddr(update, context)
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
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="connect",
        persistent=False,
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
        connect_conv,
        sync_conv,
        setpw_conv,
        CallbackQueryHandler(on_callback),
        # Catch-all: unrecognized text → "perintah tidak dikenali, kirim
        # /start". MUST be the last MessageHandler so conversation states
        # above always run first.
        MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_unknown),
        MessageHandler(filters.COMMAND, cmd_unknown),
    ]
