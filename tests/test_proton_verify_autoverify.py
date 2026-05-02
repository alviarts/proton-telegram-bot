"""Unit tests for ``auto_verify_recovery_link``.

The function is part of PR-E: after Proton emails a recovery-email
verification link to our temp mailbox, we open it in a fresh
Playwright tab and try to confirm Proton accepted it without making
the user click the link by hand.

These tests use a hand-rolled Playwright stub (``_FakePage`` /
``_FakeLocator``) — running real Playwright in CI would be too slow
and would mask logic bugs behind browser flakiness.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from proton_telegram_bot.proton_verify import auto_verify_recovery_link


class _FakeLocator:
    """Minimal Playwright Locator stand-in used by the tests below.

    ``visible_predicate`` is consulted on each ``is_visible()`` /
    ``wait_for(state="visible")`` call; ``click_recorder`` (when set)
    receives the locator's selector string after a successful click so
    tests can assert on which selectors were actually exercised.
    """

    def __init__(
        self,
        selector: str,
        *,
        visible_predicate: Callable[[str], bool],
        click_recorder: list[str] | None = None,
        wait_should_raise: bool = False,
    ) -> None:
        self.selector = selector
        self._visible_predicate = visible_predicate
        self._click_recorder = click_recorder
        self._wait_should_raise = wait_should_raise

    @property
    def first(self) -> _FakeLocator:
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        del state, timeout
        if self._wait_should_raise:
            raise TimeoutError("not visible")
        if not self._visible_predicate(self.selector):
            raise TimeoutError("not visible")

    async def is_visible(self) -> bool:
        return self._visible_predicate(self.selector)

    async def click(self) -> None:
        if self._click_recorder is not None:
            self._click_recorder.append(self.selector)


class _FakePage:
    """Playwright Page stand-in.

    ``goto_should_raise`` simulates a navigation failure (DNS, captcha
    redirect, …). ``visible_selectors`` is the set of selector strings
    that should report visible; everything else raises on
    ``wait_for(visible)`` and returns ``False`` from ``is_visible()``.
    """

    def __init__(
        self,
        *,
        visible_selectors: set[str] | None = None,
        goto_should_raise: bool = False,
    ) -> None:
        self.visible_selectors = visible_selectors or set()
        self._goto_should_raise = goto_should_raise
        self.goto_calls: list[str] = []
        self.click_recorder: list[str] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        self.goto_calls.append(url)
        if self._goto_should_raise:
            raise RuntimeError("navigation blocked")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(
            selector,
            visible_predicate=lambda s: s in self.visible_selectors,
            click_recorder=self.click_recorder,
        )


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def test_auto_verify_returns_true_when_success_indicator_visible() -> None:
    """If any of the recognised success texts is visible after navigation,
    ``auto_verify_recovery_link`` reports success."""
    page = _FakePage(visible_selectors={"text=/Email\\s+verified/i"})
    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=200,
        )
    )
    assert ok is True
    assert page.goto_calls == ["https://account.proton.me/verify?token=xxx"]


def test_auto_verify_clicks_button_then_succeeds() -> None:
    """Some link variants require a confirmation button click first.
    Verify we click whichever selector matches, *then* we look for a
    success indicator.
    """
    visible: set[str] = {"button:has-text('Verifikasi')"}
    page = _FakePage(visible_selectors=visible)

    # Mutate the visible set partway through to simulate the button
    # disappearing after click and the success state showing up.
    real_locator = page.locator

    def patched_locator(selector: str) -> _FakeLocator:
        loc = real_locator(selector)
        original_click = loc.click

        async def click_then_show_success() -> None:
            await original_click()
            visible.discard("button:has-text('Verifikasi')")
            visible.add("text=/Email\\s+verified/i")

        loc.click = click_then_show_success  # type: ignore[method-assign]
        return loc

    page.locator = patched_locator  # type: ignore[method-assign]

    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=500,
        )
    )
    assert ok is True
    assert "button:has-text('Verifikasi')" in page.click_recorder


def test_auto_verify_returns_false_when_navigation_fails() -> None:
    """Navigation errors must not propagate — caller falls back to manual."""
    page = _FakePage(goto_should_raise=True)
    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=100,
        )
    )
    assert ok is False


def test_auto_verify_returns_false_when_no_indicator_appears() -> None:
    """Page renders, but nothing we recognise → ``False`` (caller will
    DM the link for manual verification)."""
    page = _FakePage(visible_selectors=set())
    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=100,
        )
    )
    assert ok is False


def test_auto_verify_indonesian_success_indicator() -> None:
    """The Indonesian Proton UI says "Email berhasil diverifikasi"."""
    page = _FakePage(
        visible_selectors={"text=/Email.*berhasil.*diverifikasi/i"}
    )
    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=200,
        )
    )
    assert ok is True


def test_auto_verify_total_wait_bounded_by_success_timeout() -> None:
    """The poll loop must bail out within ``success_timeout_ms`` even
    when no indicator ever appears, so a failed verify doesn't block
    the bot for an unbounded amount of time."""
    page = _FakePage(visible_selectors=set())
    loop = asyncio.new_event_loop()
    try:
        start = loop.time()
        ok = loop.run_until_complete(
            auto_verify_recovery_link(
                page,  # type: ignore[arg-type]
                "https://account.proton.me/verify?token=xxx",
                success_timeout_ms=200,
                button_timeout_ms=10,
            )
        )
        elapsed_ms = (loop.time() - start) * 1000
    finally:
        loop.close()
    assert ok is False
    # Generous upper bound (2s) so the test isn't flaky on slow CI;
    # the contract is just "this returns within ~success_timeout_ms,
    # not minutes".
    assert elapsed_ms < 2_000


@pytest.mark.parametrize(
    "indicator",
    [
        "text=/Email\\s+verified/i",
        "text=/Recovery email.*verified/i",
        "text=/Email.*berhasil.*diverifikasi/i",
        "text=/Email.*sudah.*diverifikasi/i",
        "text=/Email pemulihan.*diverifikasi/i",
        "text=/Verifikasi.*berhasil/i",
    ],
)
def test_auto_verify_accepts_each_success_indicator(indicator: str) -> None:
    """Every selector in ``_VERIFY_SUCCESS_SELECTORS`` triggers success."""
    page = _FakePage(visible_selectors={indicator})
    ok = _run(
        auto_verify_recovery_link(
            page,  # type: ignore[arg-type]
            "https://account.proton.me/verify?token=xxx",
            success_timeout_ms=200,
        )
    )
    assert ok is True
