"""Tests for the Telegram message rendering and inline-keyboard wiring."""
from __future__ import annotations

import re

from proton_telegram_bot.bot import (
    _TELEGRAM_MESSAGE_LIMIT,
    _build_alias_keyboard,
    _render_email_message,
)
from proton_telegram_bot.models import AliasRecord, AliasStatus


def test_render_short_message_includes_all_sections() -> None:
    text = _render_email_message(
        "vielz50@proton.me",
        {
            "from": "Partner <partner@biz.example>",
            "subject": "Halo",
            "date": "Thu, 30 Apr 2026 10:00:00 +0000",
            "body": "Pesan singkat.",
        },
    )
    assert "<code>vielz50@proton.me</code>" in text
    assert "<pre>Pesan singkat.</pre>" in text
    # closing tag must always be present
    assert text.count("<pre>") == text.count("</pre>")
    assert "/list" in text


def test_render_truncates_huge_body_without_breaking_html() -> None:
    body = "A" * 50_000
    text = _render_email_message(
        "vielz50@proton.me",
        {
            "from": "x@y.example",
            "subject": "S",
            "date": "D",
            "body": body,
        },
    )
    # Stays under Telegram's 4096-character cap, with our defensive 4000 budget.
    assert len(text) <= _TELEGRAM_MESSAGE_LIMIT
    # All HTML tags are well-formed.
    assert text.count("<pre>") == 1 == text.count("</pre>")
    assert text.count("<b>") == text.count("</b>")
    # And the truncation marker is present.
    assert "(dipotong)" in text


def test_render_does_not_split_html_entity() -> None:
    # "&" expands to "&amp;" (5 chars) so a body of 5000 ampersands escapes to 25000
    # chars — far over budget. The truncated output must not contain a partial
    # entity like "&am" or "&" without a trailing ";".
    body = "&" * 5000
    text = _render_email_message(
        "v@p.me",
        {"from": "f@x.example", "subject": "S", "date": "D", "body": body},
    )
    inside_pre = re.search(r"<pre>(.*?)</pre>", text, re.DOTALL)
    assert inside_pre is not None
    pre_body = inside_pre.group(1)
    # Every "&" in the rendered <pre> body must start a complete "&amp;" entity.
    assert all(
        pre_body[i : i + 5] == "&amp;"
        for i in range(len(pre_body))
        if pre_body[i] == "&"
    )


def test_alias_keyboard_callback_data_is_under_64_bytes() -> None:
    long_email = "a" * 80 + "@proton.me"  # ~90 chars; would exceed limit if embedded
    aliases = [
        AliasRecord(id=42, chat_id=1, email=long_email, status=AliasStatus.AVAILABLE),
        AliasRecord(id=99999, chat_id=1, email="b@p.me", status=AliasStatus.AVAILABLE),
    ]
    markup = _build_alias_keyboard(aliases)
    pick_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("pick:")
    ]
    assert len(pick_buttons) == 2
    for button in pick_buttons:
        encoded = (button.callback_data or "").encode("utf-8")
        assert 1 <= len(encoded) <= 64
    # And the visible button text still shows the full email so users see what they're picking.
    assert pick_buttons[0].text == long_email


def test_alias_keyboard_empty_state() -> None:
    markup = _build_alias_keyboard([])
    flat = [b for row in markup.inline_keyboard for b in row]
    assert any("belum ada" in (b.text or "") for b in flat)
    assert any(b.callback_data == "refresh" for b in flat)
