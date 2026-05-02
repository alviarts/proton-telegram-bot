"""Shared pytest fixtures for the test suite.

The single fixture here patches the
:data:`proton_telegram_bot.task_message_tracker.DEFAULT_CLEANUP_DELAY_S`
module constant to ``0.0`` for the entire run so the bot's
auto-cleanup ``asyncio.sleep(3)`` doesn't add 3 seconds to every
``/genaddr`` / ``/cekimap`` integration test.

Production callers do not pass an explicit ``delay_s`` to
:class:`TaskMessageTracker`, so changing the module constant is the
single switch that affects every site.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _instant_task_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run task-message cleanup with no delay so tests don't sleep."""
    monkeypatch.setattr(
        "proton_telegram_bot.task_message_tracker.DEFAULT_CLEANUP_DELAY_S",
        0.0,
    )
