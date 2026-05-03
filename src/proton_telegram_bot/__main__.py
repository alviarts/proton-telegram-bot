"""Entry point: build the Telegram Application, restore listeners, and run polling."""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder

from .bot import TelegramNotifier, build_handlers, on_error
from .bridge_admin import BridgeAdmin
from .config import Settings, load_settings
from .crypto import CredentialCipher
from .db import Database
from .manager import ListenerManager
from .proxy_provider import ProxyProvider
from .watchdog import heartbeat_loop
from .watchdog import notify as systemd_notify

LOGGER = logging.getLogger(__name__)

# Commands surfaced as the slash-menu in Telegram (the popup that appears
# next to the chat box). Order = display order. Keep the descriptions
# short — Telegram clips them in the menu UI.
BOT_COMMAND_MENU: list[tuple[str, str]] = [
    ("start", "Mulai & lihat panduan singkat"),
    ("connect", "Tambah akun Proton baru"),
    ("list", "Daftar email utama & alias"),
    ("accounts", "Daftar akun Proton"),
    ("setprotonpw", "Simpan password master Proton"),
    ("genaddr", "Generate alamat (cth: /genaddr vielz 10)"),
    ("addalias", "Tambah alias manual"),
    ("removealias", "Hapus alias"),
    ("history", "Alias yang sudah terpakai"),
    ("reset", "Kembalikan alias ke daftar tersedia"),
    ("unlock", "Lepas kunci alias aktif"),
    ("disconnect", "Hapus akun + stop listener"),
    ("sync", "Auto-sync alias dari akun Proton"),
    ("cancel", "Batalkan dialog yang sedang jalan"),
]


async def _post_init(application: Application) -> None:
    manager: ListenerManager = application.bot_data["manager"]
    await manager.restore_all()
    # Push the slash-menu so users see a clickable command list in
    # Telegram's chat-box UI instead of having to memorise commands.
    try:
        await application.bot.set_my_commands(
            [BotCommand(name, description) for name, description in BOT_COMMAND_MENU]
        )
    except Exception:
        LOGGER.exception("failed to publish bot command menu")
    # Tell systemd we're up so ``systemctl start`` unblocks, then start
    # the heartbeat task so a deadlocked event loop trips the unit's
    # ``WatchdogSec=`` and gets restarted automatically.
    systemd_notify("READY=1")
    systemd_notify("STATUS=polling Telegram updates")
    application.bot_data["watchdog_task"] = asyncio.create_task(
        heartbeat_loop(), name="systemd-watchdog-heartbeat"
    )


async def _post_shutdown(application: Application) -> None:
    systemd_notify("STOPPING=1")
    watchdog_task: asyncio.Task | None = application.bot_data.get("watchdog_task")
    if watchdog_task is not None and not watchdog_task.done():
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass
    manager: ListenerManager = application.bot_data.get("manager")  # type: ignore[assignment]
    if manager is not None:
        await manager.stop_all()
    db: Database = application.bot_data.get("db")  # type: ignore[assignment]
    if db is not None:
        await db.close()


def _build_application(settings: Settings) -> Application:
    # ``concurrent_updates=True`` lets PTB dispatch handlers in parallel
    # tasks instead of one-at-a-time. Without this a single user whose
    # /connect flow is mid smoke-test (60-150s) blocks every other
    # update — including /start, /cancel, /accounts — making the bot
    # appear "mati" even though the polling loop is healthy. The user
    # explicitly asked: "jangan error stuck lagi kedepannya".
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    # Global error handler: any exception raised inside a handler funnels
    # here instead of bubbling up and silently killing the polling loop.
    # See ``on_error`` for the reply-and-log behaviour.
    application.add_error_handler(on_error)
    db = Database(settings.database_path)
    cipher = CredentialCipher(settings.encryption_key)
    notifier = TelegramNotifier(application)
    manager = ListenerManager(
        db=db,
        cipher=cipher,
        notifier=notifier,
        alias_sync_interval=settings.alias_sync_interval_minutes * 60,
    )
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["cipher"] = cipher
    application.bot_data["manager"] = manager
    application.bot_data["bridge_admin"] = BridgeAdmin(settings)
    # Optional Proton-bound proxy rotation. ``from_env`` returns ``None`` when
    # ``PROTON_USE_PROXY=0`` so deployments can disable it without touching code.
    proxy_provider = ProxyProvider.from_env()
    if proxy_provider is not None:
        LOGGER.info("proxy provider enabled for Proton-bound traffic")
        application.bot_data["proxy_provider"] = proxy_provider
    else:
        LOGGER.info(
            "proxy provider disabled (PROTON_USE_PROXY=0) — using direct connection"
        )
    for handler in build_handlers():
        application.add_handler(handler)
    return application


async def _bootstrap_db(application: Application) -> None:
    db: Database = application.bot_data["db"]
    await db.connect()


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = _build_application(settings)
    # Connect DB before run_polling takes over the event loop.
    asyncio.get_event_loop().run_until_complete(_bootstrap_db(application))
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
