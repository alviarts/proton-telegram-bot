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

    # Step 3: Password re-authentication dialog
    pw_input = page.locator(
        "[role='dialog'] input[type='password'], input[type='password']"
    ).first
    try:
        await pw_input.wait_for(state="visible", timeout=10_000)
        await pw_input.fill(proton_password)
        logger.info("filled re-auth password")

        auth_btn = page.locator(
            "button:has-text('Autentikasi'), "
            "button:has-text('Authenticate'), "
            "[role='dialog'] button[type='submit']"
        ).first
        await auth_btn.click()
        logger.info("clicked Autentikasi")
    except Exception:
        logger.warning("no password re-auth dialog appeared; continuing")

    await asyncio.sleep(3)

    # Step 4: Enable "Izinkan pemulihan akun melalui email" toggle if off
    try:
        toggle = page.locator(
            "label:has-text('Izinkan pemulihan akun melalui email'), "
            "label:has-text('Allow recovery by email')"
        ).first
        toggle_parent = toggle.locator("..").locator("input[type='checkbox'], [role='switch']").first
        is_checked = await toggle_parent.is_checked()
        if not is_checked:
            await toggle.click()
            logger.info("enabled 'Izinkan pemulihan akun melalui email' toggle")
            await asyncio.sleep(1)
    except Exception:
        logger.debug("could not find/toggle recovery email switch; may already be on")

    # Step 5: Click "Verifikasi" link
    verify_link = page.locator(
        "a:has-text('Verifikasi'), a:has-text('Verify')"
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
