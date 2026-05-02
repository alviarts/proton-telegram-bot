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
        # Each entry: (chat_id, message_id, text, kwargs). Lets tests
        # assert on the rolling body edits the /genaddr task drives.
        self.text_edits: list[tuple[int, int, str, dict[str, Any]]] = []
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

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **kw: Any
    ) -> None:
        self.text_edits.append((chat_id, message_id, text, kw))


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


# ---------------------------------------------------------------------------
# /genaddr re-entrancy + dedup (PR-A bug fix)
# ---------------------------------------------------------------------------


class _FakeChat:
    def __init__(self, chat_id: int = 99) -> None:
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


class _RecordingMessage:
    """``update.effective_message`` stub that records every reply text."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kw: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.effective_chat = _FakeChat()
        self.effective_user = _FakeUser()
        self.effective_message = _RecordingMessage()
        self.callback_query = None


class _FakeSettings:
    """Settings with an empty allowlist so ``_gate`` lets everyone through."""

    def __init__(self) -> None:
        self.allowed_user_ids: set[int] = set()


class _ReEntrantContext:
    def __init__(self) -> None:
        self.application = _FakeApp()
        self.application.bot_data["db"] = object()
        self.application.bot_data["cipher"] = object()
        self.application.bot_data["settings"] = _FakeSettings()
        self.chat_data: dict[str, Any] = self.application.chat_data[99]
        self.user_data: dict[str, Any] = {}
        self.args: list[str] = ["vielz", "10"]


@pytest.mark.asyncio
async def test_cmd_genaddr_re_entrancy_warns_only_once() -> None:
    """Repeated calls while ``genaddr_running`` is set must emit the
    warning **exactly once** — not once per click. This is the bug
    the user reported as "Masih ada /genaddr lain yang berjalan"
    spamming the chat 5x in a row.
    """
    update = _FakeUpdate()
    context = _ReEntrantContext()
    # Simulate "task already running": cmd_genaddr should bail at the
    # re-entrancy check before doing any DB lookups, so the user-data
    # silent-pick/random-suffix flags never get touched.
    context.chat_data["genaddr_running"] = True

    for _ in range(5):
        await bot_mod.cmd_genaddr(
            update,  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
        )

    warnings = [
        m for m in update.effective_message.replies if "Masih ada /genaddr" in m
    ]
    assert len(warnings) == 1, (
        f"expected exactly one re-entrancy warning, got {len(warnings)}: "
        f"{update.effective_message.replies}"
    )
    # The earlier "Akun yang dipakai: …" disambiguation message must
    # NOT have been sent — the early bail-out should beat the picker.
    assert not any("Akun yang dipakai" in m for m in update.effective_message.replies)


@pytest.mark.asyncio
async def test_cmd_genaddr_warning_resets_after_task_finishes() -> None:
    """Once the background task clears its chat_data flags, a fresh
    /genaddr issued *while* still running again must warn again
    (otherwise the second user attempt would be silently swallowed).
    """
    update = _FakeUpdate()
    context = _ReEntrantContext()

    context.chat_data["genaddr_running"] = True
    await bot_mod.cmd_genaddr(
        update,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
    )
    assert context.chat_data.get("genaddr_warned_running") is True

    # Background task finishes -> the finally block in _run_genaddr_background
    # pops both flags. Simulate that here.
    context.chat_data.pop("genaddr_running", None)
    context.chat_data.pop("genaddr_warned_running", None)
    # Now flag again as if a new task started, then re-enter.
    context.chat_data["genaddr_running"] = True
    update.effective_message.replies.clear()
    await bot_mod.cmd_genaddr(
        update,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
    )
    warnings = [
        m for m in update.effective_message.replies if "Masih ada /genaddr" in m
    ]
    assert len(warnings) == 1


# --------------------------------------------------------------------------- #
# PR-C #10: live body update on the /genaddr starter message.                 #
# --------------------------------------------------------------------------- #


def test_render_genaddr_body_includes_progress_counts() -> None:
    """Body renderer surfaces success/fail counts and the latest 5 emails."""
    text = bot_mod._render_genaddr_body(
        count=20,
        primary_email="vielz883@proton.me",
        proxy_note=" via proxy rotasi",
        pattern="<code>vielzNNN@proton.me</code>",
        success_count=7,
        fail_count=2,
        recent=[f"vielz{i:03d}" for i in range(1, 8)],
    )
    assert "<b>20</b>" in text  # target count
    assert "vielz883@proton.me" in text
    assert "via proxy rotasi" in text
    assert "<b>7/20</b>" in text  # success/total
    assert "<b>2</b>" in text  # fail count
    # Only the LAST 5 emails should appear in the rolling "Terbaru" list.
    assert "vielz003" in text and "vielz007" in text
    assert "vielz001" not in text  # dropped from the 5-element window
    assert "vielz002" not in text


def test_render_genaddr_body_handles_no_progress_yet() -> None:
    """Initial render (success=0) shows an empty placeholder for Terbaru."""
    text = bot_mod._render_genaddr_body(
        count=10,
        primary_email="vielz@proton.me",
        proxy_note="",
        pattern="<code>vielzNNN@proton.me</code>",
        success_count=0,
        fail_count=0,
        recent=[],
    )
    assert "Terbaru:" in text
    assert "(belum ada)" in text


def test_render_genaddr_body_caps_at_one_blank_line() -> None:
    """Body must never have 3+ consecutive newlines — the user explicitly
    asked for tight spacing on the live status message.
    """
    text = bot_mod._render_genaddr_body(
        count=5,
        primary_email="x@p.me",
        proxy_note="",
        pattern="<code>xN@p.me</code>",
        success_count=2,
        fail_count=0,
        recent=["x01", "x02"],
    )
    assert "\n\n\n" not in text


@pytest.mark.asyncio
async def test_background_genaddr_edits_body_on_milestones() -> None:
    """When a starter_message is provided, the rolling body must be
    edited via ``edit_message_text`` at every NOTIFY_EVERY milestone
    (and on the final tick).
    """
    context = _FakeContext()
    primary = _make_primary()
    starter = _FakeMessage(message_id=42)

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
            pattern="<code>vielzNNN@proton.me</code>",
            proxy_note="",
        )

    edits = context.application.bot.text_edits
    # Two milestone force-edits at success_count=5 and 10. The 2-second
    # throttle suppresses other edits in this fast synthetic run.
    assert len(edits) >= 2, f"expected ≥2 body edits, got {len(edits)}: {edits}"
    # Each edit targets the starter message id.
    for chat_id, message_id, _text, _kw in edits:
        assert chat_id == 99
        assert message_id == 42
    # The final edit reflects the final 10/10 success count.
    final_text = edits[-1][2]
    assert "<b>10/10</b>" in final_text
    # The "Terbaru" list shows the last 5 created aliases.
    assert "vielz010" in final_text
    assert "vielz006" in final_text


@pytest.mark.asyncio
async def test_background_genaddr_body_edit_suppresses_not_modified() -> None:
    """If Telegram raises ``BadRequest("message is not modified")`` the
    background task must keep going — losing a body refresh is fine.
    """
    from telegram.error import BadRequest

    context = _FakeContext()
    primary = _make_primary()
    starter = _FakeMessage(message_id=42)

    async def _raising_edit(**_kw: Any) -> None:
        raise BadRequest("message is not modified")

    context.application.bot.edit_message_text = _raising_edit  # type: ignore[assignment]

    async def _fake_run_batch(**kw: Any) -> BatchSummary:
        progress = kw["progress"]
        results = []
        for i in range(1, 6):
            r = _success(f"vielz{i:03d}")
            results.append(r)
            await progress(i, 5, r)
        return BatchSummary(
            primary=primary,
            base="vielz",
            requested=5,
            domain="proton.me",
            results=results,
        )

    with patch.object(bot_mod.address_generator, "run_batch", _fake_run_batch):
        # Must not raise — the suppress() inside _edit_body absorbs the error.
        await bot_mod._run_genaddr_background(
            context,  # type: ignore[arg-type]
            chat_id=99,
            primary=primary,
            base="vielz",
            count=5,
            domain="proton.me",
            cancel_event=__import__("asyncio").Event(),
            browser_handle={},
            proxy_provider=None,
            starter_message=starter,
            pattern="<code>x</code>",
            proxy_note="",
        )

    # Sanity check: the synthetic batch still emitted the final summary.
    msgs = context.application.bot.messages
    assert any("/genaddr selesai" in m for m in msgs)
