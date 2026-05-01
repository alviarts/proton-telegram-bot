"""Proton account web UI automation.

Proton's address-creation flow is *not* a single REST call — the browser also
generates a fresh PGP key pair, encrypts it with the user's password (and the
organization key for Business accounts), and uploads a SignedKeyList. Trying
to replicate that crypto chain in Python would mean shadowing
``@proton/crypto`` and ``@proton/key-transparency``: weeks of work and a
constant moving target.

Instead we drive the official web UI with Playwright, so Proton's own JS
handles all the cryptographic state. This module owns the lifecycle of a
headless Chromium instance plus a small typed wrapper for the few flows we
care about: login, "Add address" submission, and detection of CAPTCHA / error
states.

Selectors are defined centrally (:data:`SELECTORS`) and prefer accessible
roles and text content over CSS class names so they survive minor Proton UI
changes.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )


logger = logging.getLogger(__name__)


# Public Proton account URLs we navigate to. Kept here so deployments behind
# a reverse proxy / staging environment can patch them in one place.
LOGIN_URL = "https://account.proton.me/login"
ADDRESSES_URL_TEMPLATE = "https://account.proton.me/u/{user_index}/mail/identity-addresses"

# Default user_index in the Proton URL. The web UI renumbers logged-in
# accounts in the order they were added; a fresh login lands at /u/0.
DEFAULT_USER_INDEX = 0

# Per-action timeouts (ms). Sites under load occasionally take >10s; we keep
# the address-creation modal generous because it triggers PGP key generation
# in the browser which is CPU-bound.
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_ACTION_TIMEOUT_MS = 60_000


# -------------------------------------------------------------------- selectors

# We hold every locator string in one dict so when Proton ships a UI change
# the only file we touch is this one. Each entry is a Playwright-compatible
# selector (CSS, role-based, or text=...).
SELECTORS = {
    # Login page
    "login_email": "input[name='username'], input#username",
    "login_password": "input[name='password'], input#password",
    "login_submit": "button[type='submit']",
    # Once logged in the dashboard chrome shows a top-bar or sidebar that we
    # use as a "logged in" sentinel.
    "logged_in_sentinel": "[data-testid='heading:userdropdown'], [data-testid='topnav-link:settings']",
    # Address listing page
    "add_address_button": "button:has-text('Add address'), button:has-text('Tambah alamat')",
    # Add address modal
    "address_modal": "[role='dialog']",
    "address_local_input": "input[name='address'], input[id='address']",
    "address_display_name_input": "input[name='name'], input[id='name']",
    "address_password_input": "input[name='password'], input[id='password']",
    "address_password_confirm_input": "input[name='confirmPassword'], input[id='confirmPassword']",
    "address_submit_button": "button[type='submit']",
    # Notifications / errors. Proton renders toast notifications with role=alert
    # in a top-right corner.
    "success_notification": "[role='alert']:has-text('Address added')",
    "error_notification": "[role='alert'][aria-live='assertive'], [role='alert']:has-text('error')",
    # CAPTCHA iframe — same convention as the existing /sync flow.
    "captcha_iframe": "iframe[src*='captcha'], iframe[title*='challenge']",
}


class CreationStatus(StrEnum):
    SUCCESS = "success"
    ALREADY_EXISTS = "already_exists"
    CAPTCHA_REQUIRED = "captcha_required"
    AUTH_FAILED = "auth_failed"
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"


@dataclass(slots=True)
class AddressCreationResult:
    """Outcome of attempting to create one address."""

    local: str
    domain: str
    status: CreationStatus
    detail: str | None = None

    @property
    def success(self) -> bool:
        return self.status is CreationStatus.SUCCESS

    @property
    def email(self) -> str:
        return f"{self.local}@{self.domain}"


class ProtonBrowserError(RuntimeError):
    """Base class for unrecoverable browser-side failures."""


class CaptchaInterruptError(ProtonBrowserError):
    """Raised when a CAPTCHA challenge interrupts the automation flow.

    The bot maps this to the same UX as the /sync command — it pauses, sends
    the captcha-helper URL, and resumes once the user pastes the token.
    """

    def __init__(self, page_url: str, message: str = "CAPTCHA challenge required") -> None:
        super().__init__(message)
        self.page_url = page_url


class LoginFailedError(ProtonBrowserError):
    """Raised when login submits but Proton rejects credentials."""


class ProtonBrowser:
    """Minimal Playwright wrapper around the Proton account web UI.

    Lifecycle::

        async with ProtonBrowser.session(email, password) as browser:
            for name in names:
                result = await browser.create_address(local=name, domain="proton.me")
                ...
    """

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        user_index: int,
        email: str,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._user_index = user_index
        self._email = email

    @property
    def page(self) -> Page:
        """Expose the underlying Page for fine-grained debugging only."""
        return self._page

    # ------------------------------------------------------------------ session

    @classmethod
    @asynccontextmanager
    async def session(
        cls,
        email: str,
        password: str,
        *,
        headless: bool = True,
        user_index: int = DEFAULT_USER_INDEX,
        nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    ) -> AsyncIterator[ProtonBrowser]:
        """Open a logged-in browser session and tear it down cleanly afterwards.

        ``user_index`` matches the ``/u/N`` segment in Proton's URL — for a
        fresh login this is always ``0``. We expose the option in case a
        deployment reuses an existing storage_state with several accounts
        mounted.
        """
        # Lazy import: Playwright is an optional runtime dependency. Tests that
        # don't exercise the browser path (e.g. alias_gen tests) shouldn't
        # require it to be installed.
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        context.set_default_timeout(action_timeout_ms)
        context.set_default_navigation_timeout(nav_timeout_ms)
        page = await context.new_page()

        instance = cls(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            user_index=user_index,
            email=email,
        )
        try:
            await instance.login(password=password)
            yield instance
        finally:
            await instance.close()

    async def close(self) -> None:
        """Best-effort cleanup. Swallows individual close errors."""
        for closer in (
            self._context.close,
            self._browser.close,
            self._playwright.stop,
        ):
            try:
                await closer()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error during ProtonBrowser shutdown")

    # ------------------------------------------------------------------ login

    async def login(self, password: str) -> None:
        """Log into account.proton.me with the configured email + password.

        Raises :class:`LoginFailedError` on bad credentials, or
        :class:`CaptchaInterruptError` when Proton's anti-bot challenges the
        login form. Successful login resolves once the account dashboard is
        rendered.
        """
        page = self._page
        await page.goto(LOGIN_URL)

        await page.fill(SELECTORS["login_email"], self._email)
        await page.fill(SELECTORS["login_password"], password)
        await page.click(SELECTORS["login_submit"])

        # We race three outcomes: dashboard sentinel = success, error toast =
        # bad credentials, captcha iframe = anti-bot challenge.
        await self._wait_for_login_outcome()

    async def _wait_for_login_outcome(self) -> None:
        page = self._page
        sentinel = page.locator(SELECTORS["logged_in_sentinel"]).first
        captcha = page.locator(SELECTORS["captcha_iframe"]).first
        error_toast = page.locator(SELECTORS["error_notification"]).first

        # Use Playwright's builtin race via wait_for + return_when=FIRST_COMPLETED.
        tasks = [
            asyncio.create_task(sentinel.wait_for(state="visible")),
            asyncio.create_task(captcha.wait_for(state="visible")),
            asyncio.create_task(error_toast.wait_for(state="visible")),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        # Whichever locator resolved first decides the outcome.
        if await sentinel.is_visible():
            return
        if await captcha.is_visible():
            raise CaptchaInterruptError(page_url=page.url)
        if await error_toast.is_visible():
            text = (await error_toast.text_content()) or "login rejected"
            raise LoginFailedError(text.strip())
        raise ProtonBrowserError("login flow did not produce a recognised outcome")

    # ------------------------------------------------------------------ create address

    async def create_address(
        self,
        *,
        local: str,
        domain: str,
        display_name: str | None = None,
        password_for_keygen: str | None = None,
    ) -> AddressCreationResult:
        """Submit the "Add address" modal once.

        ``password_for_keygen`` is the same Proton master password used for
        login — Proton's modal asks for it again to derive the new address
        key. Pass ``None`` to skip filling the field (e.g. when the modal
        does not show it for organization-managed members).
        """
        page = self._page
        await page.goto(_addresses_url(self._user_index))

        try:
            await page.click(SELECTORS["add_address_button"])
        except Exception as exc:
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=CreationStatus.ERROR,
                detail=f"could not open Add address modal: {exc}",
            )

        modal = page.locator(SELECTORS["address_modal"]).first
        await modal.wait_for(state="visible")

        await modal.locator(SELECTORS["address_local_input"]).fill(local)
        if display_name is not None:
            await modal.locator(SELECTORS["address_display_name_input"]).fill(display_name)
        if password_for_keygen is not None:
            await modal.locator(SELECTORS["address_password_input"]).fill(password_for_keygen)
            await modal.locator(SELECTORS["address_password_confirm_input"]).fill(
                password_for_keygen
            )

        await modal.locator(SELECTORS["address_submit_button"]).click()

        return await self._wait_for_address_outcome(local=local, domain=domain)

    async def _wait_for_address_outcome(
        self, *, local: str, domain: str
    ) -> AddressCreationResult:
        page = self._page
        success = page.locator(SELECTORS["success_notification"]).first
        captcha = page.locator(SELECTORS["captcha_iframe"]).first
        error_toast = page.locator(SELECTORS["error_notification"]).first

        tasks = [
            asyncio.create_task(success.wait_for(state="visible")),
            asyncio.create_task(captcha.wait_for(state="visible")),
            asyncio.create_task(error_toast.wait_for(state="visible")),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        if await success.is_visible():
            return AddressCreationResult(
                local=local, domain=domain, status=CreationStatus.SUCCESS
            )
        if await captcha.is_visible():
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=CreationStatus.CAPTCHA_REQUIRED,
                detail="proton served a captcha challenge",
            )
        if await error_toast.is_visible():
            text = ((await error_toast.text_content()) or "").lower().strip()
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=_classify_error(text),
                detail=text or None,
            )
        return AddressCreationResult(
            local=local,
            domain=domain,
            status=CreationStatus.ERROR,
            detail="no toast appeared after submit",
        )


# -------------------------------------------------------------------- helpers


def _addresses_url(user_index: int) -> str:
    return ADDRESSES_URL_TEMPLATE.format(user_index=user_index)


def _classify_error(text: str) -> CreationStatus:
    """Map a Proton toast message to one of the canonical error statuses."""
    if not text:
        return CreationStatus.ERROR
    if "already" in text or "exists" in text or "taken" in text or "sudah ada" in text:
        return CreationStatus.ALREADY_EXISTS
    if "limit" in text or "quota" in text or "maximum" in text:
        return CreationStatus.LIMIT_REACHED
    if "password" in text or "credentials" in text or "auth" in text:
        return CreationStatus.AUTH_FAILED
    return CreationStatus.ERROR
