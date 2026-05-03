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


async def test_active_primary_roundtrip_and_delete_clears(db: Database) -> None:
    """``set_active_primary`` persists, and ``delete_primary_account`` clears it.

    Regression coverage for the UX bug where ``/genaddr`` ignored the user's
    just-configured account: ``/setprotonpw`` now stores the picked primary
    via ``set_active_primary`` so subsequent commands default to it. The
    delete path must NULL the column so a stale id never wins later.
    """
    chat_id = 11
    await db.upsert_user(chat_id)
    assert await db.get_active_primary_id(chat_id) is None  # nothing set yet
    p1 = await db.add_primary_account(
        chat_id=chat_id,
        email="alpha@proton.me",
        host="127.0.0.1",
        port=1143,
        username="alpha@proton.me",
        encrypted_password="e1",
        use_ssl=False,
    )
    p2 = await db.add_primary_account(
        chat_id=chat_id,
        email="beta@proton.me",
        host="127.0.0.1",
        port=1143,
        username="beta@proton.me",
        encrypted_password="e2",
        use_ssl=False,
    )
    await db.set_active_primary(chat_id, p2)
    assert await db.get_active_primary_id(chat_id) == p2

    # Deleting the active primary nulls out the pointer instead of leaving
    # a dangling reference.
    await db.delete_primary_account(chat_id, p2)
    assert await db.get_active_primary_id(chat_id) is None

    # Setting back to a still-existing primary works and survives.
    await db.set_active_primary(chat_id, p1)
    assert await db.get_active_primary_id(chat_id) == p1
    # Explicitly clearing.
    await db.set_active_primary(chat_id, None)
    assert await db.get_active_primary_id(chat_id) is None


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


async def test_delete_primary_account_clean_slate(db: Database) -> None:
    """``delete_primary_account`` purges every dependent row so that a future
    /connect for the same email starts from scratch — no stale aliases, no
    stale generator cursor, no stale active-alias / active-primary pointer,
    no leftover legacy IMAP creds.
    """
    from proton_telegram_bot.alias_gen import GenState

    chat_id = 555
    await db.upsert_user(chat_id)
    # Pre-fill legacy per-user IMAP creds (older deploys persisted them on
    # the ``users`` row before the multi-primary migration). They must be
    # wiped when the last primary is removed.
    await db.set_credentials(
        chat_id,
        host="127.0.0.1",
        port=1143,
        username="legacy@proton.me",
        encrypted_password="legacy-token",
        use_ssl=False,
    )

    pid = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz45@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz45@proton.me",
        encrypted_password="enc",
        use_ssl=False,
    )
    await db.add_aliases(
        chat_id,
        ["alpha@proton.me", "beta@proton.me"],
        primary_id=pid,
    )
    alias = await db.find_alias(chat_id, "alpha@proton.me")
    assert alias is not None
    await db.set_active_alias(chat_id, alias.id)
    await db.set_active_primary(chat_id, pid)
    await db.save_generator_state(chat_id, pid, "vielz", GenState("", 42))

    assert await db.delete_primary_account(chat_id, pid) is True

    # Aliases for this primary gone.
    assert await db.list_aliases(chat_id, primary_id=pid) == []
    assert await db.list_aliases(chat_id) == []
    # Active pointers cleared so a stale id can't win later.
    assert await db.get_active_alias(chat_id) is None
    assert await db.get_active_primary_id(chat_id) is None
    # Generator state for this (chat, primary) gone — next /genaddr starts
    # from the default cursor.
    assert await db.get_generator_state(chat_id, pid, "vielz") == GenState("", 1)
    # Legacy per-user IMAP creds wiped because no primaries are left.
    user = await db.get_user(chat_id)
    assert user is not None and user.has_credentials is False


# ---------------------------------------------------------------- proton master password


async def test_proton_password_lifecycle(db: Database) -> None:
    chat_id = 999
    primary_id = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz45@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz45@proton.me",
        encrypted_password="bridge-pw",
        use_ssl=False,
    )
    # Default: no proton password until /setprotonpw runs.
    assert await db.get_proton_password_encrypted(primary_id) is None

    assert await db.set_proton_password(chat_id, primary_id, "fernet-encrypted") is True
    assert await db.get_proton_password_encrypted(primary_id) == "fernet-encrypted"

    # Updating overwrites the previous value.
    assert await db.set_proton_password(chat_id, primary_id, "rotated") is True
    assert await db.get_proton_password_encrypted(primary_id) == "rotated"

    # Wrong chat_id must not silently mutate the row.
    assert (
        await db.set_proton_password(chat_id + 1, primary_id, "attacker") is False
    )
    assert await db.get_proton_password_encrypted(primary_id) == "rotated"

    assert await db.clear_proton_password(chat_id, primary_id) is True
    assert await db.get_proton_password_encrypted(primary_id) is None


# ---------------------------------------------------------------- alias generator state


async def test_generator_state_default_and_save(db: Database) -> None:
    from proton_telegram_bot.alias_gen import GenState

    chat_id = 42
    primary_id = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz45@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz45@proton.me",
        encrypted_password="bridge-pw",
        use_ssl=False,
    )

    # Default cursor is GenState("", 1) when nothing has been saved yet.
    assert await db.get_generator_state(chat_id, primary_id, "vielz") == GenState("", 1)

    await db.save_generator_state(chat_id, primary_id, "vielz", GenState("", 50))
    assert await db.get_generator_state(chat_id, primary_id, "vielz") == GenState("", 50)

    # Distinct bases keep separate cursors.
    await db.save_generator_state(chat_id, primary_id, "alt", GenState("a", 7))
    assert await db.get_generator_state(chat_id, primary_id, "alt") == GenState("a", 7)
    assert await db.get_generator_state(chat_id, primary_id, "vielz") == GenState("", 50)

    # Base lookup is case-insensitive — saving "Vielz" must match reads of "vielz".
    await db.save_generator_state(chat_id, primary_id, "Vielz", GenState("b", 3))
    assert await db.get_generator_state(chat_id, primary_id, "vielz") == GenState("b", 3)


async def test_proton_password_column_is_nullable_for_legacy_rows(tmp_path) -> None:
    """Existing primary_accounts rows from before the migration must keep working."""
    import aiosqlite

    db_path = tmp_path / "legacy.sqlite3"

    # Build an old-shape DB by hand (no proton_password_encrypted column).
    # The ``users`` table must include the legacy single-account columns so
    # the backfill query in ``_backfill_primary_accounts_from_legacy_users``
    # can run cleanly even when no legacy rows exist.
    legacy_schema = """
    CREATE TABLE users (
        chat_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        imap_host TEXT,
        imap_port INTEGER,
        imap_username TEXT,
        imap_password_encrypted TEXT,
        imap_use_ssl INTEGER NOT NULL DEFAULT 0,
        active_alias_id INTEGER
    );
    CREATE TABLE aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        consumed_at TEXT,
        last_message_id TEXT,
        primary_id INTEGER,
        UNIQUE(chat_id, email)
    );
    CREATE TABLE primary_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        imap_host TEXT NOT NULL,
        imap_port INTEGER NOT NULL,
        imap_username TEXT NOT NULL,
        imap_password_encrypted TEXT NOT NULL,
        imap_use_ssl INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(chat_id, email)
    );
    """
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(legacy_schema)
        await conn.execute(
            "INSERT INTO users (chat_id, created_at) VALUES (1, '2026-01-01T00:00:00+00:00')"
        )
        await conn.execute(
            "INSERT INTO primary_accounts "
            "(chat_id, email, imap_host, imap_port, imap_username, "
            "imap_password_encrypted, imap_use_ssl, created_at) "
            "VALUES (1, 'old@proton.me', '127.0.0.1', 1143, 'old@proton.me', "
            "'enc', 0, '2026-01-01T00:00:00+00:00')"
        )
        await conn.commit()

    # Open via the new Database — migration should add the new column.
    database = Database(db_path)
    await database.connect()
    try:
        primaries = await database.list_primary_accounts(1)
        assert len(primaries) == 1
        # New column is nullable; legacy row reads as None.
        assert await database.get_proton_password_encrypted(primaries[0].id) is None
        # And we can subsequently write to it.
        assert await database.set_proton_password(1, primaries[0].id, "new-pw") is True
        assert await database.get_proton_password_encrypted(primaries[0].id) == "new-pw"
    finally:
        await database.close()


async def test_record_alias_sender_resolves_default_label(db: Database) -> None:
    """A sender from a known default domain gets auto-labelled."""
    await db.upsert_user(1)
    await db.add_aliases(1, ["vielz008@proton.me"], primary_id=None)
    alias = await db.find_alias(1, "vielz008@proton.me")
    assert alias is not None

    label = await db.record_alias_sender(
        1, alias.id, "no-reply@cognition.ai", "cognition.ai"
    )
    assert label == "Devin"
    history = await db.list_alias_senders(1, alias.id)
    assert len(history) == 1
    assert history[0].sender_domain == "cognition.ai"
    assert history[0].seen_count == 1

    # Same domain again: bumps seen_count, doesn't insert a new row.
    await db.record_alias_sender(
        1, alias.id, "billing@cognition.ai", "cognition.ai"
    )
    history2 = await db.list_alias_senders(1, alias.id)
    assert len(history2) == 1
    assert history2[0].seen_count == 2


async def test_record_alias_sender_walks_subdomain(db: Database) -> None:
    """A subdomain like ``mail.github.com`` resolves to ``GitHub``."""
    await db.upsert_user(1)
    await db.add_aliases(1, ["test@proton.me"], primary_id=None)
    alias = await db.find_alias(1, "test@proton.me")
    assert alias is not None

    label = await db.record_alias_sender(
        1, alias.id, "noreply@notifications.github.com", "notifications.github.com"
    )
    assert label == "GitHub"


async def test_service_label_user_override_wins(db: Database) -> None:
    """User-defined chat-level mapping beats the built-in default."""
    await db.upsert_user(1)
    await db.set_service_label(1, "cognition.ai", "MyDevin")
    assert await db.resolve_service_label(1, "cognition.ai") == "MyDevin"
    assert await db.resolve_service_label(1, "sub.cognition.ai") == "MyDevin"


async def test_service_label_remove(db: Database) -> None:
    await db.upsert_user(1)
    await db.set_service_label(1, "roboneo.com", "Roboneo")
    assert await db.resolve_service_label(1, "roboneo.com") == "Roboneo"
    assert await db.remove_service_label(1, "roboneo.com") is True
    # After removal: no chat override, no built-in match → None.
    assert await db.resolve_service_label(1, "roboneo.com") is None
    # Removing again returns False.
    assert await db.remove_service_label(1, "roboneo.com") is False


async def test_get_alias_service_labels_groups_by_alias(db: Database) -> None:
    """``get_alias_service_labels`` returns ``{alias_id: [labels…]}``."""
    await db.upsert_user(1)
    await db.add_aliases(
        1, ["a@proton.me", "b@proton.me"], primary_id=None
    )
    alias_a = await db.find_alias(1, "a@proton.me")
    alias_b = await db.find_alias(1, "b@proton.me")
    assert alias_a is not None and alias_b is not None

    await db.record_alias_sender(1, alias_a.id, "x@cognition.ai", "cognition.ai")
    await db.record_alias_sender(1, alias_a.id, "x@github.com", "github.com")
    await db.record_alias_sender(1, alias_b.id, "x@stripe.com", "stripe.com")

    labels = await db.get_alias_service_labels(1)
    assert set(labels[alias_a.id]) == {"Devin", "GitHub"}
    assert labels[alias_b.id] == ["Stripe"]


async def test_get_alias_service_labels_unknown_domain_uses_raw(
    db: Database,
) -> None:
    """When neither chat override nor default matches, the raw domain shows up."""
    await db.upsert_user(1)
    await db.add_aliases(1, ["x@proton.me"], primary_id=None)
    alias = await db.find_alias(1, "x@proton.me")
    assert alias is not None
    await db.record_alias_sender(
        1, alias.id, "support@some-random-saas.example", "some-random-saas.example"
    )
    labels = await db.get_alias_service_labels(1)
    assert labels[alias.id] == ["some-random-saas.example"]


async def test_service_label_empty_string_suppresses_default(
    db: Database,
) -> None:
    """An empty-string chat row is the "🗑 Hapus label" sentinel — even
    a built-in default like ``cognition.ai → Devin`` is suppressed and
    the resolver returns ``None`` (the badge disappears from /list and
    the email header)."""
    await db.upsert_user(1)
    # cognition.ai resolves to "Devin" by default.
    assert await db.resolve_service_label(1, "cognition.ai") == "Devin"
    # User taps 🗑 Hapus label → empty-string sentinel is written.
    await db.set_service_label(1, "cognition.ai", "")
    assert await db.resolve_service_label(1, "cognition.ai") is None
    assert await db.is_service_label_suppressed(1, "cognition.ai") is True
    # Subdomain inherits the parent suppression.
    assert await db.resolve_service_label(1, "alerts.cognition.ai") is None
    # And /list excludes it instead of falling back to "cognition.ai".
    await db.add_aliases(1, ["a@proton.me"], primary_id=None)
    alias = await db.find_alias(1, "a@proton.me")
    assert alias is not None
    await db.record_alias_sender(1, alias.id, "x@cognition.ai", "cognition.ai")
    labels = await db.get_alias_service_labels(1)
    assert labels.get(alias.id, []) == []
    # /services del re-enables the default.
    assert await db.remove_service_label(1, "cognition.ai") is True
    assert await db.resolve_service_label(1, "cognition.ai") == "Devin"


async def test_forwarded_emails_record_and_pop(db: Database) -> None:
    """``record_forwarded_email`` + ``pop_forwarded_email_message_ids``
    let the bot bulk-delete an alias' forwarded emails when the user
    switches alias."""
    await db.upsert_user(1)
    await db.add_aliases(1, ["a@proton.me", "b@proton.me"], primary_id=None)
    alias_a = await db.find_alias(1, "a@proton.me")
    alias_b = await db.find_alias(1, "b@proton.me")
    assert alias_a is not None and alias_b is not None

    await db.record_forwarded_email(1, alias_a.id, 100)
    await db.record_forwarded_email(1, alias_a.id, 101)
    await db.record_forwarded_email(1, alias_b.id, 200)

    popped_a = await db.pop_forwarded_email_message_ids(1, alias_a.id)
    assert sorted(popped_a) == [100, 101]
    # The pop is destructive — second call returns nothing.
    assert await db.pop_forwarded_email_message_ids(1, alias_a.id) == []
    # Other alias' rows are untouched.
    assert await db.pop_forwarded_email_message_ids(1, alias_b.id) == [200]


async def test_transient_status_messages_record_and_pop(db: Database) -> None:
    """``record_transient_status_message`` + ``pop_transient_status_message_ids``
    underpin the wipe-old-confirmations behavior on alias-switch.
    """
    await db.upsert_user(1)
    await db.upsert_user(2)

    await db.record_transient_status_message(1, 10, "lock")
    await db.record_transient_status_message(1, 11, "lock")
    await db.record_transient_status_message(1, 12, "unlock")
    await db.record_transient_status_message(2, 99, "lock")

    # Pop just 'lock' kind for chat 1 — leaves the unlock row alone.
    popped_lock = await db.pop_transient_status_message_ids(1, kinds=("lock",))
    assert sorted(popped_lock) == [10, 11]
    assert await db.pop_transient_status_message_ids(1, kinds=("lock",)) == []

    # The unlock row is still there for chat 1.
    popped_all_chat1 = await db.pop_transient_status_message_ids(1)
    assert popped_all_chat1 == [12]

    # Other chats untouched.
    assert await db.pop_transient_status_message_ids(2) == [99]
    assert await db.pop_transient_status_message_ids(2) == []
