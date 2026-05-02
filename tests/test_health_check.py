"""Unit tests for the /cekimap health check task and the onboarding
keyboard helpers added alongside it.

The full Bridge SMTP + IMAP round-trip can't run in CI so we mock the
SMTP transport and substitute a fake aioimaplib client. The fake
client matches the surface of ``aioimaplib.IMAP4`` that
``run_health_check`` uses (``wait_hello_from_server``, ``login``,
``select``, ``uid_search``, ``uid("fetch", ...)``, ``uid("store",
...)``, ``expunge``, ``logout``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from proton_telegram_bot import health_check
from proton_telegram_bot.bot import (
    CB_HEALTHCHECK_PICK,
    CB_QUICK_GENADDR,
    CB_QUICK_HEALTHCHECK,
    CB_QUICK_SETPW,
    _build_cekimap_picker_keyboard,
    _build_post_connect_keyboard,
    _build_post_connect_keyboard_with_aliases,
)
from proton_telegram_bot.bridge_admin import BridgeImapCredentials
from proton_telegram_bot.models import PrimaryAccount

# ----------------------------- onboarding keyboards --------------------------


def test_post_connect_keyboard_has_all_three_onboarding_buttons() -> None:
    """Fresh-account keyboard exposes setprotonpw, genaddr, healthcheck."""
    kb = _build_post_connect_keyboard(primary_id=42)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    assert f"{CB_QUICK_SETPW}:42" in callbacks
    assert f"{CB_QUICK_GENADDR}:42:20" in callbacks
    assert f"{CB_QUICK_HEALTHCHECK}:42" in callbacks


def test_post_connect_keyboard_with_aliases_offers_health_check() -> None:
    """Existing-aliases keyboard prioritises the Cek listener button."""
    kb = _build_post_connect_keyboard_with_aliases(primary_id=7, alias_count=20)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    labels = [btn.text for row in rows for btn in row]
    assert f"{CB_QUICK_HEALTHCHECK}:7" in callbacks
    # The label must surface the alias count so the user knows what
    # they're about to validate.
    assert any("20" in label for label in labels)


def test_post_connect_keyboard_with_aliases_offers_genaddr_too() -> None:
    """User wants to be able to extend an existing-aliases account
    without retyping the email — the keyboard must include the same
    "Generate 20 alamat sekarang" button the fresh-account onboarding
    uses, with ``CB_QUICK_GENADDR:<primary_id>:20`` so the existing
    callback router runs the random-suffix /genaddr batch.

    /genaddr and /cekimap use independent ``chat_data`` locks so
    clicking this button while a background health check is running
    is safe.
    """
    kb = _build_post_connect_keyboard_with_aliases(primary_id=7, alias_count=20)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    # All three one-tap actions are reachable.
    assert f"{CB_QUICK_GENADDR}:7:20" in callbacks
    assert f"{CB_QUICK_HEALTHCHECK}:7" in callbacks
    # /list passthrough is still wired to the picker callback.
    assert any(
        cb is not None and cb.startswith("pickp:7") for cb in callbacks
    )


def test_cekimap_picker_lists_all_primaries() -> None:
    """Picker emits one row per primary with its alias count in the label."""
    primaries = [
        PrimaryAccount(
            id=1,
            chat_id=99,
            email="vielz74@proton.me",
            imap_host="127.0.0.1",
            imap_port=1143,
            imap_username="vielz74@proton.me",
            imap_use_ssl=False,
            created_at="2026-05-02T00:00:00Z",
        ),
        PrimaryAccount(
            id=2,
            chat_id=99,
            email="aldohaw@proton.me",
            imap_host="127.0.0.1",
            imap_port=1143,
            imap_username="aldohaw@proton.me",
            imap_use_ssl=False,
            created_at="2026-05-02T00:00:00Z",
        ),
    ]
    counts = {1: 21, 2: 5}
    kb = _build_cekimap_picker_keyboard(primaries, counts)
    rows = kb.inline_keyboard
    callbacks = [btn.callback_data for row in rows for btn in row]
    labels = [btn.text for row in rows for btn in row]
    assert f"{CB_HEALTHCHECK_PICK}:1" in callbacks
    assert f"{CB_HEALTHCHECK_PICK}:2" in callbacks
    assert any("21" in label for label in labels)
    assert any("5" in label for label in labels)


def test_cekimap_picker_handles_empty_primary_list() -> None:
    """Empty list still renders a placeholder row instead of crashing."""
    kb = _build_cekimap_picker_keyboard(primaries=[], counts={})
    rows = kb.inline_keyboard
    assert rows  # at least one row
    labels = [btn.text for row in rows for btn in row]
    assert any("belum ada" in label.lower() for label in labels)


# ----------------------------- run_health_check ------------------------------


@dataclass
class _FakeMessage:
    """Captures every send_message kwargs so tests can assert on them."""
    chat_id: int
    text: str
    parse_mode: Any = None
    message_id: int | None = None


@dataclass
class _FakeEdit:
    """Captures edit_message_text kwargs so tests can verify the
    rolling progress message gets updated in place rather than
    spamming new messages.
    """
    chat_id: int
    message_id: int
    text: str
    parse_mode: Any = None


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []
        self.edits: list[_FakeEdit] = []
        self._next_message_id = 1000

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: Any = None,
    ) -> _FakeMessage:
        self._next_message_id += 1
        msg = _FakeMessage(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            message_id=self._next_message_id,
        )
        self.messages.append(msg)
        return msg

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Any = None,
    ) -> None:
        self.edits.append(
            _FakeEdit(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
            )
        )


class _FakeBridgeAdmin:
    def __init__(self, creds: BridgeImapCredentials | None) -> None:
        self._creds = creds
        self.calls: list[str] = []

    async def fetch_imap_credentials(
        self, email: str
    ) -> BridgeImapCredentials | None:
        self.calls.append(email)
        return self._creds


@dataclass
class _ImapResp:
    result: str
    lines: list[bytes]


class _FakeImap:
    """Lightweight stand-in for ``aioimaplib.IMAP4``.

    Tracks every call so tests can assert on the IMAP traffic, and lets
    a callback "deliver" messages with a synthetic UID so the
    ``UID SEARCH`` / ``UID FETCH`` round trip in
    :func:`health_check._scan_inbox_for_tokens` returns predictable
    UIDs and Subject headers.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        # Map of UID -> raw subject bytes. New "delivered" messages get
        # appended with the next UID.
        self._messages: dict[int, bytes] = {}
        self._next_uid = 100
        self.expunged: list[int] = []
        self.stored: list[tuple[str, str, str]] = []
        self.logged_out = False

    def deliver(self, subject: str) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self._messages[uid] = subject.encode()
        return uid

    async def wait_hello_from_server(self) -> None:
        self.calls.append(("wait_hello", ()))

    async def login(self, username: str, password: str) -> _ImapResp:
        self.calls.append(("login", (username, password)))
        return _ImapResp("OK", [])

    async def select(self, mailbox: str) -> _ImapResp:
        self.calls.append(("select", (mailbox,)))
        return _ImapResp("OK", [])

    async def uid_search(self, query: str) -> _ImapResp:
        self.calls.append(("uid_search", (query,)))
        # The production code calls ``uid_search("ALL")`` once for the
        # baseline, then ``uid_search(f"UID {n+1}:*")`` repeatedly. For
        # the baseline we always start with the empty inbox.
        if query == "ALL":
            return _ImapResp("OK", [b""])  # empty digit-only line
        # Range query: collect every UID we have above the lower bound.
        try:
            after = int(query.split()[1].split(":")[0]) - 1
        except Exception:
            after = 0
        uids = sorted(uid for uid in self._messages if uid > after)
        if not uids:
            return _ImapResp("OK", [b""])
        return _ImapResp("OK", [(" ".join(str(u) for u in uids)).encode()])

    async def uid(self, command: str, *args: str) -> _ImapResp:
        self.calls.append(("uid", (command, *args)))
        if command == "fetch":
            uid_set = args[0]
            uids = [
                int(token) for token in uid_set.split(",") if token.isdigit()
            ]
            lines: list[bytes] = []
            for uid in uids:
                subject = self._messages.get(uid)
                if subject is None:
                    continue
                # Imitate aioimaplib's per-FETCH framing. The closing
                # line carries ``UID <n>)`` because the production
                # parser pairs each token with the UID that follows
                # it inside the same FETCH chunk.
                lines.append(
                    f"* {uid} FETCH (BODY[HEADER.FIELDS (SUBJECT)] {{}}".encode()
                )
                lines.append(b"Subject: " + subject)
                lines.append(f" UID {uid})".encode())
            lines.append(b"OK FETCH completed")
            return _ImapResp("OK", lines)
        if command == "store":
            self.stored.append((args[0], args[1], args[2]))
            return _ImapResp("OK", [])
        if command == "search":
            return _ImapResp("OK", [b""])
        return _ImapResp("OK", [])

    async def expunge(self) -> _ImapResp:
        self.calls.append(("expunge", ()))
        self.expunged.extend(sorted(self._messages))
        return _ImapResp("OK", [])

    async def logout(self) -> _ImapResp:
        self.calls.append(("logout", ()))
        self.logged_out = True
        return _ImapResp("OK", [])


async def test_run_health_check_happy_path_via_bridge_imap() -> None:
    """Happy path: every alias's SMTP send lands in the primary's
    INBOX (faked via ``_FakeImap``) and the rolling progress message is
    edited in place rather than spamming one ✅ per alias."""
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=1,
        chat_id=42,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    fake_imap = _FakeImap()

    def _fake_smtp_send(**kw: Any) -> None:
        # Simulate Proton-internal alias→primary delivery: every send
        # immediately appears in the primary's INBOX with the same
        # Subject we just sent out.
        fake_imap.deliver(kw["subject"])

    async def _open_imap(**_kw: Any) -> _FakeImap:
        return fake_imap

    targets = ["vielz74@proton.me", "vielz001@proton.me", "vielz002@proton.me"]

    with (
        patch.object(health_check, "_open_imap", _open_imap),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 5),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=42,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # Static messages: starter + initial progress + final summary.
    assert len(bot.messages) == 3
    starter, _progress_initial, summary = (m.text for m in bot.messages)
    assert "Health check" in starter
    assert "vielz74@proton.me" in starter
    assert "selesai" in summary.lower()
    assert f"{len(targets)}/{len(targets)}" in summary

    # Rolling progress edited in place; final edit reports full success.
    assert bot.edits, "rolling progress message must be edited in place"
    final_edit_text = bot.edits[-1].text
    assert f"{len(targets)}/{len(targets)}" in final_edit_text
    progress_id = bot.messages[1].message_id
    for edit in bot.edits:
        assert edit.message_id == progress_id

    # Bridge IMAP was used: baseline + range UID searches, at least one
    # UID FETCH, and a final logout. (login/select happen inside
    # ``_open_imap`` which is itself patched in this test, so we only
    # assert the calls that go through the returned client.)
    call_names = [c[0] for c in fake_imap.calls]
    assert "logout" in call_names
    assert any(c[0] == "uid_search" for c in fake_imap.calls)
    assert any(c[0] == "uid" and c[1][0] == "fetch" for c in fake_imap.calls)
    # Cleanup: STORE \\Deleted + EXPUNGE removes the health-check noise.
    assert fake_imap.stored, "expected STORE \\Deleted on detected UIDs"
    assert fake_imap.expunged


async def test_run_health_check_marks_unanswered_aliases_as_failed() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=2,
        chat_id=43,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    fake_imap = _FakeImap()

    delivered_for_alias = "vielz001@proton.me"
    broken_alias = "vielzbroken@proton.me"

    def _fake_smtp_send(**kw: Any) -> None:
        if delivered_for_alias in kw["subject"]:
            fake_imap.deliver(kw["subject"])

    async def _open_imap(**_kw: Any) -> _FakeImap:
        return fake_imap

    targets = [delivered_for_alias, broken_alias]

    with (
        patch.object(health_check, "_open_imap", _open_imap),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=43,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    summary = bot.messages[-1].text
    assert "1/2" in summary
    assert broken_alias in summary


async def test_run_health_check_aborts_when_bridge_admin_unavailable() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=3,
        chat_id=44,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )

    await health_check.run_health_check(
        bot=bot,
        chat_id=44,
        db=None,  # type: ignore[arg-type]
        bridge_admin=None,
        primary=primary,
        targets=["vielz74@proton.me"],
    )

    assert len(bot.messages) == 1
    assert "Bridge admin nonaktif" in bot.messages[0].text


async def test_run_health_check_handles_send_failures_per_alias() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=4,
        chat_id=45,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    fake_imap = _FakeImap()

    def _fake_smtp_send(**_kw: Any) -> None:
        raise RuntimeError("Bridge SMTP refused: alias not found")

    async def _open_imap(**_kw: Any) -> _FakeImap:
        return fake_imap

    targets = ["vielz74@proton.me", "vielz999@proton.me"]

    with (
        patch.object(health_check, "_open_imap", _open_imap),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=45,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # Every send raised → 0/2 (or "Tidak ada email yang berhasil dikirim").
    summary = bot.messages[-1].text
    haystack_lower = summary.lower()
    assert "0/2" in summary or "tidak ada email" in haystack_lower
    haystack = summary + " ".join(e.text for e in bot.edits)
    assert "vielz74@proton.me" in haystack
    assert "vielz999@proton.me" in haystack


async def test_run_health_check_dedupes_targets() -> None:
    bot = _FakeBot()
    primary = PrimaryAccount(
        id=5,
        chat_id=46,
        email="vielz74@proton.me",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username="vielz74@proton.me",
        imap_use_ssl=False,
        created_at="2026-05-02T00:00:00Z",
    )
    creds = BridgeImapCredentials(
        email="vielz74@proton.me",
        imap_username="vielz74@proton.me",
        imap_password="bridge-pw",
    )
    admin = _FakeBridgeAdmin(creds)
    fake_imap = _FakeImap()

    sent: list[str] = []

    def _fake_smtp_send(**kw: Any) -> None:
        sent.append(kw["from_addr"])
        fake_imap.deliver(kw["subject"])

    async def _open_imap(**_kw: Any) -> _FakeImap:
        return fake_imap

    # Primary is in the list twice, plus uppercase variant — dedupe to 1.
    targets = ["vielz74@proton.me", "VIELZ74@proton.me", "vielz74@proton.me"]

    with (
        patch.object(health_check, "_open_imap", _open_imap),
        patch.object(health_check, "_smtp_send", _fake_smtp_send),
        patch.object(health_check, "HEALTH_CHECK_RECEIVE_TIMEOUT_S", 1),
        patch.object(health_check, "HEALTH_CHECK_POLL_INTERVAL_S", 0.01),
        patch.object(health_check, "PROGRESS_EDIT_INTERVAL_S", 0.0),
    ):
        await health_check.run_health_check(
            bot=bot,
            chat_id=46,
            db=None,  # type: ignore[arg-type]
            bridge_admin=admin,  # type: ignore[arg-type]
            primary=primary,
            targets=targets,
        )

    # Only one SMTP call (rest were dupes), and the summary reports 1/1.
    assert len(sent) == 1
    summary = bot.messages[-1].text
    assert "1/1" in summary


@pytest.mark.parametrize(
    "blob, expected",
    [
        (b"Subject: [health-check] abc1234567890def vielz@proton.me", "abc1234567890def"),
        (b"Subject: unrelated mail", None),
        (b"", None),
    ],
)
def test_extract_token_from_header_blob(
    blob: bytes, expected: str | None
) -> None:
    """``_extract_token_from_header_blob`` returns the lowercase 16-hex
    token only when the blob looks like a health-check tag.
    """
    assert health_check._extract_token_from_header_blob(blob) == expected


def test_token_uid_pair_re_matches_real_aioimaplib_wire_format() -> None:
    """Regression for production /cekimap returning 0/N: aioimaplib
    splits each FETCH into a header line, a ``bytearray`` literal
    payload, and a closing line containing ``UID <n>)``. The previous
    parser required ``* `` at the start of the line and looked for the
    Subject blob in a separate iteration step, which silently dropped
    every match. The regex must pair token → UID across the joined
    blob regardless of how aioimaplib slices the response.
    """
    fetch_lines: list[bytes | bytearray] = [
        b"3 FETCH (BODY[HEADER.FIELDS (SUBJECT)] {63}",
        bytearray(
            b"Subject: [health-check] ce819f05206f43d0 vielz883@proton.me\r\n\r\n"
        ),
        b" UID 3)",
        b"4 FETCH (BODY[HEADER.FIELDS (SUBJECT)] {66}",
        bytearray(
            b"Subject: [health-check] 4001997d9e931de6 vielz883001@proton.me\r\n\r\n"
        ),
        b" UID 4)",
        b"OK FETCH completed.",
    ]
    blob = b"\n".join(health_check._coerce_to_bytes(line) for line in fetch_lines)
    matches = list(health_check._TOKEN_UID_PAIR_RE.finditer(blob))
    assert [(m.group(1).decode(), int(m.group(2))) for m in matches] == [
        ("ce819f05206f43d0", 3),
        ("4001997d9e931de6", 4),
    ]
