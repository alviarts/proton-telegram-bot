"""Disposable email helper using the Mail.tm REST API.

Used during the /connect flow to automatically:
1. Create a throwaway inbox.
2. Set it as the Proton account's recovery email (via Proton API).
3. Poll for incoming verification codes from Proton.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string

import httpx

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.mail.tm"
POLL_INTERVAL = 3
MAX_POLL_ATTEMPTS = 60  # ~3 minutes


def _random_local_part(length: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


class TempMailError(Exception):
    """Any failure talking to the Mail.tm API."""


class TempMailbox:
    """A single disposable Mail.tm inbox."""

    def __init__(self, address: str, password: str, token: str) -> None:
        self.address = address
        self.password = password
        self._token = token

    @classmethod
    async def create(cls, client: httpx.AsyncClient) -> TempMailbox:
        """Create a fresh random inbox and return a ready-to-use instance."""
        resp = await client.get(f"{API_BASE}/domains", timeout=15)
        resp.raise_for_status()
        domains = resp.json()
        domain_list = domains.get("hydra:member") or domains
        if not domain_list:
            raise TempMailError("Mail.tm returned no available domains")
        domain = domain_list[0]["domain"]

        local = _random_local_part()
        address = f"{local}@{domain}"
        password = _random_local_part(20)

        resp = await client.post(
            f"{API_BASE}/accounts",
            json={"address": address, "password": password},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise TempMailError(
                f"Failed to create temp mail account: {resp.status_code} {resp.text}"
            )

        resp = await client.post(
            f"{API_BASE}/token",
            json={"address": address, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        LOGGER.info("created temp mailbox %s", address)
        return cls(address=address, password=password, token=token)

    async def wait_for_code(
        self,
        client: httpx.AsyncClient,
        *,
        sender_pattern: str = "proton",
        max_attempts: int = MAX_POLL_ATTEMPTS,
    ) -> str | None:
        """Poll the inbox until a verification code arrives.

        Returns the 6-digit code string, or ``None`` on timeout.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in range(max_attempts):
            try:
                resp = await client.get(
                    f"{API_BASE}/messages",
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                messages = resp.json().get("hydra:member", [])
                for msg in messages:
                    from_addr = (msg.get("from") or {}).get("address", "")
                    subject = msg.get("subject", "")
                    intro = msg.get("intro", "")
                    if sender_pattern.lower() not in from_addr.lower():
                        continue
                    code = _extract_code(subject + " " + intro)
                    if code:
                        LOGGER.info("verification code found: %s", code)
                        return code
                    # Fetch full message body for deeper extraction
                    msg_id = msg.get("id")
                    if msg_id:
                        detail = await client.get(
                            f"{API_BASE}/messages/{msg_id}",
                            headers=headers,
                            timeout=15,
                        )
                        if detail.status_code == 200:
                            body = detail.json().get("text", "")
                            code = _extract_code(body)
                            if code:
                                LOGGER.info("verification code found in body: %s", code)
                                return code
            except Exception:
                LOGGER.debug("poll attempt %d failed", attempt, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)
        LOGGER.warning("timed out waiting for verification code")
        return None

    async def wait_for_verify_link(
        self,
        client: httpx.AsyncClient,
        *,
        sender_pattern: str = "proton",
        max_attempts: int = MAX_POLL_ATTEMPTS,
    ) -> str | None:
        """Poll the inbox until a Proton verification link arrives.

        Returns the URL string, or ``None`` on timeout.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in range(max_attempts):
            try:
                resp = await client.get(
                    f"{API_BASE}/messages",
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                messages = resp.json().get("hydra:member", [])
                for msg in messages:
                    from_addr = (msg.get("from") or {}).get("address", "")
                    if sender_pattern.lower() not in from_addr.lower():
                        continue
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    LOGGER.info(
                        "saw candidate verification email id=%s from=%s subject=%r",
                        msg_id,
                        from_addr,
                        msg.get("subject", ""),
                    )
                    detail = await client.get(
                        f"{API_BASE}/messages/{msg_id}",
                        headers=headers,
                        timeout=15,
                    )
                    if detail.status_code != 200:
                        continue
                    body_data = detail.json()
                    html_body = body_data.get("html") or ""
                    text_body = body_data.get("text") or ""
                    link = _extract_verify_link(html_body) or _extract_verify_link(text_body)
                    if link:
                        LOGGER.info("verification link found: %s", link)
                        return link
                    LOGGER.warning(
                        "candidate email %s had no parsable verification link "
                        "(html_type=%s text_type=%s html_len=%d text_len=%d)",
                        msg_id,
                        type(html_body).__name__,
                        type(text_body).__name__,
                        len(_coerce_text(html_body)),
                        len(_coerce_text(text_body)),
                    )
            except Exception:
                LOGGER.warning("poll attempt %d failed", attempt, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)
        LOGGER.warning("timed out waiting for verification link")
        return None


    async def wait_for_subject(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        max_attempts: int = 30,
        poll_interval: float = 2.0,
    ) -> bool:
        """Poll the inbox for a message whose subject contains ``token``.

        Used by the post-connect smoke test: we send a unique-token email
        from the just-connected Bridge SMTP and confirm Mail.tm receives
        it. Returns True on hit, False on timeout.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in range(max_attempts):
            try:
                resp = await client.get(
                    f"{API_BASE}/messages",
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                for msg in resp.json().get("hydra:member", []):
                    subject = msg.get("subject") or ""
                    if token in subject:
                        LOGGER.info("smoke-test token %s found in inbox", token)
                        return True
            except Exception:
                LOGGER.debug("smoke-test poll attempt %d failed", attempt, exc_info=True)
            await asyncio.sleep(poll_interval)
        LOGGER.warning("smoke-test token %s not seen in inbox after %d polls", token, max_attempts)
        return False


def _coerce_text(value: object) -> str:
    """Normalize a Mail.tm body field to a single string.

    Mail.tm returns ``html`` (and occasionally ``text``) as a **list**
    of multipart sections rather than a flat string, so feeding it
    straight into ``re.search`` raises
    ``TypeError: expected string or bytes-like object, got 'list'``.
    Coerce list/None/anything-else into a usable string here so
    callers don't have to.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(_coerce_text(v) for v in value)
    return str(value)


def _extract_code(text: object) -> str | None:
    """Extract a 6-digit verification code from text."""
    text_str = _coerce_text(text)
    match = re.search(r"\b(\d{6})\b", text_str)
    return match.group(1) if match else None


def _extract_verify_link(text: object) -> str | None:
    """Extract a Proton verification link from email text/HTML."""
    text_str = _coerce_text(text)
    match = re.search(
        r'https?://account\.proton\.me/[^\s"\'<>]+verify[^\s"\'<>]*',
        text_str,
        re.IGNORECASE,
    )
    if match:
        return match.group(0)
    match = re.search(
        r'https?://[^\s"\'<>]*proton[^\s"\'<>]*verify[^\s"\'<>]*',
        text_str,
        re.IGNORECASE,
    )
    return match.group(0) if match else None
