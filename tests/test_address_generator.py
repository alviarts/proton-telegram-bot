"""End-to-end orchestration tests for :mod:`address_generator`.

A fake browser is injected via ``browser_factory`` so we never actually
launch Chromium. The fake records every ``create_address`` call and lets the
test script the outcome of each call.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from proton_telegram_bot.address_generator import (
    AddressGenerationError,
    run_batch,
)
from proton_telegram_bot.alias_gen import GenState
from proton_telegram_bot.crypto import CredentialCipher
from proton_telegram_bot.db import Database
from proton_telegram_bot.proton_browser import (
    AddressCreationResult,
    CaptchaInterruptError,
    CreationStatus,
    LoginFailedError,
)

# ---------------------------------------------------------------- fakes


@dataclass
class FakeBrowser:
    """Records create_address calls and returns scripted statuses."""

    statuses: list[CreationStatus]
    calls: list[str] = field(default_factory=list)

    async def create_address(
        self,
        *,
        local: str,
        domain: str,
        display_name: str | None = None,
        password_for_keygen: str | None = None,
    ) -> AddressCreationResult:
        self.calls.append(local)
        if not self.statuses:
            status = CreationStatus.SUCCESS
        else:
            status = self.statuses.pop(0)
        return AddressCreationResult(local=local, domain=domain, status=status)


def _factory(statuses: list[CreationStatus]) -> tuple[FakeBrowser, object]:
    """Return (browser, factory_callable). The factory yields the same browser
    instance so tests can inspect ``browser.calls`` after run_batch returns."""
    fake = FakeBrowser(statuses=list(statuses))

    @asynccontextmanager
    async def factory(email: str, password: str):
        yield fake

    return fake, factory


def _login_failed_factory():
    @asynccontextmanager
    async def factory(email: str, password: str):
        raise LoginFailedError("bad password")
        yield  # pragma: no cover

    return factory


def _captcha_at_login_factory():
    @asynccontextmanager
    async def factory(email: str, password: str):
        raise CaptchaInterruptError(page_url="https://account.proton.me/login")
        yield  # pragma: no cover

    return factory


@dataclass
class CaptchaMidwayBrowser:
    """Browser that succeeds for the first N calls then raises a captcha."""

    succeed_count: int
    calls: list[str] = field(default_factory=list)

    async def create_address(
        self, *, local: str, domain: str, **_: object
    ) -> AddressCreationResult:
        self.calls.append(local)
        if len(self.calls) <= self.succeed_count:
            return AddressCreationResult(
                local=local, domain=domain, status=CreationStatus.SUCCESS
            )
        raise CaptchaInterruptError(page_url="https://account.proton.me/x")


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(CredentialCipher.generate_key())


@pytest.fixture
async def db_and_primary(tmp_path: Path, cipher: CredentialCipher):
    db = Database(tmp_path / "bot.sqlite3")
    await db.connect()
    chat_id = 7
    primary_id = await db.add_primary_account(
        chat_id=chat_id,
        email="vielz45@proton.me",
        host="127.0.0.1",
        port=1143,
        username="vielz45@proton.me",
        encrypted_password="bridge-pw",
        use_ssl=False,
    )
    await db.set_proton_password(
        chat_id, primary_id, cipher.encrypt("master-password")
    )
    primary = await db.get_primary_account_by_id(primary_id)
    assert primary is not None
    try:
        yield db, chat_id, primary
    finally:
        await db.close()


# ---------------------------------------------------------------- success paths


async def test_run_batch_full_success(db_and_primary, cipher: CredentialCipher) -> None:
    db, chat_id, primary = db_and_primary
    browser, factory = _factory([CreationStatus.SUCCESS] * 5)

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=5,
        domain="proton.me",
        browser_factory=factory,
    )

    assert browser.calls == ["vielz001", "vielz002", "vielz003", "vielz004", "vielz005"]
    assert len(summary.created) == 5
    assert summary.failed == []

    # Aliases were inserted into the DB scoped to the primary.
    aliases = await db.list_aliases(chat_id, primary_id=primary.id)
    assert sorted(a.email for a in aliases) == [
        "vielz001@proton.me",
        "vielz002@proton.me",
        "vielz003@proton.me",
        "vielz004@proton.me",
        "vielz005@proton.me",
    ]

    # Cursor advanced to "vielz006".
    state = await db.get_generator_state(chat_id, primary.id, "vielz")
    assert state == GenState("", 6)


async def test_run_batch_resumes_from_existing_state(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    await db.save_generator_state(chat_id, primary.id, "vielz", GenState("", 22))
    _, factory = _factory([CreationStatus.SUCCESS] * 3)

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=factory,
    )

    assert [r.email for r in summary.created] == [
        "vielz022@proton.me",
        "vielz023@proton.me",
        "vielz024@proton.me",
    ]
    assert await db.get_generator_state(chat_id, primary.id, "vielz") == GenState("", 25)


async def test_progress_callback_invoked_per_address(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    _, factory = _factory([CreationStatus.SUCCESS] * 3)

    seen: list[tuple[int, int, str]] = []

    async def cb(index: int, total: int, result: AddressCreationResult) -> None:
        seen.append((index, total, result.email))

    await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=factory,
        progress=cb,
    )

    assert seen == [
        (1, 3, "vielz001@proton.me"),
        (2, 3, "vielz002@proton.me"),
        (3, 3, "vielz003@proton.me"),
    ]


# ---------------------------------------------------------------- partial / abort paths


async def test_already_exists_advances_cursor_but_not_aliases(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    _, factory = _factory(
        [
            CreationStatus.SUCCESS,
            CreationStatus.ALREADY_EXISTS,
            CreationStatus.SUCCESS,
        ]
    )

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=factory,
    )

    assert len(summary.created) == 2
    assert len(summary.already_existing) == 1

    # Only the *successful* addresses are stored as aliases — duplicates that
    # already exist on Proton's side aren't re-inserted (they would already
    # be in /sync's view).
    aliases = await db.list_aliases(chat_id, primary_id=primary.id)
    assert sorted(a.email for a in aliases) == [
        "vielz001@proton.me",
        "vielz003@proton.me",
    ]

    # Cursor still advanced past the duplicate (Proton accepted that name).
    assert await db.get_generator_state(chat_id, primary.id, "vielz") == GenState("", 4)


async def test_error_does_not_advance_cursor(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    _, factory = _factory(
        [CreationStatus.SUCCESS, CreationStatus.ERROR, CreationStatus.SUCCESS]
    )

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=factory,
    )

    assert len(summary.created) == 2
    assert len(summary.failed) == 1

    # Two acceptances → cursor at 003, not 004 — the failed name is retried
    # next time.
    assert await db.get_generator_state(chat_id, primary.id, "vielz") == GenState("", 3)


async def test_limit_reached_aborts_batch(db_and_primary, cipher: CredentialCipher) -> None:
    db, chat_id, primary = db_and_primary
    _, factory = _factory(
        [
            CreationStatus.SUCCESS,
            CreationStatus.LIMIT_REACHED,
            CreationStatus.SUCCESS,  # Should never be reached
        ]
    )

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=factory,
    )

    assert len(summary.results) == 2
    assert summary.aborted_reason is not None
    assert "limit" in summary.aborted_reason.lower()


async def test_captcha_midway_aborts_and_records_position(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    midway = CaptchaMidwayBrowser(succeed_count=2)

    @asynccontextmanager
    async def factory(email: str, password: str):
        yield midway

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=5,
        domain="proton.me",
        browser_factory=factory,
    )

    assert summary.captcha_interrupted_at == "vielz003"
    assert len(summary.created) == 2
    # Cursor moved past the two successes only.
    assert await db.get_generator_state(chat_id, primary.id, "vielz") == GenState("", 3)


async def test_login_failed_raises(db_and_primary, cipher: CredentialCipher) -> None:
    db, chat_id, primary = db_and_primary

    with pytest.raises(AddressGenerationError, match="login failed"):
        await run_batch(
            db=db,
            cipher=cipher,
            chat_id=chat_id,
            primary=primary,
            base="vielz",
            count=3,
            domain="proton.me",
            browser_factory=_login_failed_factory(),
        )

    # No aliases inserted, no cursor change.
    assert await db.get_generator_state(chat_id, primary.id, "vielz") == GenState("", 1)


async def test_captcha_at_login_records_reason(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary

    summary = await run_batch(
        db=db,
        cipher=cipher,
        chat_id=chat_id,
        primary=primary,
        base="vielz",
        count=3,
        domain="proton.me",
        browser_factory=_captcha_at_login_factory(),
    )

    assert summary.aborted_reason is not None
    assert "captcha" in summary.aborted_reason.lower()
    assert summary.results == []


# ---------------------------------------------------------------- preflight errors


async def test_missing_proton_password_raises(
    tmp_path: Path, cipher: CredentialCipher
) -> None:
    db = Database(tmp_path / "x.sqlite3")
    await db.connect()
    try:
        chat_id = 9
        primary_id = await db.add_primary_account(
            chat_id=chat_id,
            email="x@proton.me",
            host="127.0.0.1",
            port=1143,
            username="x@proton.me",
            encrypted_password="bridge",
            use_ssl=False,
        )
        primary = await db.get_primary_account_by_id(primary_id)
        assert primary is not None

        _, factory = _factory([CreationStatus.SUCCESS])
        with pytest.raises(AddressGenerationError, match="setprotonpw"):
            await run_batch(
                db=db,
                cipher=cipher,
                chat_id=chat_id,
                primary=primary,
                base="v",
                count=1,
                domain="proton.me",
                browser_factory=factory,
            )
    finally:
        await db.close()


async def test_corrupted_proton_password_raises(
    db_and_primary, cipher: CredentialCipher
) -> None:
    db, chat_id, primary = db_and_primary
    # Intentionally store an invalid Fernet token to simulate a key rotation
    # or DB corruption.
    await db.set_proton_password(chat_id, primary.id, "not-a-valid-token")

    _, factory = _factory([CreationStatus.SUCCESS])
    with pytest.raises(AddressGenerationError, match="decrypt"):
        await run_batch(
            db=db,
            cipher=cipher,
            chat_id=chat_id,
            primary=primary,
            base="vielz",
            count=1,
            domain="proton.me",
            browser_factory=factory,
        )
