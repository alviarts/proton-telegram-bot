"""Unit tests for the new background-mode /genaddr task.

The full Playwright + Proton round-trip can't run in CI; these tests
mock ``address_generator.run_batch`` so that we can drive the
progress callback synthetically and assert the per-N-success
notification logic + the final summary message.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from proton_telegram_bot import bot as bot_mod
from proton_telegram_bot.address_generator import BatchSummary
from proton_telegram_bot.models import PrimaryAccount
from proton_telegram_bot.proton_browser import (
    AddressCreationResult,
    CreationStatus,
)


def _make_primary(email: str = "vielz883@proton.me") -> PrimaryAccount:
    return PrimaryAccount(
        id=42,
        chat_id=99,
        email=email,
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username=email,
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, *, chat_id: int, text: str, **_kw: Any) -> None:
        self.messages.append(text)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.bot_data: dict[str, Any] = {}
        self.chat_data: dict[int, dict[str, Any]] = {99: {}}


class _FakeContext:
    def __init__(self) -> None:
        self.application = _FakeApp()
        # bot_data references the application's bot_data so DB / cipher
        # lookups go through ``_bot_db`` / ``_bot_cipher`` helpers.
        self.application.bot_data["db"] = object()
        self.application.bot_data["cipher"] = object()


def _success(local: str, domain: str = "proton.me") -> AddressCreationResult:
    return AddressCreationResult(
        local=local,
        domain=domain,
        status=CreationStatus.SUCCESS,
    )


@pytest.mark.parametrize("count", [5, 10, 20])
async def test_background_genaddr_emits_progress_every_5_successes(
    count: int,
) -> None:
    """Verifies the new GENADDR_NOTIFY_EVERY=5 contract.

    Drives a fake ``run_batch`` that synthesises ``count`` successful
    creations, then asserts the chat received exactly one progress
    message per multiple of 5 successes (plus one final summary).
    """
    context = _FakeContext()
    primary = _make_primary()

    async def _fake_run_batch(**kw: Any) -> BatchSummary:
        progress = kw["progress"]
        results = []
        for i in range(1, count + 1):
            r = _success(f"vielz{i:03d}")
            results.append(r)
            await progress(i, count, r)
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=count,
            domain="proton.me",
            results=results,
        )

    with patch.object(
        bot_mod.address_generator, "run_batch", _fake_run_batch
    ):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=count,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    msgs = context.application.bot.messages
    progress_msgs = [m for m in msgs if "🔄" in m]
    summary_msgs = [m for m in msgs if "/genaddr selesai" in m]

    # One progress message per multiple of 5 (5, 10, 15, …) up to ``count``.
    expected_progress = count // bot_mod.GENADDR_NOTIFY_EVERY
    assert len(progress_msgs) == expected_progress, (
        f"expected {expected_progress} progress messages for count={count}, "
        f"got {len(progress_msgs)}: {progress_msgs}"
    )
    # Exactly one final summary.
    assert len(summary_msgs) == 1
    assert f"<b>{count}</b>" in summary_msgs[0]


async def test_background_genaddr_progress_includes_recent_emails() -> None:
    """The progress message lists the last batch of newly-created emails
    so the user can sanity-check they look right.
    """
    context = _FakeContext()
    primary = _make_primary()

    async def _fake_run_batch(**kw: Any) -> BatchSummary:
        progress = kw["progress"]
        results = []
        for i in range(1, 11):
            r = _success(f"vielz{i:03d}")
            results.append(r)
            await progress(i, 10, r)
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=10,
            domain="proton.me",
            results=results,
        )

    with patch.object(
        bot_mod.address_generator, "run_batch", _fake_run_batch
    ):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=10,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    msgs = context.application.bot.messages
    progress = [m for m in msgs if "🔄" in m]
    # First progress msg is at success_count=5 → should mention vielz001..005.
    first = progress[0]
    for i in range(1, 6):
        assert f"vielz{i:03d}" in first, f"missing vielz{i:03d} in {first}"
    # Second progress msg is at success_count=10 → should mention 006..010.
    second = progress[1]
    for i in range(6, 11):
        assert f"vielz{i:03d}" in second, f"missing vielz{i:03d} in {second}"


async def test_background_genaddr_clears_chat_data_on_completion() -> None:
    """Once the background task finishes, the per-chat ``genaddr_*`` keys
    must be cleared so the next /genaddr can start.
    """
    context = _FakeContext()
    chat_data = context.application.chat_data[99]
    chat_data["genaddr_running"] = True
    chat_data["genaddr_cancel_event"] = __import__("asyncio").Event()
    chat_data["genaddr_browser_handle"] = {}

    primary = _make_primary()

    async def _fake_run_batch(**_kw: Any) -> BatchSummary:
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=1,
            domain="proton.me",
            results=[_success("vielz001")],
        )

    with patch.object(
        bot_mod.address_generator, "run_batch", _fake_run_batch
    ):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=1,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    # All four genaddr_* keys gone.
    assert "genaddr_running" not in chat_data
    assert "genaddr_cancel_event" not in chat_data
    assert "genaddr_browser_handle" not in chat_data


async def test_background_genaddr_reports_run_batch_setup_error() -> None:
    """When ``run_batch`` raises ``AddressGenerationError`` (e.g. no
    Proton master password stored), the user gets a friendly error
    message via send_message — not a stuck "running" state.
    """
    context = _FakeContext()
    chat_data = context.application.chat_data[99]
    chat_data["genaddr_running"] = True

    primary = _make_primary()

    async def _fake_run_batch(**_kw: Any) -> BatchSummary:
        raise bot_mod.address_generator.AddressGenerationError(
            "no Proton master password is stored for this account"
        )

    with patch.object(
        bot_mod.address_generator, "run_batch", _fake_run_batch
    ):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=10,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    msgs = context.application.bot.messages
    # At least one ❌ error message, no "/genaddr selesai" final summary.
    assert any("❌" in m for m in msgs)
    assert not any("/genaddr selesai" in m for m in msgs)
    # chat_data must be cleared even on error so the next /genaddr works.
    assert "genaddr_running" not in chat_data


def test_genaddr_notify_every_default_is_five() -> None:
    """The user explicitly asked for 'laporan text per 5 alias'. Lock
    the default in so a future refactor can't silently change it.
    """
    assert bot_mod.GENADDR_NOTIFY_EVERY == 5
