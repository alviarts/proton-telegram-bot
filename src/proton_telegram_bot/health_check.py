"""Background health check for IMAP/SMTP listeners on /cekimap.

Workflow per primary:

1. Pull Bridge IMAP/SMTP credentials for the primary email out of the
   Bridge vault.
2. Open a dedicated IMAP4 connection to Bridge (separate from the
   running listener — Bridge accepts concurrent IMAP sessions). Select
   ``INBOX``.
3. For each alias of the primary (+ the primary itself), send a tagged
   test email **from** that alias **to** the primary's own address via
   Bridge SMTP. Proton routes alias-to-primary internally so the
   message lands in the primary's INBOX within seconds — no external
   mail provider is involved.
4. Poll the dedicated INBOX connection with ``UID SEARCH SUBJECT`` for
   the unique ``[health-check] <token>`` tags. As soon as a token is
   seen, mark its alias confirmed.
5. Maintain a single rolling **progress message** in Telegram and edit
   it in place as aliases land — no spam of one ✅/❌ per alias. The
   "started" header and the final "selesai" summary stay as distinct
   messages so they remain visible above the rolling line.
6. After the poll completes, mark every detected health-check message
   ``\\Deleted`` and EXPUNGE so the user's INBOX isn't polluted.

This design avoids Mail.tm entirely. Mail.tm rate-limits account
creation to 1/60s per IP (``ratelimit-policy: 1; w=60``) which makes
per-alias temp inboxes infeasible for primaries with more than one
alias. Routing via Proton-internal mail (alias → primary) is faster,
isolated per alias by token, and has no external dependency.

Runs in a background asyncio task so the user can keep using the bot
(read mail, /list, /genaddr, …) while it executes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import smtplib
import time
from email.message import EmailMessage

import aioimaplib
from telegram.constants import ParseMode

from .bridge_admin import BridgeAdmin
from .db import Database
from .models import PrimaryAccount

LOGGER = logging.getLogger(__name__)

# Bridge defaults (must match bot.CONNECT_DEFAULT_HOST + BRIDGE_SMTP_PORT).
BRIDGE_SMTP_HOST = "127.0.0.1"
BRIDGE_SMTP_PORT = 1025
BRIDGE_IMAP_HOST = "127.0.0.1"
BRIDGE_IMAP_PORT = 1143
BRIDGE_IMAP_USE_SSL = False
SMTP_SEND_TIMEOUT_S = 30
IMAP_TIMEOUT_S = 30

# Total time we allow Bridge to receive every alias's test email after
# we finish sending the whole batch. Proton-internal alias→primary
# delivery is normally < 5s but routing for very fresh aliases can lag.
HEALTH_CHECK_RECEIVE_TIMEOUT_S = 90
# How often the IMAP poll task runs UID SEARCH against the dedicated
# health-check connection.
HEALTH_CHECK_POLL_INTERVAL_S = 3.0
# How often the rolling Telegram progress message is edited. Telegram
# limits edits to ~1/sec per chat; 2.0s is conservative and avoids
# burning ratelimits when a primary has 20+ aliases.
PROGRESS_EDIT_INTERVAL_S = 2.0
# Subject prefix every health-check message carries so we can find them
# from a UID SEARCH and clean them up afterwards.
HEALTH_CHECK_SUBJECT_PREFIX = "[health-check]"


def _smtp_send(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> None:
    """Synchronous SMTP send, identical wire format to the post-/connect
    smoke test in ``bot._smtp_send_test_message`` so Bridge + Proton
    handle it the same way."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=SMTP_SEND_TIMEOUT_S) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)


def _format_progress(
    primary_email: str,
    total: int,
    confirmed: set[str],
    failed: set[str],
    waiting: list[str],
) -> str:
    """Format the rolling progress message body.

    Confirmed aliases are summarised as a count to keep the message
    short ("yang sudah berhasil tidak perlu di tampilkan lagi"). The
    in-flight ("Menunggu") and failed lists carry full names so the
    user can see what's still pending or what to retry.
    """
    pending = [a for a in waiting if a not in confirmed and a not in failed]

    lines = [
        f"🩺 <b>Health check {primary_email}</b>",
        "",
        f"✅ Sukses: <b>{len(confirmed)}/{total}</b>",
    ]
    if failed:
        sample = ", ".join(sorted(failed)[:8])
        more = "" if len(failed) <= 8 else f" (+{len(failed) - 8} lagi)"
        lines.append(f"❌ Gagal: <b>{len(failed)}</b> — <code>{sample}</code>{more}")
    if pending:
        sample = ", ".join(pending[:6])
        more = "" if len(pending) <= 6 else f" (+{len(pending) - 6} lagi)"
        lines.append(
            f"⏳ Menunggu: <b>{len(pending)}</b> — <code>{sample}</code>{more}"
        )
    return "\n".join(lines)


def _parse_uids(lines: list[bytes | str]) -> list[int]:
    """Extract UIDs from an aioimaplib SEARCH response.

    Mirrors the parser in ``imap_listener._parse_uids`` — only
    digits-only lines count as UID lines, so trailing OK status lines
    don't get parsed as UIDs.
    """
    uids: list[int] = []
    for raw in lines:
        if isinstance(raw, bytes):
            try:
                line = raw.decode()
            except UnicodeDecodeError:
                continue
        else:
            line = raw
        tokens = line.strip().split()
        if not tokens:
            continue
        if not all(t.isdigit() for t in tokens):
            continue
        uids.extend(int(t) for t in tokens)
    return uids


_TOKEN_IN_SUBJECT_RE = re.compile(rb"\[health-check\]\s+([0-9a-f]{16})", re.IGNORECASE)


def _extract_token_from_header_blob(blob: bytes) -> str | None:
    """Return the 16-char hex token in a fetched ``BODY[HEADER.FIELDS
    (SUBJECT)]`` blob, or ``None`` if it isn't a health-check tag.

    Email headers may be folded over multiple lines so we scan the
    whole blob rather than pattern-matching just the first line.
    """
    match = _TOKEN_IN_SUBJECT_RE.search(blob)
    if match is None:
        return None
    return match.group(1).decode().lower()


async def _open_imap(
    *, host: str, port: int, use_ssl: bool, username: str, password: str
) -> aioimaplib.IMAP4:
    """Open one short-lived IMAP4 session against Bridge for the
    health-check polls. Caller is responsible for ``logout()``.
    """
    if use_ssl:
        client = aioimaplib.IMAP4_SSL(host=host, port=port, timeout=IMAP_TIMEOUT_S)
    else:
        client = aioimaplib.IMAP4(host=host, port=port, timeout=IMAP_TIMEOUT_S)
    await client.wait_hello_from_server()
    await client.login(username, password)
    await client.select("INBOX")
    return client


async def _scan_inbox_for_tokens(
    client: aioimaplib.IMAP4, baseline_uid: int
) -> dict[str, int]:
    """Search INBOX for health-check messages newer than ``baseline_uid``
    and return ``{token: uid}`` for every one we recognise.
    """
    response = await client.uid_search(
        f"UID {baseline_uid + 1}:*"
    )
    if response.result != "OK":
        return {}
    uids = _parse_uids(response.lines)
    if not uids:
        return {}
    # Fetch only the Subject header for matching UIDs in one round trip.
    uid_set = ",".join(str(u) for u in uids)
    fetch_resp = await client.uid(
        "fetch", uid_set, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])"
    )
    if fetch_resp.result != "OK":
        return {}
    found: dict[str, int] = {}
    current_uid: int | None = None
    for raw in fetch_resp.lines:
        line = raw if isinstance(raw, bytes) else raw.encode()
        m = re.match(rb"\* (\d+) FETCH ", line)
        if m:
            try:
                current_uid = int(m.group(1))
            except ValueError:
                current_uid = None
            continue
        if current_uid is None:
            continue
        token = _extract_token_from_header_blob(line)
        if token is not None:
            found[token] = current_uid
    return found


async def _cleanup_inbox(
    client: aioimaplib.IMAP4, uids: list[int]
) -> None:
    """Mark the listed UIDs ``\\Deleted`` and EXPUNGE so health-check
    messages don't clutter the user's INBOX.
    """
    if not uids:
        return
    uid_set = ",".join(str(u) for u in uids)
    try:
        await client.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
        await client.expunge()
    except Exception:
        LOGGER.debug(
            "health check: cleanup of UIDs %s raised", uids, exc_info=True
        )


async def run_health_check(
    *,
    bot,
    chat_id: int,
    db: Database,
    bridge_admin: BridgeAdmin | None,
    primary: PrimaryAccount,
    targets: list[str],
) -> None:
    """Run a Mode-A health check for ``targets`` (alias emails) under
    ``primary``. Posts an initial "started" message, a rolling progress
    line that's edited in place as aliases confirm, and a final summary.

    ``targets`` should already be the list the user wants validated
    (typically primary.email + every alias of that primary). Duplicates
    are removed inside.
    """
    # Dedupe + normalise. Keep insertion order (primary first) so the
    # progress messages read naturally.
    seen: set[str] = set()
    deduped: list[str] = []
    for addr in targets:
        norm = addr.strip().lower()
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(norm)
    targets = deduped
    if not targets:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Tidak ada alias untuk dicek.",
        )
        return

    if bridge_admin is None:
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Bridge admin nonaktif — health check butuh akses Bridge "
            "vault untuk ambil kredensial SMTP.",
        )
        return

    try:
        creds = await bridge_admin.fetch_imap_credentials(primary.email)
    except Exception as exc:
        LOGGER.exception("health check: fetch_imap_credentials failed")
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ Gagal ambil kredensial Bridge: {exc!s}",
        )
        return
    if creds is None:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Bridge tidak punya kredensial untuk "
                f"<code>{primary.email}</code>. Coba /connect ulang akun "
                f"ini dulu."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # 1. "Started" header — kept above the rolling line so the user has
    # context that doesn't get overwritten.
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🩺 Health check <b>{primary.email}</b> dimulai.\n"
            f"Total target: <b>{len(targets)}</b> alamat.\n"
            f"Tes pakai routing internal Proton (no Mail.tm). "
            f"Bot tetap bisa dipakai sambil menunggu hasil."
        ),
        parse_mode=ParseMode.HTML,
    )

    # 2. Rolling progress message — edited in place from now on.
    confirmed: set[str] = set()
    failed: set[str] = set()
    progress_lock = asyncio.Lock()

    msg = await bot.send_message(
        chat_id=chat_id,
        text=_format_progress(
            primary.email, len(targets), confirmed, failed, list(targets)
        ),
        parse_mode=ParseMode.HTML,
    )
    progress_msg_id = getattr(msg, "message_id", None) if msg is not None else None

    last_edit = 0.0
    last_text = ""

    async def _refresh_progress(force: bool = False) -> None:
        nonlocal last_edit, last_text
        async with progress_lock:
            now = time.monotonic()
            if not force and now - last_edit < PROGRESS_EDIT_INTERVAL_S:
                return
            text = _format_progress(
                primary.email, len(targets), confirmed, failed, list(targets)
            )
            if text == last_text:
                return
            last_edit = now
            last_text = text
            if progress_msg_id is None:
                return
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                LOGGER.debug(
                    "health check: edit_message_text failed", exc_info=True
                )

    # 3. Open the dedicated IMAP connection up front, capture the
    # current high-water UID so we only consider messages that arrive
    # after we start sending. Bridge accepts concurrent connections;
    # this is independent of the always-on listener.
    try:
        imap = await _open_imap(
            host=BRIDGE_IMAP_HOST,
            port=BRIDGE_IMAP_PORT,
            use_ssl=BRIDGE_IMAP_USE_SSL,
            username=creds.imap_username,
            password=creds.imap_password,
        )
    except Exception as exc:
        LOGGER.exception("health check: failed to open IMAP session")
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Gagal buka IMAP ke Bridge: {exc!s}\n"
                f"Bridge mungkin belum running atau kredensial salah."
            ),
        )
        return

    try:
        baseline_resp = await imap.uid_search("ALL")
        baseline_uids = (
            _parse_uids(baseline_resp.lines)
            if baseline_resp.result == "OK"
            else []
        )
        baseline_uid = max(baseline_uids, default=0)

        # 4. SMTP send (sequential — Bridge serializes SMTP sessions).
        expected: dict[str, str] = {}  # alias -> token
        send_failures: list[tuple[str, str]] = []

        async def _send_one(alias_email: str) -> None:
            token = secrets.token_hex(8)
            subject = (
                f"{HEALTH_CHECK_SUBJECT_PREFIX} {token} {alias_email}"
            )
            body = (
                f"Health check from {alias_email} via Bridge SMTP.\n"
                f"Token: {token}\n"
                f"Routes alias→primary internally; ignore.\n"
            )
            try:
                await asyncio.to_thread(
                    _smtp_send,
                    host=BRIDGE_SMTP_HOST,
                    port=BRIDGE_SMTP_PORT,
                    username=creds.imap_username,
                    password=creds.imap_password,
                    from_addr=alias_email,
                    to_addr=primary.email,
                    subject=subject,
                    body=body,
                )
            except Exception as exc:  # pragma: no cover - network failures
                LOGGER.warning(
                    "health check: SMTP send from %s failed: %s",
                    alias_email,
                    exc,
                )
                send_failures.append((alias_email, str(exc)[:200]))
                failed.add(alias_email)
                await _refresh_progress()
                return
            expected[alias_email] = token

        for alias_email in targets:
            await _send_one(alias_email)

        await _refresh_progress(force=True)

        if not expected:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🩺 Health check selesai — <b>0/{len(targets)}</b> sync. "
                    f"Tidak ada email yang berhasil dikirim."
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        # 5. Poll INBOX for tokens. One scan handles every alias at
        # once via UID SEARCH + batched FETCH, so an N-alias batch
        # still only needs O(deadline / poll_interval) round trips.
        token_to_alias = {tok: alias for alias, tok in expected.items()}
        all_uids: set[int] = set()
        deadline = time.monotonic() + HEALTH_CHECK_RECEIVE_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(HEALTH_CHECK_POLL_INTERVAL_S)
            try:
                found = await _scan_inbox_for_tokens(imap, baseline_uid)
            except Exception:
                LOGGER.debug(
                    "health check: scan failed (will retry)", exc_info=True
                )
                continue
            for token, uid in found.items():
                all_uids.add(uid)
                alias = token_to_alias.get(token)
                if alias and alias not in confirmed:
                    confirmed.add(alias)
            if confirmed >= set(expected):
                break
            await _refresh_progress()

        # 6. Anything still expected and unseen has timed out.
        for alias in expected:
            if alias not in confirmed:
                failed.add(alias)

        await _refresh_progress(force=True)

        # 7. Cleanup health-check messages from the INBOX so the user
        # doesn't see a pile of [health-check] entries when they open
        # Proton webmail.
        await _cleanup_inbox(imap, sorted(all_uids))
    finally:
        try:
            await imap.logout()
        except Exception:
            LOGGER.debug("health check: imap.logout raised", exc_info=True)

    total_ok = len(confirmed)
    total = len(targets)
    if total_ok == total:
        summary = (
            f"🩺 Health check selesai — <b>{total_ok}/{total}</b> sync OK. "
            f"Semua alias <code>{primary.email}</code> sehat. ✅"
        )
    else:
        broken_sample = ", ".join(sorted(failed)[:10])
        more = "" if len(failed) <= 10 else f" (+{len(failed) - 10} lagi)"
        summary = (
            f"🩺 Health check selesai — <b>{total_ok}/{total}</b> sync OK. "
            f"{total - total_ok} alamat bermasalah: "
            f"<code>{broken_sample}</code>{more}\n\n"
            f"Routing alias baru di Proton kadang butuh 5-10 menit untuk "
            f"propagate. Coba /cekimap lagi nanti."
        )
    await bot.send_message(
        chat_id=chat_id,
        text=summary,
        parse_mode=ParseMode.HTML,
    )
    # Keep ``db`` in the signature for future modes (DB-backed history).
    _ = db
    _ = send_failures
