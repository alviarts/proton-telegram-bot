"""Helpers for extracting recipient addresses and previewing email bodies."""
from __future__ import annotations

from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses

RECIPIENT_HEADERS = ("Delivered-To", "X-Original-To", "To", "Cc", "Bcc")
MAX_BODY_PREVIEW_CHARS = 1500


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


def get_text_body(message: Message) -> str:
    """Best-effort extraction of a readable text body from an email message."""
    if message.is_multipart():
        text_part: Message | None = None
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type == "text/plain":
                text_part = part
                break
            if content_type == "text/html" and text_part is None:
                text_part = part
        if text_part is None:
            return ""
        payload = text_part.get_payload(decode=True) or b""
        charset = text_part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    payload = message.get_payload(decode=True) or b""
    if isinstance(payload, bytes):
        charset = message.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return str(payload)


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
