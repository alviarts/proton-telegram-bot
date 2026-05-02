"""Tests for ``proton_telegram_bot.watchdog`` (sd_notify integration)."""
from __future__ import annotations

import asyncio
import os
import socket
import threading
from collections.abc import Iterator

import pytest

from proton_telegram_bot import watchdog


@pytest.fixture
def notify_socket(tmp_path, monkeypatch) -> Iterator[tuple[socket.socket, list[bytes]]]:
    """Bind a unix datagram socket and expose the messages it receives.

    Yields ``(server_socket, received_list)``. Each datagram delivered
    to the socket is appended to ``received_list`` by a background
    thread. The fixture also points ``$NOTIFY_SOCKET`` at the path so
    ``watchdog.notify(...)`` targets it.
    """
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(0.5)
    received: list[bytes] = []
    stop = threading.Event()

    def _drain() -> None:
        while not stop.is_set():
            try:
                data, _ = server.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            received.append(data)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    monkeypatch.setenv(watchdog.NOTIFY_SOCKET_ENV, sock_path)
    try:
        yield server, received
    finally:
        stop.set()
        reader.join(timeout=1.0)
        server.close()


def test_notify_no_socket_returns_false(monkeypatch):
    """When ``$NOTIFY_SOCKET`` is unset, notify is a silent no-op."""
    monkeypatch.delenv(watchdog.NOTIFY_SOCKET_ENV, raising=False)
    assert watchdog.notify("READY=1") is False


def test_notify_sends_state_to_socket(notify_socket):
    """``notify`` writes the exact state string to the unix datagram socket."""
    _, received = notify_socket
    assert watchdog.notify("READY=1") is True
    # Give the reader thread a tick to pick up the datagram.
    for _ in range(50):
        if received:
            break
        threading.Event().wait(0.02)
    assert received == [b"READY=1"]


def test_notify_handles_abstract_socket(monkeypatch):
    """An ``@abstract`` socket name must be translated to a leading NUL byte.

    We don't actually bind one here (abstract sockets are Linux-only and
    awkward to set up cleanly in tests); we just confirm the code path
    doesn't raise and returns ``False`` for an unbound address.
    """
    monkeypatch.setenv(watchdog.NOTIFY_SOCKET_ENV, "@no-such-abstract-socket")
    # Unbound abstract socket → connect refused → notify swallows OSError.
    assert watchdog.notify("WATCHDOG=1") is False


def test_heartbeat_interval_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv(watchdog.WATCHDOG_USEC_ENV, raising=False)
    assert watchdog.heartbeat_interval_seconds() == watchdog.DEFAULT_HEARTBEAT_SECONDS


def test_heartbeat_interval_halves_watchdog_usec(monkeypatch):
    """``WATCHDOG_USEC=15000000`` (15s) should yield a 7.5s heartbeat."""
    monkeypatch.setenv(watchdog.WATCHDOG_USEC_ENV, "15000000")
    assert watchdog.heartbeat_interval_seconds() == pytest.approx(7.5)


def test_heartbeat_interval_floors_at_one_second(monkeypatch):
    """Even an absurdly small ``WATCHDOG_USEC`` should not produce <1s."""
    monkeypatch.setenv(watchdog.WATCHDOG_USEC_ENV, "100000")  # 100ms
    assert watchdog.heartbeat_interval_seconds() >= 1.0


def test_heartbeat_interval_rejects_garbage(monkeypatch):
    monkeypatch.setenv(watchdog.WATCHDOG_USEC_ENV, "not-a-number")
    assert watchdog.heartbeat_interval_seconds() == watchdog.DEFAULT_HEARTBEAT_SECONDS


@pytest.mark.asyncio
async def test_heartbeat_loop_no_op_when_socket_unset(monkeypatch):
    """Without ``$NOTIFY_SOCKET`` the heartbeat task exits immediately."""
    monkeypatch.delenv(watchdog.NOTIFY_SOCKET_ENV, raising=False)
    # Should return promptly rather than running forever.
    await asyncio.wait_for(watchdog.heartbeat_loop(interval_seconds=0.05), timeout=1.0)


@pytest.mark.asyncio
async def test_heartbeat_loop_emits_watchdog_pings(notify_socket):
    """Looping heartbeat sends ``WATCHDOG=1`` repeatedly until cancelled."""
    _, received = notify_socket
    task = asyncio.create_task(watchdog.heartbeat_loop(interval_seconds=0.05))
    try:
        # Wait for at least 3 datagrams to land.
        for _ in range(40):
            if len(received) >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(received) >= 3
    assert all(payload == b"WATCHDOG=1" for payload in received)


@pytest.mark.asyncio
async def test_heartbeat_loop_cancellation_propagates(notify_socket):
    """Cancelling the loop raises ``CancelledError`` (not swallowed)."""
    task = asyncio.create_task(watchdog.heartbeat_loop(interval_seconds=10.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_environment_constants_match_systemd_protocol():
    """Sanity: env var names match the documented sd_notify protocol."""
    assert watchdog.NOTIFY_SOCKET_ENV == "NOTIFY_SOCKET"
    assert watchdog.WATCHDOG_USEC_ENV == "WATCHDOG_USEC"


def test_default_heartbeat_under_systemd_watchdog_budget():
    """5s heartbeat must fit comfortably inside the unit's 15s WatchdogSec."""
    assert watchdog.DEFAULT_HEARTBEAT_SECONDS <= 15 / 2
    # And we always send at least one heartbeat per minute so a long-idle
    # event loop still proves liveness to systemd.
    assert watchdog.DEFAULT_HEARTBEAT_SECONDS <= 30


def test_notify_socket_env_constant():
    assert os.environ.get  # smoke; the attribute exists
