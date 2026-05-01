"""High-level orchestrator for the ``/genaddr`` command.

The browser-driving glue lives in :mod:`proton_browser`; this module ties
together the database, the alias-name generator, and the browser so the bot
handler can stay short.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from .alias_gen import generate_batch
from .crypto import CredentialCipher
from .db import Database
from .models import PrimaryAccount
from .proton_browser import (
    AddressCreationResult,
    CaptchaInterruptError,
    CreationStatus,
    LoginFailedError,
    ProtonBrowser,
    ProtonBrowserError,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BatchSummary:
    """Aggregate result returned to the bot for human-readable reporting."""

    primary: PrimaryAccount
    base: str
    requested: int
    domain: str
    results: list[AddressCreationResult] = field(default_factory=list)
    captcha_interrupted_at: str | None = None
    aborted_reason: str | None = None

    @property
    def created(self) -> list[AddressCreationResult]:
        return [r for r in self.results if r.status is CreationStatus.SUCCESS]

    @property
    def already_existing(self) -> list[AddressCreationResult]:
        return [r for r in self.results if r.status is CreationStatus.ALREADY_EXISTS]

    @property
    def failed(self) -> list[AddressCreationResult]:
        terminal = (CreationStatus.SUCCESS, CreationStatus.ALREADY_EXISTS)
        return [r for r in self.results if r.status not in terminal]


# Type alias for the optional browser factory injected by tests. The factory
# returns an async context manager that yields a browser-shaped object with
# ``create_address``.
BrowserFactory = Callable[[str, str], AbstractAsyncContextManager[Any]]


class AddressGenerationError(RuntimeError):
    """Raised when the orchestrator cannot start the batch at all."""


async def run_batch(
    *,
    db: Database,
    cipher: CredentialCipher,
    chat_id: int,
    primary: PrimaryAccount,
    base: str,
    count: int,
    domain: str,
    display_name_template: str | None = None,
    browser_factory: BrowserFactory | None = None,
    progress: Callable[[int, int, AddressCreationResult], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> BatchSummary:
    """Drive the end-to-end ``/genaddr`` flow for one primary account.

    The split between ``BrowserFactory`` (for tests) and the default
    Playwright session (for production) means the orchestrator stays
    independently testable without spawning Chromium.
    """
    encrypted = await db.get_proton_password_encrypted(primary.id)
    if not encrypted:
        raise AddressGenerationError(
            "no Proton master password is stored for this account; "
            "run /setprotonpw first"
        )
    try:
        password = cipher.decrypt(encrypted)
    except Exception as exc:  # cryptography raises a private exception type
        raise AddressGenerationError(
            f"could not decrypt Proton password (key rotated?): {exc}"
        ) from exc

    state = await db.get_generator_state(chat_id, primary.id, base)
    names, _next_state_unused = generate_batch(
        base=base,
        state=state,
        count=count,
        domain=domain,
    )

    summary = BatchSummary(
        primary=primary,
        base=base,
        requested=count,
        domain=domain.lstrip("@").lower(),
    )

    factory = browser_factory or _default_browser_factory
    try:
        async with factory(primary.email, password) as browser:
            for index, full_email in enumerate(names, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    summary.aborted_reason = "cancelled by user"
                    break
                local = full_email.split("@", 1)[0]
                display = (
                    display_name_template.format(local=local)
                    if display_name_template
                    else None
                )
                try:
                    result = await browser.create_address(
                        local=local,
                        domain=summary.domain,
                        display_name=display,
                        password_for_keygen=password,
                    )
                except CaptchaInterruptError as exc:
                    summary.captcha_interrupted_at = local
                    summary.aborted_reason = (
                        f"captcha interrupted at {local}: {exc}"
                    )
                    break
                except ProtonBrowserError as exc:
                    summary.aborted_reason = f"browser error at {local}: {exc}"
                    break

                summary.results.append(result)
                if result.status is CreationStatus.SUCCESS:
                    await db.add_aliases(
                        chat_id=chat_id,
                        emails=[result.email],
                        primary_id=primary.id,
                    )
                if progress is not None:
                    await progress(index, count, result)
                if result.status is CreationStatus.LIMIT_REACHED:
                    summary.aborted_reason = "Proton reported address limit reached"
                    break
    except LoginFailedError as exc:
        raise AddressGenerationError(f"Proton login failed: {exc}") from exc
    except CaptchaInterruptError as exc:
        # Login itself was challenged before we ever started a name.
        summary.aborted_reason = (
            f"captcha required at login: {exc.page_url}"
        )

    # Persist the cursor advanced by however many names Proton ACCEPTED
    # (success or already-exists). Names that errored aren't consumed, so a
    # re-run picks them up again.
    consumed = sum(
        1
        for r in summary.results
        if r.status in (CreationStatus.SUCCESS, CreationStatus.ALREADY_EXISTS)
    )
    if consumed > 0:
        # Re-derive the cursor by replaying the generator from the original
        # state — this keeps alias_gen the single source of truth even when
        # only a subset of the batch was consumed.
        _, advanced = generate_batch(
            base=base, state=state, count=consumed, domain=domain
        )
        await db.save_generator_state(chat_id, primary.id, base, advanced)

    return summary


def _default_browser_factory(email: str, password: str):
    """Return a real :class:`ProtonBrowser` session context manager."""
    return ProtonBrowser.session(email=email, password=password)
