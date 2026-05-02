"""Automated Proton recovery-email changer and Bridge verification solver.

Recovery-email change flow (from user screenshots):
  1. Login to account.proton.me
  2. Detect user_index from redirect URL (e.g. /u/19/...)
  3. Navigate to ``account.proton.me/u/{N}/mail/recovery``
  4. Fill "Alamat email pemulihan" input with temp mail → click **Simpan**
  5. Password re-auth: "Masukkan kata sandi Anda" → fill → **Autentikasi**
  6. Enable toggle "Izinkan pemulihan akun melalui email" if off
  7. Click **Verifikasi** link → dialog → **Verifikasi melalui email**
  8. Poll temp mail for verification **link** → return it to caller
     (caller sends it to Telegram user for manual click)

Bridge login verification (verify.proton.me):
  - After recovery email is verified, Bridge login verification sends
    a 6-digit **code** to the recovery email.
  - Bot polls temp mail for the code and submits it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .tempmail import TempMailbox

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("/tmp/proton_login_debug")


async def _dump_page(page: Page, prefix: str) -> Path | None:
    """Save a screenshot + HTML of the current page for post-mortem.

    Returns the screenshot path on success, ``None`` on any failure
    (we never want diagnostics to mask the real error).
    """
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        png = DEBUG_DIR / f"{prefix}_{ts}.png"
        html = DEBUG_DIR / f"{prefix}_{ts}.html"
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(png), full_page=True)
        with contextlib.suppress(Exception):
            html.write_text(await page.content(), encoding="utf-8")
        return png if png.exists() else None
    except Exception:
        return None

VERIFY_URL_RE = re.compile(
    r"https://verify\.proton\.me/\?.*methods=ownership-email",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Proton "Masukkan kata sandi Anda" re-auth modal (Modal-Two component)
# ---------------------------------------------------------------------------
# Proton renders this dialog as a wrapper ``<div class="modal-two">``
# containing a native HTML5 ``<dialog class="modal-two-dialog">``. The
# native ``<dialog>`` carries no ``role="dialog"`` attribute (the role
# is implicit) and no ``open`` attribute, so the user-agent stylesheet
# sets ``display: none`` on it. Visibility is actually controlled by
# the wrapper via ``.modal-two-backdrop--in``, so selectors like
# ``[role='dialog']`` or ``dialog:has-text(...)`` time out as "not
# visible" even when the modal IS on screen.
#
# We anchor primary detection on ``form#auth-form`` (only present when
# this specific modal is open), with ``.modal-two`` and text-based
# fallbacks for forward compatibility.
_REAUTH_DIALOG_SELECTOR = (
    "form#auth-form, "
    ".modal-two:has-text('Masukkan kata sandi'), "
    ".modal-two:has-text('Enter your password'), "
    ".modal-two-dialog:has-text('Masukkan kata sandi'), "
    ".modal-two-dialog:has-text('Enter your password'), "
    "[role='dialog']:has-text('Masukkan kata sandi'), "
    "[role='dialog']:has-text('Enter your password'), "
    "dialog:has-text('Masukkan kata sandi'), "
    "dialog:has-text('Enter your password')"
)
_REAUTH_PASSWORD_INPUT_SELECTOR = (
    "form#auth-form input#password, "
    "form#auth-form input[type='password'], "
    ".modal-two input#password, "
    ".modal-two input[type='password']"
)
_REAUTH_SUBMIT_BUTTON_SELECTOR = (
    "button[form='auth-form'][type='submit'], "
    "button[form='auth-form']:has-text('Autentikasi'), "
    "button[form='auth-form']:has-text('Authenticate'), "
    ".modal-two button:has-text('Autentikasi'), "
    ".modal-two button:has-text('Authenticate'), "
    ".modal-two button[type='submit']"
)


async def _handle_reauth_modal(
    page: Page,
    proton_password: str,
    *,
    label: str = "",
    appear_timeout_ms: int = 10_000,
) -> bool:
    """Detect and complete the Proton ``Masukkan kata sandi Anda`` modal.

    Returns:
      ``True`` if no modal appears OR the modal appears AND is
      successfully submitted (i.e. we are clear to continue), ``False``
      if the modal appears but cannot be completed (caller should
      record a failure and abort).

    Submission strategy: press ``Enter`` on the password input first
    (the Autentikasi button lives in ``modal-two-footer`` outside the
    form, only wired via ``form="auth-form"`` — clicking it can be
    flaky). If that doesn't close the modal within 8s, fall back to
    clicking the button.

    The ``label`` argument is used purely for logging so the operator
    can tell which call site (e.g. ``post-Simpan``, ``post-toggle``)
    triggered the modal.
    """
    tag = f"[{label}] " if label else ""
    dialog = page.locator(_REAUTH_DIALOG_SELECTOR).first
    try:
        await dialog.wait_for(state="visible", timeout=appear_timeout_ms)
    except Exception:
        logger.info("%sno password re-auth dialog appeared; continuing", tag)
        return True

    try:
        pw_input = page.locator(_REAUTH_PASSWORD_INPUT_SELECTOR).first
        await pw_input.wait_for(state="visible", timeout=5_000)
        await pw_input.click()
        await pw_input.fill("")
        await pw_input.fill(proton_password)
        logger.info("%sfilled re-auth password in dialog", tag)

        await pw_input.press("Enter")
        logger.info("%spressed Enter on re-auth password input", tag)

        try:
            await dialog.wait_for(state="hidden", timeout=8_000)
            logger.info("%sre-auth dialog closed after Enter", tag)
            return True
        except Exception:
            logger.info("%sEnter did not close re-auth dialog; clicking Autentikasi", tag)
            auth_btn = page.locator(_REAUTH_SUBMIT_BUTTON_SELECTOR).first
            await auth_btn.click()
            logger.info("%sclicked Autentikasi", tag)
            await dialog.wait_for(state="hidden", timeout=15_000)
            logger.info("%sre-auth dialog closed after button click", tag)
            return True
    except Exception:
        logger.error("%sre-auth dialog appeared but could not be completed", tag)
        return False

# ---------------------------------------------------------------------------
# Selectors for the Bridge verification page (verify.proton.me)
# ---------------------------------------------------------------------------
VERIFY_SELECTORS = {
    "email_method_button": (
        "button:has-text('Email'), "
        "button:has-text('email'), "
        "[data-testid='tab-header-ownership-email']"
    ),
    "code_input": (
        "input[id='verification-code'], "
        "input[name='code'], "
        "input[type='text'][inputmode='numeric'], "
        "input[placeholder*='code' i], "
        "input[placeholder*='kode' i], "
        "input[placeholder*='123456']"
    ),
    "verify_button": (
        "button:has-text('Verify account'), "
        "button:has-text('Verify'), "
        "button:has-text('Verifikasi akun'), "
        "button[type='submit']"
    ),
    "get_code_button": (
        "button:has-text('Get verification code'), "
        "button:has-text('Dapatkan kode'), "
        "button:has-text('Send'), "
        "button:has-text('Kirim'), "
        "button:has-text('Request'), "
        "button:has-text('Resend code'), "
        "button:has-text('Get code'), "
        "a:has-text('Resend code'), "
        "a:has-text('Kirim ulang')"
    ),
}


async def _diagnose_login_blocker(page: Page) -> str:
    """Best-effort detection of *why* a Proton login is stuck.

    Returns a short tag describing the most likely blocker:
    ``"2fa"``, ``"captcha"``, ``"bad_credentials"``, ``"unlock"``,
    ``"unknown"``. Used purely for logging — do not use for control flow.
    """
    try:
        url = page.url
        if "/login/2fa" in url or "/2fa" in url:
            return "2fa"
        # CAPTCHA iframe (HCaptcha is the one Proton ships).
        if await page.locator("iframe[src*='hcaptcha'], iframe[title*='captcha' i]").count() > 0:
            return "captcha"
        # Visible 2FA prompt
        if await page.locator(
            "input[name='twoFactorCode'], "
            "input[placeholder*='2FA' i], "
            "input[placeholder*='kode' i][maxlength='6']"
        ).count() > 0:
            return "2fa"
        # Inline credentials error toast
        body = (await page.locator("body").inner_text()).lower()
        if "incorrect" in body or "salah" in body or "tidak benar" in body:
            return "bad_credentials"
        if "unlock" in url or "humanverification" in url.lower():
            return "unlock"
    except Exception:
        pass
    return "unknown"


async def _login_proton(
    page: Page,
    email: str,
    password: str,
) -> int | None:
    """Log into Proton web and return the user_index for this session.

    The redirect after submit depends on which products the user has:
      * Single-product:  ``account.proton.me/u/<N>/...``
      * Multi-product:   ``account.proton.me/applications`` (Welcome / app
        picker, e.g. Mail · Calendar · Pass · VPN · Drive · Docs · ...)
    For the second case we click the Mail tile and read ``user_index``
    from the resulting ``mail.proton.me/u/<N>/inbox`` URL.

    Returns None if login failed. Detailed diagnostics
    (screenshot + HTML + blocker tag) are written to ``DEBUG_DIR`` and
    are also exposed on this function via ``last_failure`` for the
    caller to surface to the user.
    """
    logger.info("logging into Proton web as %s", email)
    await page.goto(
        "https://account.proton.me/login",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    await asyncio.sleep(3)

    # Fill credentials
    username_input = page.locator("input[id='username'], input[name='username']").first
    await username_input.wait_for(state="visible", timeout=15_000)
    await username_input.fill(email)

    password_input = page.locator("input[id='password'], input[name='password']").first
    await password_input.fill(password)

    submit_btn = page.locator("button[type='submit']").first
    await submit_btn.click()
    logger.info("submitted login form")

    # Wait for any navigation off /login (success), regardless of which
    # post-login URL Proton picked.
    try:
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/login')",
            timeout=60_000,
        )
    except Exception:
        blocker = await _diagnose_login_blocker(page)
        dump = await _dump_page(page, f"login_failed_{blocker}")
        _login_proton.last_failure = {  # type: ignore[attr-defined]
            "blocker": blocker,
            "url": page.url,
            "screenshot": dump,
        }
        logger.error(
            "Proton login stuck on /login: blocker=%s url=%s dump=%s",
            blocker,
            page.url,
            dump,
        )
        return None

    # Direct redirect into a product (legacy / single-product accounts).
    m = re.search(r"/u/(\d+)", page.url)
    if m:
        user_index = int(m.group(1))
        logger.info(
            "login successful (direct), user_index=%d (url=%s)",
            user_index,
            page.url,
        )
        return user_index

    # Welcome / app picker. Click Mail to land on mail.proton.me/u/<N>/inbox.
    logger.info("login landed on app picker (%s); clicking Mail tile", page.url)
    mail_tile = page.locator(
        "a[href*='mail.proton.me'], "
        "a:has-text('Mail'):not(:has-text('Mailto'))"
    ).first
    try:
        await mail_tile.wait_for(state="visible", timeout=15_000)
        await mail_tile.click()
    except Exception:
        blocker = "no_mail_tile"
        dump = await _dump_page(page, f"login_failed_{blocker}")
        _login_proton.last_failure = {  # type: ignore[attr-defined]
            "blocker": blocker,
            "url": page.url,
            "screenshot": dump,
        }
        logger.error(
            "could not click Mail tile on Welcome page; url=%s dump=%s",
            page.url,
            dump,
        )
        return None

    try:
        await page.wait_for_url(
            re.compile(r"mail\.proton\.me/u/\d+"),
            timeout=30_000,
        )
    except Exception:
        blocker = "mail_redirect_failed"
        dump = await _dump_page(page, f"login_failed_{blocker}")
        _login_proton.last_failure = {  # type: ignore[attr-defined]
            "blocker": blocker,
            "url": page.url,
            "screenshot": dump,
        }
        logger.error(
            "Mail tile click did not redirect to mail.proton.me/u/<n>; url=%s dump=%s",
            page.url,
            dump,
        )
        return None

    m = re.search(r"/u/(\d+)", page.url)
    user_index = int(m.group(1)) if m else 0
    logger.info(
        "login successful (via Mail tile), user_index=%d (url=%s)",
        user_index,
        page.url,
    )
    return user_index


async def change_recovery_email(
    page: Page,
    new_email: str,
    proton_password: str,
    tempmail: TempMailbox,
    client: httpx.AsyncClient,
    *,
    user_index: int = 0,
) -> str | None:
    """Navigate to Proton recovery settings and change the recovery email.

    Assumes the page is already logged in (session cookies present).

    Returns the verification link URL (to be sent to the user via
    Telegram for manual click), or None on failure.
    """
    url = f"https://account.proton.me/u/{user_index}/mail/recovery"
    logger.info("navigating to recovery settings: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(5)

    # Step 1: Find and fill the recovery email input
    email_input = page.locator("input[type='email']").first
    try:
        await email_input.wait_for(state="visible", timeout=15_000)
        await email_input.click(click_count=3)  # select all existing text
        await asyncio.sleep(0.5)
        await email_input.fill(new_email)
        logger.info("filled recovery email input with %s", new_email)
    except Exception:
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "fill_email",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_fill_email"),
        }
        logger.error("could not find or fill recovery email input")
        return None

    await asyncio.sleep(1)

    # Step 2: Click "Simpan" (Save) — the first one (recovery email section)
    save_buttons = page.locator("button:has-text('Simpan'), button:has-text('Save')")
    try:
        await save_buttons.first.click()
        logger.info("clicked Simpan button")
    except Exception:
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "click_simpan",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_simpan"),
        }
        logger.error("could not click Simpan button")
        return None

    await asyncio.sleep(2)

    # Step 3: Password re-authentication dialog ("Masukkan kata sandi Anda").
    #
    # Proton requires re-auth on every settings change that touches
    # account credentials. Our flow triggers it at least twice — once
    # after Simpan (Step 2) and once after the recovery toggle (Step 4).
    # Each invocation is its own modal instance (heading IDs differ:
    # ``modal-147``, ``modal-160``, …), so we must handle it
    # idempotently every time.
    if not await _handle_reauth_modal(page, proton_password, label="post-Simpan"):
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "reauth_dialog",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_reauth"),
        }
        return None

    await asyncio.sleep(3)

    # Step 4: Enable "Izinkan pemulihan akun melalui email" toggle if off
    toggle_clicked = False
    try:
        toggle = page.locator(
            "label:has-text('Izinkan pemulihan akun melalui email'), "
            "label:has-text('Allow recovery by email')"
        ).first
        toggle_parent = toggle.locator("..").locator("input[type='checkbox'], [role='switch']").first
        is_checked = await toggle_parent.is_checked()
        if not is_checked:
            await toggle.click()
            toggle_clicked = True
            logger.info("enabled 'Izinkan pemulihan akun melalui email' toggle")
            await asyncio.sleep(1)
    except Exception:
        logger.debug("could not find/toggle recovery email switch; may already be on")

    # Step 4b: Toggling the switch is itself a credentialed settings
    # change, so Proton fires another re-auth modal. Handle it the same
    # way — fall through silently if it doesn't appear (e.g. toggle was
    # already on and we never clicked).
    if toggle_clicked and not await _handle_reauth_modal(
        page, proton_password, label="post-toggle"
    ):
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "reauth_dialog_after_toggle",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_reauth_toggle"),
        }
        return None
    if toggle_clicked:
        await asyncio.sleep(2)

    # Step 5: Click "Verifikasi" link.
    #
    # In the live recovery settings page this is rendered as a
    # ``<button class="link" type="button" aria-label="Verify now this
    # recovery email address: ...">Verifikasi</button>`` next to a
    # ``"Alamat email belum diverifikasi."`` notice — NOT as an ``<a>``
    # tag. Older versions of the page may have used an ``<a>``, so we
    # try both. Anchor primary detection on the unique aria-label so
    # we never accidentally match an unrelated button.
    verify_link = page.locator(
        "button[aria-label*='Verify now this recovery email' i], "
        "button.link:has-text('Verifikasi'), "
        "button.link:has-text('Verify'), "
        "a:has-text('Verifikasi'), "
        "a:has-text('Verify')"
    ).first
    try:
        await verify_link.wait_for(state="visible", timeout=10_000)
        await verify_link.click()
        logger.info("clicked Verifikasi link")
    except Exception:
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "click_verifikasi",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_verifikasi_link"),
        }
        logger.warning("could not find Verifikasi link; email may already be verified")
        return None

    await asyncio.sleep(2)

    # Step 6: "Verifikasi email pemulihan?" dialog → "Verifikasi melalui email"
    verify_email_btn = page.locator(
        "button:has-text('Verifikasi melalui email'), "
        "button:has-text('Verify via email')"
    ).first
    try:
        await verify_email_btn.wait_for(state="visible", timeout=10_000)
        await verify_email_btn.click()
        logger.info("clicked 'Verifikasi melalui email'")
    except Exception:
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "click_verify_via_email",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_verify_via_email"),
        }
        logger.error("could not find 'Verifikasi melalui email' button")
        return None

    await asyncio.sleep(2)

    # Step 7: Poll temp mail for the verification LINK
    logger.info("polling temp mail %s for verification link...", tempmail.address)
    verify_link_url = await tempmail.wait_for_verify_link(client, max_attempts=60)
    if verify_link_url is None:
        change_recovery_email.last_failure = {  # type: ignore[attr-defined]
            "step": "poll_verify_link",
            "url": page.url,
            "screenshot": await _dump_page(page, "recovery_failed_poll_link"),
        }
        logger.error("timed out waiting for recovery verification link")
        return None

    logger.info("got verification link: %s", verify_link_url)
    return verify_link_url


async def solve_email_verification(
    page: Page,
    verify_url: str,
    tempmail: TempMailbox,
    client: httpx.AsyncClient,
) -> bool:
    """Open the Proton verification URL and solve it via email code.

    The verification page at verify.proton.me shows:
      - "Verification code" input (placeholder "123456")
      - "Verify account" button
      - "Resend code" link

    The code is sent to the recovery email (which should already be set
    to our temp mail address and verified).

    Returns True if verification succeeded.
    """
    logger.info("opening verification URL: %s", verify_url)
    await page.goto(verify_url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(3)

    # Select email verification method if multiple are offered
    email_btn = page.locator(VERIFY_SELECTORS["email_method_button"]).first
    try:
        await email_btn.wait_for(state="visible", timeout=5_000)
        await email_btn.click()
        await asyncio.sleep(1)
    except Exception:
        logger.debug("no email method tab found; may already be selected")

    # Click "Get verification code" / "Resend code" if present
    get_code_btn = page.locator(VERIFY_SELECTORS["get_code_button"]).first
    try:
        await get_code_btn.wait_for(state="visible", timeout=5_000)
        await get_code_btn.click()
        logger.info("triggered verification code send")
        await asyncio.sleep(2)
    except Exception:
        logger.debug("no explicit send button; code may be auto-sent")

    # Poll temp mail for the verification code
    logger.info("polling temp mail %s for verification code...", tempmail.address)
    code = await tempmail.wait_for_code(client, max_attempts=60)
    if code is None:
        logger.error("timed out waiting for verification code from temp mail")
        return False

    # Enter the code
    code_input = page.locator(VERIFY_SELECTORS["code_input"]).first
    try:
        await code_input.wait_for(state="visible", timeout=10_000)
        await code_input.fill(code)
        logger.info("filled verification code: %s", code)
    except Exception:
        logger.error("could not find code input field")
        return False

    # Click "Verify account"
    verify_btn = page.locator(VERIFY_SELECTORS["verify_button"]).first
    try:
        await verify_btn.click()
        logger.info("clicked Verify account button")
    except Exception:
        logger.error("could not click verify button")
        return False

    await asyncio.sleep(3)
    logger.info("verification code submitted successfully")
    return True


# ---------------------------------------------------------------- list addresses


_ADDRESSES_API_RE = re.compile(r"/api/core/v4/addresses(?:\?|/?$)")


async def fetch_all_addresses(
    page: Page,
    *,
    user_index: int | None = None,
) -> list[str] | None:
    """Return every address Proton's account API knows for this user.

    Drives the *already-logged-in* Playwright page to navigate to
    ``account.proton.me/u/{N}/mail/users-addresses`` ("Pengguna dan
    alamat") and captures the ``/api/core/v4/addresses`` response(s)
    that Proton's React app fires off on mount. We can't just call
    ``fetch('/api/core/v4/addresses')`` from ``page.evaluate``: Proton
    rejects API requests that lack the ``x-pm-uid`` /
    ``x-pm-appversion`` headers (returns ``400 Bad Request``), and a
    bare ``fetch`` from page context can't include them. Letting the
    React app issue the request gives us the headers for free, and
    cross-origin nav (e.g. from ``mail.proton.me``) into
    ``account.proton.me`` is handled transparently by Playwright.

    ``user_index`` defaults to whatever ``/u/<N>/`` is in the page's
    current URL (falling back to ``0`` when unrecognised). Pass it
    explicitly if the caller has it (e.g. directly after
    :func:`_login_proton`).

    The Proton "Pengguna dan alamat" page renders only a few rows by
    default ("18 alamat lainnya" expandable), but the underlying API
    call returns the full set in one or more paginated responses.
    We listen on every response during navigation so multi-page
    accounts are handled correctly.

    Returns a sorted, lower-cased list of email strings, or ``None``
    on any error.
    """
    if user_index is None:
        m = re.search(r"/u/(\d+)/", page.url or "")
        user_index = int(m.group(1)) if m else 0

    target_url = f"https://account.proton.me/u/{user_index}/mail/users-addresses"
    captured: list = []

    def _on_response(resp) -> None:
        try:
            if (
                _ADDRESSES_API_RE.search(resp.url)
                and resp.request.method == "GET"
            ):
                captured.append(resp)
        except Exception:  # pragma: no cover - listener must never raise
            pass

    page.on("response", _on_response)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        # React fires the /addresses fetch after mount; give it a
        # moment so we don't tear down the listener before the
        # request has even left the page.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # pragma: no cover - networkidle is best-effort
            pass
    except Exception as exc:  # pragma: no cover - nav flake / timeout
        logger.warning(
            "fetch_all_addresses: navigation to %s failed: %s", target_url, exc
        )
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:  # pragma: no cover
            pass

    if not captured:
        logger.warning(
            "fetch_all_addresses: no /api/core/v4/addresses responses "
            "captured after navigating to %s",
            target_url,
        )
        return None

    addresses_set: set[str] = set()
    saw_ok = False
    for resp in captured:
        try:
            if not resp.ok:
                logger.warning(
                    "fetch_all_addresses: /api/core/v4/addresses returned "
                    "status=%d",
                    resp.status,
                )
                continue
            data = await resp.json()
        except Exception as exc:  # pragma: no cover - body read flake
            logger.warning(
                "fetch_all_addresses: failed to read response body: %s", exc
            )
            continue

        saw_ok = True
        raw = data.get("Addresses") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            # Status: 1 = enabled (the /api endpoint also returns
            # disabled ones with Status=2; the UI hides them but
            # they still receive mail until explicitly deleted, so
            # we keep them).
            email = entry.get("Email")
            if isinstance(email, str) and "@" in email:
                addresses_set.add(email.lower())

    if not saw_ok:
        return None
    return sorted(addresses_set)


async def fetch_all_addresses_via_browser(
    email: str,
    proton_password: str,
) -> list[str] | None:
    """Spawn a Playwright session, log into Proton, fetch addresses, close.

    Convenience wrapper for paths that don't already have a logged-in
    Playwright page (e.g. ``/connect`` taking the existing-creds short
    path where the recovery-email Playwright flow is skipped).

    Returns the address list from :func:`fetch_all_addresses`, or
    ``None`` if Playwright isn't installed, login was blocked
    (CAPTCHA / 2FA / bad creds), or the API call failed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "playwright not installed; cannot fetch Proton addresses via browser"
        )
        return None

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        user_index = await _login_proton(page, email, proton_password)
        if user_index is None:
            failure = getattr(_login_proton, "last_failure", {}) or {}
            logger.warning(
                "address sync skipped: Proton login blocker=%s url=%s",
                failure.get("blocker"),
                failure.get("url"),
            )
            return None
        return await fetch_all_addresses(page, user_index=user_index)
    except Exception:
        logger.exception("fetch_all_addresses_via_browser unexpected failure")
        return None
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


# --------------------------------------------------------------- recovery-email auto-verify

# Buttons that the recovery-link landing page may render before the
# success state appears. Some link variants auto-verify on load (no
# button), so we click best-effort and never fail on absence.
_VERIFY_BUTTON_SELECTORS: tuple[str, ...] = (
    "button:has-text('Verifikasi email')",
    "button:has-text('Verifikasi sekarang')",
    "button:has-text('Verifikasi')",
    "button:has-text('Verify email')",
    "button:has-text('Verify now')",
    "button:has-text('Verify')",
    "a:has-text('Verifikasi email')",
    "a:has-text('Verifikasi')",
    "a:has-text('Verify email')",
    "a:has-text('Verify')",
)

# Locators that confirm Proton accepted the recovery-email verification.
# We treat ANY visible match as success — Proton's wording differs by
# locale and by which surface (account.proton.me vs verify.proton.me)
# the link lands on.
_VERIFY_SUCCESS_SELECTORS: tuple[str, ...] = (
    "text=/Email\\s+verified/i",
    "text=/Recovery email.*verified/i",
    "text=/Email.*berhasil.*diverifikasi/i",
    "text=/Email.*sudah.*diverifikasi/i",
    "text=/Email pemulihan.*diverifikasi/i",
    "text=/Verifikasi.*berhasil/i",
)


async def auto_verify_recovery_link(
    page: Page,
    verify_link: str,
    *,
    success_timeout_ms: int = 20_000,
    button_timeout_ms: int = 2_500,
) -> bool:
    """Open ``verify_link`` and confirm Proton accepted the recovery email.

    Some Proton recovery-link variants auto-verify on page load; others
    show a "Verify" / "Verifikasi" confirmation button. We attempt the
    button click optimistically (best-effort, short timeout) and then
    poll for any of the recognised success indicators.

    Returns ``True`` on success, ``False`` on navigation failure or
    when no success indicator appears within ``success_timeout_ms``.
    Never raises — the caller is expected to fall back to the manual
    "click the link yourself, then reply ok" flow.
    """
    try:
        await page.goto(
            verify_link, wait_until="domcontentloaded", timeout=30_000
        )
    except Exception:
        logger.warning(
            "auto_verify: navigation to verify link failed", exc_info=True
        )
        return False

    # Best-effort click on a Verify / Verifikasi button if the link
    # variant requires it. Proton sometimes renders the button as
    # ``<button>``, sometimes as an ``<a>`` styled like a button.
    for sel in _VERIFY_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=button_timeout_ms)
            await btn.click()
            logger.info("auto_verify: clicked confirmation button (%s)", sel)
            break
        except Exception:
            continue

    # Poll for any success indicator. We share a single deadline across
    # all selectors so the total wait is bounded by ``success_timeout_ms``
    # regardless of how many indicator variants we try.
    deadline = time.monotonic() + success_timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in _VERIFY_SUCCESS_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible():
                    logger.info("auto_verify: success indicator visible (%s)", sel)
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)

    logger.warning(
        "auto_verify: no success indicator after %dms for %s",
        success_timeout_ms,
        verify_link,
    )
    return False
