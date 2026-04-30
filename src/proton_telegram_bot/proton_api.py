"""Proton Mail API client for fetching account addresses."""
from __future__ import annotations

import logging
from typing import Any

from proton.api import Session

LOGGER = logging.getLogger(__name__)

# Proton address status: 1 = enabled
_STATUS_ENABLED = 1


def fetch_addresses(username: str, password: str) -> list[str]:
    """Authenticate with Proton API and return all enabled email addresses.

    This uses the official ``proton-client`` library which handles the SRP
    authentication handshake.  The call is synchronous because ``proton-client``
    uses ``requests`` under the hood; wrap in ``asyncio.to_thread`` when calling
    from async code.
    """
    session = Session(
        api_url="https://mail.proton.me/api",
        appversion="Other",
        user_agent="ProtonTelegramBot",
        TLSPinning=False,
    )
    session.authenticate(username, password)
    resp: dict[str, Any] = session.api_request("/core/v4/addresses")
    addresses: list[str] = []
    for addr in resp.get("Addresses", []):
        if addr.get("Status") == _STATUS_ENABLED:
            addresses.append(addr["Email"].lower())
    LOGGER.info("fetched %d addresses from Proton API for %s", len(addresses), username)
    return sorted(addresses)
