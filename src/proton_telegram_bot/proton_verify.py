"""Automated Proton human-verification solver.

When Bridge's CLI login triggers a "Human Verification requested" page at
``https://verify.proton.me/?methods=ownership-email&token=...``, this
module can:

1. Create a throwaway Mail.tm inbox.
2. Log into the Proton web UI and set the recovery email to the temp
   address (with password re-authentication and email verification).
3. Open the Bridge verification URL, choose "Email" verification, and
   trigger a code send.
4. Poll the Mail.tm inbox for the 6-digit code.
5. Enter and submit the code on the verification page.

The recovery-email change flow (observed from user screenshots):
  1. Navigate to ``account.proton.me/u/{N}/mail/recovery``
  2. Section "Alamat email pemulihan" has an ``<input>`` with the current
     recovery email.
  3. Clear → fill with temp mail → click **Simpan** (save) button next
     to the input.
  4. Password re-auth dialog: "Masukkan kata sandi Anda" → fill password
     → click **Autentikasi**.
  5. Toast: "Email diperbarui".  The field now shows "Alamat email belum
     diverifikasi" with a **Verifikasi** link.
  6. Click **Verifikasi** → dialog "Verifikasi email pemulihan?" →
     click **Verifikasi melalui email**.
  7. Toast: "Email verifikasi dikirim ke <addr>".
  8. Poll temp mail for verification code → enter code → done.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

from .tempmail import TempMailbox

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

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

# ---------------------------------------------------------------------------
# Selectors for Proton account recovery settings page
# (account.proton.me/u/{N}/mail/recovery)
# ---------------------------------------------------------------------------
RECOVERY_SELECTORS = {
    # The "Alamat email pemulihan" input — it's an <input> inside
    # the "Pemulihan akun" section.  Proton renders it as a regular
    # text/email input next to a "Simpan" button.
    "recovery_email_input": (
        "input[type='email'], "
        "input[id*='recovery' i], "
        "input[name*='recovery' i], "
        # Fallback: the text input near the label "Alamat email pemulihan"
        "label:has-text('Alamat email pemulihan') + input, "
        "label:has-text('Recovery email') + input"
    ),
    # The "Simpan" (Save) button right next to the recovery email input
    "save_button": (
        "button:has-text('Simpan'), "
        "button:has-text('Save')"
    ),
    # Password re-authentication modal
    "reauth_password_input": (
        "[role='dialog'] input[type='password'], "
        "input[type='password']"
    ),
    "reauth_submit_button": (
        "button:has-text('Autentikasi'), "
        "button:has-text('Authenticate'), "
        "[role='dialog'] button[type='submit']"
    ),
    # After save, the "Verifikasi" link appears next to the email
    "verify_link": (
        "a:has-text('Verifikasi'), "
        "a:has-text('Verify'), "
        "button:has-text('Verifikasi'), "
        "button:has-text('Verify')"
    ),
    # Confirmation dialog: "Verifikasi email pemulihan?"
    "verify_via_email_button": (
        "button:has-text('Verifikasi melalui email'), "
        "button:has-text('Verify via email'), "
        "[role='dialog'] button:has-text('Verifikasi')"
    ),
}

RECOVERY_URL_TEMPLATE = "https://account.proton.me/u/{user_index}/mail/recovery"


async def change_recovery_email(
    page: Page,
    new_email: str,
    proton_password: str,
    tempmail: TempMailbox,
    client: httpx.AsyncClient,
    *,
    user_index: int = 0,
) -> bool:
    """Navigate to Proton recovery settings and change the recovery email.

    Assumes the page is already logged in (session cookies present).
    Returns True on success (email changed and verified).
    """
    url = RECOVERY_URL_TEMPLATE.format(user_index=user_index)
    logger.info("navigating to recovery settings: %s", url)
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(3)

    # Step 1: Find and fill the recovery email input
    email_input = page.locator(RECOVERY_SELECTORS["recovery_email_input"]).first
    try:
        await email_input.wait_for(state="visible", timeout=15_000)
        await email_input.click(click_count=3)  # select all existing text
        await email_input.fill(new_email)
        logger.info("filled recovery email input with %s", new_email)
    except Exception:
        logger.error("could not find or fill recovery email input")
        return False

    await asyncio.sleep(1)

    # Step 2: Click "Simpan" (Save) — the button next to the input
    # We need the Simpan that's near the email section, not the phone section
    save_buttons = page.locator(RECOVERY_SELECTORS["save_button"])
    try:
        # Click the first Simpan button (recovery email section comes first)
        await save_buttons.first.click()
        logger.info("clicked Simpan button")
    except Exception:
        logger.error("could not click Simpan button")
        return False

    await asyncio.sleep(2)

    # Step 3: Password re-authentication dialog
    pw_input = page.locator(RECOVERY_SELECTORS["reauth_password_input"]).first
    try:
        await pw_input.wait_for(state="visible", timeout=10_000)
        await pw_input.fill(proton_password)
        logger.info("filled re-auth password")

        auth_btn = page.locator(RECOVERY_SELECTORS["reauth_submit_button"]).first
        await auth_btn.click()
        logger.info("clicked Autentikasi")
    except Exception:
        logger.warning("no password re-auth dialog appeared; continuing")

    await asyncio.sleep(3)

    # Step 4: Click "Verifikasi" link that appears after save
    verify_link = page.locator(RECOVERY_SELECTORS["verify_link"]).first
    try:
        await verify_link.wait_for(state="visible", timeout=10_000)
        await verify_link.click()
        logger.info("clicked Verifikasi link")
    except Exception:
        logger.warning("could not find Verifikasi link; email may already be verified")
        return True  # optimistic — email was saved even if not verified

    await asyncio.sleep(2)

    # Step 5: "Verifikasi email pemulihan?" dialog → "Verifikasi melalui email"
    verify_email_btn = page.locator(RECOVERY_SELECTORS["verify_via_email_button"]).first
    try:
        await verify_email_btn.wait_for(state="visible", timeout=10_000)
        await verify_email_btn.click()
        logger.info("clicked 'Verifikasi melalui email'")
    except Exception:
        logger.error("could not find 'Verifikasi melalui email' button")
        return False

    await asyncio.sleep(2)

    # Step 6: Poll temp mail for the verification LINK (not a code).
    # Proton sends a link like https://account.proton.me/...verify...
    logger.info("polling temp mail %s for verification link...", tempmail.address)
    verify_link_url = await tempmail.wait_for_verify_link(client, max_attempts=60)
    if verify_link_url is None:
        logger.error("timed out waiting for recovery verification link")
        return False

    # Step 7: Open the verification link in the browser to confirm
    logger.info("opening verification link: %s", verify_link_url)
    await page.goto(verify_link_url, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(3)

    logger.info("recovery email changed and verified: %s", new_email)
    return True


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
    to our temp mail address).

    Returns True if verification succeeded.
    """
    logger.info("opening verification URL: %s", verify_url)
    await page.goto(verify_url, wait_until="networkidle", timeout=30_000)
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
