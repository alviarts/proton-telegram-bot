"""Proton Mail API client for fetching account addresses."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from proton.api import Session
from proton.srp._pysrp import User as PmsrpUser

LOGGER = logging.getLogger(__name__)

# Proton address status: 1 = enabled
_STATUS_ENABLED = 1

_SESSION_DIR = Path("data")
_API_URL = "https://mail.proton.me/api"


@dataclass
class CaptchaChallenge:
    """Holds state needed to complete a CAPTCHA-interrupted auth flow."""

    web_url: str
    token: str
    username: str
    auth_payload: dict[str, str]


class CaptchaRequiredError(Exception):
    """Raised when Proton requires CAPTCHA to proceed with authentication."""

    def __init__(self, challenge: CaptchaChallenge) -> None:
        self.challenge = challenge
        super().__init__(f"CAPTCHA required: {challenge.web_url}")


# ------------------------------------------------------------------ session cache


def _session_path(username: str) -> Path:
    return _SESSION_DIR / f".proton_session_{username}.json"


def _save_session(username: str, session: Session) -> None:
    path = _session_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.dump()))
    LOGGER.info("saved Proton session for %s", username)


def _load_session(username: str) -> Session | None:
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
        api_url=_API_URL,
        appversion="Other",
        user_agent="ProtonTelegramBot",
        TLSPinning=False,
    )


# ------------------------------------------------------------------ addresses


def _get_addresses(session: Session) -> list[str]:
    resp: dict[str, Any] = session.api_request("/core/v4/addresses")
    addresses: list[str] = []
    for addr in resp.get("Addresses", []):
        if addr.get("Status") == _STATUS_ENABLED:
            addresses.append(addr["Email"].lower())
    return sorted(addresses)


# ------------------------------------------------------------------ raw SRP auth


def _build_srp_payload(username: str, password: str) -> dict[str, str]:
    """Perform the SRP handshake up to the client-proof stage.

    Returns the JSON payload ready to POST to ``/auth``.  If the POST
    returns error 9001 (CAPTCHA), the caller can retry with the same
    payload plus human-verification headers.
    """
    http = requests.Session()
    headers = {"x-pm-appversion": "Other", "User-Agent": "ProtonTelegramBot"}

    info = http.post(f"{_API_URL}/auth/info", json={"Username": username}, headers=headers)
    info.raise_for_status()
    info_data = info.json()

    modulus_b64 = info_data["Modulus"].split("-----")[2].strip()
    modulus = base64.b64decode(modulus_b64)
    server_challenge = base64.b64decode(info_data["ServerEphemeral"])
    salt = base64.b64decode(info_data["Salt"])
    version = info_data["Version"]

    usr = PmsrpUser(password, modulus)
    client_challenge = usr.get_challenge()
    client_proof = usr.process_challenge(salt, server_challenge, version)
    if client_proof is None:
        raise ValueError("SRP challenge computation failed")

    return {
        "Username": username,
        "ClientEphemeral": base64.b64encode(client_challenge).decode(),
        "ClientProof": base64.b64encode(client_proof).decode(),
        "SRPSession": info_data["SRPSession"],
    }


def _post_auth(
    payload: dict[str, str],
    captcha_token: str | None = None,
) -> dict[str, Any]:
    """POST to ``/auth`` and return the JSON response.

    When *captcha_token* is provided the human-verification headers are
    included so the server accepts the previously-blocked request.
    """
    headers: dict[str, str] = {
        "x-pm-appversion": "Other",
        "User-Agent": "ProtonTelegramBot",
    }
    if captcha_token is not None:
        headers["x-pm-human-verification-token"] = captcha_token
        headers["x-pm-human-verification-token-type"] = "captcha"

    resp = requests.post(f"{_API_URL}/auth", json=payload, headers=headers)
    return resp.json()


# ------------------------------------------------------------------ public API


def start_auth(username: str, password: str) -> CaptchaChallenge | Session:
    """Begin authentication.  Returns a ready `Session` or a `CaptchaChallenge`.

    If Proton demands a CAPTCHA, the caller should present the
    ``CaptchaChallenge.web_url`` to the user and, after the user solves
    it, call ``complete_auth_with_captcha``.
    """
    # Try cached session first
    session = _load_session(username)
    if session is not None:
        try:
            _get_addresses(session)  # quick sanity check
            return session
        except Exception:
            LOGGER.info("cached session expired for %s", username)

    payload = _build_srp_payload(username, password)
    result = _post_auth(payload)

    if result.get("Code") == 9001:
        details = result.get("Details", {})
        return CaptchaChallenge(
            web_url=details.get("WebUrl", ""),
            token=details.get("HumanVerificationToken", ""),
            username=username,
            auth_payload=payload,
        )

    if result.get("Code") != 1000:
        raise ValueError(result.get("Error", "Authentication failed"))

    # Build a Session object with the auth tokens
    session = _make_session()
    session._session_data = {
        "UID": result["UID"],
        "AccessToken": result["AccessToken"],
        "RefreshToken": result["RefreshToken"],
        "Scope": result["Scope"].split(),
    }
    session.s.headers["x-pm-uid"] = result["UID"]
    session.s.headers["Authorization"] = f"Bearer {result['AccessToken']}"

    _save_session(username, session)
    return session


def complete_auth_with_captcha(challenge: CaptchaChallenge, captcha_token: str) -> Session:
    """Finish authentication after the user solved the CAPTCHA."""
    result = _post_auth(challenge.auth_payload, captcha_token=captcha_token)

    if result.get("Code") != 1000:
        raise ValueError(result.get("Error", "Authentication failed after CAPTCHA"))

    session = _make_session()
    session._session_data = {
        "UID": result["UID"],
        "AccessToken": result["AccessToken"],
        "RefreshToken": result["RefreshToken"],
        "Scope": result["Scope"].split(),
    }
    session.s.headers["x-pm-uid"] = result["UID"]
    session.s.headers["Authorization"] = f"Bearer {result['AccessToken']}"

    _save_session(challenge.username, session)
    return session


def fetch_addresses_from_session(session: Session) -> list[str]:
    """Return all enabled email addresses using an authenticated session."""
    return _get_addresses(session)


def fetch_addresses(username: str, password: str) -> list[str]:
    """Authenticate and return all enabled email addresses.

    Raises ``CaptchaRequiredError`` if Proton demands human verification.
    """
    result = start_auth(username, password)
    if isinstance(result, CaptchaChallenge):
        raise CaptchaRequiredError(result)
    return _get_addresses(result)
