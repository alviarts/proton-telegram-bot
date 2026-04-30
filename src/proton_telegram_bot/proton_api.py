"""Proton Mail API client for fetching account addresses."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from proton.api import Session

LOGGER = logging.getLogger(__name__)

# Proton address status: 1 = enabled
_STATUS_ENABLED = 1

_SESSION_DIR = Path("data")


def _session_path(username: str) -> Path:
    return _SESSION_DIR / f".proton_session_{username}.json"


def _save_session(username: str, session: Session) -> None:
    """Persist session to disk so future calls skip authentication."""
    path = _session_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.dump()))
    LOGGER.info("saved Proton session for %s", username)


def _load_session(username: str) -> Session | None:
    """Try to restore a previously saved session."""
    path = _session_path(username)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        session = Session.load(data, TLSPinning=False)
        LOGGER.info("loaded cached Proton session for %s", username)
        return session
    except Exception:
        LOGGER.warning("failed to load cached session for %s, will re-auth", username)
        path.unlink(missing_ok=True)
        return None


def _make_session() -> Session:
    return Session(
        api_url="https://mail.proton.me/api",
        appversion="Other",
        user_agent="ProtonTelegramBot",
        TLSPinning=False,
    )


def _get_addresses(session: Session) -> list[str]:
    resp: dict[str, Any] = session.api_request("/core/v4/addresses")
    addresses: list[str] = []
    for addr in resp.get("Addresses", []):
        if addr.get("Status") == _STATUS_ENABLED:
            addresses.append(addr["Email"].lower())
    return sorted(addresses)


def fetch_addresses(username: str, password: str) -> list[str]:
    """Authenticate with Proton API and return all enabled email addresses.

    Tries to reuse a cached session first.  Falls back to fresh
    authentication when no cache exists or the cache is stale.
    """
    # Try cached session first
    session = _load_session(username)
    if session is not None:
        try:
            addresses = _get_addresses(session)
            LOGGER.info(
                "fetched %d addresses from cached session for %s",
                len(addresses),
                username,
            )
            return addresses
        except Exception:
            LOGGER.info("cached session expired for %s, re-authenticating", username)

    # Fresh authentication
    session = _make_session()
    session.authenticate(username, password)
    _save_session(username, session)
    addresses = _get_addresses(session)
    LOGGER.info("fetched %d addresses from Proton API for %s", len(addresses), username)
    return addresses
