"""Unit tests for :mod:`proton_telegram_bot.proxy_provider`.

We don't hit the network -- every fetch is patched to return a canned
payload so the test suite stays hermetic. The behaviours covered:

1. JSON parser handles geonode + proxyscrape + bare-list shapes.
2. Plain-text parser handles roosterkid (HTTPS-only, no scheme) and
   spys.me (extra columns).
3. ``acquire`` short-circuits on ``PROTON_PROXY_URL`` and returns
   without probing.
4. ``acquire`` returns ``None`` when every probe fails.
5. ``_rank_and_dedupe`` puts entries with reported latency before
   un-timed ones, then sorts by latency, then by protocol preference.
"""
from __future__ import annotations

import asyncio

import pytest

from proton_telegram_bot.proxy_provider import (
    ProxyEntry,
    ProxyProvider,
    _parse_payload,
    _parse_text_lines,
    _rank_and_dedupe,
)


def test_parse_payload_geonode_shape() -> None:
    payload = {
        "data": [
            {"ip": "1.1.1.1", "port": "8080", "protocols": ["http"], "responseTime": 250},
            {"ip": "2.2.2.2", "port": 3128, "protocols": ["https"], "responseTime": 100},
        ]
    }
    entries = _parse_payload(payload, source="geonode")
    assert len(entries) == 2
    assert entries[0].host == "1.1.1.1"
    assert entries[0].response_time_ms == 250
    assert entries[1].response_time_ms == 100


def test_parse_payload_proxyscrape_shape() -> None:
    payload = {
        "proxies": [
            {"proxy": "http://3.3.3.3:80"},
            {"proxy": "https://4.4.4.4:443"},
        ]
    }
    entries = _parse_payload(payload, source="proxyscrape")
    assert [e.server_url for e in entries] == [
        "http://3.3.3.3:80",
        "https://4.4.4.4:443",
    ]
    assert all(e.response_time_ms is None for e in entries)


def test_parse_payload_bare_list() -> None:
    payload = ["http://5.5.5.5:80", "6.6.6.6:8080"]
    entries = _parse_payload(payload, source="custom")
    assert len(entries) == 2
    assert entries[1].server_url == "http://6.6.6.6:8080"


def test_parse_text_roosterkid_defaults_to_https() -> None:
    body = "# header\n\n7.7.7.7:443\n8.8.8.8:8443\n"
    entries = _parse_text_lines(body, source="roosterkid")
    assert [e.server_url for e in entries] == [
        "https://7.7.7.7:443",
        "https://8.8.8.8:8443",
    ]


def test_parse_text_spys_strips_extra_columns() -> None:
    body = "9.9.9.9:8080 US-H-S-! 0.5\n10.10.10.10:3128 RU-A 1.2\n"
    entries = _parse_text_lines(body, source="spys.me")
    assert [e.server_url for e in entries] == [
        "http://9.9.9.9:8080",
        "http://10.10.10.10:3128",
    ]


def test_parse_text_skips_blank_and_comment_lines() -> None:
    body = "\n# comment\n\n11.11.11.11:80\n# another\n"
    entries = _parse_text_lines(body, source="x")
    assert len(entries) == 1


def test_rank_and_dedupe_sorts_by_latency_then_protocol() -> None:
    entries = [
        ProxyEntry(protocol="https", host="a", port=1, response_time_ms=None),
        ProxyEntry(protocol="https", host="b", port=2, response_time_ms=500),
        ProxyEntry(protocol="http", host="c", port=3, response_time_ms=200),
        ProxyEntry(protocol="http", host="b", port=2),  # dupe of (https, b, 2)? No: protocol differs.
    ]
    ranked = _rank_and_dedupe(entries)
    # Timed entries come first, sorted by latency.
    assert ranked[0].host == "c"  # 200ms
    assert ranked[1].host == "b" and ranked[1].response_time_ms == 500
    # Untimed entries follow, sorted by protocol preference (http before https).
    assert ranked[2].protocol == "http"
    assert ranked[3].protocol == "https"


def test_rank_and_dedupe_drops_duplicates() -> None:
    entries = [
        ProxyEntry(protocol="http", host="1.1.1.1", port=80, response_time_ms=100),
        ProxyEntry(protocol="http", host="1.1.1.1", port=80, response_time_ms=200),
    ]
    ranked = _rank_and_dedupe(entries)
    assert len(ranked) == 1
    # First-seen wins after sorting by latency, so the 100ms entry stays.
    assert ranked[0].response_time_ms == 100


@pytest.mark.asyncio
async def test_acquire_returns_explicit_proxy_without_probing(monkeypatch) -> None:
    """``PROTON_PROXY_URL`` -> use it verbatim; the public list is irrelevant."""
    provider = ProxyProvider(explicit_proxy="http://user:pass@private:8080")
    # Patching ``_fetch_all`` so the test fails loudly if the provider tries
    # to consult the public lists when an explicit proxy is set.
    async def _explode():
        raise AssertionError("should not fetch when explicit_proxy is set")

    provider._fetch_all = _explode  # type: ignore[assignment]
    entry = await provider.acquire()
    assert entry is not None
    assert entry.server_url == "http://private:8080"


@pytest.mark.asyncio
async def test_acquire_returns_none_when_every_probe_fails(monkeypatch) -> None:
    """If TCP probes all fail, callers fall back to a direct connection."""
    provider = ProxyProvider(probe_timeout=0.05, max_probes=3)
    fake = [
        ProxyEntry(protocol="http", host="0.0.0.1", port=1, response_time_ms=10),
        ProxyEntry(protocol="http", host="0.0.0.2", port=2, response_time_ms=20),
        ProxyEntry(protocol="http", host="0.0.0.3", port=3, response_time_ms=30),
    ]

    async def fake_fetch_all() -> list[ProxyEntry]:
        return fake

    provider._fetch_all = fake_fetch_all  # type: ignore[assignment]

    async def fake_probe(entry: ProxyEntry, *, timeout: float):
        from proton_telegram_bot.proxy_provider import _Probe

        return _Probe(entry=entry, ms=None, error=OSError("nope"))

    provider._probe = fake_probe  # type: ignore[assignment]
    entry = await provider.acquire()
    assert entry is None


@pytest.mark.asyncio
async def test_acquire_returns_first_successful_probe(monkeypatch) -> None:
    """The fastest live proxy wins, even if it isn't the lowest reported latency."""
    provider = ProxyProvider(probe_timeout=0.5, max_probes=3)
    fake = [
        ProxyEntry(protocol="http", host="slow", port=1, response_time_ms=10),
        ProxyEntry(protocol="http", host="fast", port=2, response_time_ms=20),
        ProxyEntry(protocol="http", host="dead", port=3, response_time_ms=30),
    ]

    async def fake_fetch_all() -> list[ProxyEntry]:
        return fake

    provider._fetch_all = fake_fetch_all  # type: ignore[assignment]

    async def fake_probe(entry: ProxyEntry, *, timeout: float):
        from proton_telegram_bot.proxy_provider import _Probe

        # ``slow`` takes a while and ``dead`` errors; ``fast`` resolves first.
        if entry.host == "slow":
            await asyncio.sleep(0.1)
            return _Probe(entry=entry, ms=100.0)
        if entry.host == "dead":
            return _Probe(entry=entry, ms=None, error=OSError("dead"))
        return _Probe(entry=entry, ms=5.0)

    provider._probe = fake_probe  # type: ignore[assignment]
    entry = await provider.acquire()
    assert entry is not None and entry.host == "fast"


def test_from_env_disabled_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("PROTON_USE_PROXY", "0")
    assert ProxyProvider.from_env() is None
    monkeypatch.setenv("PROTON_USE_PROXY", "false")
    assert ProxyProvider.from_env() is None


def test_from_env_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PROTON_USE_PROXY", raising=False)
    provider = ProxyProvider.from_env()
    assert provider is not None
    assert isinstance(provider, ProxyProvider)


def test_proxy_entry_parse_rejects_invalid() -> None:
    assert ProxyEntry.parse("not a url") is None
    assert ProxyEntry.parse("ftp://x:21") is None  # unsupported scheme
    assert ProxyEntry.parse("http://no-port") is None


def test_proxy_entry_parse_accepts_protocols() -> None:
    for scheme in ("http", "https", "socks4", "socks5"):
        entry = ProxyEntry.parse(f"{scheme}://1.2.3.4:8080")
        assert entry is not None
        assert entry.protocol == scheme


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """Make sure ``PROTON_PROXY_URL`` from the dev shell doesn't leak in."""
    monkeypatch.delenv("PROTON_PROXY_URL", raising=False)
    monkeypatch.delenv("PROTON_PROXY_LIST_URLS", raising=False)
    monkeypatch.delenv("PROTON_PROXY_PROBE_TIMEOUT", raising=False)
    monkeypatch.delenv("PROTON_PROXY_MAX_PROBES", raising=False)
    yield
