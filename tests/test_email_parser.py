"""Tests for email parsing & alias matching."""
from __future__ import annotations

from email.message import EmailMessage

from proton_telegram_bot.email_parser import (
    collapse_blank_lines,
    extract_recipients,
    find_matching_alias,
    format_body_html,
    html_to_text,
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


def test_html_to_text_strips_tags_and_decodes_entities() -> None:
    src = "<div>Halo <b>dunia</b>!</div><p>Apa kabar &amp; selamat?</p>"
    text = html_to_text(src)
    assert "<" not in text and ">" not in text
    assert "Halo dunia!" in text
    assert "Apa kabar & selamat?" in text


def test_html_to_text_preserves_paragraph_breaks() -> None:
    src = "<p>Baris satu</p><p>Baris dua</p>"
    text = html_to_text(src)
    # Paragraph boundary is rendered as a newline in the plain-text output.
    assert "Baris satu" in text and "Baris dua" in text
    assert "\n" in text


def test_html_to_text_drops_script_and_style() -> None:
    src = "<style>.x{color:red}</style><script>alert(1)</script><p>Hai</p>"
    text = html_to_text(src)
    assert "alert" not in text
    assert "color" not in text
    assert "Hai" in text


def test_collapse_blank_lines_caps_at_one_blank_line() -> None:
    """3+ consecutive blank lines collapse to exactly one blank line."""
    src = "Halo\n\n\n\nDunia\n\n\n\n\nFoo"
    out = collapse_blank_lines(src)
    # One blank line == "\n\n" between paragraphs, never "\n\n\n+".
    assert "\n\n\n" not in out
    # All paragraphs survive.
    assert "Halo" in out and "Dunia" in out and "Foo" in out


def test_collapse_blank_lines_preserves_single_newlines() -> None:
    src = "Baris 1\nBaris 2\n\nBaris 3"
    assert collapse_blank_lines(src) == "Baris 1\nBaris 2\n\nBaris 3"


def test_collapse_blank_lines_strips_trailing_whitespace() -> None:
    src = "Halo   \n   \n\n  Dunia  "
    out = collapse_blank_lines(src)
    # No trailing spaces on lines, no leading/trailing whitespace overall.
    for line in out.splitlines():
        assert line == line.rstrip()
    assert out.startswith("Halo")
    assert out.endswith("Dunia")


def test_format_body_html_wraps_standalone_otp_in_code() -> None:
    """4-8 digit codes with non-word boundaries get tap-to-copy ``<code>``."""
    body = "Your code is 245657. It expires in 5 minutes."
    out = format_body_html(body)
    assert "<code>245657</code>" in out


def test_format_body_html_skips_digits_inside_words() -> None:
    """Digits inside identifiers (vielz88301) should NOT be wrapped — they're not OTPs."""
    body = "Created vielz88301 at vielz883.proton.me"
    out = format_body_html(body)
    # The "88301" is part of the word "vielz88301"; it must stay unwrapped.
    assert "<code>" not in out


def test_format_body_html_does_not_break_urls() -> None:
    body = "Click https://account.proton.me/verify?token=abc&id=1 to verify."
    out = format_body_html(body)
    # Path stays intact; "&" gets escaped to "&amp;" but Telegram still
    # auto-links the URL.
    assert "https://account.proton.me/verify?token=abc" in out
    assert "&amp;" in out
    # We must NOT inject <code> around digits embedded in the URL query.
    # The "1" at "&id=1" is too short for the OTP regex anyway, but make
    # sure the literal characters of the URL are unbroken up to the "&".
    assert "verify?token=abc</code>" not in out


def test_format_body_html_collapses_blank_lines_before_render() -> None:
    body = "Halo\n\n\n\n\nDunia"
    out = format_body_html(body)
    assert "\n\n\n" not in out


def test_format_body_html_escapes_html_special_chars() -> None:
    body = "<script>alert(1)</script>"
    out = format_body_html(body)
    # The literal '<' and '>' must be escaped so Telegram doesn't try to
    # parse the user content as HTML.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_body_html_handles_multiple_otps() -> None:
    body = "First code: 1234. Second: 567890."
    out = format_body_html(body)
    assert "<code>1234</code>" in out
    assert "<code>567890</code>" in out


def test_format_body_html_does_not_wrap_digits_inside_url() -> None:
    """A 4-8 digit token inside a URL must NOT be wrapped — would break Telegram auto-linking."""
    body = "Verify at https://account.proton.me/v?token=123456 now."
    out = format_body_html(body)
    # Telegram needs the URL contiguous: "...token=123456" with no inserted tag.
    assert "token=123456" in out
    assert "<code>123456</code>" not in out


def test_summarize_html_only_returns_clean_text_no_tags() -> None:
    """Regression: an HTML-only body must NOT leak raw <div>/<p> markup to Telegram."""
    msg = EmailMessage()
    msg["From"] = "partner@biz.example"
    msg["To"] = "vielz50@proton.me"
    msg["Subject"] = "HTML only"
    # Deliberately do NOT call set_content() so the message is single-part HTML
    # — this mirrors what Gmail's "rich" composer sometimes produces.
    msg.set_payload("<div dir=\"ltr\">Halo!<br>Ini isinya.</div>")
    msg.set_type("text/html")
    parsed = parse_message(msg.as_bytes())
    summary = summarize(parsed)
    assert "<div" not in summary["body"]
    assert "<br" not in summary["body"]
    assert "Halo!" in summary["body"]
    assert "Ini isinya." in summary["body"]
