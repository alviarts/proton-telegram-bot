"""Telegram bot wiring: handlers, menus, and notifier implementation."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import cast

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

from .config import Settings
from .crypto import CredentialCipher
from .db import Database
from .manager import ListenerManager, Notifier
from .models import AliasRecord, AliasStatus, PrimaryAccount

LOGGER = logging.getLogger(__name__)

# Conversation states for /connect
(
    CONNECT_EMAIL,
    CONNECT_HOST,
    CONNECT_PORT,
    CONNECT_USERNAME,
    CONNECT_PASSWORD,
    CONNECT_SSL,
) = range(6)

# Conversation states for /sync
SYNC_CAPTCHA = 10

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


def _bot_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return cast(Settings, context.application.bot_data["settings"])


def _build_captcha_url(
    context: ContextTypes.DEFAULT_TYPE,
    challenge: object,
) -> str:
    """Build the URL the user should open to solve the CAPTCHA.

    When ``captcha_helper_base_url`` is configured, returns a URL that
    points to the self-hosted captcha-helper page (which embeds the Proton
    verification iframe and exposes the hCaptcha response token for the
    user to copy).  Otherwise falls back to the raw Proton verification
    URL (which won't expose the token — only useful for debugging).
    """
    from .proton_api import CaptchaChallenge

    ch = cast(CaptchaChallenge, challenge)
    settings = _bot_settings(context)
    base = settings.captcha_helper_base_url.rstrip("/")
    if base:
        return f"{base}?token={ch.token}&methods=captcha"
    return ch.web_url


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


@_gate
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    await db.upsert_user(chat.id)
    commands_help = (
        "\n\n<b>Perintah yang tersedia:</b>\n"
        "/start — Tampilkan pesan ini\n"
        "/connect — Setup kredensial IMAP Proton Bridge\n"
        "/disconnect — Hapus kredensial dan stop listener\n"
        "/sync &lt;user&gt; &lt;pass&gt; — Auto-sync alias dari akun Proton\n"
        "/addalias — Tambah alias secara manual\n"
        "/removealias — Hapus alias\n"
        "/list — Lihat semua alias dan pilih yang aktif\n"
        "/unlock — Lepas kunci alias yang sedang aktif\n"
        "/history — Lihat alias yang sudah terpakai\n"
        "/reset — Kembalikan alias ke daftar tersedia\n"
        "/cancel — Batalkan dialog /connect"
    )
    primaries = await db.list_primary_accounts(chat.id)
    if not primaries:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Halo! Aku akan memberitahumu kalau ada email masuk ke alias Proton-mu.\n\n"
            "Langkah:\n"
            "1) Jalankan Proton Bridge dan login akun Proton di sana.\n"
            "2) Kirim /connect untuk daftarin akun itu ke bot.\n"
            "3) Kamu bisa /connect lagi untuk akun Proton lain (multi-akun didukung).\n"
            "4) Kirim /list untuk lihat semua email utama + alias-aliasnya."
            + commands_help,
            parse_mode=ParseMode.HTML,
        )
        return
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Halo!" + commands_help,
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
        captcha_url = _build_captcha_url(context, result)
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Proton memerlukan verifikasi CAPTCHA.\n\n"
            f"1. Buka link ini di browser:\n{captcha_url}\n\n"
            "2. Selesaikan CAPTCHA\n"
            "3. Copy token yang muncul, lalu kirim (paste) ke sini.\n\n"
            "Kirim /cancel untuk membatalkan.",
        )
        return SYNC_CAPTCHA

    return await _sync_complete(update, context, result)


async def sync_captcha_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user pasting the hCaptcha response token."""
    user_data = cast(dict, context.user_data)
    challenge = user_data.pop("sync_challenge", None)
    if challenge is None:
        await update.effective_message.reply_text("Sesi sync sudah kedaluwarsa. Coba /sync lagi.")  # type: ignore[union-attr]
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if text.lower() == "done":
        # "done" is no longer valid — the user must paste the token.
        user_data["sync_challenge"] = challenge  # keep challenge alive
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Jangan kirim 'done'. Setelah CAPTCHA selesai, <b>copy token</b> "
            "yang muncul di halaman lalu <b>paste di sini</b>.\n\n"
            "Kirim /cancel untuk membatalkan.",
            parse_mode=ParseMode.HTML,
        )
        return SYNC_CAPTCHA

    # User pasted the hCaptcha response token.
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


@_gate
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    db = _bot_db(context)
    existing = await db.list_primary_accounts(chat.id)
    intro = (
        "Tambah akun Proton baru. Setiap kali /connect kamu menambahkan satu "
        "akun email utama (multi-akun didukung)."
    )
    if existing:
        emails = ", ".join(p.email for p in existing)
        intro += (
            f"\n\nSaat ini terdaftar: <b>{html.escape(emails)}</b>. "
            "Kalau email yang sama dimasukkan ulang, kredensial-nya akan ditimpa."
        )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        intro,
        parse_mode=ParseMode.HTML,
    )
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Alamat email Proton akun ini? (mis. vielz43@proton.me)"
    )
    return CONNECT_EMAIL


async def connect_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if "@" not in text:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Itu bukan alamat email yang valid. Masukkan email Proton-nya:"
        )
        return CONNECT_EMAIL
    context.user_data["primary_email"] = text.lower()  # type: ignore[index]
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Host Bridge? (default: 127.0.0.1)"
    )
    return CONNECT_HOST


async def connect_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    context.user_data["imap_host"] = text or "127.0.0.1"  # type: ignore[index]
    await update.effective_message.reply_text("Port? (default Bridge: 1143)")  # type: ignore[union-attr]
    return CONNECT_PORT


async def connect_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        text = "1143"
    if not text.isdigit():
        await update.effective_message.reply_text("Port harus angka. Coba lagi:")  # type: ignore[union-attr]
        return CONNECT_PORT
    context.user_data["imap_port"] = int(text)  # type: ignore[index]
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Username IMAP Bridge? (biasanya sama dengan alamat email di atas — "
        "tekan Enter / kirim '.' untuk pakai email yang sudah kamu kasih)"
    )
    return CONNECT_USERNAME


async def connect_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text or text == ".":
        text = cast(dict, context.user_data)["primary_email"]
    context.user_data["imap_username"] = text  # type: ignore[index]
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Password yang di-generate Proton Bridge? (akan disimpan terenkripsi). "
        "Pesan ini bisa kamu hapus setelah bot mengkonfirmasi."
    )
    return CONNECT_PASSWORD


async def connect_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        await update.effective_message.reply_text("Password tidak boleh kosong:")  # type: ignore[union-attr]
        return CONNECT_PASSWORD
    context.user_data["imap_password"] = text  # type: ignore[index]
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Pakai SSL/TLS? Ketik 'yes' untuk SSL langsung (jarang), 'no' untuk STARTTLS (default Bridge)."
    )
    return CONNECT_SSL


async def connect_ssl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip().lower()  # type: ignore[union-attr]
    use_ssl = text in {"y", "yes", "true", "1", "ssl"}
    chat = update.effective_chat
    if chat is None:
        return ConversationHandler.END
    db = _bot_db(context)
    cipher = _bot_cipher(context)
    manager = _bot_manager(context)
    user_data = cast(dict, context.user_data)
    encrypted = cipher.encrypt(user_data["imap_password"])
    primary_id = await db.add_primary_account(
        chat_id=chat.id,
        email=user_data["primary_email"],
        host=user_data["imap_host"],
        port=user_data["imap_port"],
        username=user_data["imap_username"],
        encrypted_password=encrypted,
        use_ssl=use_ssl,
    )
    user_data.pop("imap_password", None)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        f"Kredensial untuk <b>{html.escape(user_data['primary_email'])}</b> "
        "disimpan. Mulai memantau inbox...",
        parse_mode=ParseMode.HTML,
    )
    try:
        await manager.start_for_primary(primary_id)
    except Exception as exc:
        LOGGER.exception(
            "failed to start listener after /connect for primary %s", primary_id
        )
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal terhubung ke Bridge: {exc}\n"
            "Coba /connect lagi setelah Bridge siap, atau /disconnect untuk hapus."
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Tersambung! Bot akan auto-discover alias dari INBOX akun ini. "
        "Kirim /list untuk lihat semua email utama."
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = cast(dict, context.user_data)
    user_data.pop("imap_password", None)
    if update.effective_message is not None:
        await update.effective_message.reply_text("Dibatalkan.")
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


def build_handlers() -> list:
    connect_conv = ConversationHandler(
        entry_points=[CommandHandler("connect", cmd_connect)],
        states={
            CONNECT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_email)],
            CONNECT_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_host)],
            CONNECT_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_port)],
            CONNECT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_username)],
            CONNECT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_password)],
            CONNECT_SSL: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_ssl)],
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

    return [
        CommandHandler("start", cmd_start),
        CommandHandler("list", cmd_list),
        CommandHandler("unlock", cmd_unlock),
        CommandHandler("history", cmd_history),
        CommandHandler("addalias", cmd_addalias),
        CommandHandler("removealias", cmd_removealias),
        CommandHandler("reset", cmd_reset),
        CommandHandler("disconnect", cmd_disconnect),
        connect_conv,
        sync_conv,
        CallbackQueryHandler(on_callback),
    ]
