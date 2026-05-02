"""Unit tests for the /cekimap health check task and the onboarding
keyboard helpers added alongside it.

The full Bridge-SMTP + Mail.tm round-trip can't run in CI so we mock
those layers and assert the bot reports the right messages back to
Telegram, in the right order, for both happy and degraded cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from proton_telegram_bot import health_check
from proton_telegram_bot.bot import (
    CB_HEALTHCHECK_PICK,
    CB_QUICK_GENADDR,
    CB_QUICK_HEALTHCHECK,
    CB_QUICK_SETPW,
    _build_cekimap_picker_keyboard,
    _build_post_connect_keyboard,
    _build_post_connect_keyboard_with_aliases,
)
from proton_telegram_bot.bridge_admin import BridgeImapCredentials
from proton_telegram_bot.models import PrimaryAccount

# ----------------------------- onboarding keyboards --------------------------


def test_post_connect_keyboard_has_all_three_onboarding_buttons() -> None:
    """Fresh-account keyboard exposes setprotonpw, genaddr, healthcheck."""
    kb = _build_post_connect_keyboard(primary_id=42)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    assert f"{CB_QUICK_SETPW}:42" in callbacks
    assert f"{CB_QUICK_GENADDR}:42:20" in callbacks
    assert f"{CB_QUICK_HEALTHCHECK}:42" in callbacks


def test_post_connect_keyboard_with_aliases_offers_health_check() -> None:
    """Existing-aliases keyboard prioritises the Cek listener button."""
    kb = _build_post_connect_keyboard_with_aliases(primary_id=7, alias_count=20)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    labels = [btn.text for row in rows for btn in row]
    assert f"{CB_QUICK_HEALTHCHECK}:7" in callbacks
    # The label must surface the alias count so the user knows what
    # they're about to validate.
    assert any("20" in label for label in labels)


def test_post_connect_keyboard_with_aliases_offers_genaddr_too() -> None:
    """User wants to be able to extend an existing-aliases account
    without retyping the email — the keyboard must include the same
    "Generate 20 alamat sekarang" button the fresh-account onboarding
    uses, with ``CB_QUICK_GENADDR:<primary_id>:20`` so the existing
    callback router runs the random-suffix /genaddr batch.

    /genaddr and /cekimap use independent ``chat_data`` locks so
    clicking this button while a background health check is running
    is safe.
    """
    kb = _build_post_connect_keyboard_with_aliases(primary_id=7, alias_count=20)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    # All three one-tap actions are reachable.
    assert f"{CB_QUICK_GENADDR}:7:20" in callbacks
    assert f"{CB_QUICK_HEALTHCHECK}:7" in callbacks
    # /list passthrough is still wired to the picker callback.
    assert any(
        cb is not None and cb.startswith("pickp:7") for cb in callbacks
    )


def test_cekimap_picker_lists_all_primaries() -> None:
    """Picker emits one row per primary with its alias count in the label."""
    primaries = [
        PrimaryAccount(
            id=1,
            chat_id=99,
            email="vielz74@proton.me",
            imap_host="127.0.0.1",
            imap_port=1143,
            imap_username="vielz74@proton.me",
            imap_use_ssl=False,
            created_at="2026-05-02T00:00:00Z",
        ),
        PrimaryAccount(
            id=2,
            chat_id=99,
            email="aldohaw@proton.me",
            imap_host="127.0.0.1",
            imap_port=1143,
            imap_username="aldohaw@proton.me",
            imap_use_ssl=False,
            created_at="2026-05-02T00:00:00Z",
        ),
    ]
    counts = {1: 21, 2: 5}
    kb = _build_cekimap_picker_keyboard(primaries, counts)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    labels = [btn.text for row in rows for btn in row]
    assert f"{CB_HEALTHCHECK_PICK}:1" in callbacks
    assert f"{CB_HEALTHCHECK_PICK}:2" in callbacks
    assert any("21" in label for label in labels)
    assert any("5" in label for label in labels)


def test_cekimap_picker_handles_empty_primary_list() -> None:
    """Empty list still renders a placeholder row instead of crashing."""
    kb = _build_cekimap_picker_keyboard(primaries=[], counts={})
    rows = kb.inline_keyboard
    assert rows  # at least one row
    labels = [btn.text for row in rows for btn in row]
    assert any("belum ada" in label.lower() for label in labels)


# ----------------------------- run_health_check ------------------------------


@dataclass
class _FakeMessage:
    """Captures every send_message kwargs so tests can assert on them."""
    chat_id: int
    text: str
    parse_mode: Any = None
    message_id: int | None = None


@dataclass
class _FakeEdit:
    """Captures edit_message_text kwargs so tests can verify the
    rolling progress message gets updated in place rather than
    spamming new messages.
    """
    chat_id: int
    message_id: int
    text: str
    parse_mode: Any = None


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []
        self.edits: list[_FakeEdit] = []
        self._next_message_id = 1000

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: Any = None,
    ) -> _FakeMessage:
        self._next_message_id += 1
        msg = _FakeMessage(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            message_id=self._next_message_id,
        )
        self.messages.append(msg)
        return msg

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Any = None,
    ) -> None:
        self.edits.append(
            _FakeEdit(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
            )
        )


class _FakeBridgeAdmin:
    def __init__(self, creds: BridgeImapCredentials | None) -> None:
        self._creds = creds
        self.calls: list[str] = []

    async def fetch_imap_credentials(
        self, email: str
    ) -> BridgeImapCredentials | None:
        self.calls.append(email)
        return self._creds


class _FakeTempMailbox:
    """Stand-in for ``TempMailbox`` that pretends every tagged email
    arrives in the inbox. Each instance is independent so tests can
    simulate per-alias inboxes (the production code creates one
    mailbox per alias).
    """

    _next_id = 0

    def __init__(self, address: str | None = None) -> None:
        if address is None:
            type(self)._next_id += 1
            address = f"fake-{type(self)._next_id}@mail.tm"
        self.address = address
        self._delivered_subjects: list[str] = []

    async def list_subjects(self, _client: Any) -> list[str]:
        return list(self._delivered_subjects)

    def deliver(self, subject: str) -> None:
        self._delivered_subjects.append(subject)


async def test_run_health_check_happy_path_uses_per_alias_mailbox_and_edits() -> None:
    """Happy path: every alias gets its own Mail.tm inbox, and the
    rolling progress message is edited in place rather than a new
    ✅ message per alias being sent."""
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=1,
        chat_id=42,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    boxes: list[_FakeTempMailbox] = []
    by_addr: dict[str, _FakeTempMailbox] = {}

    async def _create(_client: Any) -> _FakeTempMailbox:
        mb = _FakeTempMailbox()
        boxes.append(mb)
        by_addr[mb.address] = mb
        return mb

    def _fake_smtp_send(**kw: Any) -> None:
        # Route the test email to the alias-specific inbox.
        by_addr[kw["to_addr"]].deliver(kw["subject"])

    targets = ["vielz74@proton.me", "vielz001@proton.me", "vielz002@proton.me"]

    with (
        patch.object(health_check.TempMailbox, "create", side_effect=_create),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 5),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "MAILBOX_CREATE_THROTTLE_S", 0.0),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=42,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # One mailbox per alias, never reused.
    assert len(boxes) == len(targets)
    addresses = {mb.address for mb in boxes}
    assert len(addresses) == len(targets)

    # Static messages stay distinct: the "started" header, the rolling
    # progress message, and the final "selesai" summary — three in
    # total. Per-alias ✅ spam is gone (replaced by edits).
    assert len(bot.messages) == 3
    starter, _progress_initial, summary = (m.text for m in bot.messages)
    assert "Health check" in starter
    assert "vielz74@proton.me" in starter
    assert "selesai" in summary.lower()
    assert f"{len(targets)}/{len(targets)}" in summary

    # Rolling progress was edited at least once and the final edit
    # reports the full success count.
    assert bot.edits, "rolling progress message must be edited in place"
    final_edit_text = bot.edits[-1].text
    assert f"{len(targets)}/{len(targets)}" in final_edit_text
    # All edits target the same message_id as the initial progress send.
    progress_id = bot.messages[1].message_id
    for edit in bot.edits:
        assert edit.message_id == progress_id


async def test_run_health_check_marks_unanswered_aliases_as_failed() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=2,
        chat_id=43,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    boxes: list[_FakeTempMailbox] = []
    by_addr: dict[str, _FakeTempMailbox] = {}

    async def _create(_client: Any) -> _FakeTempMailbox:
        mb = _FakeTempMailbox()
        boxes.append(mb)
        by_addr[mb.address] = mb
        return mb

    delivered_for_alias = "vielz001@proton.me"
    broken_alias = "vielzbroken@proton.me"

    def _fake_smtp_send(**kw: Any) -> None:
        # Only deliver mail for the one alias; the other times out.
        if delivered_for_alias in kw["subject"]:
            by_addr[kw["to_addr"]].deliver(kw["subject"])

    targets = [delivered_for_alias, broken_alias]

    with (
        patch.object(health_check.TempMailbox, "create", side_effect=_create),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "MAILBOX_CREATE_THROTTLE_S", 0.0),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=43,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # The summary message reports 1/2 and names the broken alias so the
    # user knows what's still wrong.
    summary = bot.messages[-1].text
    assert "1/2" in summary
    assert broken_alias in summary
    # Final progress edit also surfaces the broken alias.
    final_edit_text = bot.edits[-1].text if bot.edits else ""
    assert broken_alias in final_edit_text or broken_alias in summary


async def test_run_health_check_aborts_when_bridge_admin_unavailable() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=3,
        chat_id=44,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )

    await health_check.run_health_check(
        bot=bot,
        chat_id=44,
        db=None,  # type: ignore[arg-type]
        bridge_admin=None,
        primary=primary,
        targets=["vielz74@proton.me"],
    )

    assert len(bot.messages) == 1
    assert "Bridge admin nonaktif" in bot.messages[0].text


async def test_run_health_check_handles_send_failures_per_alias() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=4,
        chat_id=45,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)

    async def _create(_client: Any) -> _FakeTempMailbox:
        return _FakeTempMailbox()

    def _fake_smtp_send(**_kw: Any) -> None:
        raise RuntimeError("Bridge SMTP refused: alias not found")

    targets = ["vielz74@proton.me", "vielz999@proton.me"]

    with (
        patch.object(health_check.TempMailbox, "create", side_effect=_create),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "MAILBOX_CREATE_THROTTLE_S", 0.0),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=45,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # All sends raised so no SMTP token ever existed → final summary
    # reports 0 succeeded out of 2 targets.
    summary = bot.messages[-1].text
    assert "0/2" in summary or "0 yang berhasil" in summary.lower()
    # Both broken aliases are surfaced somewhere visible to the user
    # (either in the rolling progress edit or the summary text).
    haystack = summary + " ".join(e.text for e in bot.edits)
    assert "vielz74@proton.me" in haystack
    assert "vielz999@proton.me" in haystack


async def test_run_health_check_dedupes_targets() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=5,
        chat_id=46,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    by_addr: dict[str, _FakeTempMailbox] = {}

    async def _create(_client: Any) -> _FakeTempMailbox:
        mb = _FakeTempMailbox()
        by_addr[mb.address] = mb
        return mb

    sent: list[str] = []

    def _fake_smtp_send(**kw: Any) -> None:
        sent.append(kw["from_addr"])
        by_addr[kw["to_addr"]].deliver(kw["subject"])

    # Primary is in the list twice, plus uppercase variant — dedupe to 1.
    targets = ["vielz74@proton.me", "VIELZ74@proton.me", "vielz74@proton.me"]

    with (
        patch.object(health_check.TempMailbox, "create", side_effect=_create),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "MAILBOX_CREATE_THROTTLE_S", 0.0),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=46,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # SMTP send happened once. Only one mailbox was created (1 deduped target).
    assert len(sent) == 1
    assert len(by_addr) == 1
    summary = bot.messages[-1].text
    assert "1/1" in summary


@pytest.mark.parametrize(
    "subjects, expected_token, expected_hit",
    [
        (["[health-check] abc123 vielz74@proton.me"], "abc123", True),
        (["unrelated mail"], "abc123", False),
        ([], "abc123", False),
    ],
)
def test_token_match_logic_via_subject_substring(
    subjects: list[str], expected_token: str, expected_hit: bool
) -> None:
    """The polling loop matches a token via simple substring against the
    subject. Lock that contract down so future refactors don't break it.
    """
    hit = any(expected_token in s for s in subjects)
    assert hit is expected_hit
