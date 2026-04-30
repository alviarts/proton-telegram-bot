"""Entry point: build the Telegram Application, restore listeners, and run polling."""
from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, ApplicationBuilder

from .bot import TelegramNotifier, build_handlers
from .config import Settings, load_settings
from .crypto import CredentialCipher
from .db import Database
from .manager import ListenerManager

LOGGER = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    manager: ListenerManager = application.bot_data["manager"]
    await manager.restore_all()


async def _post_shutdown(application: Application) -> None:
    manager: ListenerManager = application.bot_data.get("manager")  # type: ignore[assignment]
    if manager is not None:
        await manager.stop_all()
    db: Database = application.bot_data.get("db")  # type: ignore[assignment]
    if db is not None:
        await db.close()


def _build_application(settings: Settings) -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    db = Database(settings.database_path)
    cipher = CredentialCipher(settings.encryption_key)
    notifier = TelegramNotifier(application)
    manager = ListenerManager(db=db, cipher=cipher, notifier=notifier)
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["cipher"] = cipher
    application.bot_data["manager"] = manager
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
