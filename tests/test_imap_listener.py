"""Tests for the IMAP listener.

These tests deliberately stay close to mechanical behavior — driving a real
``IMAP4`` session in tests would require a live IMAP server. Instead we:

1. Lock in the public ``aioimaplib`` API the listener depends on, so a future
   library upgrade or a typo immediately fails CI rather than at runtime.
2. Verify the connection is always closed (``logout``) even if the very early
   handshake (``wait_hello_from_server``/``login``) fails — i.e. no leaked
   socket per failed reconnect.
3. Verify ``_fetch_new_messages`` parses UIDs and dispatches each new message
   exactly once, advancing ``_last_seen_uid`` afterwards.
"""
from __future__ import annotations

from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from aioimaplib import aioimaplib

from proton_telegram_bot.imap_listener import IMAPListener
from proton_telegram_bot.models import BridgeCredentials

# --------------------------------------------------------------------------- #
# 1. Lock in the aioimaplib public API the listener relies on.
# --------------------------------------------------------------------------- #


def test_aioimaplib_public_api_exists() -> None:
    """The names the listener uses must exist on ``IMAP4`` (the public class)."""
    for name in (
        "wait_hello_from_server",
        "login",
        "select",
        "uid_search",
        "uid",
        "noop",
        "logout",
    ):
        assert hasattr(aioimaplib.IMAP4, name), f"aioimaplib.IMAP4 lost {name}()"


def test_listener_does_not_use_protocol_internal_names() -> None:
    """``has_pending_idle_command`` and ``idle_queue`` only exist on the internal
    protocol class, not on ``IMAP4``. Reaching for them caused a runtime
    ``AttributeError`` in the first revision of this listener (PR #1)."""
    src = (
        Path(__file__).parent.parent
        / "src/proton_telegram_bot/imap_listener.py"
    ).read_text()
    assert "has_pending_idle_command" not in src
    assert ".idle_queue" not in src
    # Polling mode uses noop() instead of IDLE.
    assert "noop()" in src
    assert "POLL_INTERVAL_SECONDS" in src


# --------------------------------------------------------------------------- #
# 2. Connection-leak guard: failure in handshake must still call logout().
# --------------------------------------------------------------------------- #


class _LoginFailingClient:
    def __init__(self) -> None:
        self.logged_out = False

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, *_: Any) -> None:
        raise RuntimeError("invalid credentials")

    async def logout(self) -> None:
        self.logged_out = True


@pytest.mark.asyncio
async def test_login_failure_still_closes_connection() -> None:
    fake = _LoginFailingClient()

    async def on_new(*_: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("on_new must not be called when login fails")

    creds = BridgeCredentials(host="127.0.0.1", port=1143, username="u", password="p")
    listener = IMAPListener(chat_id=1, credentials=creds, on_new_message=on_new)
    listener._build_client = lambda: fake  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="invalid credentials"):
        await listener._run_session()
    assert fake.logged_out is True, "must always call logout(), even on early failure"


# --------------------------------------------------------------------------- #
# 3. Fetch-and-dispatch behavior: new UIDs are parsed and forwarded once.
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, result: str, lines: list[bytes]) -> None:
        self.result = result
        self.lines = lines


class _FetchOnlyClient:
    """Stub that satisfies just the calls ``_fetch_new_messages`` makes."""

    def __init__(
        self,
        search_lines: list[bytes],
        fetch_payloads: dict[int, bytes],
    ) -> None:
        self._search_lines = search_lines
        self._fetch_payloads = fetch_payloads

    async def uid_search(self, _criteria: str) -> _Response:
        return _Response("OK", self._search_lines)

    async def uid(self, command: str, uid: str, *_: Any) -> _Response:
        assert command == "fetch"
        payload = self._fetch_payloads[int(uid)]
        return _Response(
            "OK",
            [
                f"* {uid} FETCH (UID {uid} RFC822 {{{len(payload)}}})".encode(),
                payload,
                b")",
            ],
        )


@pytest.mark.asyncio
async def test_fetch_new_messages_dispatches_each_uid_once() -> None:
    received: list[tuple[int, Message, str]] = []

    async def on_new(chat_id: int, msg: Message, uid: str) -> None:
        received.append((chat_id, msg, uid))

    creds = BridgeCredentials(host="127.0.0.1", port=1143, username="u", password="p")
    listener = IMAPListener(chat_id=42, credentials=creds, on_new_message=on_new)
    listener._last_seen_uid = 5
    fake = _FetchOnlyClient(
        search_lines=[b"6 7"],
        fetch_payloads={
            6: b"From: a@x.example\r\nTo: target@proton.me\r\nSubject: Hi\r\n\r\nbody-6",
            7: b"From: b@x.example\r\nTo: other@proton.me\r\nSubject: Hi2\r\n\r\nbody-7",
        },
    )

    await listener._fetch_new_messages(fake)  # type: ignore[arg-type]

    assert [uid for _, _, uid in received] == ["6", "7"]
    assert listener._last_seen_uid == 7
    # Subjects are correctly parsed by ``email_parser``.
    assert [msg["Subject"] for _, msg, _ in received] == ["Hi", "Hi2"]


class _BaselineProbeClient:
    """Records each call to ``uid_search`` so we can assert it wasn't issued."""

    def __init__(self, uids_to_return: bytes) -> None:
        self._uids_to_return = uids_to_return
        self.searches: list[str] = []

    async def uid_search(self, criteria: str) -> _Response:
        self.searches.append(criteria)
        return _Response("OK", [self._uids_to_return])


@pytest.mark.asyncio
async def test_baseline_is_preserved_across_reconnects() -> None:
    """On the first successful session the listener seeds the baseline from
    ``UID SEARCH ALL``; on reconnect it must NOT re-seed, otherwise UIDs that
    arrived while the connection was down get folded into the new baseline and
    are silently lost (regression of Devin Review BUG_*_0001)."""
    creds = BridgeCredentials(host="127.0.0.1", port=1143, username="u", password="p")
    listener = IMAPListener(
        chat_id=1, credentials=creds, on_new_message=lambda *_: _async_noop()
    )

    # First connection: empty mailbox → baseline 0.
    fake1 = _BaselineProbeClient(b"")
    await listener._initialize_uid_baseline(fake1)  # type: ignore[arg-type]
    assert listener._last_seen_uid == 0
    assert fake1.searches == ["ALL"]

    # First message arrives in this session — listener advances watermark to 101.
    listener._last_seen_uid = 101

    # Connection drops. While disconnected, UIDs 102 and 103 arrive on the server.
    # On reconnect, _initialize_uid_baseline must NOT issue UID SEARCH ALL —
    # otherwise it would set _last_seen_uid to 103 and _fetch_new_messages
    # (which searches UID > _last_seen_uid) would never see 102 or 103.
    fake2 = _BaselineProbeClient(b"102 103")
    await listener._initialize_uid_baseline(fake2)  # type: ignore[arg-type]
    assert fake2.searches == [], "must not re-seed baseline on reconnect"
    assert listener._last_seen_uid == 101


async def _async_noop() -> None:
    return None


@pytest.mark.asyncio
async def test_fetch_skips_uids_already_seen() -> None:
    received: list[str] = []

    async def on_new(_chat_id: int, _msg: Message, uid: str) -> None:
        received.append(uid)

    creds = BridgeCredentials(host="127.0.0.1", port=1143, username="u", password="p")
    listener = IMAPListener(chat_id=1, credentials=creds, on_new_message=on_new)
    listener._last_seen_uid = 10
    fake = _FetchOnlyClient(
        search_lines=[b"7 11"],  # 7 is below the baseline; only 11 is new
        fetch_payloads={
            11: b"From: x@y.example\r\nTo: a@proton.me\r\nSubject: New\r\n\r\nbody",
        },
    )

    await listener._fetch_new_messages(fake)  # type: ignore[arg-type]

    assert received == ["11"]
    assert listener._last_seen_uid == 11
