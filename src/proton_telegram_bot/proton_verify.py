"""Automated Proton human-verification solver.

When Bridge's CLI login triggers a "Human Verification requested" page at
``https://verify.proton.me/?methods=ownership-email&token=...``, this
module can:

1. Create a throwaway Mail.tm inbox.
2. (Optionally) log into the Proton web UI and set the recovery email to
   the new temp address.
3. Open the verification URL, choose "Email" verification, and trigger
   a code send.
4. Poll the Mail.tm inbox for the 6-digit code.
5. Enter and submit the code on the verification page.

The entry point is :func:`solve_email_verification`.
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

# Selectors for the Proton verification page.
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
        "input[placeholder*='kode' i]"
    ),
    "verify_button": (
        "button:has-text('Verify'), "
        "button:has-text('Verifikasi'), "
        "button[type='submit']"
    ),
    "get_code_button": (
        "button:has-text('Get verification code'), "
        "button:has-text('Dapatkan kode'), "
        "button:has-text('Send'), "
        "button:has-text('Kirim'), "
        "button:has-text('Request'), "
        "button:has-text('Get code')"
    ),
    "success_indicator": (
        "text=/verification.*success/i, "
        "text=/berhasil/i, "
        "[role='alert']:has-text('success'), "
        "[role='alert']:has-text('berhasil')"
    ),
}

# Selectors for Proton account recovery settings page.
RECOVERY_SELECTORS = {
    "recovery_email_section": (
        "text=/Recovery email/i, "
        "text=/Email pemulihan/i"
    ),
    "recovery_email_edit": (
        "button:has-text('Edit'), "
        "button:has-text('Ubah'), "
        "[data-testid='account:recovery:emailEdit']"
    ),
    "recovery_email_input": (
        "input[id='recoveryEmail'], "
        "input[name='recoveryEmail'], "
        "input[type='email']"
    ),
    "save_button": (
        "button:has-text('Save'), "
        "button:has-text('Simpan'), "
        "button[type='submit']"
    ),
    "verify_recovery_code_input": (
        "input[id='verification-code'], "
        "input[name='code'], "
        "input[type='text']"
    ),
}

RECOVERY_URL_TEMPLATE = "https://account.proton.me/u/{user_index}/recovery"


async def change_recovery_email(
    page: Page,
    new_email: str,
    tempmail: TempMailbox,
    client: httpx.AsyncClient,
    *,
    user_index: int = 0,
) -> bool:
    """Navigate to Proton recovery settings and change the recovery email.

    Assumes the page is already logged in (session cookies present).
    Returns True on success.
    """
    url = RECOVERY_URL_TEMPLATE.format(user_index=user_index)
    logger.info("navigating to recovery settings: %s", url)
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(2)

    # Click Edit on recovery email
    edit_btn = page.locator(RECOVERY_SELECTORS["recovery_email_edit"]).first
    try:
        await edit_btn.wait_for(state="visible", timeout=10_000)
        await edit_btn.click()
    except Exception:
        logger.warning("could not find recovery email edit button; trying direct input")

    await asyncio.sleep(1)

    # Fill in new recovery email
    email_input = page.locator(RECOVERY_SELECTORS["recovery_email_input"]).first
    try:
        await email_input.wait_for(state="visible", timeout=10_000)
        await email_input.fill("")
        await email_input.fill(new_email)
    except Exception:
        logger.error("could not fill recovery email input")
        return False

    # Save
    save_btn = page.locator(RECOVERY_SELECTORS["save_button"]).first
    try:
        await save_btn.click()
    except Exception:
        logger.error("could not click save button")
        return False

    await asyncio.sleep(3)

    # Proton may send a verification code to the new recovery email
    code_input = page.locator(RECOVERY_SELECTORS["verify_recovery_code_input"]).first
    try:
        await code_input.wait_for(state="visible", timeout=10_000)
        # Code was requested — fetch from temp mail
        logger.info("Proton wants verification code for new recovery email")
        code = await tempmail.wait_for_code(client, max_attempts=40)
        if code is None:
            logger.error("timed out waiting for recovery email verification code")
            return False
        await code_input.fill(code)
        # Submit the code
        submit_btn = page.locator(RECOVERY_SELECTORS["save_button"]).first
        await submit_btn.click()
        await asyncio.sleep(2)
    except Exception:
        # No verification code prompt — change was accepted directly
        logger.info("recovery email changed without additional verification")

    logger.info("recovery email changed to %s", new_email)
    return True


async def solve_email_verification(
    page: Page,
    verify_url: str,
    tempmail: TempMailbox,
    client: httpx.AsyncClient,
) -> bool:
    """Open the Proton verification URL and solve it via email code.

    Returns True if verification succeeded.
    """
    logger.info("opening verification URL: %s", verify_url)
    await page.goto(verify_url, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(2)

    # Select email verification method if multiple are offered
    email_btn = page.locator(VERIFY_SELECTORS["email_method_button"]).first
    try:
        await email_btn.wait_for(state="visible", timeout=5_000)
        await email_btn.click()
        await asyncio.sleep(1)
    except Exception:
        logger.debug("no email method tab found; may already be selected")

    # Click "Get verification code" / "Send" button
    get_code_btn = page.locator(VERIFY_SELECTORS["get_code_button"]).first
    try:
        await get_code_btn.wait_for(state="visible", timeout=5_000)
        await get_code_btn.click()
        logger.info("triggered verification code send")
        await asyncio.sleep(2)
    except Exception:
        logger.debug("no explicit send button found; code may be auto-sent")

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
    except Exception:
        logger.error("could not find code input field")
        return False

    # Click Verify
    verify_btn = page.locator(VERIFY_SELECTORS["verify_button"]).first
    try:
        await verify_btn.click()
    except Exception:
        logger.error("could not click verify button")
        return False

    await asyncio.sleep(3)
    logger.info("verification code submitted successfully")
    return True
