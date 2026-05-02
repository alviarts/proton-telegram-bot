"""Background health check for IMAP/SMTP listeners on /cekimap.

Workflow per primary:

1. Pull Bridge IMAP/SMTP credentials for the primary email out of the
   Bridge vault (Bridge serves all aliases of a primary through one
   single login).
2. Create a fresh Mail.tm temp inbox.
3. For each alias of the primary (+ the primary itself), send a tagged
   test email **from** that alias **to** the temp inbox via Bridge SMTP.
   The ``From`` header carries the alias address so a working alias is
   the one that successfully delivers a message Mail.tm can read.
4. Poll the temp inbox in a loop. As soon as a tagged subject appears,
   notify the user with ``✅ <alias> sudah berhasil di-sync``. After the
   timeout, anything not seen is reported as ``❌ <alias> gagal sync``.
5. Send a final summary line ``🩺 Health check selesai — N/M sync OK``.

The whole thing runs in a background asyncio task so the user can keep
using the bot (read mail, /list, /genaddr, ...) while it executes.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import smtplib
import time
from email.message import EmailMessage

import httpx
from telegram.constants import ParseMode

from .bridge_admin import BridgeAdmin
from .db import Database
from .models import PrimaryAccount
from .tempmail import TempMailbox, TempMailError

LOGGER = logging.getLogger(__name__)

# Bridge defaults (must match bot.CONNECT_DEFAULT_HOST + BRIDGE_SMTP_PORT).
BRIDGE_SMTP_HOST = "127.0.0.1"
BRIDGE_SMTP_PORT = 1025
SMTP_SEND_TIMEOUT_S = 30

# Total time we allow Mail.tm to receive every alias's test email after we
# finish sending the whole batch. Mail.tm typically delivers within ~10s,
# but Bridge → Proton routing of internal sender→tempmail can occasionally
# be slow so we err on the generous side.
HEALTH_CHECK_RECEIVE_TIMEOUT_S = 120
# How often we poll Mail.tm for new messages while waiting.
HEALTH_CHECK_POLL_INTERVAL_S = 3.0


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
    ``primary``. Posts live progress + a final summary to ``chat_id``.

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

    async with httpx.AsyncClient() as client:
        try:
            tempmail = await TempMailbox.create(client)
        except TempMailError as exc:
            LOGGER.warning("health check: TempMailbox.create failed: %s", exc)
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Gagal bikin temp mail: {exc!s}",
            )
            return

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🩺 Health check <b>{primary.email}</b> dimulai.\n"
            f"Total target: <b>{len(targets)}</b> alamat.\n"
            f"Temp mail: <code>{tempmail.address}</code>\n"
            f"Bot tetap bisa dipakai sambil menunggu hasil."
        ),
        parse_mode=ParseMode.HTML,
    )

    # Send all test emails first, in parallel via to_thread. We tag each
    # one with a unique 8-byte token so we can match it back to a
    # specific alias when subjects show up in the temp inbox.
    expected: dict[str, str] = {}
    send_failures: list[tuple[str, str]] = []  # [(alias, error_summary)]

    async def _send_one(alias_email: str) -> None:
        token = secrets.token_hex(8)
        subject = f"[health-check] {token} {alias_email}"
        body = (
            f"Health check from {alias_email} via Bridge SMTP.\n"
            f"Token: {token}\n"
        )
        try:
            await asyncio.to_thread(
                _smtp_send,
                host=BRIDGE_SMTP_HOST,
                port=BRIDGE_SMTP_PORT,
                username=creds.imap_username,
                password=creds.imap_password,
                from_addr=alias_email,
                to_addr=tempmail.address,
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
            return
        expected[alias_email] = token

    # Send sequentially (not gather'd): Bridge serializes SMTP sessions
    # in practice, and with ~1-2s per send a 20-alias batch finishes in
    # ~30s — well below the receive-deadline. Serializing avoids
    # spurious "too many connections" rejections from Bridge.
    for alias_email in targets:
        await _send_one(alias_email)

    # Notify per-alias for any send failures right away (no point waiting
    # for them in the receive loop).
    for alias_email, err in send_failures:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ <code>{alias_email}</code> gagal SMTP send "
                f"({err}). Bridge mungkin belum kenal alias ini."
            ),
            parse_mode=ParseMode.HTML,
        )

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

    # Poll the temp inbox until everything in ``expected`` is confirmed
    # or the deadline passes.
    confirmed: set[str] = set()
    deadline = time.monotonic() + HEALTH_CHECK_RECEIVE_TIMEOUT_S
    async with httpx.AsyncClient() as client:
        while confirmed != set(expected) and time.monotonic() < deadline:
            await asyncio.sleep(HEALTH_CHECK_POLL_INTERVAL_S)
            try:
                subjects = await tempmail.list_subjects(client)
            except Exception:
                LOGGER.debug("health check: list_subjects raised", exc_info=True)
                continue
            for alias_email, token in list(expected.items()):
                if alias_email in confirmed:
                    continue
                if any(token in subj for subj in subjects):
                    confirmed.add(alias_email)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"✅ <code>{alias_email}</code> sudah berhasil "
                            f"di-sync."
                        ),
                        parse_mode=ParseMode.HTML,
                    )

    failed = [a for a in expected if a not in confirmed]
    for alias_email in failed:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ <code>{alias_email}</code> gagal sync (timeout "
                f"{HEALTH_CHECK_RECEIVE_TIMEOUT_S}s). IMAP listener atau "
                f"routing alias mungkin bermasalah."
            ),
            parse_mode=ParseMode.HTML,
        )

    total_ok = len(confirmed)
    total = len(targets)
    if total_ok == total:
        summary = (
            f"🩺 Health check selesai — <b>{total_ok}/{total}</b> sync OK. "
            f"Semua alias <code>{primary.email}</code> sehat. ✅"
        )
    else:
        summary = (
            f"🩺 Health check selesai — <b>{total_ok}/{total}</b> sync OK. "
            f"{total - total_ok} alamat bermasalah."
        )
    await bot.send_message(
        chat_id=chat_id,
        text=summary,
        parse_mode=ParseMode.HTML,
    )
    # Mark unused for static analysers; ``db`` is kept in the signature
    # so future extensions (e.g. persisting per-alias health status) can
    # land without changing call sites.
    _ = db
