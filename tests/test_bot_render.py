"""Tests for the Telegram message rendering and inline-keyboard wiring."""
from __future__ import annotations

import re

from proton_telegram_bot.bot import (
    _TELEGRAM_MESSAGE_LIMIT,
    _build_alias_keyboard_for_primary,
    _build_primary_keyboard,
    _render_email_message,
)
from proton_telegram_bot.models import AliasRecord, AliasStatus, PrimaryAccount


def _fake_primary(pid: int = 1, email: str = "vielz43@proton.me") -> PrimaryAccount:
    return PrimaryAccount(
        id=pid,
        chat_id=1,
        email=email,
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_username=email,
        imap_use_ssl=False,
        created_at="2026-01-01",
    )


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
    markup = _build_alias_keyboard_for_primary(_fake_primary(), aliases)
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


def test_alias_keyboard_empty_state_has_back_button() -> None:
    markup = _build_alias_keyboard_for_primary(_fake_primary(), [])
    flat = [b for row in markup.inline_keyboard for b in row]
    assert any("belum ada" in (b.text or "") for b in flat)
    # Back-to-primaries button is always present so users can escape an empty
    # alias list.
    assert any(b.callback_data == "backp" for b in flat)


def test_primary_keyboard_includes_alias_counts_and_active_marker() -> None:
    p1 = _fake_primary(pid=1, email="vielz43@proton.me")
    p2 = _fake_primary(pid=2, email="vielz22@proton.me")
    counts = {1: 14, 2: 7}
    markup = _build_primary_keyboard([p1, p2], counts, active_primary_id=2)
    flat = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [
        b for b in flat if (b.callback_data or "").startswith("pickp:")
    ]
    assert len(pick_buttons) == 2
    # Alias counts are in the labels (compact ``· N`` format).
    assert any("· 14" in (b.text or "") for b in pick_buttons)
    assert any("· 7" in (b.text or "") for b in pick_buttons)
    # Active primary gets the lock marker.
    locked_button = next(
        b for b in pick_buttons if (b.callback_data or "") == "pickp:2"
    )
    assert "🔒" in (locked_button.text or "")
    # Each primary also gets a per-row Sync button on the same row.
    sync_buttons = [
        b for b in flat if (b.callback_data or "").startswith("syncp:")
    ]
    assert len(sync_buttons) == 2


def test_primary_keyboard_renders_healthcheck_stats_when_available() -> None:
    """When /cekimap has run for a primary, the keyboard label
    switches from ``· N`` (DB count) to ``· ok/total`` (last health
    check) so the user can spot a primary whose aliases have
    started failing.
    """
    p1 = _fake_primary(pid=1, email="vielz43@proton.me")
    p2 = _fake_primary(pid=2, email="vielz22@proton.me")
    counts = {1: 20, 2: 7}
    healthcheck_stats = {1: (9, 20)}  # only p1 has run /cekimap
    markup = _build_primary_keyboard(
        [p1, p2], counts, healthcheck_stats=healthcheck_stats
    )
    flat = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [
        b for b in flat if (b.callback_data or "").startswith("pickp:")
    ]
    p1_btn = next(b for b in pick_buttons if (b.callback_data or "") == "pickp:1")
    p2_btn = next(b for b in pick_buttons if (b.callback_data or "") == "pickp:2")
    assert "· 9/20" in (p1_btn.text or "")
    # p2 falls back to plain count because no health check ran.
    assert "· 7" in (p2_btn.text or "")
    assert "/" not in (p2_btn.text or "").split("·", 1)[1]


def test_primary_keyboard_empty_state() -> None:
    markup = _build_primary_keyboard([])
    flat = [b for row in markup.inline_keyboard for b in row]
    assert any("belum ada" in (b.text or "") for b in flat)
    assert any(b.callback_data == "refresh" for b in flat)
