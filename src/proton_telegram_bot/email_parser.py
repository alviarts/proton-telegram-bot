"""Helpers for extracting recipient addresses and previewing email bodies."""
from __future__ import annotations

import re
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses
from html import escape as html_escape
from html import unescape
from html.parser import HTMLParser
from typing import ClassVar

RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "To", "Cc", "Bcc")
# Cap on the body preview rendered into a Telegram message. The user
# complained that Stripe / Cognition-style notifications produced huge
# walls of whitespace + boilerplate footer ("Unsubscribe Preferences",
# legal address, "View in browser" …) that pushed the actual content
# off-screen. 700 chars fits roughly one phone-screen of text; the
# footer-stripping pass below removes most marketing fluff before
# truncation kicks in.
MAX_BODY_PREVIEW_CHARS = 700

# Lines / phrases that mark the boundary between the actual email
# content and marketing / legal boilerplate. Anything from the FIRST
# match onward is dropped before the body is shown to the user. Match
# is case-insensitive and looks at *whole-line* matches so a sentence
# like "please don't unsubscribe yet!" inside the actual body isn't
# accidentally treated as a footer marker.
_FOOTER_MARKERS: tuple[str, ...] = (
    "unsubscribe",
    "unsubscribe preferences",
    "manage preferences",
    "manage your preferences",
    "update your preferences",
    "view this email in your browser",
    "view in browser",
    "view it in your browser",
    "this email was sent to",
    "you are receiving this email because",
    "you received this email because",
    "no longer wish to receive",
    "© ",
    "(c) ",
    "all rights reserved",
)

# Match a 4-8 digit code that stands alone (surrounded by non-digit / boundary).
# Telegram's HTML parser will render ``<code>...</code>`` as tap-to-copy on
# mobile. We deliberately match BOTH the raw digit run and explicit "code: NNN"
# / "kode: NNN" framings so the bot doesn't miss OTPs sandwiched between
# punctuation (".", ":", etc.).
_OTP_RE = re.compile(r"(?<![\w\d])(\d{4,8})(?![\w\d])")
# Match URLs (http/https). We use this to *skip* OTP wrapping inside URLs:
# if a 6-digit token shows up in a query string we must NOT inject ``<code>``
# around it because Telegram won't auto-link a URL that's been interrupted
# by HTML tags.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


class _HTMLToText(HTMLParser):
    """Tiny HTML-to-text extractor that preserves line breaks for common tags."""

    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset({
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "section", "article", "header", "footer",
    })
    _SKIP_TAGS: ClassVar[frozenset[str]] = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self._chunks.append("\n")
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse 3+ consecutive newlines into 2 for readability.
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html_source: str) -> str:
    """Convert an HTML email body to readable plain text.

    Strips tags, decodes entities, and preserves paragraph breaks. Falls back
    to a regex-based tag stripper if the parser raises (malformed HTML).
    """
    if not html_source:
        return ""
    try:
        parser = _HTMLToText()
        parser.feed(html_source)
        parser.close()
        return parser.get_text()
    except Exception:
        # Best-effort fallback: drop tags and decode entities.
        return unescape(re.sub(r"<[^>]+>", "", html_source)).strip()


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (LookupError, ValueError):
        return raw


def parse_message(raw_bytes: bytes) -> Message:
    return message_from_bytes(raw_bytes)


def extract_recipients(message: Message) -> set[str]:
    """Return the set of normalized (lower-cased) recipient addresses in a message."""
    recipients: set[str] = set()
    for header in RECIPIENT_HEADERS:
        values = message.get_all(header)
        if not values:
            continue
        for _, addr in getaddresses(values):
            if addr:
                recipients.add(addr.strip().lower())
    return recipients


def extract_sender(message: Message) -> tuple[str, str]:
    """Return ``(sender_email, sender_domain)`` from the From header.

    Falls back to ``("", "")`` when the header is missing or unparsable.
    """
    raw_from = message.get("From") or ""
    pairs = getaddresses([raw_from])
    for _name, addr in pairs:
        addr = addr.strip().lower()
        if "@" in addr:
            domain = addr.rsplit("@", 1)[1]
            return addr, domain
    return "", ""


def find_matching_alias(message: Message, candidate_aliases: set[str]) -> str | None:
    """Return the first candidate alias that appears in the message recipients, if any."""
    recipients = extract_recipients(message)
    for alias in candidate_aliases:
        if alias.lower() in recipients:
            return alias.lower()
    return None


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    if not isinstance(payload, (bytes, bytearray)):
        return str(payload)
    charset = part.get_content_charset() or "utf-8"
    try:
        return bytes(payload).decode(charset, errors="replace")
    except LookupError:
        return bytes(payload).decode("utf-8", errors="replace")


def _strip_email_footer(text: str) -> str:
    """Drop everything from the first known boilerplate marker onward.

    Marketing emails (Stripe, Mailchimp, Cognition transactional, …)
    almost always end with "Unsubscribe / Unsubscribe Preferences /
    legal address / © Year Co." — and those footers convert to huge
    whitespace blobs after HTML→text. The user explicitly complained
    that those footers were pushing the real content off-screen
    ("hasilnya terlalu makan banyak text"). We scan whole-line
    occurrences (case-insensitive) and cut the body at the first
    match. The match is anchored to "whole line ≈ marker" — i.e. the
    line, after stripping leading/trailing whitespace, is *only* the
    marker — so a sentence like "please don't unsubscribe yet" in
    the actual body never accidentally triggers a cut.
    """
    if not text:
        return text
    lines = text.splitlines()
    cut_at: int | None = None
    for idx, line in enumerate(lines):
        normalized = line.strip().lower()
        if not normalized:
            continue
        for marker in _FOOTER_MARKERS:
            # Whole-line marker: the line is essentially just the
            # marker (possibly with trailing punctuation/colon).
            if normalized == marker:
                cut_at = idx
                break
            if normalized.startswith(marker) and len(normalized) <= len(marker) + 6:
                cut_at = idx
                break
        if cut_at is not None:
            break
    if cut_at is None:
        return text
    trimmed = "\n".join(lines[:cut_at]).rstrip()
    return trimmed


def get_text_body(message: Message) -> str:
    """Best-effort extraction of a readable plain-text body from an email message.

    Prefers ``text/plain`` parts. Falls back to ``text/html`` and strips the
    HTML so users see clean text in Telegram instead of raw markup.
    Marketing/legal footers ("Unsubscribe", "© 2026 Co.", "view in
    browser", …) are removed before the body is returned so the
    Telegram preview doesn't get drowned in boilerplate.
    """
    plain_part: Message | None = None
    html_part: Message | None = None
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and plain_part is None:
                plain_part = part
            elif content_type == "text/html" and html_part is None:
                html_part = part
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_part = message
        elif content_type == "text/html":
            html_part = message
        else:
            # Unknown single-part body; return whatever decodes.
            return _decode_part(message)
    if plain_part is not None:
        return _strip_email_footer(_decode_part(plain_part))
    if html_part is not None:
        return _strip_email_footer(html_to_text(_decode_part(html_part)))
    return ""


def summarize(message: Message, max_chars: int = MAX_BODY_PREVIEW_CHARS) -> dict[str, str]:
    """Build a small dict of human-readable fields suitable for a Telegram message."""
    subject = _decode_header_value(message.get("Subject"))
    from_ = _decode_header_value(message.get("From"))
    to = _decode_header_value(message.get("To"))
    date = _decode_header_value(message.get("Date"))
    body = get_text_body(message).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n…(dipotong)"
    return {
        "subject": subject or "(tanpa subject)",
        "from": from_ or "(tidak diketahui)",
        "to": to,
        "date": date,
        "body": body,
    }


def collapse_blank_lines(text: str) -> str:
    """Collapse runs of 2+ blank lines into a single blank line and
    drop "decorative" lines that contribute no information.

    Forwarded emails often arrive with huge vertical gaps (HTML→text
    conversion + signature padding) and decorator lines like ``"---"``
    or ``"   -   "`` between sections. Telegram renders both as wasted
    space on mobile, so we:

    * strip trailing whitespace from every line;
    * drop lines that are only punctuation / whitespace (e.g. ``"-"``,
      ``"  -  "``, ``"==="``) — the surrounding paragraph break is
      kept;
    * cap consecutive newlines at 2 (one blank line) so paragraphs
      stay separated but never sprawl into screenfuls of empty space.

    Returns ``text`` with trailing whitespace stripped per-line and at
    most ``\\n\\n`` between non-empty paragraphs.
    """
    if not text:
        return text
    # Strip trailing spaces per line so " \n" doesn't read as content.
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # Drop separator-only lines like "---", "  -  ", "===", "***"
        # — they provide visual structure in HTML emails but only
        # eat space in Telegram. Keep blank lines (they participate
        # in paragraph collapsing below).
        stripped = line.strip()
        if stripped and not re.search(r"[A-Za-z0-9]", stripped):
            # No alphanumeric content → decorator only. Replace with a
            # blank line so paragraphs above and below stay separated
            # without an explicit "  -  " in between.
            cleaned_lines.append("")
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    # Collapse 3+ consecutive newlines down to 2 (one blank line).
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def format_body_html(body: str) -> str:
    """Render an email body as Telegram-safe HTML.

    * HTML-escapes everything (so user content can never inject markup).
    * Re-injects ``<code>...</code>`` around standalone 4-8 digit OTPs
      so Telegram mobile users can tap-to-copy.
    * Leaves URLs intact — Telegram auto-links bare URLs in HTML
      messages as long as the URL itself isn't broken across tags or
      mid-character.
    * Caps consecutive blank lines at one.

    Returns a string ready to embed directly inside a Telegram HTML
    message. Callers must NOT wrap the result in ``<pre>`` (that would
    disable URL auto-linking).
    """
    if not body:
        return ""
    collapsed = collapse_blank_lines(body)

    def _wrap_outside_urls(piece: str) -> str:
        return _OTP_RE.sub(r"<code>\1</code>", html_escape(piece, quote=False))

    # Walk the body in two regions: URL spans (escape only, no OTP wrap) and
    # everything else (escape + OTP wrap). Splitting before HTML escape
    # keeps the regex's byte offsets stable; html.escape never affects
    # digits so the OTP regex is safe to run on the escaped output.
    parts: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(collapsed):
        start, end = match.span()
        if cursor < start:
            parts.append(_wrap_outside_urls(collapsed[cursor:start]))
        parts.append(html_escape(collapsed[start:end], quote=False))
        cursor = end
    if cursor < len(collapsed):
        parts.append(_wrap_outside_urls(collapsed[cursor:]))
    return "".join(parts)
