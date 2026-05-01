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


async def test_multiple_primary_accounts_per_chat(db: Database) -> None:
    """Two Proton accounts on the same chat keep their aliases separated."""
    chat_id = 7
    await db.upsert_user(chat_id)
    p1 = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz43@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz43@proton.me",
        encrypted_password="enc1",
        use_ssl=False,
    )
    p2 = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz22@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz22@proton.me",
        encrypted_password="enc2",
        use_ssl=False,
    )
    assert p1 != p2
    primaries = await db.list_primary_accounts(chat_id)
    assert sorted(p.email for p in primaries) == [
        "vielz22@proton.me",
        "vielz43@proton.me",
    ]
    assert sorted(await db.list_all_primary_ids()) == sorted([p1, p2])

    # Aliases are scoped per primary.
    await db.add_aliases(
        chat_id, ["a@proton.me", "b@proton.me"], primary_id=p1
    )
    await db.add_aliases(chat_id, ["c@proton.me"], primary_id=p2)
    a_p1 = await db.list_aliases(chat_id, primary_id=p1)
    a_p2 = await db.list_aliases(chat_id, primary_id=p2)
    assert sorted(a.email for a in a_p1) == ["a@proton.me", "b@proton.me"]
    assert [a.email for a in a_p2] == ["c@proton.me"]
    # All aliases combined.
    assert len(await db.list_aliases(chat_id)) == 3

    # Stored credentials roundtrip.
    assert await db.get_primary_encrypted_password(p1) == "enc1"
    assert await db.get_primary_encrypted_password(p2) == "enc2"

    # Re-inserting the same email updates credentials in place rather than
    # creating a duplicate row.
    p1_again = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz43@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz43@proton.me",
        encrypted_password="enc1-rotated",
        use_ssl=False,
    )
    assert p1_again == p1
    assert await db.get_primary_encrypted_password(p1) == "enc1-rotated"
    assert len(await db.list_primary_accounts(chat_id)) == 2

    # Deleting a primary cleans up its aliases.
    assert await db.delete_primary_account(chat_id, p1) is True
    assert await db.list_aliases(chat_id, primary_id=p1) == []
    assert len(await db.list_aliases(chat_id)) == 1
    assert [p.id for p in await db.list_primary_accounts(chat_id)] == [p2]
