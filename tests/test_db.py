from __future__ import annotations

from pathlib import Path

import pytest

from proton_telegram_bot.db import Database
from proton_telegram_bot.models import AliasStatus


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.sqlite3")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_user_lifecycle(db: Database) -> None:
    await db.upsert_user(42)
    user = await db.get_user(42)
    assert user is not None
    assert user.chat_id == 42
    assert user.has_credentials is False

    await db.set_credentials(42, "127.0.0.1", 1143, "vielz45@proton.me", "encrypted-token", False)
    user = await db.get_user(42)
    assert user is not None and user.has_credentials is True
    assert user.imap_host == "127.0.0.1"
    assert user.imap_port == 1143
    assert user.imap_username == "vielz45@proton.me"

    encrypted = await db.get_encrypted_password(42)
    assert encrypted == "encrypted-token"

    await db.clear_credentials(42)
    user = await db.get_user(42)
    assert user is not None and user.has_credentials is False


async def test_alias_lifecycle(db: Database) -> None:
    await db.upsert_user(1)
    inserted = await db.add_aliases(1, ["A@proton.me", "b@proton.me", "a@proton.me"])
    # "A@proton.me" and "a@proton.me" collapse to the same lower-cased entry.
    assert inserted == 2

    aliases = await db.list_aliases(1, status=AliasStatus.AVAILABLE)
    emails = sorted(a.email for a in aliases)
    assert emails == ["a@proton.me", "b@proton.me"]

    alias = await db.find_alias(1, "A@proton.me")
    assert alias is not None and alias.status == AliasStatus.AVAILABLE
    by_id = await db.find_alias_by_id(1, alias.id)
    assert by_id is not None and by_id.email == "a@proton.me"
    assert await db.find_alias_by_id(1, 9999) is None
    # find_alias_by_id is scoped to chat_id, so a stranger can't find it.
    assert await db.find_alias_by_id(2, alias.id) is None
    await db.mark_consumed(alias.id, message_id="<msg-1@example>")

    available = await db.list_aliases(1, status=AliasStatus.AVAILABLE)
    consumed = await db.list_aliases(1, status=AliasStatus.CONSUMED)
    assert [a.email for a in available] == ["b@proton.me"]
    assert [a.email for a in consumed] == ["a@proton.me"]
    assert consumed[0].last_message_id == "<msg-1@example>"

    assert await db.reset_alias(1, "a@proton.me") is True
    assert len(await db.list_aliases(1, status=AliasStatus.CONSUMED)) == 0

    assert await db.remove_alias(1, "b@proton.me") is True
    assert await db.remove_alias(1, "missing@proton.me") is False


async def test_users_with_credentials(db: Database) -> None:
    await db.upsert_user(1)
    await db.upsert_user(2)
    await db.set_credentials(2, "h", 1143, "u", "x", False)
    assert await db.list_users_with_credentials() == [2]
