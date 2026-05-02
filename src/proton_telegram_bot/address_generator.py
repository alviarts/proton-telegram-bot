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

from .alias_gen import generate_batch, iter_names
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
    _is_browser_closed_error,
)
from .proxy_provider import ProxyProvider

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
    browser_handle: dict[str, Any] | None = None,
    proxy_provider: ProxyProvider | None = None,
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
    domain_clean = domain.lstrip("@").lower()

    summary = BatchSummary(
        primary=primary,
        base=base,
        requested=count,
        domain=domain_clean,
    )

    # New semantics (per user feedback): ``count`` is the target number of
    # SUCCESSFUL addresses, not the number of attempts. We generate names
    # lazily from the iterator, advancing the cursor on every attempt
    # (success OR failure -- once Proton has seen a name we should not
    # try it again), and stop once ``successes == count`` or the safety
    # cap kicks in. The cap exists so a Proton-side issue (e.g. every
    # request returning ``ALREADY_EXISTS``) doesn't loop forever -- if we
    # need 22 successes and have already attempted ``MAX_ATTEMPTS_FACTOR
    # * 22 = 66`` names without getting there, something is structurally
    # broken and bailing out is more useful than spinning.
    max_attempts_factor = 3
    max_attempts = max(count * max_attempts_factor, count + 10)

    if browser_factory is not None:
        factory = browser_factory
    else:
        # Build a closure that captures ``proxy_provider`` so the default
        # factory routes Chromium through a rotating IP without widening
        # the public ``BrowserFactory`` signature with extra kwargs.
        def factory(email: str, password: str):
            return _default_browser_factory(
                email, password, proxy_provider=proxy_provider
            )
    # Stop early if the modal returns this many non-success results in a
    # row without any success in between. This catches "account is full"
    # cases where Proton's error toast doesn't classify as LIMIT_REACHED
    # (e.g. it's localised: "Anda sudah mencapai jumlah maksimum alamat")
    # and we'd otherwise burn the entire ``max_attempts`` cap producing
    # the exact same error N times. Tuned to a small number because there
    # is no realistic flow where 4 consecutive attempts genuinely fail
    # for transient reasons but the next one succeeds.
    consecutive_failure_cap = 4

    try:
        async with factory(primary.email, password) as browser:
            # Expose the live browser to the caller so the bot's Cancel button
            # can ``force_close()`` it and abort an in-flight create_address
            # without waiting for the Playwright timeout to fire.
            if browser_handle is not None:
                browser_handle["browser"] = browser
            # The user may have clicked Cancel during the login phase,
            # before we ever entered this ``async with``. Set the event
            # here so ``async with`` exits cleanly via the context
            # manager's ``__aexit__`` instead of forcing the caller to
            # wait for the first attempt to start.
            if cancel_event is not None and cancel_event.is_set():
                summary.aborted_reason = "cancelled by user"
                return summary
            successes = 0
            attempts = 0
            consecutive_failures = 0
            name_iter = iter_names(base, state)
            while successes < count and attempts < max_attempts:
                if cancel_event is not None and cancel_event.is_set():
                    summary.aborted_reason = "cancelled by user"
                    break
                # Pull the next candidate name; the iterator's post-state
                # is reconstructed below from ``len(summary.results)`` so
                # we don't track it here.
                local, _ = next(name_iter)
                attempts += 1
                display = (
                    display_name_template.format(local=local)
                    if display_name_template
                    else None
                )
                try:
                    result = await browser.create_address(
                        local=local,
                        domain=domain_clean,
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
                except asyncio.CancelledError:
                    # Cooperative cancellation path: the bot cancelled our
                    # task. Record the outcome and let the context manager
                    # close the browser, then re-raise so asyncio's
                    # cancellation contract is honoured upstream.
                    summary.aborted_reason = f"cancelled by user at {local}"
                    raise
                except Exception as exc:
                    # Force-close path: the bot closed the browser via
                    # ``ProtonBrowser.force_close()`` while we were awaiting
                    # a Playwright call. Detect this and surface it as a
                    # clean cancellation instead of a generic crash.
                    if (
                        cancel_event is not None
                        and cancel_event.is_set()
                        and _is_browser_closed_error(exc)
                    ):
                        summary.aborted_reason = f"cancelled by user at {local}"
                        break
                    raise

                summary.results.append(result)
                if result.status is CreationStatus.SUCCESS:
                    successes += 1
                    consecutive_failures = 0
                    await db.add_aliases(
                        chat_id=chat_id,
                        emails=[result.email],
                        primary_id=primary.id,
                    )
                else:
                    consecutive_failures += 1
                if progress is not None:
                    # Progress callback signature stays (current, target, result)
                    # but ``current`` is now successes-so-far so the user sees
                    # "5/22 sukses" advance only on real success. Failures still
                    # show in the result detail so the UI can include them in a
                    # secondary counter.
                    await progress(successes, count, result)
                if result.status is CreationStatus.LIMIT_REACHED:
                    summary.aborted_reason = "Proton reported address limit reached"
                    break
                if (
                    result.status is CreationStatus.AUTH_FAILED
                ):
                    # Auth failure mid-batch is unrecoverable: re-trying the
                    # same password against the same locked account just
                    # burns more retries.
                    summary.aborted_reason = (
                        "Proton rejected credentials mid-batch"
                    )
                    break
                if consecutive_failures >= consecutive_failure_cap:
                    # Many consecutive failures with no success in between
                    # is a strong signal that something structural is wrong
                    # (account full, password rejected at modal, Proton
                    # localised the limit-reached toast, etc.). Bail out
                    # so the user sees a clear summary instead of the bot
                    # silently grinding through ``max_attempts`` retries.
                    summary.aborted_reason = (
                        f"berhenti otomatis: {consecutive_failures} percobaan "
                        "gagal berturut-turut tanpa sukses (kemungkinan akun "
                        "sudah penuh / limit alamat tercapai)"
                    )
                    break

            if (
                successes < count
                and attempts >= max_attempts
                and summary.aborted_reason is None
            ):
                summary.aborted_reason = (
                    f"safety cap reached after {attempts} attempts "
                    f"(only {successes}/{count} succeeded)"
                )
    except LoginFailedError as exc:
        raise AddressGenerationError(f"Proton login failed: {exc}") from exc
    except CaptchaInterruptError as exc:
        # Login itself was challenged before we ever started a name.
        summary.aborted_reason = (
            f"captcha required at login: {exc.page_url}"
        )
    finally:
        # The browser is gone once we leave the ``async with`` block above.
        # Drop the handle so a stale Cancel click can't act on a closed
        # browser.
        if browser_handle is not None:
            browser_handle.pop("browser", None)

    # Persist the cursor advanced by however many names we actually
    # ATTEMPTED. With the new "loop until N successes" semantics every
    # attempt -- success, already-exists, or transient error -- has been
    # seen by Proton, so re-trying the same name on the next /genaddr
    # would only burn more retries. The exception is when we couldn't
    # even start (e.g. login failure before the first attempt) -- in
    # that case ``summary.results`` is empty and the cursor stays put.
    if summary.results:
        # Re-derive the cursor by replaying the generator one step per
        # attempt. This keeps alias_gen the single source of truth and
        # avoids leaking the live ``cursor`` variable from inside the
        # ``async with`` block (where exceptions may have unwound it).
        _, advanced = generate_batch(
            base=base,
            state=state,
            count=len(summary.results),
            domain=domain,
        )
        await db.save_generator_state(chat_id, primary.id, base, advanced)

    return summary


def _default_browser_factory(
    email: str,
    password: str,
    *,
    proxy_provider: ProxyProvider | None = None,
):
    """Return a real :class:`ProtonBrowser` session context manager."""
    return ProtonBrowser.session(
        email=email, password=password, proxy_provider=proxy_provider
    )
