"""Tests for ListenerManager dispatch logic (without actual IMAP)."""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from proton_telegram_bot.crypto import CredentialCipher
from proton_telegram_bot.db import Database
from proton_telegram_bot.email_parser import parse_message
from proton_telegram_bot.manager import ListenerManager, Notifier
from proton_telegram_bot.models import AliasStatus


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, str]]] = []
        self.discovery_calls: list[tuple[int, list[str]]] = []

    async def notify_email_received(self, chat_id, alias_email, summary) -> None:
        self.calls.append((chat_id, alias_email, summary))

    async def notify_aliases_discovered(self, chat_id, aliases) -> None:
        self.discovery_calls.append((chat_id, aliases))


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "x.sqlite3")
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


async def _seed_primary(db: Database, chat_id: int, email: str) -> int:
    """Helper: insert a primary account row for tests."""
    return await db.add_primary_account(
        chat_id=chat_id,
        email=email,
        host="127.0.0.1",
        port=1143,
        username=email,
        encrypted_password="x" * 64,
        use_ssl=False,
    )


def _make_message(to: str = "vielz50@proton.me") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "partner@biz.example"
    msg["To"] = to
    msg["Subject"] = "Halo"
    msg.set_content("Pesan pertama.")
    return parse_message(msg.as_bytes())  # type: ignore[return-value]


async def test_handle_message_forwards_only_active_alias(db: Database) -> None:
    """Lock-mode: only emails to the chat's *active* alias get forwarded.

    The alias stays in /list (no auto-consume) and the lock is preserved so
    follow-up emails to the same address keep being forwarded.
    """
    chat_id = 100
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz43@proton.me")
    await db.add_aliases(
        chat_id, ["vielz50@proton.me", "vielz51@proton.me"], primary_id=primary_id
    )
    active = await db.find_alias(chat_id, "vielz50@proton.me")
    assert active is not None
    await db.set_active_alias(chat_id, active.id)
    notifier = _RecordingNotifier()
    cipher = CredentialCipher(CredentialCipher.generate_key())
    manager = ListenerManager(db=db, cipher=cipher, notifier=notifier)

    await manager._handle_new_message(
        chat_id, primary_id, _make_message("vielz50@proton.me"), "1"
    )
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("vielz50@proton.me"), "2"
    )

    # Both aliases are still listed; nothing was deleted or consumed.
    available = await db.list_aliases(chat_id, status=AliasStatus.AVAILABLE)
    consumed = await db.list_aliases(chat_id, status=AliasStatus.CONSUMED)
    assert sorted(a.email for a in available) == ["vielz50@proton.me", "vielz51@proton.me"]
    assert consumed == []
    # Both messages were forwarded — lock persists across emails.
    assert len(notifier.calls) == 2
    assert {call[1] for call in notifier.calls} == {"vielz50@proton.me"}
    # Active alias is still locked-in.
    current = await db.get_active_alias(chat_id)
    assert current is not None and current.email == "vielz50@proton.me"


async def test_handle_message_ignores_when_no_active_alias(db: Database) -> None:
    """No active alias = nothing is forwarded; new recipients are still tracked."""
    chat_id = 7
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz43@proton.me")
    await db.add_aliases(chat_id, ["only@proton.me"], primary_id=primary_id)
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("someone-else@proton.me"), "1"
    )
    # No notification because no alias is locked-in for this chat.
    assert notifier.calls == []
    # New recipient was still auto-added so /list shows it later.
    all_aliases = await db.list_aliases(chat_id)
    assert sorted(a.email for a in all_aliases) == ["only@proton.me", "someone-else@proton.me"]
    # Nothing got consumed.
    assert await db.list_aliases(chat_id, status=AliasStatus.CONSUMED) == []


async def test_handle_message_ignores_other_aliases_when_locked(db: Database) -> None:
    """Email to a non-active alias must NOT be forwarded, even if the alias exists."""
    chat_id = 200
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz43@proton.me")
    await db.add_aliases(
        chat_id, ["a@proton.me", "b@proton.me"], primary_id=primary_id
    )
    active = await db.find_alias(chat_id, "a@proton.me")
    assert active is not None
    await db.set_active_alias(chat_id, active.id)
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )

    await manager._handle_new_message(
        chat_id, primary_id, _make_message("b@proton.me"), "10"
    )

    assert notifier.calls == []
    # Active alias still locked, neither alias consumed.
    assert (await db.get_active_alias(chat_id)).email == "a@proton.me"
    assert await db.list_aliases(chat_id, status=AliasStatus.CONSUMED) == []


async def test_discovered_aliases_adds_and_notifies(db: Database) -> None:
    """Inbox scan discovers new addresses and notifies only the new ones."""
    chat_id = 42
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz43@proton.me")
    await db.add_aliases(
        chat_id, ["existing@proton.me"], primary_id=primary_id
    )
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_discovered_aliases(
        chat_id,
        primary_id,
        {"existing@proton.me", "new1@proton.me", "new2@proton.me"},
    )
    all_aliases = await db.list_aliases(chat_id)
    assert sorted(a.email for a in all_aliases) == [
        "existing@proton.me",
        "new1@proton.me",
        "new2@proton.me",
    ]
    assert len(notifier.discovery_calls) == 1
    assert notifier.discovery_calls[0][0] == chat_id
    # Only the NEW aliases should be in the notification, not the pre-existing one.
    assert sorted(notifier.discovery_calls[0][1]) == ["new1@proton.me", "new2@proton.me"]


async def test_lock_on_other_primary_does_not_drop_email_for_this_primary(
    db: Database,
) -> None:
    """Regression: locking an alias on primary B must not silently drop
    incoming mail for primary A. This was the multi-primary bug that broke
    vielz43's inbox after the user clicked vielz003 (under vielz64) in
    /list — the cross-primary lock check returned early for *every*
    listener, including the one not holding the lock.
    """
    chat_id = 300
    await db.upsert_user(chat_id)
    primary_a = await _seed_primary(db, chat_id, "vielz43@proton.me")
    primary_b = await _seed_primary(db, chat_id, "vielz64@proton.me")
    await db.add_aliases(
        chat_id, ["vielz54@proton.me", "vielz55@proton.me"], primary_id=primary_a
    )
    await db.add_aliases(
        chat_id, ["vielz003@proton.me"], primary_id=primary_b
    )
    locked = await db.find_alias(chat_id, "vielz003@proton.me")
    assert locked is not None and locked.primary_id == primary_b
    await db.set_active_alias(chat_id, locked.id)

    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )

    # Email arrives via primary_a's listener for one of A's known aliases.
    # Even though primary_b has a lock, A's mail must still flow.
    await manager._handle_new_message(
        chat_id, primary_a, _make_message("vielz54@proton.me"), "1"
    )
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1] == "vielz54@proton.me"

    # Email arrives via primary_b's listener targeting the locked alias.
    await manager._handle_new_message(
        chat_id, primary_b, _make_message("vielz003@proton.me"), "2"
    )
    assert len(notifier.calls) == 2
    assert notifier.calls[1][1] == "vielz003@proton.me"

    # Email arrives via primary_b's listener for a *different* primary_b
    # alias (not the locked one). With B's lock pinned to vielz003, that
    # email must be dropped.
    await db.add_aliases(chat_id, ["vielz004@proton.me"], primary_id=primary_b)
    await manager._handle_new_message(
        chat_id, primary_b, _make_message("vielz004@proton.me"), "3"
    )
    assert len(notifier.calls) == 2  # unchanged


async def test_no_lock_forwards_known_alias_of_same_primary(db: Database) -> None:
    """No active lock + email targets a previously-known alias of the
    listener's primary → forwarded. This is the "primary baru otomatis
    aktif" flow: a freshly-connected primary with no alias pinned should
    forward email to any of its aliases out of the box.
    """
    chat_id = 400
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz64@proton.me")
    await db.add_aliases(
        chat_id, ["vielz003@proton.me", "vielz004@proton.me"], primary_id=primary_id
    )
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("vielz004@proton.me"), "1"
    )
    assert [c[1] for c in notifier.calls] == ["vielz004@proton.me"]


async def test_no_lock_does_not_forward_brand_new_alias_on_first_email(
    db: Database,
) -> None:
    """Brand-new addresses get auto-added to /list but NOT forwarded the
    same instant they're discovered, so a third party can't trigger a
    Telegram notification by emailing a never-seen-before address.
    """
    chat_id = 500
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz64@proton.me")
    # No aliases pre-seeded.
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("brand-new@proton.me"), "1"
    )
    # No notification on first sighting.
    assert notifier.calls == []
    # But the alias is now in /list, so a *second* email to it forwards.
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("brand-new@proton.me"), "2"
    )
    assert [c[1] for c in notifier.calls] == ["brand-new@proton.me"]


async def test_handle_message_does_not_rematch_consumed_alias(db: Database) -> None:
    chat_id = 9
    await db.upsert_user(chat_id)
    primary_id = await _seed_primary(db, chat_id, "vielz43@proton.me")
    await db.add_aliases(chat_id, ["x@proton.me"], primary_id=primary_id)
    alias = await db.find_alias(chat_id, "x@proton.me")
    assert alias is not None
    await db.mark_consumed(alias.id, "<m1>")
    notifier = _RecordingNotifier()
    manager = ListenerManager(
        db=db,
        cipher=CredentialCipher(CredentialCipher.generate_key()),
        notifier=notifier,
    )
    await manager._handle_new_message(
        chat_id, primary_id, _make_message("x@proton.me"), "2"
    )
    # No active alias is locked, so nothing is forwarded.
    assert notifier.calls == []
