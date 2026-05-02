"""systemd ``sd_notify`` integration for liveness and watchdog signaling.

The bot is deployed under ``systemd`` with ``Type=notify`` and a
``WatchdogSec=`` directive. Under that contract the service must:

1. Send ``READY=1`` once startup has finished. Until this is sent
   ``systemctl start`` blocks and ``ActiveState`` stays ``activating``.
2. Send ``WATCHDOG=1`` at least every ``WatchdogSec`` seconds. systemd
   exposes the recommended interval (half of ``WatchdogSec``) via the
   ``WATCHDOG_USEC`` environment variable.
3. Send ``STOPPING=1`` before clean shutdown so systemd records the
   transition correctly.

If ``WATCHDOG=1`` stops arriving — because the asyncio event loop is
genuinely deadlocked, a sync call has blocked indefinitely, or the
process is otherwise hung — systemd kills the unit (SIGTERM, then
SIGKILL after ``TimeoutStopSec``) and ``Restart=always`` brings it
back up. This catches "process running but stuck" cases that
``Restart=on-failure`` alone cannot detect.

This module is a pure-Python implementation of the protocol so we
don't have to ship ``libsystemd`` / ``python-systemd``. When
``$NOTIFY_SOCKET`` is unset (e.g. local dev, pytest, Docker without
``--systemd``) every entry point becomes a silent no-op so the bot
runs unchanged outside systemd.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Final

LOGGER = logging.getLogger(__name__)

NOTIFY_SOCKET_ENV: Final = "NOTIFY_SOCKET"
WATCHDOG_USEC_ENV: Final = "WATCHDOG_USEC"
# Fallback heartbeat interval used when systemd doesn't advertise a
# ``WATCHDOG_USEC``. Five seconds matches the cadence the user asked
# for and is short enough to detect a real hang well within the unit's
# ``WatchdogSec=15s`` budget.
DEFAULT_HEARTBEAT_SECONDS: Final = 5.0


def notify(state: str) -> bool:
    """Send a single ``sd_notify``-style datagram to systemd.

    Returns ``True`` when the datagram was sent, ``False`` when there
    is no notify socket configured or the send failed (e.g. systemd
    was restarted out from under us). Errors are logged at DEBUG so
    they don't pollute the journal in non-systemd contexts.
    """
    addr = os.environ.get(NOTIFY_SOCKET_ENV)
    if not addr:
        return False
    # Linux abstract-namespace sockets are advertised with a leading
    # ``@`` which sd_notify clients must translate back to the NUL
    # byte the kernel actually expects.
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
        return True
    except OSError as exc:
        LOGGER.debug("sd_notify(%r) failed: %s", state, exc)
        return False


def heartbeat_interval_seconds(default: float = DEFAULT_HEARTBEAT_SECONDS) -> float:
    """Compute the heartbeat cadence from ``$WATCHDOG_USEC``.

    systemd sets ``WATCHDOG_USEC`` to the timeout in microseconds. The
    convention is to send ``WATCHDOG=1`` at half that interval so
    transient scheduling jitter doesn't trip the watchdog.
    """
    raw = os.environ.get(WATCHDOG_USEC_ENV)
    if not raw:
        return default
    try:
        usec = int(raw)
    except ValueError:
        return default
    if usec <= 0:
        return default
    return max(1.0, usec / 1_000_000 / 2)


async def heartbeat_loop(interval_seconds: float | None = None) -> None:
    """Background task: send ``WATCHDOG=1`` every ``interval_seconds``.

    Exits immediately when ``$NOTIFY_SOCKET`` is unset so this is safe
    to spawn unconditionally during application startup.
    """
    if not os.environ.get(NOTIFY_SOCKET_ENV):
        LOGGER.debug("NOTIFY_SOCKET not set — watchdog heartbeat disabled")
        return
    if interval_seconds is None:
        interval_seconds = heartbeat_interval_seconds()
    LOGGER.info(
        "systemd watchdog heartbeat enabled (interval=%.1fs)", interval_seconds
    )
    try:
        while True:
            notify("WATCHDOG=1")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        LOGGER.info("watchdog heartbeat task cancelled")
        raise
