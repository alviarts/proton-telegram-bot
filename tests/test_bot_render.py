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
    # Body is rendered as plain text (no <pre>) so URL auto-linking and OTP
    # tap-to-copy work in the Telegram client.
    assert "Pesan singkat." in text
    assert "<pre>" not in text and "</pre>" not in text
    assert text.count("<b>") == text.count("</b>")
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
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<code>") == text.count("</code>")
    # And the truncation marker is present.
    assert "(dipotong)" in text


def test_render_does_not_split_html_entity() -> None:
    # "&" expands to "&amp;" (5 chars) so a body of 5000 ampersands escapes to
    # 25000 chars — far over budget. The truncated output must not contain a
    # partial entity like "&am" or a "&" without a trailing ";".
    body = "&" * 5000
    text = _render_email_message(
        "v@p.me",
        {"from": "f@x.example", "subject": "S", "date": "D", "body": body},
    )
    # Strip all known well-formed entities and tags; a leftover "&" would
    # mean the truncator cut an entity in half.
    stripped = re.sub(r"&amp;", "", text)
    stripped = re.sub(r"<[^>]+>", "", stripped)
    assert "&" not in stripped


def test_render_collapses_blank_lines() -> None:
    """Forward email body should never have more than one blank line."""
    body = "Halo,\n\n\n\nIni baris 2.\n\n\n\n\nBaris 3."
    text = _render_email_message(
        "v@p.me",
        {"from": "f@x.example", "subject": "S", "date": "D", "body": body},
    )
    # Anywhere in the rendered message we must not see 3+ consecutive newlines.
    assert "\n\n\n" not in text


def test_render_wraps_otp_in_code_tag() -> None:
    """Standalone 4-8 digit codes get ``<code>`` wrappers for tap-to-copy."""
    body = "Your code is 245657. It expires in 5 minutes."
    text = _render_email_message(
        "v@p.me",
        {"from": "f@x.example", "subject": "S", "date": "D", "body": body},
    )
    assert "<code>245657</code>" in text


def test_render_preserves_url_intact() -> None:
    """URLs must not be split or escaped in a way that breaks Telegram auto-linking."""
    body = "Click here: https://account.proton.me/verify-email?token=xxx&user=1"
    text = _render_email_message(
        "v@p.me",
        {"from": "f@x.example", "subject": "S", "date": "D", "body": body},
    )
    # The URL up to "&" remains intact; "&" is escaped to "&amp;" which
    # Telegram still treats as a valid URL char during auto-linking.
    assert "https://account.proton.me/verify-email?token=xxx" in text
    # No <pre> wrapper that would suppress auto-linking.
    assert "<pre>" not in text


def test_primary_keyboard_includes_copy_active_button_when_locked() -> None:
    """PR-C #3: when an alias is locked, the /list keyboard surfaces a
    "📋 Copy email aktif" button so desktop users can grab the address
    in one click without text selection.
    """
    from proton_telegram_bot.bot import CB_COPY_ACTIVE_EMAIL

    primary = _fake_primary()
    markup = _build_primary_keyboard(
        [primary], {1: 5}, active_primary_id=1, with_copy_active_button=True
    )
    flat = [b for row in markup.inline_keyboard for b in row]
    copy_buttons = [b for b in flat if b.callback_data == CB_COPY_ACTIVE_EMAIL]
    assert len(copy_buttons) == 1
    assert "Copy email aktif" in (copy_buttons[0].text or "")


def test_primary_keyboard_omits_copy_active_button_when_unlocked() -> None:
    """No active alias → no Copy button. Keeps the keyboard compact."""
    from proton_telegram_bot.bot import CB_COPY_ACTIVE_EMAIL

    primary = _fake_primary()
    markup = _build_primary_keyboard(
        [primary], {1: 5}, active_primary_id=None, with_copy_active_button=False
    )
    flat = [b for row in markup.inline_keyboard for b in row]
    assert not any(b.callback_data == CB_COPY_ACTIVE_EMAIL for b in flat)


def test_poll_now_keyboard_optionally_adds_copy_active_button() -> None:
    """The lock-confirmation reply (after CB_PICK_PRIMARY:N pick) hands a
    keyboard that includes both ``📋 Copy email aktif`` and
    ``📥 Cek email sekarang`` so users have one-click copy *and* a
    manual poll without typing.
    """
    from proton_telegram_bot.bot import (
        CB_COPY_ACTIVE_EMAIL,
        CB_POLL_NOW,
        _build_poll_now_keyboard,
    )

    plain = _build_poll_now_keyboard()
    plain_flat = [b for row in plain.inline_keyboard for b in row]
    assert any(b.callback_data == CB_POLL_NOW for b in plain_flat)
    assert not any(b.callback_data == CB_COPY_ACTIVE_EMAIL for b in plain_flat)

    with_copy = _build_poll_now_keyboard(with_copy_active=True)
    with_copy_flat = [b for row in with_copy.inline_keyboard for b in row]
    assert any(b.callback_data == CB_POLL_NOW for b in with_copy_flat)
    assert any(b.callback_data == CB_COPY_ACTIVE_EMAIL for b in with_copy_flat)


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


def test_alias_keyboard_shows_smart_topup_when_below_target() -> None:
    """User screenshot: with 12 aliases (target 20), the alias list view
    must show a "✨ Tambah 8 alamat lagi" button so the user can top
    up without computing the math themselves. The callback embeds the
    primary id and the missing count so /genaddr generates the exact
    number requested.
    """
    primary = _fake_primary(pid=7, email="vielz64@proton.me")
    aliases = [
        AliasRecord(
            id=i, chat_id=1, email=f"vielz0{i}@proton.me", status=AliasStatus.AVAILABLE
        )
        for i in range(12)
    ]
    markup = _build_alias_keyboard_for_primary(primary, aliases)
    flat = [b for row in markup.inline_keyboard for b in row]
    topup_buttons = [
        b for b in flat if (b.callback_data or "").startswith("qgenaddr:")
    ]
    assert len(topup_buttons) == 1
    assert topup_buttons[0].callback_data == "qgenaddr:7:8"
    assert topup_buttons[0].text == "✨ Tambah 8 alamat lagi"


def test_alias_keyboard_hides_smart_topup_when_at_or_above_target() -> None:
    """When the primary already meets / exceeds ``ALIAS_TARGET_PER_PRIMARY``
    the smart top-up button must NOT be rendered — the user explicitly
    asked for it to disappear ("klo sudh 20 tidak usah di tampilkan
    lagi"). Otherwise tapping the button would generate 0 aliases.
    """
    primary = _fake_primary(pid=7, email="vielz64@proton.me")
    aliases = [
        AliasRecord(
            id=i, chat_id=1, email=f"vielz0{i}@proton.me", status=AliasStatus.AVAILABLE
        )
        for i in range(20)
    ]
    markup = _build_alias_keyboard_for_primary(primary, aliases)
    flat = [b for row in markup.inline_keyboard for b in row]
    topup_buttons = [
        b for b in flat if (b.callback_data or "").startswith("qgenaddr:")
    ]
    assert topup_buttons == []


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


# ----------------------------------------------------------- pagination


def test_primary_keyboard_paginates_long_lists_into_4_row_pages() -> None:
    """PR-D: with > LIST_PAGE_SIZE primaries, the keyboard shows only
    page 0 worth of rows + a Prev/Next nav row.
    """
    from proton_telegram_bot.bot import LIST_PAGE_SIZE

    primaries = [
        _fake_primary(pid=i, email=f"v{i}@proton.me")
        for i in range(1, LIST_PAGE_SIZE + 3)  # one full page + 2 overflow
    ]
    markup = _build_primary_keyboard(primaries, page=0)
    flat = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [
        b for b in flat if (b.callback_data or "").startswith("pickp:")
    ]
    assert len(pick_buttons) == LIST_PAGE_SIZE  # only the first page is rendered
    # Nav row: no "◀ Prev" on page 0, "Next ▶" must be present, and the
    # central "Page 1/2" label is NOOP-routed.
    nav_buttons = [
        b for b in flat if (b.callback_data or "").startswith("ppg:")
    ]
    assert any(b.text == "Next ▶" for b in nav_buttons)
    assert not any(b.text == "◀ Prev" for b in nav_buttons)
    assert any("Page 1/2" in (b.text or "") for b in flat)


def test_primary_keyboard_last_page_has_prev_only() -> None:
    """On the final page the Next button disappears so the user can't
    scroll past the end of the list.
    """
    from proton_telegram_bot.bot import LIST_PAGE_SIZE

    primaries = [
        _fake_primary(pid=i, email=f"v{i}@proton.me")
        for i in range(1, LIST_PAGE_SIZE * 2 + 1)
    ]
    markup = _build_primary_keyboard(primaries, page=1)
    flat = [b for row in markup.inline_keyboard for b in row]
    nav_buttons = [
        b for b in flat if (b.callback_data or "").startswith("ppg:")
    ]
    assert any(b.text == "◀ Prev" for b in nav_buttons)
    assert not any(b.text == "Next ▶" for b in nav_buttons)
    assert any("Page 2/2" in (b.text or "") for b in flat)


def test_primary_keyboard_no_pagination_row_for_short_lists() -> None:
    """A list that fits on one page must NOT render a Prev/Next row."""
    primaries = [_fake_primary(pid=1, email="v1@proton.me")]
    markup = _build_primary_keyboard(primaries)
    flat = [b for row in markup.inline_keyboard for b in row]
    assert not any((b.callback_data or "").startswith("ppg:") for b in flat)
    # Page label is also absent.
    assert not any("Page" in (b.text or "") for b in flat)


def test_primary_keyboard_pagination_clamps_out_of_range_page() -> None:
    """A bogus page index (> total_pages) collapses to the last valid
    page rather than rendering an empty keyboard.
    """
    from proton_telegram_bot.bot import LIST_PAGE_SIZE

    primaries = [
        _fake_primary(pid=i, email=f"v{i}@proton.me")
        for i in range(1, LIST_PAGE_SIZE + 2)
    ]
    markup = _build_primary_keyboard(primaries, page=99)
    flat = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [
        b for b in flat if (b.callback_data or "").startswith("pickp:")
    ]
    # Total = LIST_PAGE_SIZE + 1, page_size = LIST_PAGE_SIZE. Last
    # valid page index is 1 → 1 button on that page.
    assert len(pick_buttons) == 1
    assert any("Page 2/2" in (b.text or "") for b in flat)


def test_alias_keyboard_paginates_drill_down() -> None:
    """Alias drill-down honours the same 4-row-per-page rule and the
    Prev/Next callback embeds the primary id.
    """
    from proton_telegram_bot.bot import LIST_PAGE_SIZE

    primary = _fake_primary(pid=42, email="vielz@proton.me")
    aliases = [
        AliasRecord(id=i, chat_id=1, email=f"v{i}@p.me", status=AliasStatus.AVAILABLE)
        for i in range(1, LIST_PAGE_SIZE + 3)
    ]
    markup = _build_alias_keyboard_for_primary(primary, aliases, page=0)
    flat = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [
        b for b in flat if (b.callback_data or "").startswith("pick:")
    ]
    assert len(pick_buttons) == LIST_PAGE_SIZE
    nav_buttons = [
        b for b in flat if (b.callback_data or "").startswith("apg:")
    ]
    # Callback prefix carries the primary id so the handler can re-render
    # the right drill-down without state.
    assert any((b.callback_data or "").startswith("apg:42:") for b in nav_buttons)
    assert any(b.text == "Next ▶" for b in nav_buttons)


def test_alias_keyboard_no_pagination_row_for_short_lists() -> None:
    """Single-page alias drill-down: no Prev/Next/Page row."""
    primary = _fake_primary()
    aliases = [
        AliasRecord(id=1, chat_id=1, email="a@p.me", status=AliasStatus.AVAILABLE),
    ]
    markup = _build_alias_keyboard_for_primary(primary, aliases)
    flat = [b for row in markup.inline_keyboard for b in row]
    assert not any((b.callback_data or "").startswith("apg:") for b in flat)
    assert not any("Page" in (b.text or "") for b in flat)


def test_pagination_callback_data_under_64_bytes() -> None:
    """Both pagination prefixes stay well under Telegram's 64-byte
    callback_data ceiling, even with large primary ids and high page
    numbers.
    """
    from proton_telegram_bot.bot import _build_pagination_row

    # Worst case: 12-digit primary id + 4-digit page index (~99 pages
    # of LIST_PAGE_SIZE means hundreds of aliases per primary).
    row = _build_pagination_row("apg:999999999999", 99, 200)
    assert row is not None
    for button in row:
        encoded = (button.callback_data or "").encode("utf-8")
        assert 1 <= len(encoded) <= 64
