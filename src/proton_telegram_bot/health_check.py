"""Background health check for IMAP/SMTP listeners on /cekimap.

Workflow per primary:

1. Pull Bridge IMAP/SMTP credentials for the primary email out of the
   Bridge vault (Bridge serves all aliases of a primary through one
   single login).
2. Create **one fresh Mail.tm temp inbox per alias** so each alias has
   an isolated audit trail. Throttled to one create per
   ``MAILBOX_CREATE_THROTTLE_S`` to stay under Mail.tm's rate limit.
3. For each alias of the primary (+ the primary itself), send a tagged
   test email **from** that alias **to** that alias's dedicated temp
   inbox via Bridge SMTP. The token in the subject still uniquely ties
   subject ↔ alias even if Mail.tm assigns identical addresses.
4. Poll each temp inbox in parallel. As soon as a tagged subject
   appears, mark the alias confirmed.
5. Maintain a single rolling **progress message** in Telegram and edit
   it in place as aliases land — no spam of one ✅/❌ per alias.
   Important transitions (initial "started", final summary) stay as
   distinct messages so they remain visible above the rolling line.

Runs in a background asyncio task so the user can keep using the bot
(read mail, /list, /genaddr, …) while it executes.
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
# How often each per-alias poll task hits Mail.tm.
HEALTH_CHECK_POLL_INTERVAL_S = 3.0
# Spacing between Mail.tm account-create calls. Mail.tm's free tier
# rate-limits API calls; 1s is a comfortable default — for 20 aliases
# that's ~20s of setup, well below the receive deadline.
MAILBOX_CREATE_THROTTLE_S = 1.0
# How often the rolling Telegram progress message is edited. Telegram
# limits edits to ~1/sec per chat; 2.0s is conservative and avoids
# burning ratelimits when a primary has 20+ aliases.
PROGRESS_EDIT_INTERVAL_S = 2.0


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
        # Truncate long failure lists so the message stays under
        # Telegram's 4096-char cap.
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


async def _create_mailbox_with_retry(
    client: httpx.AsyncClient,
    *,
    attempts: int = 3,
    backoff_s: float = 2.0,
) -> TempMailbox | None:
    """Create one ``TempMailbox`` with retry on transient failures.

    Returns ``None`` after ``attempts`` failures so the caller can mark
    the corresponding alias as send-failed instead of crashing the
    whole batch.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await TempMailbox.create(client)
        except (TempMailError, httpx.HTTPError) as exc:
            last_exc = exc
            LOGGER.warning(
                "health check: temp mailbox create attempt %d/%d failed: %s",
                i + 1,
                attempts,
                exc,
            )
            await asyncio.sleep(backoff_s * (i + 1))
    LOGGER.error("health check: gave up creating temp mailbox: %s", last_exc)
    return None


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
            f"Setiap alias dapat temp mailbox sendiri. "
            f"Bot tetap bisa dipakai sambil menunggu hasil."
        ),
        parse_mode=ParseMode.HTML,
    )

    # 2. Rolling progress message — edited in place from now on.
    confirmed: set[str] = set()
    failed: set[str] = set()
    progress_lock = asyncio.Lock()

    async def _send_initial_progress() -> int | None:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=_format_progress(
                primary.email, len(targets), confirmed, failed, list(targets)
            ),
            parse_mode=ParseMode.HTML,
        )
        # python-telegram-bot returns a Message object; tests use a fake
        # bot that returns ``None`` so we tolerate both.
        return getattr(msg, "message_id", None) if msg is not None else None

    progress_msg_id = await _send_initial_progress()

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

    # 3. Per-alias mailbox creation, throttled. Tracks ``alias -> mailbox``.
    mailboxes: dict[str, TempMailbox] = {}
    setup_failures: list[tuple[str, str]] = []
    async with httpx.AsyncClient() as client:
        for alias_email in targets:
            mb = await _create_mailbox_with_retry(client)
            if mb is None:
                setup_failures.append(
                    (alias_email, "tidak bisa bikin temp mailbox (Mail.tm error)")
                )
                failed.add(alias_email)
                await _refresh_progress()
                continue
            mailboxes[alias_email] = mb
            # Mark the alias as still pending; let the progress refresh
            # show the user mailbox setup is making progress.
            await _refresh_progress()
            await asyncio.sleep(MAILBOX_CREATE_THROTTLE_S)

    # 4. SMTP send (sequential — Bridge serializes SMTP sessions).
    expected: dict[str, tuple[str, TempMailbox]] = {}  # alias -> (token, mailbox)
    send_failures: list[tuple[str, str]] = []

    async def _send_one(alias_email: str, mailbox: TempMailbox) -> None:
        token = secrets.token_hex(8)
        subject = f"[health-check] {token} {alias_email}"
        body = (
            f"Health check from {alias_email} via Bridge SMTP.\n"
            f"Token: {token}\n"
            f"Inbox: {mailbox.address}\n"
        )
        try:
            await asyncio.to_thread(
                _smtp_send,
                host=BRIDGE_SMTP_HOST,
                port=BRIDGE_SMTP_PORT,
                username=creds.imap_username,
                password=creds.imap_password,
                from_addr=alias_email,
                to_addr=mailbox.address,
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
        expected[alias_email] = (token, mailbox)

    for alias_email, mailbox in mailboxes.items():
        await _send_one(alias_email, mailbox)

    # If everything failed at the send stage, short-circuit with a final
    # summary so the user isn't left staring at a frozen progress bar.
    if not expected:
        await _refresh_progress(force=True)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🩺 Health check selesai — <b>0/{len(targets)}</b> sync. "
                f"Tidak ada email yang berhasil dikirim."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # 5. Per-alias poll tasks. Each polls its own mailbox until the
    # token shows up or the global deadline passes; result feeds the
    # rolling progress refresh.
    deadline = time.monotonic() + HEALTH_CHECK_RECEIVE_TIMEOUT_S

    async def _watch(alias_email: str, token: str, mailbox: TempMailbox) -> None:
        async with httpx.AsyncClient() as poll_client:
            while time.monotonic() < deadline:
                await asyncio.sleep(HEALTH_CHECK_POLL_INTERVAL_S)
                try:
                    subjects = await mailbox.list_subjects(poll_client)
                except Exception:
                    LOGGER.debug(
                        "health check: list_subjects raised for %s",
                        alias_email,
                        exc_info=True,
                    )
                    continue
                if any(token in subj for subj in subjects):
                    confirmed.add(alias_email)
                    await _refresh_progress()
                    return
        # Deadline exceeded without the token showing up.
        if alias_email not in confirmed:
            failed.add(alias_email)
            await _refresh_progress()

    await asyncio.gather(
        *[
            _watch(alias_email, token, mailbox)
            for alias_email, (token, mailbox) in expected.items()
        ]
    )

    # 6. Final progress refresh + summary message (separate so the user
    # sees a distinct "selesai" line).
    await _refresh_progress(force=True)

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
    # Mark unused for static analysers; ``db`` is kept in the signature
    # so future modes (DB-backed history) don't have to refactor every
    # call site.
    _ = db
    _ = setup_failures
    _ = send_failures
