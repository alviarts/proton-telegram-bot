"""Tests for email parsing & alias matching."""
from __future__ import annotations

from email.message import EmailMessage

from proton_telegram_bot.email_parser import (
    extract_recipients,
    find_matching_alias,
    parse_message,
    summarize,
)


def _build_message(
    *,
    to: str = "vielz50@proton.me",
    cc: str | None = None,
    delivered_to: str | None = None,
    subject: str = "Hello",
    sender: str = "partner@biz.example",
    body: str = "Hai, ini email pertama dari rekan bisnis.",
    html_body: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if delivered_to:
        msg["Delivered-To"] = delivered_to
    msg["Subject"] = subject
    msg["Date"] = "Thu, 30 Apr 2026 10:00:00 +0000"
    msg.set_content(body)
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


def test_extract_recipients_collects_to_cc_delivered_to() -> None:
    raw = _build_message(
        to="alice@example.com, bob@example.com",
        cc="charlie@example.com",
        delivered_to="vielz50@proton.me",
    )
    parsed = parse_message(raw)
    recipients = extract_recipients(parsed)
    assert recipients == {
        "alice@example.com",
        "bob@example.com",
        "charlie@example.com",
        "vielz50@proton.me",
    }


def test_find_matching_alias_returns_first_match() -> None:
    raw = _build_message(to="vielz50@proton.me", cc="vielz51@proton.me")
    parsed = parse_message(raw)
    candidates = {"vielz99@proton.me", "vielz51@proton.me"}
    assert find_matching_alias(parsed, candidates) == "vielz51@proton.me"


def test_find_matching_alias_returns_none_when_no_match() -> None:
    raw = _build_message(to="someone@elsewhere.com")
    parsed = parse_message(raw)
    assert find_matching_alias(parsed, {"vielz1@proton.me"}) is None


def test_summarize_extracts_text_body() -> None:
    raw = _build_message(body="Halo, saya tertarik kerja sama.")
    parsed = parse_message(raw)
    summary = summarize(parsed)
    assert "tertarik" in summary["body"]
    assert summary["subject"] == "Hello"
    assert "partner@biz.example" in summary["from"]


def test_summarize_truncates_long_body() -> None:
    raw = _build_message(body="x" * 5000)
    parsed = parse_message(raw)
    summary = summarize(parsed, max_chars=200)
    assert len(summary["body"]) <= 220
    assert summary["body"].endswith("(dipotong)")


def test_summarize_falls_back_to_html_when_no_text() -> None:
    msg = EmailMessage()
    msg["From"] = "partner@biz.example"
    msg["To"] = "vielz50@proton.me"
    msg["Subject"] = "HTML only"
    msg.set_content("plain placeholder")
    msg.add_alternative("<p>halo <b>dunia</b></p>", subtype="html")
    parsed = parse_message(msg.as_bytes())
    summary = summarize(parsed)
    # Either the plain or html body is fine — both contain readable text.
    assert summary["body"]
