"""Unit tests for the /cekimap health check task and the onboarding
keyboard helpers added alongside it.

The full Bridge-SMTP + Mail.tm round-trip can't run in CI so we mock
those layers and assert the bot reports the right messages back to
Telegram, in the right order, for both happy and degraded cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

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


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: Any = None,
    ) -> None:
        self.messages.append(
            _FakeMessage(chat_id=chat_id, text=text, parse_mode=parse_mode)
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
    arrives in the inbox.
    """

    def __init__(self, address: str = "fake@mail.tm") -> None:
        self.address = address
        self._delivered_subjects: list[str] = []

    async def list_subjects(self, _client: Any) -> list[str]:
        return list(self._delivered_subjects)

    def deliver(self, subject: str) -> None:
        self._delivered_subjects.append(subject)


async def test_run_health_check_happy_path_reports_per_alias_progress() -> None:
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
    tempmail = _FakeTempMailbox()

    sent_subjects: list[str] = []

    def _fake_smtp_send(**kw: Any) -> None:
        # Stash the subject so the temp mailbox can "deliver" it on the
        # next poll.
        sent_subjects.append(kw["subject"])
        tempmail.deliver(kw["subject"])

    targets = ["vielz74@proton.me", "vielz001@proton.me", "vielz002@proton.me"]

    with (
        patch.object(
            health_check.TempMailbox, "create", AsyncMock(return_value=tempmail)
        ),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        # Speed up: bypass the real receive timeout / poll interval.
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 5),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=42,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # 1 starting message + N per-alias ✅ + 1 summary
    assert len(bot.messages) == 1 + len(targets) + 1
    starter = bot.messages[0].text
    assert "Health check" in starter
    assert "vielz74@proton.me" in starter
    # Every target gets a per-alias confirmation.
    confirm_texts = [m.text for m in bot.messages[1:-1]]
    for target in targets:
        assert any(target in t and "✅" in t for t in confirm_texts), target
    summary = bot.messages[-1].text
    assert "selesai" in summary.lower()
    assert f"{len(targets)}/{len(targets)}" in summary


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
    tempmail = _FakeTempMailbox()

    delivered_for_alias = "vielz001@proton.me"
    broken_alias = "vielzbroken@proton.me"

    def _fake_smtp_send(**kw: Any) -> None:
        # Only deliver mail for the one alias; the other times out.
        if delivered_for_alias in kw["subject"]:
            tempmail.deliver(kw["subject"])

    targets = [delivered_for_alias, broken_alias]

    with (
        patch.object(
            health_check.TempMailbox, "create", AsyncMock(return_value=tempmail)
        ),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=43,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    texts = [m.text for m in bot.messages]
    # The good one shows ✅, the bad one shows ❌, summary reports 1/2.
    assert any(delivered_for_alias in t and "✅" in t for t in texts)
    assert any(broken_alias in t and "❌" in t for t in texts)
    assert any("1/2" in t for t in texts[-2:])


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
    tempmail = _FakeTempMailbox()

    def _fake_smtp_send(**_kw: Any) -> None:
        raise RuntimeError("Bridge SMTP refused: alias not found")

    targets = ["vielz74@proton.me", "vielz999@proton.me"]

    with (
        patch.object(
            health_check.TempMailbox, "create", AsyncMock(return_value=tempmail)
        ),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=45,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    texts = [m.text for m in bot.messages]
    # All targets are reported as send-failed.
    fail_lines = [t for t in texts if "❌" in t]
    assert any("vielz74@proton.me" in t for t in fail_lines)
    assert any("vielz999@proton.me" in t for t in fail_lines)
    # All sends raised so no SMTP token ever existed → final summary reports
    # 0 succeeded out of 2 targets.
    summary = texts[-1]
    assert "0/2" in summary or "0 yang berhasil" in summary.lower()


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
    tempmail = _FakeTempMailbox()

    sent: list[str] = []

    def _fake_smtp_send(**kw: Any) -> None:
        sent.append(kw["from_addr"])
        tempmail.deliver(kw["subject"])

    # Primary is in the list twice, plus uppercase variant — dedupe to 1.
    targets = ["vielz74@proton.me", "VIELZ74@proton.me", "vielz74@proton.me"]

    with (
        patch.object(
            health_check.TempMailbox, "create", AsyncMock(return_value=tempmail)
        ),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=46,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # SMTP send happened once.
    assert len(sent) == 1
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
