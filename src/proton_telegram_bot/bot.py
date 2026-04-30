"""Telegram bot wiring: handlers, menus, and notifier implementation."""
from __future__ import annotations

import html
import logging
from collections.abc import Iterable
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
from .models import AliasStatus

LOGGER = logging.getLogger(__name__)

# Conversation states for /connect
CONNECT_HOST, CONNECT_PORT, CONNECT_USERNAME, CONNECT_PASSWORD, CONNECT_SSL = range(5)

CB_PICK = "pick"
CB_REFRESH = "refresh"
CB_RESET = "reset"
CB_DELETE = "delete"
CB_NOOP = "noop"


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


def _build_alias_keyboard(emails: Iterable[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    emails_list = list(emails)
    if not emails_list:
        rows.append(
            [InlineKeyboardButton("(belum ada alias tersedia)", callback_data=CB_NOOP)]
        )
    else:
        for email in emails_list:
            rows.append([InlineKeyboardButton(email, callback_data=f"{CB_PICK}:{email}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=CB_REFRESH)])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------- /start


@_gate
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    await db.upsert_user(chat.id)
    user = await db.get_user(chat.id)
    if user is None or not user.has_credentials:
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Halo! Aku akan memberitahumu kalau ada email masuk ke alias Proton-mu.\n\n"
            "Langkah:\n"
            "1) Jalankan Proton Bridge di komputermu (atau VPS).\n"
            "2) Kirim /connect untuk memasukkan detail IMAP dari Bridge.\n"
            "3) Kirim /addalias diikuti daftar alamat alias-mu.\n"
            "4) Kirim /list untuk melihat alias yang masih tersedia."
        )
        return
    aliases = await db.list_aliases(chat.id, status=AliasStatus.AVAILABLE)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Halo! Berikut alias yang masih tersedia:",
        reply_markup=_build_alias_keyboard([a.email for a in aliases]),
    )


# --------------------------------------------------------------- /list


@_gate
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    aliases = await db.list_aliases(chat.id, status=AliasStatus.AVAILABLE)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Alias yang masih tersedia:",
        reply_markup=_build_alias_keyboard([a.email for a in aliases]),
    )


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
    inserted = await db.add_aliases(chat.id, valid)
    skipped = len(valid) - inserted
    msg = f"Ditambahkan: {inserted} alias."
    if skipped:
        msg += f" Sudah ada sebelumnya: {skipped}."
    aliases = await db.list_aliases(chat.id, status=AliasStatus.AVAILABLE)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        msg,
        reply_markup=_build_alias_keyboard([a.email for a in aliases]),
    )


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
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Setup Proton Bridge IMAP. Kirim host (default: 127.0.0.1):"
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
        "Username Bridge? (biasanya alamat email Proton-mu)"
    )
    return CONNECT_USERNAME


async def connect_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if not text:
        await update.effective_message.reply_text("Username tidak boleh kosong:")  # type: ignore[union-attr]
        return CONNECT_USERNAME
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
    await db.set_credentials(
        chat_id=chat.id,
        host=user_data["imap_host"],
        port=user_data["imap_port"],
        username=user_data["imap_username"],
        encrypted_password=encrypted,
        use_ssl=use_ssl,
    )
    user_data.pop("imap_password", None)
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Kredensial disimpan. Mulai memantau inbox..."
    )
    try:
        await manager.start_for_user(chat.id)
    except Exception as exc:
        LOGGER.exception("failed to start listener after /connect")
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Gagal terhubung ke Bridge: {exc}\nCoba /connect lagi setelah Bridge siap."
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(  # type: ignore[union-attr]
        "Tersambung. Kirim /addalias untuk daftarkan alamat alias-mu."
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = cast(dict, context.user_data)
    user_data.pop("imap_password", None)
    if update.effective_message is not None:
        await update.effective_message.reply_text("Dibatalkan.")
    return ConversationHandler.END


@_gate
async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db = _bot_db(context)
    manager = _bot_manager(context)
    await manager.stop_for_user(chat.id)
    await db.clear_credentials(chat.id)
    await update.effective_message.reply_text("Kredensial dihapus dan listener dihentikan.")  # type: ignore[union-attr]


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
    if data == CB_REFRESH:
        aliases = await db.list_aliases(chat_id, status=AliasStatus.AVAILABLE)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_build_alias_keyboard([a.email for a in aliases])
            )
        except Exception:
            pass
        return
    if data.startswith(f"{CB_PICK}:"):
        email = data.split(":", 1)[1]
        alias = await db.find_alias(chat_id, email)
        if alias is None:
            await query.edit_message_text("Alias tidak ditemukan.")
            return
        if alias.status == AliasStatus.CONSUMED:
            await query.edit_message_text(
                f"Alias <b>{html.escape(alias.email)}</b> sudah dipakai.",
                parse_mode=ParseMode.HTML,
            )
            return
        await query.message.reply_text(  # type: ignore[union-attr]
            f"Aktif: <b>{html.escape(alias.email)}</b>\n"
            "Kasih alamat ini ke rekan bisnismu. Aku tunggu emailnya, "
            "dan akan kirim isinya ke sini begitu masuk.",
            parse_mode=ParseMode.HTML,
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
        body = summary.get("body") or "(tidak ada isi text)"
        text = (
            f"<b>Email masuk untuk</b> <code>{html.escape(alias_email)}</code>\n"
            f"<b>Dari:</b> {html.escape(summary.get('from', '?'))}\n"
            f"<b>Subjek:</b> {html.escape(summary.get('subject', ''))}\n"
            f"<b>Tanggal:</b> {html.escape(summary.get('date', ''))}\n\n"
            f"<pre>{html.escape(body)}</pre>\n\n"
            "Alias ini sudah dihapus dari daftar. /list untuk lihat sisanya."
        )
        # Telegram message limit is 4096 chars; trim defensively.
        if len(text) > 4000:
            text = text[:4000] + "\n…(dipotong)"
        await self._application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )


# --------------------------------------------------------------- registration


def build_handlers() -> list:
    connect_conv = ConversationHandler(
        entry_points=[CommandHandler("connect", cmd_connect)],
        states={
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

    return [
        CommandHandler("start", cmd_start),
        CommandHandler("list", cmd_list),
        CommandHandler("history", cmd_history),
        CommandHandler("addalias", cmd_addalias),
        CommandHandler("removealias", cmd_removealias),
        CommandHandler("reset", cmd_reset),
        CommandHandler("disconnect", cmd_disconnect),
        connect_conv,
        CallbackQueryHandler(on_callback),
    ]
