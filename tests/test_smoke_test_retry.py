"""Unit tests for the PR-G smoke-test retry helper.

``_smoke_test_with_retry`` runs the SMTP→tempmail probe up to
``SMTP_SMOKE_MAX_ATTEMPTS`` times before giving up so that a slow
Bridge warm-up doesn't roll back the freshly-added primary on the
first 60s timeout. We can't drive a real SMTP server in CI, so we
swap the inner ``_smoke_test_via_tempmail`` with a hand-rolled stub
and exercise just the loop behaviour.
"""
from __future__ import annotations

from typing import Any

import pytest

from proton_telegram_bot import bot


class _FakeTempmail:
    """Minimal stand-in for ``TempMailbox`` (only `address` is read)."""

    address = "fake@example.com"


async def _success_smoke(**_: Any) -> bool:
    return True


def _make_smoke_sequence(results: list[bool]):
    """Return a ``_smoke_test_via_tempmail`` stub that yields each
    item from ``results`` on successive calls."""
    iterator = iter(results)

    async def _fake(**_: Any) -> bool:
        return next(iterator)

    return _fake


async def _noop_sleep(_seconds: float) -> None:
    """Drop-in for ``asyncio.sleep`` so tests don't actually wait."""
    return None


async def test_smoke_test_with_retry_returns_true_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt succeeds → no retry, no on_retry callback fires."""
    monkeypatch.setattr(bot, "_smoke_test_via_tempmail", _success_smoke)
    retries: list[int] = []

    async def _on_retry(attempt: int) -> None:
        retries.append(attempt)

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=_on_retry,
        sleep=_noop_sleep,
    )
    assert ok is True
    assert retries == []


async def test_smoke_test_with_retry_succeeds_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt fails, second succeeds → on_retry fired exactly once
    with attempt index 1, sleep called exactly once."""
    monkeypatch.setattr(
        bot,
        "_smoke_test_via_tempmail",
        _make_smoke_sequence([False, True]),
    )
    retries: list[int] = []
    sleeps: list[float] = []

    async def _on_retry(attempt: int) -> None:
        retries.append(attempt)

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=_on_retry,
        sleep=_sleep,
    )
    assert ok is True
    assert retries == [1]
    assert sleeps == [bot.SMTP_SMOKE_RETRY_DELAY_SECONDS]


async def test_smoke_test_with_retry_returns_false_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both attempts fail → returns False, on_retry fired once (between
    the two attempts), no sleep AFTER the final attempt."""
    monkeypatch.setattr(
        bot,
        "_smoke_test_via_tempmail",
        _make_smoke_sequence([False, False]),
    )
    retries: list[int] = []
    sleeps: list[float] = []

    async def _on_retry(attempt: int) -> None:
        retries.append(attempt)

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=_on_retry,
        sleep=_sleep,
    )
    assert ok is False
    # on_retry fired between attempt 1 and 2, NOT after the final one.
    assert retries == [1]
    assert sleeps == [bot.SMTP_SMOKE_RETRY_DELAY_SECONDS]


async def test_smoke_test_with_retry_swallows_on_retry_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the on_retry callback itself raises (e.g. transient Telegram
    API error), the helper must keep going with its retry attempt
    rather than aborting early."""
    monkeypatch.setattr(
        bot,
        "_smoke_test_via_tempmail",
        _make_smoke_sequence([False, True]),
    )

    async def _boom(_attempt: int) -> None:
        raise RuntimeError("telegram is down")

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=_boom,
        sleep=_noop_sleep,
    )
    assert ok is True


async def test_smoke_test_with_retry_no_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_retry is optional — passing None must not raise."""
    monkeypatch.setattr(
        bot,
        "_smoke_test_via_tempmail",
        _make_smoke_sequence([False, True]),
    )

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=None,
        sleep=_noop_sleep,
    )
    assert ok is True


async def test_smoke_test_with_retry_respects_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_attempts=3`` → up to 3 attempts and 2 retries between them."""
    calls: list[int] = []

    async def _fake(**_: Any) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(bot, "_smoke_test_via_tempmail", _fake)
    retries: list[int] = []

    async def _on_retry(attempt: int) -> None:
        retries.append(attempt)

    ok = await bot._smoke_test_with_retry(
        email="x@proton.me",
        imap_username="x@proton.me",
        imap_password="pw",
        tempmail=_FakeTempmail(),  # type: ignore[arg-type]
        on_retry=_on_retry,
        sleep=_noop_sleep,
        max_attempts=3,
    )
    assert ok is False
    assert len(calls) == 3
    assert retries == [1, 2]


def test_smoke_test_retry_constants_are_sensible() -> None:
    """Smoke check: shipped defaults are at least 2 attempts and a
    non-trivial retry delay so the production wiring matches the
    behaviour these tests cover."""
    assert bot.SMTP_SMOKE_MAX_ATTEMPTS >= 2
    assert bot.SMTP_SMOKE_RETRY_DELAY_SECONDS >= 5
    assert bot.SMTP_SMOKE_TIMEOUT_SECONDS >= 30
