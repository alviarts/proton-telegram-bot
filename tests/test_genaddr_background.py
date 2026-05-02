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


class _FakeMessage:
    """Minimal stand-in for ``telegram.Message`` (only ``message_id``)."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        # Captured as ``(text, kwargs)`` so newer tests can assert on the
        # ``reply_markup`` attached to the final-summary message without
        # disrupting the existing text-only assertions.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.deleted_message_ids: list[int] = []
        self.markup_edits: list[tuple[int, int, Any]] = []
        self._next_id = 1000

    def _next_message_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(
        self, *, chat_id: int, text: str, **kw: Any
    ) -> _FakeMessage:
        self.messages.append(text)
        self.calls.append((text, kw))
        return _FakeMessage(self._next_message_id())

    async def delete_message(
        self, *, chat_id: int, message_id: int
    ) -> None:
        self.deleted_message_ids.append(message_id)

    async def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> None:
        self.markup_edits.append((chat_id, message_id, reply_markup))


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


async def test_final_summary_attaches_cekimap_button_when_aliases_created() -> None:
    """The post-/genaddr summary must include a one-tap "🩺 Cek hasil
    sekarang" button so the user doesn't have to retype /cekimap.

    Reuses ``CB_QUICK_HEALTHCHECK:<primary_id>`` so the existing
    callback router runs the same health-check task /cekimap launches.
    """
    context = _FakeContext()
    primary = _make_primary()

    async def _fake_run_batch(**kw: Any) -> BatchSummary:
        progress = kw["progress"]
        results = []
        for i in range(1, 4):
            r = _success(f"vielz{i:03d}")
            results.append(r)
            await progress(i, 3, r)
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=3,
            domain="proton.me",
            results=results,
        )

    with patch.object(bot_mod.address_generator, "run_batch", _fake_run_batch):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=3,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    summary_calls = [
        (text, kw)
        for text, kw in context.application.bot.calls
        if "/genaddr selesai" in text
    ]
    assert len(summary_calls) == 1
    _, summary_kw = summary_calls[0]
    markup = summary_kw.get("reply_markup")
    assert markup is not None, "expected an InlineKeyboardMarkup on the summary"
    # Single row, single button, callback wired to the existing
    # CB_QUICK_HEALTHCHECK router with the primary id we just generated for.
    assert len(markup.inline_keyboard) == 1
    row = markup.inline_keyboard[0]
    assert len(row) == 1
    button = row[0]
    assert "Cek hasil sekarang" in button.text
    assert "3 alias" in button.text
    assert button.callback_data == f"{bot_mod.CB_QUICK_HEALTHCHECK}:{primary.id}"


async def test_final_summary_omits_cekimap_button_when_zero_aliases_created() -> None:
    """When all addresses failed (or were duplicates), there is nothing
    to validate yet — don't show a misleading "Cek hasil" button.
    """
    context = _FakeContext()
    primary = _make_primary()

    async def _fake_run_batch(**_kw: Any) -> BatchSummary:
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=2,
            domain="proton.me",
            # Both attempts failed: BatchSummary.created stays empty.
            results=[
                AddressCreationResult(
                    local="vielz001",
                    domain="proton.me",
                    status=CreationStatus.ERROR,
                ),
                AddressCreationResult(
                    local="vielz002",
                    domain="proton.me",
                    status=CreationStatus.ERROR,
                ),
            ],
        )

    with patch.object(bot_mod.address_generator, "run_batch", _fake_run_batch):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=2,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
        )

    summary_calls = [
        (text, kw)
        for text, kw in context.application.bot.calls
        if "/genaddr selesai" in text
    ]
    assert len(summary_calls) == 1
    _, summary_kw = summary_calls[0]
    # ``reply_markup`` is either absent or explicitly None — never an
    # empty keyboard, so the user doesn't see a dangling button.
    assert summary_kw.get("reply_markup") is None


# ----------------------- TaskMessageTracker integration --------------------


async def test_background_genaddr_deletes_transient_messages_on_completion() -> None:
    """Every transient progress message (starter + per-5 progress +
    final summary) must be tracked and deleted after the task ends so
    the chat returns to the canonical /list view.

    Locks the user-approved "auto bersih setelah selesai" UX.
    """
    context = _FakeContext()
    primary = _make_primary()
    starter = _FakeMessage(message_id=999)

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

    with patch.object(bot_mod.address_generator, "run_batch", _fake_run_batch):
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
            starter_message=starter,
            cancel_button_row=None,
        )

    bot = context.application.bot
    # During the run we emit: starter (passed in) + 2 progress (at 5/10
    # successes) + 1 final summary = 4 transient messages. After the
    # task ends the tracker must delete each of them.
    assert len(bot.deleted_message_ids) >= 4, (
        f"expected >=4 deletes (starter + 2 progress + summary), "
        f"got {len(bot.deleted_message_ids)}: {bot.deleted_message_ids}"
    )
    # The starter id we provided manually must be among the deleted.
    assert 999 in bot.deleted_message_ids


async def test_background_genaddr_status_reporter_edits_starter_message() -> None:
    """The live-status row replaces the starter's reply_markup via
    ``editMessageReplyMarkup``. Each progress callback should trigger
    at least one edit (subject to the StatusReporter throttle, which
    we bypass with ``force=True`` on the initial label).
    """
    context = _FakeContext()
    primary = _make_primary()
    starter = _FakeMessage(message_id=777)

    async def _fake_run_batch(**kw: Any) -> BatchSummary:
        progress = kw["progress"]
        results = []
        for i in range(1, 4):
            r = _success(f"vielz{i:03d}")
            results.append(r)
            await progress(i, 3, r)
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=3,
            domain="proton.me",
            results=results,
        )

    with patch.object(bot_mod.address_generator, "run_batch", _fake_run_batch):
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=3,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
            starter_message=starter,
            cancel_button_row=None,
        )

    bot = context.application.bot
    # The very first status update + the final ``status.done`` are both
    # ``force``'d, so we should see at least 2 edit_message_reply_markup
    # calls — one for "🌐 Buka browser proxy…" and one for the final
    # "✅ Selesai · …" label.
    assert len(bot.markup_edits) >= 2, (
        f"expected at least 2 markup edits (initial + done), got "
        f"{len(bot.markup_edits)}"
    )
    # All edits target the starter message id we passed in.
    assert all(mid == 777 for _chat, mid, _markup in bot.markup_edits)


# --------------------------- _maybe_offer_alias_topup -----------------------

@pytest.mark.asyncio
async def test_topup_offer_posts_button_when_under_target() -> None:
    """When the alias count is below the soft target, the helper
    sends a follow-up message with a one-tap genaddr top-up button.
    """
    bot = _FakeBot()
    primary = _make_primary()
    await bot_mod._maybe_offer_alias_topup(
        bot,  # type: ignore[arg-type]
        chat_id=99,
        primary=primary,
        current_count=11,
        target=21,
    )
    assert len(bot.calls) == 1
    text, kw = bot.calls[0]
    # Mentions both the deficit and the recommendation.
    assert "11" in text and "21" in text
    markup = kw.get("reply_markup")
    assert markup is not None
    # Single button row.
    button = markup.inline_keyboard[0][0]
    assert button.text == "✨ Tambah 10 alamat lagi"
    # Routes through the existing CB_QUICK_GENADDR pipeline.
    assert button.callback_data == f"{bot_mod.CB_QUICK_GENADDR}:{primary.id}:10"


@pytest.mark.asyncio
async def test_topup_offer_skips_when_at_or_above_target() -> None:
    """When the alias count already meets the target, the helper is
    a no-op so the chat doesn't get a noisy "everything's fine"
    message after each /cekimap or sync run.
    """
    bot = _FakeBot()
    primary = _make_primary()
    await bot_mod._maybe_offer_alias_topup(
        bot,  # type: ignore[arg-type]
        chat_id=99,
        primary=primary,
        current_count=21,
        target=21,
    )
    assert bot.calls == []
    await bot_mod._maybe_offer_alias_topup(
        bot,  # type: ignore[arg-type]
        chat_id=99,
        primary=primary,
        current_count=25,
        target=21,
    )
    assert bot.calls == []
