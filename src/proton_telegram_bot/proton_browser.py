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
import contextlib
import logging
import os
import re
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )

    from .proxy_provider import ProxyEntry, ProxyProvider


logger = logging.getLogger(__name__)


# Public Proton account URLs we navigate to. Kept here so deployments behind
# a reverse proxy / staging environment can patch them in one place.
LOGIN_URL = "https://account.proton.me/login"
# For Business / org-admin accounts the address management page lives at
# ``users-addresses``; personal accounts use ``identity-addresses``. We
# default to the Business path because that's where the "Tambah alamat"
# / "Add address" button lives for accounts that can manage org members.
ADDRESSES_URL_TEMPLATE = "https://account.proton.me/u/{user_index}/mail/users-addresses"

# Regex that matches the post-login URL Proton redirects to. After login
# Proton sends users to one of:
#   * ``/u/<index>/...`` — direct dashboard land for personal accounts
#   * ``/apps`` — app picker for Business / multi-product accounts
#   * ``/dashboard`` — billing/plan dashboard for new sessions
#   * ``/setup-internal-address`` — Business first-login onboarding
# Failures keep us on ``/login`` (sometimes with a ``?error=...`` query).
# This regex matches any of the success paths above.
LOGGED_IN_URL_RE = re.compile(
    r"https://account\.proton\.me/(?:u/\d+/|apps(?:[/?#]|$)|dashboard(?:[/?#]|$)|setup-internal-address)"
)

# Default user_index in the Proton URL. The web UI renumbers logged-in
# accounts in the order they were added; a fresh login lands at /u/0.
DEFAULT_USER_INDEX = 0

# Per-action timeouts (ms). Sites under load occasionally take >10s; we keep
# the address-creation modal generous because it triggers PGP key generation
# in the browser which is CPU-bound.
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_ACTION_TIMEOUT_MS = 60_000
# How many proxy candidates to try before giving up and going direct.
# Each attempt does a launch + warm-up goto, so this caps the worst case
# at ``DEFAULT_PROXY_RETRIES * (launch + goto_timeout)``.
DEFAULT_PROXY_RETRIES = 3
# Network errors that almost always mean the proxy itself is broken
# (vs. an actual problem with the destination). When we see one of these
# during the warm-up navigation we mark the proxy as failed and rotate.
PROXY_FAILURE_MARKERS = (
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_EMPTY_RESPONSE",
    "ERR_CERT_AUTHORITY_INVALID",  # MITM proxy or expired cert
    "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "ERR_NO_SUPPORTED_PROXIES",
)
LOGIN_OUTCOME_TIMEOUT_MS = 90_000

# Where to dump screenshots + HTML when a flow fails in an unexpected way.
# Operators can mount this to a host volume (or set the env var) for
# postmortem analysis of UI changes Proton ships.
DEBUG_DUMP_DIR = Path(os.environ.get("PROTON_BROWSER_DUMP_DIR", "/tmp/proton-browser-debug"))


# -------------------------------------------------------------------- selectors

# We hold every locator string in one dict so when Proton ships a UI change
# the only file we touch is this one. Each entry is a Playwright-compatible
# selector (CSS, role-based, or text=...).
SELECTORS = {
    # Login page
    "login_email": "input[name='username'], input#username",
    "login_password": "input[name='password'], input#password",
    "login_submit": "button[type='submit']",
    # Once logged in the dashboard chrome shows a top-bar or sidebar with one
    # of these elements. We keep them as a fallback for the URL check below;
    # Proton has shipped enough renames over the years that the URL is the
    # single most reliable signal.
    "logged_in_sentinel": (
        "[data-testid='heading:userdropdown'], "
        "[data-testid='topnav-link:settings'], "
        "[data-testid='user-dropdown'], "
        "[data-testid='heading:dashboard'], "
        "button[aria-label='User menu'], "
        "a[href*='/dashboard']"
    ),
    # Address listing page — the page header has both "Tambah alamat" (Add
    # address) and "Tambahkan pengguna" (Add user). We need to be specific
    # to avoid matching "Tambahkan pengguna" by accident.
    "add_address_button": (
        "button:has-text('Tambah alamat'):not(:has-text('pengguna')), "
        "button:has-text('Add address'):not(:has-text('user'))"
    ),
    # Add address modal. Proton renders modals as overlay divs and the
    # ``role='dialog'`` attribute is on the inner card. Easiest reliable
    # marker is the modal title text "Tambahkan alamat" / "Add address".
    "address_modal_title": (
        "text=/^\\s*(Tambahkan alamat|Add address)\\s*$/i"
    ),
    "address_modal": (
        "[role='dialog'], dialog, "
        "div.modal-two, [class*='modal']:has-text('Tambahkan alamat')"
    ),
    # Address input — sits to the left of the @proton.me dropdown. We
    # try common attribute names first, then fall back to "first text
    # input inside the modal" which is what the layout guarantees.
    "address_local_input": (
        "input[name='address'], input#address, "
        "input[placeholder*='vielz'], "
        "[role='dialog'] input[type='text']:not([readonly])"
    ),
    "address_display_name_input": (
        "input[name='name'], input#name, "
        "input[placeholder*='nama tampilan' i], "
        "input[placeholder*='display name' i]"
    ),
    # Some flows ask for the master password as a re-auth step (sensitive
    # operation modal). Optional — we skip filling when the field isn't
    # rendered.
    "address_password_input": "input[name='password'], input#password, input[type='password']",
    "address_password_confirm_input": (
        "input[name='confirmPassword'], input#confirmPassword"
    ),
    # Submit button is labelled "Simpan alamat" (Save address) inside the
    # modal. ``button[type='submit']`` is too broad and will sometimes
    # match the wrong form.
    "address_submit_button": (
        "button:has-text('Simpan alamat'), "
        "button:has-text('Save address'), "
        "[role='dialog'] button[type='submit']"
    ),
    # Notifications / errors. Proton renders toast notifications with
    # role=alert in a top-right corner. Proton's success copy on this page
    # is something like "Alamat ditambahkan" — we match either language.
    "success_notification": (
        "[role='alert']:has-text('Address added'), "
        "[role='alert']:has-text('Alamat ditambahkan'), "
        "[role='alert']:has-text('berhasil')"
    ),
    "error_notification": (
        "[role='alert'][aria-live='assertive'], "
        "[role='alert']:has-text('error'), "
        "[role='alert']:has-text('gagal')"
    ),
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
        self._closed = False

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
        proxy_provider: ProxyProvider | None = None,
    ) -> AsyncIterator[ProtonBrowser]:
        """Open a logged-in browser session and tear it down cleanly afterwards.

        ``user_index`` matches the ``/u/N`` segment in Proton's URL — for a
        fresh login this is always ``0``. We expose the option in case a
        deployment reuses an existing storage_state with several accounts
        mounted.

        ``proxy_provider``, if given, is queried for one proxy URL before
        Chromium launches so all Proton-bound traffic is routed through a
        rotating IP. Falls back to a direct connection if the provider
        returns ``None`` (no working proxy found).
        """
        # Lazy import: Playwright is an optional runtime dependency. Tests that
        # don't exercise the browser path (e.g. alias_gen tests) shouldn't
        # require it to be installed.
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        # Open Chromium with proxy rotation: try a handful of probed proxies
        # in order, marking each one as failed in the provider so the next
        # call doesn't pick the same dead proxy. Falls back to a direct
        # connection if every proxy fails.
        browser, context, page, used_proxy = await _launch_with_proxy_retry(
            playwright=playwright,
            headless=headless,
            nav_timeout_ms=nav_timeout_ms,
            action_timeout_ms=action_timeout_ms,
            proxy_provider=proxy_provider,
        )

        instance = cls(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            user_index=user_index,
            email=email,
        )
        try:
            await instance.login(password=password, skip_initial_goto=used_proxy is not None)
            try:
                yield instance
            except Exception:
                # Capture the page state on any error from the caller
                # (e.g. address-modal timeout) so operators can diagnose
                # without a second run. Skip when the browser was already
                # force-closed (Cancel button) — the page is gone and
                # screenshotting it would just log noise.
                if not instance._closed:
                    await instance._dump_debug("session-error")
                raise
        finally:
            await instance.close()

    async def _dump_debug(self, label: str) -> None:
        """Drop a screenshot + HTML snapshot to :data:`DEBUG_DUMP_DIR`."""
        await _dump_page_state(self._page, label)

    async def close(self) -> None:
        """Best-effort cleanup. Swallows individual close errors."""
        if self._closed:
            return
        self._closed = True
        for closer in (
            self._context.close,
            self._browser.close,
            self._playwright.stop,
        ):
            try:
                await closer()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error during ProtonBrowser shutdown")

    async def force_close(self, *, step_timeout: float = 3.0) -> None:
        """Aggressively shut the browser down so an in-flight ``create_address``
        await raises immediately.

        Used by ``/genaddr``'s Cancel button: closing the context makes any
        pending Playwright call (``page.click``, ``wait_for_url``, …) raise
        ``TargetClosedError`` instead of running until its 60s timeout. If a
        step itself stalls, we SIGKILL the underlying Chromium process so the
        bot doesn't deadlock waiting on a wedged renderer.
        """
        if self._closed:
            return
        self._closed = True

        chromium_pid: int | None = None
        try:
            proc = getattr(self._browser, "process", None)
            if proc is not None:
                chromium_pid = getattr(proc, "pid", None)
        except Exception:  # pragma: no cover - defensive
            chromium_pid = None

        for label, closer in (
            ("context", self._context.close),
            ("browser", self._browser.close),
            ("playwright", self._playwright.stop),
        ):
            try:
                await asyncio.wait_for(closer(), timeout=step_timeout)
            except TimeoutError:
                logger.warning(
                    "force_close: %s did not finish within %.1fs; will fall back to SIGKILL",
                    label,
                    step_timeout,
                )
                break
            except Exception:
                logger.exception("force_close: error while closing %s", label)

        if chromium_pid:
            _kill_pid(chromium_pid)

    # ------------------------------------------------------------------ login

    async def login(self, password: str, *, skip_initial_goto: bool = False) -> None:
        """Log into account.proton.me with the configured email + password.

        Raises :class:`LoginFailedError` on bad credentials, or
        :class:`CaptchaInterruptError` when Proton's anti-bot challenges the
        login form. Successful login resolves once the account dashboard is
        rendered.

        ``skip_initial_goto=True`` lets callers (specifically
        :meth:`session`) re-use a page that already navigated to the
        login URL during proxy warm-up, avoiding a redundant nav.
        """
        page = self._page
        if not skip_initial_goto:
            await page.goto(LOGIN_URL)

        await page.fill(SELECTORS["login_email"], self._email)
        await page.fill(SELECTORS["login_password"], password)
        await page.click(SELECTORS["login_submit"])

        # We race four outcomes: URL switching to ``/u/<N>/`` = success,
        # dashboard DOM sentinel = success (fallback for URL races), error
        # toast = bad credentials, captcha iframe = anti-bot challenge.
        try:
            await self._wait_for_login_outcome()
        except ProtonBrowserError:
            await self._dump_debug("login-failed")
            raise

    async def _wait_for_login_outcome(self) -> None:
        page = self._page
        sentinel = page.locator(SELECTORS["logged_in_sentinel"]).first
        captcha = page.locator(SELECTORS["captcha_iframe"]).first
        error_toast = page.locator(SELECTORS["error_notification"]).first

        async def _wait_for_logged_in_url() -> None:
            await page.wait_for_url(LOGGED_IN_URL_RE, timeout=LOGIN_OUTCOME_TIMEOUT_MS)

        # Use Playwright's builtin race via wait + return_when=FIRST_COMPLETED.
        url_task = asyncio.create_task(_wait_for_logged_in_url())
        sentinel_task = asyncio.create_task(
            sentinel.wait_for(state="visible", timeout=LOGIN_OUTCOME_TIMEOUT_MS)
        )
        captcha_task = asyncio.create_task(
            captcha.wait_for(state="visible", timeout=LOGIN_OUTCOME_TIMEOUT_MS)
        )
        error_task = asyncio.create_task(
            error_toast.wait_for(state="visible", timeout=LOGIN_OUTCOME_TIMEOUT_MS)
        )
        tasks = [url_task, sentinel_task, captcha_task, error_task]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Drain cancellations so we don't print "Task exception was never
            # retrieved" warnings about Playwright timeouts on the cancelled
            # branches.
            for task in tasks:
                with _SuppressCancelledOrTimeout():
                    await task

        # Whichever signal fired first decides the outcome. URL match wins
        # by default because it's the most reliable; we fall through to
        # captcha / error / sentinel checks for older Proton flows.
        if LOGGED_IN_URL_RE.search(page.url):
            return
        if await captcha.is_visible():
            raise CaptchaInterruptError(page_url=page.url)
        if await error_toast.is_visible():
            text = (await error_toast.text_content()) or "login rejected"
            raise LoginFailedError(text.strip())
        if await sentinel.is_visible():
            return
        raise ProtonBrowserError(
            f"login flow did not produce a recognised outcome (page.url={page.url!r})"
        )

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

        # Wait until the page is interactive — the "Tambah alamat" button is
        # disabled until the user list has loaded.
        add_btn = page.locator(SELECTORS["add_address_button"]).first
        try:
            await add_btn.wait_for(state="visible", timeout=30_000)
            await add_btn.click()
        except Exception as exc:
            await self._dump_debug(f"add-address-button-failed-{local}")
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=CreationStatus.ERROR,
                detail=f"could not open Add address modal: {exc}",
            )

        # Detect modal opening via the title text — this is the most stable
        # signal across Proton WebClient releases.
        title = page.locator(SELECTORS["address_modal_title"]).first
        try:
            await title.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            await self._dump_debug(f"modal-not-visible-{local}")
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=CreationStatus.ERROR,
                detail=f"address modal did not open: {exc}",
            )

        # Some flows render ``role='dialog'`` on the inner card; others
        # don't. Scope inputs to the page (not modal) and rely on Proton
        # only ever showing one address modal at a time.
        await page.locator(SELECTORS["address_local_input"]).first.fill(local)
        if display_name is not None:
            try:
                await page.locator(SELECTORS["address_display_name_input"]).first.fill(
                    display_name
                )
            except Exception:
                logger.debug("display_name input not found, skipping")

        # Optional re-auth password modal: fill only if the field is
        # actually visible (org-managed members usually don't see it).
        if password_for_keygen is not None:
            pw_input = page.locator(SELECTORS["address_password_input"]).first
            if await pw_input.is_visible():
                await pw_input.fill(password_for_keygen)
                pw_confirm = page.locator(
                    SELECTORS["address_password_confirm_input"]
                ).first
                if await pw_confirm.is_visible():
                    await pw_confirm.fill(password_for_keygen)

        await page.locator(SELECTORS["address_submit_button"]).first.click()

        return await self._wait_for_address_outcome(local=local, domain=domain)

    async def _wait_for_address_outcome(
        self, *, local: str, domain: str
    ) -> AddressCreationResult:
        page = self._page
        success = page.locator(SELECTORS["success_notification"]).first
        captcha = page.locator(SELECTORS["captcha_iframe"]).first
        error_toast = page.locator(SELECTORS["error_notification"]).first
        # The modal disappears once Proton accepts the submission. We treat
        # the title becoming hidden as a success signal even if no toast
        # appeared (some Proton builds skip the toast on quick succession).
        modal_title = page.locator(SELECTORS["address_modal_title"]).first

        outcome_timeout_ms = 60_000
        tasks = [
            asyncio.create_task(success.wait_for(state="visible", timeout=outcome_timeout_ms)),
            asyncio.create_task(captcha.wait_for(state="visible", timeout=outcome_timeout_ms)),
            asyncio.create_task(error_toast.wait_for(state="visible", timeout=outcome_timeout_ms)),
            asyncio.create_task(modal_title.wait_for(state="hidden", timeout=outcome_timeout_ms)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with _SuppressCancelledOrTimeout():
                    await task

        # Error wins over modal-closed because Proton sometimes briefly
        # closes the modal then reopens with the error toast visible.
        if await error_toast.is_visible():
            text = ((await error_toast.text_content()) or "").lower().strip()
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=_classify_error(text),
                detail=text or None,
            )
        if await captcha.is_visible():
            return AddressCreationResult(
                local=local,
                domain=domain,
                status=CreationStatus.CAPTCHA_REQUIRED,
                detail="proton served a captcha challenge",
            )
        if await success.is_visible():
            return AddressCreationResult(
                local=local, domain=domain, status=CreationStatus.SUCCESS
            )
        # Modal-closed-without-error = success.
        if not await modal_title.is_visible():
            return AddressCreationResult(
                local=local, domain=domain, status=CreationStatus.SUCCESS
            )
        return AddressCreationResult(
            local=local,
            domain=domain,
            status=CreationStatus.ERROR,
            detail="no outcome signal after submit",
        )


# -------------------------------------------------------------------- helpers


def _addresses_url(user_index: int) -> str:
    return ADDRESSES_URL_TEMPLATE.format(user_index=user_index)


def _kill_pid(pid: int) -> None:
    """Send SIGKILL to ``pid`` if the process still exists.

    Best-effort: never raises. Used as the last-resort fallback in
    :meth:`ProtonBrowser.force_close` when a Playwright ``close()`` step does
    not return within the per-step timeout (a wedged Chromium renderer).
    """
    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("force_close: SIGKILL sent to chromium pid=%d", pid)
    except ProcessLookupError:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.exception("force_close: failed to SIGKILL chromium pid=%d", pid)


def _is_browser_closed_error(exc: BaseException) -> bool:
    """Best-effort detection of the Playwright "target closed" family.

    Used by :func:`address_generator.run_batch` to recognise the exception
    raised when the Cancel button force-closes the browser mid-flight, so we
    can surface a clean "cancelled by user" outcome instead of a generic
    crash.
    """
    name = type(exc).__name__
    if name in {"TargetClosedError", "BrowserClosedError"}:
        return True
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "target closed" in msg
        or "browser has been closed" in msg
        or "browser closed" in msg
    )


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


class _SuppressCancelledOrTimeout:
    """Context manager that swallows ``CancelledError`` and Playwright timeouts.

    Used when draining the losing branches of an ``asyncio.wait`` race so they
    don't print noisy "Task exception was never retrieved" warnings.
    """

    def __enter__(self) -> _SuppressCancelledOrTimeout:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        if exc_type is None:
            return False
        if issubclass(exc_type, asyncio.CancelledError):
            return True
        # Playwright raises its own ``TimeoutError``; matching by name keeps us
        # decoupled from the optional dependency at import time.
        if exc_type.__name__ == "TimeoutError":
            return True
        return False


# -------------------------------------------------------------------- debug dump


async def _dump_page_state(page: Page, label: str) -> Path | None:
    """Best-effort screenshot + HTML dump for postmortem debugging.

    Returns the directory the artefacts were written to, or ``None`` when
    capture failed (we never raise — the caller has its own error to surface).
    """
    try:
        DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("could not create debug dump dir %s", DEBUG_DUMP_DIR)
        return None

    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = DEBUG_DUMP_DIR / f"{label}-{stamp}"
    png_path = base.with_suffix(".png")
    html_path = base.with_suffix(".html")
    try:
        await page.screenshot(path=str(png_path), full_page=True)
    except Exception:
        logger.exception("could not capture screenshot to %s", png_path)
    try:
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        logger.exception("could not capture html to %s", html_path)
    logger.warning(
        "proton_browser %s — debug artefacts saved under %s (screenshot=%s, html=%s, url=%s)",
        label,
        DEBUG_DUMP_DIR,
        png_path.name,
        html_path.name,
        page.url,
    )
    return DEBUG_DUMP_DIR


# ----------------------------------------------------------------- proxy retry


def _is_proxy_failure(exc: BaseException) -> bool:
    """Did Chromium fail because the proxy itself is broken?

    We pattern-match the message because Playwright wraps these in a
    generic ``Page.goto: net::ERR_FOO`` string. Conservative — when in
    doubt, returns ``False`` so we don't blacklist a working proxy on a
    transient hiccup.
    """
    msg = str(exc)
    return any(marker in msg for marker in PROXY_FAILURE_MARKERS)


async def _launch_with_proxy_retry(
    *,
    playwright: Playwright,
    headless: bool,
    nav_timeout_ms: int,
    action_timeout_ms: int,
    proxy_provider: ProxyProvider | None,
    retries: int = DEFAULT_PROXY_RETRIES,
) -> tuple[Browser, BrowserContext, Page, ProxyEntry | None]:
    """Launch Chromium with retry-with-rotated-proxy semantics.

    Strategy:

    1. Ask the provider for ``retries`` candidate proxies (already
       probed at TCP level).
    2. For each: launch Chromium with ``proxy={"server": ...}``, open
       a context+page, do a warm-up ``page.goto(LOGIN_URL)``.
    3. If the goto fails with a proxy-layer error
       (``ERR_SOCKS_CONNECTION_FAILED`` &c.), mark the proxy as failed
       in the provider, tear the browser down, try the next candidate.
    4. If every proxy fails (or the provider returns nothing), fall
       back to a direct connection.

    Returns ``(browser, context, page, used_proxy)`` where
    ``used_proxy`` is the entry the caller should pass back to
    ``provider.mark_failed`` if a *later* navigation reveals the proxy
    is bad. ``None`` means we're on a direct connection.
    """
    candidates: list[ProxyEntry] = []
    if proxy_provider is not None:
        candidates = await proxy_provider.acquire_many(retries)
        if not candidates:
            logger.warning(
                "ProtonBrowser: proxy_provider returned no candidates; "
                "launching Chromium with direct connection"
            )

    for entry in candidates:
        logger.info(
            "ProtonBrowser: trying proxy %s (source=%s)",
            entry.server_url,
            entry.source,
        )
        try:
            browser = await playwright.chromium.launch(
                headless=headless, proxy={"server": entry.server_url}
            )
        except Exception as exc:
            logger.warning(
                "ProtonBrowser: launch with %s failed (%s); trying next",
                entry.server_url,
                exc,
            )
            if proxy_provider is not None:
                proxy_provider.mark_failed(entry)
            continue

        context = await browser.new_context()
        context.set_default_timeout(action_timeout_ms)
        context.set_default_navigation_timeout(nav_timeout_ms)
        page = await context.new_page()
        try:
            # Warm up: confirm the proxy can actually reach Proton. This
            # surfaces ``ERR_SOCKS_CONNECTION_FAILED`` and friends BEFORE
            # we ever type a password into a half-loaded page.
            await page.goto(LOGIN_URL, timeout=nav_timeout_ms)
        except Exception as exc:
            if _is_proxy_failure(exc):
                logger.warning(
                    "ProtonBrowser: proxy %s failed warm-up (%s); blacklisting",
                    entry.server_url,
                    type(exc).__name__,
                )
                if proxy_provider is not None:
                    proxy_provider.mark_failed(entry)
                with contextlib.suppress(Exception):
                    await context.close()
                with contextlib.suppress(Exception):
                    await browser.close()
                continue
            # Non-proxy failure (e.g. Proton itself is down) -- close and
            # re-raise so the caller doesn't silently retry on a real bug.
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()
            raise
        logger.info(
            "ProtonBrowser: warm-up via %s succeeded; proceeding with login",
            entry.server_url,
        )
        return browser, context, page, entry

    # Either no proxy was configured, or every candidate failed -- direct.
    browser = await playwright.chromium.launch(headless=headless)
    context = await browser.new_context()
    context.set_default_timeout(action_timeout_ms)
    context.set_default_navigation_timeout(nav_timeout_ms)
    page = await context.new_page()
    return browser, context, page, None
