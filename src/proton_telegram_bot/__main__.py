"""Entry point: build the Telegram Application, restore listeners, and run polling."""
from __future__ import annotations

import asyncio
import logging
import os

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


# Telegram-API HTTP timeouts. The defaults baked into
# ``python-telegram-bot`` (5 s connect / 5 s read / 5 s write) are too
# tight for VPS-to-Telegram links with elevated latency or transient
# packet loss — observed on the Indonesian VPS where ping to
# ``api.telegram.org`` showed ~33% loss and ~170 ms RTT, which made
# every other ``send_message`` raise ``httpcore.ConnectTimeout`` and
# bubble up as ``uncaught exception in handler``. Bumping the
# connect/read/write budget to ~15-20 s absorbs those blips without
# changing behaviour on healthy networks (a successful round-trip is
# still <500 ms).
_TELEGRAM_CONNECT_TIMEOUT = 15.0
_TELEGRAM_READ_TIMEOUT = 20.0
_TELEGRAM_WRITE_TIMEOUT = 20.0
_TELEGRAM_POOL_TIMEOUT = 30.0
# ``getUpdates`` long-poll keeps the connection open for the duration
# of ``timeout`` in the request payload (PTB sets this to 10 s by
# default), so the read budget must be larger than that plus margin.
_GET_UPDATES_READ_TIMEOUT = 60.0


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
        .connect_timeout(_TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(_TELEGRAM_READ_TIMEOUT)
        .write_timeout(_TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(_TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(_TELEGRAM_CONNECT_TIMEOUT)
        .get_updates_read_timeout(_GET_UPDATES_READ_TIMEOUT)
        .get_updates_write_timeout(_TELEGRAM_WRITE_TIMEOUT)
        .get_updates_pool_timeout(_TELEGRAM_POOL_TIMEOUT)
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
    # Expose the singleton notifier so ``/inbox`` (in bot.py) can
    # re-deliver fetched emails through exactly the same render +
    # keyboard pipeline that the live IMAP listener uses.
    application.bot_data["notifier"] = notifier
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


# Third-party loggers we always pin to WARNING regardless of the
# user's ``LOG_LEVEL``:
#
# - ``aioimaplib`` and ``aioimaplib.aioimaplib`` log every IMAP frame
#   at DEBUG, which means raw email bodies (including OTPs, account
#   recovery links, and unredacted message contents) end up in
#   ``journalctl`` whenever the operator turns on DEBUG to debug the
#   bot. That's both a privacy leak and an unmanageable amount of
#   log volume.
# - ``httpx`` and ``httpcore`` log every TCP connect, request, and
#   response at DEBUG. Useful occasionally, but in steady state they
#   triple the journal size and hide the application's own log lines.
#
# Operators who genuinely need third-party DEBUG can still get it by
# setting ``PROTON_BOT_VERBOSE_THIRDPARTY=1`` in the environment.
_QUIET_THIRDPARTY_LOGGERS: tuple[str, ...] = (
    "aioimaplib",
    "aioimaplib.aioimaplib",
    "httpx",
    "httpcore",
    "hpack",
)


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if os.environ.get("PROTON_BOT_VERBOSE_THIRDPARTY") == "1":
        return
    for name in _QUIET_THIRDPARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    settings = load_settings()
    _configure_logging(settings)
    application = _build_application(settings)
    # Connect DB before run_polling takes over the event loop.
    asyncio.get_event_loop().run_until_complete(_bootstrap_db(application))
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
