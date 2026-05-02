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
MAX_BODY_PREVIEW_CHARS = 1500

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


def get_text_body(message: Message) -> str:
    """Best-effort extraction of a readable plain-text body from an email message.

    Prefers ``text/plain`` parts. Falls back to ``text/html`` and strips the
    HTML so users see clean text in Telegram instead of raw markup.
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
        return _decode_part(plain_part)
    if html_part is not None:
        return html_to_text(_decode_part(html_part))
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
    """Collapse runs of 2+ blank lines into a single blank line.

    Forwarded emails often arrive with huge vertical gaps (HTML→text
    conversion + signature padding). Telegram doesn't render extra blank
    space well on mobile, so we cap at one blank line between paragraphs.
    Returns ``text`` with trailing whitespace stripped per-line and at
    most ``\\n\\n`` between non-empty paragraphs.
    """
    if not text:
        return text
    # Strip trailing spaces per line so " \n" doesn't read as content.
    cleaned = "\n".join(line.rstrip() for line in text.splitlines())
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
