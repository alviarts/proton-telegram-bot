"""Unit tests for the pure helpers in :mod:`proton_browser`.

The Playwright-driven flows are deliberately not exercised here — they live
behind a browser-shaped factory that the orchestrator tests inject.
"""
from __future__ import annotations

import pytest

from proton_telegram_bot.proton_browser import (
    AddressCreationResult,
    CreationStatus,
    _addresses_url,
    _classify_error,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("address already exists", CreationStatus.ALREADY_EXISTS),
        ("This name is taken", CreationStatus.ALREADY_EXISTS),
        ("Alamat sudah ada", CreationStatus.ALREADY_EXISTS),
        ("Email ini sudah terdaftar", CreationStatus.ALREADY_EXISTS),
        ("You have reached your address limit", CreationStatus.LIMIT_REACHED),
        ("Plan quota exceeded", CreationStatus.LIMIT_REACHED),
        ("maximum number of addresses", CreationStatus.LIMIT_REACHED),
        # Indonesian phrasings -- the user's locale flips toast text.
        ("Anda sudah mencapai jumlah maksimum alamat", CreationStatus.LIMIT_REACHED),
        ("Sudah mencapai batas maksimal", CreationStatus.LIMIT_REACHED),
        ("Daftar alamat sudah penuh", CreationStatus.LIMIT_REACHED),
        ("invalid password provided", CreationStatus.AUTH_FAILED),
        ("authentication required", CreationStatus.AUTH_FAILED),
        ("Kata sandi salah", CreationStatus.AUTH_FAILED),
        ("unknown server error", CreationStatus.ERROR),
        ("", CreationStatus.ERROR),
    ],
)
def test_classify_error(text: str, expected: CreationStatus) -> None:
    assert _classify_error(text) == expected


def test_addresses_url_default_user_index() -> None:
    assert _addresses_url(0).startswith("https://account.proton.me/u/0/")


def test_addresses_url_custom_user_index() -> None:
    assert "/u/7/" in _addresses_url(7)


def test_address_creation_result_helpers() -> None:
    ok = AddressCreationResult(local="vielz001", domain="proton.me", status=CreationStatus.SUCCESS)
    assert ok.success is True
    assert ok.email == "vielz001@proton.me"

    err = AddressCreationResult(
        local="vielz002",
        domain="proton.me",
        status=CreationStatus.ALREADY_EXISTS,
        detail="taken",
    )
    assert err.success is False
    assert err.email == "vielz002@proton.me"
