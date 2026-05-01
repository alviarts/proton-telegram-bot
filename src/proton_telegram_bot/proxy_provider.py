"""Proxy rotation for Proton-bound traffic.

The Proton account web UI rate-limits unfamiliar IPs aggressively; running
``/genaddr`` repeatedly from the same VPS IP is a fast track to a ban. This
module fetches public proxy lists, ranks candidates by reported latency
(plus a TCP probe for sources that don't expose timing), and hands out
the fastest working proxy to the caller.

Four upstream sources are combined by default (in priority order):

* `geonode <https://proxylist.geonode.com>`_ — exposes ``responseTime`` and
  ``latency`` per entry, so ranking is meaningful without an extra probe.
* `roosterkid/openproxylist <https://github.com/roosterkid/openproxylist>`_
  ``HTTPS.txt`` — plain text, HTTPS-only, regularly refreshed.
* `spys.me <https://spys.me/proxy.txt>`_ — plain text dump from spys.
* `proxyscrape <https://api.proxyscrape.com>`_ — large list, no timing
  metadata, ranked purely by TCP-connect probe time.

Configuration (environment variables):

* ``PROTON_USE_PROXY`` -- ``"0"`` to disable entirely (default: enabled).
* ``PROTON_PROXY_URL`` -- explicit single proxy URL, e.g.
  ``http://user:pass@host:port``. When set, the public lists are not
  consulted. Use this for paid/private proxies.
* ``PROTON_PROXY_LIST_URLS`` -- comma-separated override of the source
  URLs. Defaults to geonode + proxyscrape.
* ``PROTON_PROXY_PROBE_TIMEOUT`` -- per-proxy TCP probe timeout in seconds
  (default ``3.0``).
* ``PROTON_PROXY_MAX_PROBES`` -- max proxies to probe per ``acquire()``
  (default ``10``).

The provider is intentionally best-effort: if every probed proxy fails it
returns ``None`` and the caller falls back to a direct connection.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

GEONODE_LIST_URL = (
    "https://proxylist.geonode.com/api/proxy-list"
    "?limit=500&page=1&sort_by=lastChecked&sort_type=desc"
)
ROOSTERKID_HTTPS_URL = (
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS.txt"
)
SPYSME_URL = "https://spys.me/proxy.txt"
PROXYSCRAPE_LIST_URL = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport&format=json"
)
DEFAULT_LIST_URLS = (
    GEONODE_LIST_URL,
    ROOSTERKID_HTTPS_URL,
    SPYSME_URL,
    PROXYSCRAPE_LIST_URL,
)
DEFAULT_LIST_TTL_SECONDS = 600.0
DEFAULT_PROBE_TIMEOUT = 3.0
DEFAULT_MAX_PROBES = 10
ACCEPTABLE_PROTOCOLS = ("http", "https", "socks4", "socks5")
# Playwright Chromium only supports HTTP and SOCKS5 cleanly; deprioritise the
# other protocols by giving them a high sort key so they sit at the end.
PROTOCOL_SORT_RANK = {"http": 0, "https": 1, "socks5": 2, "socks4": 3}


@dataclass(frozen=True)
class ProxyEntry:
    """One proxy candidate, optionally annotated with latency from upstream."""

    protocol: str
    host: str
    port: int
    # Source latency in milliseconds, when the upstream list provides it
    # (geonode does, proxyscrape does not). ``None`` means we'll fall back
    # to a TCP-connect probe to derive an effective latency.
    response_time_ms: float | None = None
    source: str = "unknown"

    @classmethod
    def parse(cls, raw: str, *, source: str = "unknown") -> ProxyEntry | None:
        """Parse a ``"protocol://host:port"`` string into an entry."""
        try:
            parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        except ValueError:
            return None
        protocol = (parsed.scheme or "http").lower()
        if protocol not in ACCEPTABLE_PROTOCOLS:
            return None
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            return None
        return cls(protocol=protocol, host=host, port=port, source=source)

    @property
    def server_url(self) -> str:
        """Return a Playwright-friendly ``"protocol://host:port"`` URL."""
        return f"{self.protocol}://{self.host}:{self.port}"


@dataclass
class _Cache:
    fetched_at: float
    entries: list[ProxyEntry]


@dataclass
class _Probe:
    """Result of a single TCP-connect probe; ``ms=None`` means failed."""

    entry: ProxyEntry
    ms: float | None = None
    error: BaseException | None = field(default=None, repr=False)


class ProxyProvider:
    """Cached, async-friendly source of fast working proxies.

    Concurrency: callers may call :meth:`acquire` from multiple coroutines.
    Internal state (the cached list + cache timestamp) is guarded by an
    ``asyncio.Lock``.
    """

    def __init__(
        self,
        *,
        list_urls: tuple[str, ...] | None = None,
        explicit_proxy: str | None = None,
        ttl_seconds: float = DEFAULT_LIST_TTL_SECONDS,
        probe_timeout: float | None = None,
        max_probes: int | None = None,
    ) -> None:
        if list_urls is None:
            env_urls = os.environ.get("PROTON_PROXY_LIST_URLS")
            if env_urls:
                list_urls = tuple(
                    u.strip() for u in env_urls.split(",") if u.strip()
                )
            else:
                list_urls = DEFAULT_LIST_URLS
        self._list_urls = list_urls
        self._explicit_proxy = explicit_proxy or os.environ.get(
            "PROTON_PROXY_URL"
        ) or None
        self._ttl = ttl_seconds
        self._probe_timeout = (
            probe_timeout
            if probe_timeout is not None
            else float(os.environ.get("PROTON_PROXY_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT))
        )
        self._max_probes = (
            max_probes
            if max_probes is not None
            else int(os.environ.get("PROTON_PROXY_MAX_PROBES", DEFAULT_MAX_PROBES))
        )
        self._lock = asyncio.Lock()
        self._cache: _Cache | None = None

    @classmethod
    def from_env(cls) -> ProxyProvider | None:
        """Build a provider from env vars, or return ``None`` if disabled.

        The bot's main entrypoint calls this and stows the result on
        ``application.bot_data["proxy_provider"]``. ``ProtonBrowser.session``
        consults that on every login so deployments can flip proxies on/off
        without a code change.
        """
        if os.environ.get("PROTON_USE_PROXY", "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return None
        return cls()

    async def acquire(self) -> ProxyEntry | None:
        """Return the fastest probably-alive proxy, or ``None`` on total failure.

        When ``PROTON_PROXY_URL`` is set we always return that one without
        probing -- a paid proxy is the user's problem to maintain.
        """
        if self._explicit_proxy:
            entry = ProxyEntry.parse(self._explicit_proxy, source="env")
            if entry is not None:
                return entry
            logger.warning(
                "PROTON_PROXY_URL=%r could not be parsed; falling back to direct",
                self._explicit_proxy,
            )
            return None

        async with self._lock:
            entries = await self._get_entries_locked()

        if not entries:
            logger.warning("proxy provider: no entries available; falling back to direct")
            return None

        # Take the top ``max_probes`` candidates (already sorted by reported
        # latency + protocol rank) and probe them in parallel. Whoever
        # finishes first AND succeeds wins -- that's the fastest live proxy
        # we can confirm without doing a full HTTPS handshake here.
        candidates = entries[: self._max_probes]
        probes = [
            asyncio.create_task(self._probe(c, timeout=self._probe_timeout))
            for c in candidates
        ]
        try:
            for future in asyncio.as_completed(probes):
                probe = await future
                if probe.ms is not None:
                    logger.info(
                        "proxy provider: using %s (source=%s, probe=%.0fms, "
                        "reported=%s, out of %d probed)",
                        probe.entry.server_url,
                        probe.entry.source,
                        probe.ms,
                        f"{probe.entry.response_time_ms:.0f}ms"
                        if probe.entry.response_time_ms is not None
                        else "n/a",
                        len(candidates),
                    )
                    return probe.entry
        finally:
            for task in probes:
                if not task.done():
                    task.cancel()
        logger.warning(
            "proxy provider: all %d probed candidates failed; falling back to direct",
            len(candidates),
        )
        return None

    async def _get_entries_locked(self) -> list[ProxyEntry]:
        now = time.monotonic()
        if self._cache and (now - self._cache.fetched_at) < self._ttl:
            return self._cache.entries
        try:
            entries = await self._fetch_all()
        except Exception:
            logger.exception(
                "proxy provider: fetch failed; serving stale (or empty) cache"
            )
            return self._cache.entries if self._cache else []
        self._cache = _Cache(fetched_at=now, entries=entries)
        logger.info(
            "proxy provider: refreshed list (%d candidates from %d sources)",
            len(entries),
            len(self._list_urls),
        )
        return entries

    async def _fetch_all(self) -> list[ProxyEntry]:
        """Fetch every configured source in parallel and merge the results.

        A source failing is non-fatal -- we want the union of what's
        reachable, not a hard dependency on every list being up.
        """
        results = await asyncio.gather(
            *(self._fetch_one(url) for url in self._list_urls),
            return_exceptions=True,
        )
        merged: list[ProxyEntry] = []
        for url, res in zip(self._list_urls, results, strict=False):
            if isinstance(res, BaseException):
                logger.warning(
                    "proxy provider: source %s failed: %s",
                    url,
                    res,
                )
                continue
            merged.extend(res)
        return _rank_and_dedupe(merged)

    async def _fetch_one(self, url: str) -> list[ProxyEntry]:
        # Lazy-import httpx so the module is importable in environments that
        # don't have it (some unit tests).
        import httpx

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                url, headers={"User-Agent": "proton-telegram-bot/1.0"}
            )
            resp.raise_for_status()
            body = resp.text
        source = _source_label_from_url(url)
        # Sniff format: JSON payloads start with ``{`` or ``[``; everything
        # else is treated as a line-oriented plain text list (roosterkid,
        # spys.me, raw .txt mirrors, ...).
        stripped = body.lstrip()
        if stripped.startswith(("{", "[")):
            import json

            try:
                payload = json.loads(body)
            except ValueError:
                logger.warning(
                    "proxy provider: %s returned non-JSON despite leading %r; "
                    "falling back to text parser",
                    source,
                    stripped[:1],
                )
                return _parse_text_lines(body, source=source)
            return _parse_payload(payload, source=source)
        return _parse_text_lines(body, source=source)

    async def _probe(self, entry: ProxyEntry, *, timeout: float) -> _Probe:
        """TCP-connect to the proxy, returning the round-trip time in ms.

        We deliberately do NOT do a full HTTP CONNECT/handshake here -- many
        free proxies pass a TCP connect but fail the upgrade. A tighter probe
        catches more issues but takes longer; we trade specificity for speed
        because Playwright will do the real handshake in seconds anyway and
        we'll fall back if it fails.
        """
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _tcp_probe, entry.host, entry.port),
                timeout=timeout,
            )
        except (TimeoutError, OSError) as exc:
            return _Probe(entry=entry, ms=None, error=exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("proxy probe %s failed", entry.server_url, exc_info=True)
            return _Probe(entry=entry, ms=None, error=exc)
        return _Probe(entry=entry, ms=(time.monotonic() - start) * 1000.0)


def _tcp_probe(host: str, port: int) -> None:
    """Synchronous TCP-connect helper run in a thread."""
    with socket.create_connection((host, port), timeout=3.0):
        pass


def _source_label_from_url(url: str) -> str:
    """Short label for log messages -- e.g. ``"geonode"`` from the geonode URL."""
    host = (urlparse(url).hostname or "").lower()
    if "geonode" in host:
        return "geonode"
    if "proxyscrape" in host:
        return "proxyscrape"
    if "roosterkid" in url or "openproxylist" in url:
        return "roosterkid"
    if "spys.me" in host:
        return "spys.me"
    return host or "unknown"


def _parse_payload(payload: object, *, source: str) -> list[ProxyEntry]:
    """Extract :class:`ProxyEntry` items from a source's JSON payload.

    Supported shapes:

    * geonode: ``{"data": [{"ip": "...", "port": "8080", "protocols":
      ["http"], "responseTime": 1234, "latency": 456, ...}, ...]}``
    * proxyscrape v4: ``{"proxies": [{"proxy": "http://1.2.3.4:8080",
      ...}, ...]}``
    * Older variants returning ``[...]`` or strings, also supported.
    """
    raw_list: list[object] = []
    if isinstance(payload, dict):
        for key in ("data", "proxies"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_list = value
                break
    elif isinstance(payload, list):
        raw_list = payload

    entries: list[ProxyEntry] = []
    for item in raw_list:
        parsed = _parse_item(item, source=source)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_item(item: object, *, source: str) -> ProxyEntry | None:
    if isinstance(item, str):
        return ProxyEntry.parse(item, source=source)
    if not isinstance(item, dict):
        return None

    # geonode: ip + port + protocols + responseTime/latency.
    # proxyscrape: ``proxy`` (full URL) or ``url``.
    url = item.get("proxy") or item.get("url")
    if isinstance(url, str):
        base = ProxyEntry.parse(url, source=source)
    else:
        ip = item.get("ip") or item.get("host")
        port = item.get("port")
        protocols = item.get("protocols") or item.get("protocol") or "http"
        if isinstance(protocols, list):
            protocol = protocols[0] if protocols else "http"
        else:
            protocol = protocols
        if not (isinstance(ip, str) and ip and port is not None):
            return None
        try:
            port_int = int(port)
        except (TypeError, ValueError):
            return None
        base = ProxyEntry.parse(
            f"{protocol}://{ip}:{port_int}", source=source
        )
    if base is None:
        return None

    # Pull latency from any of the well-known field names. We prefer
    # ``responseTime`` (geonode's end-to-end metric) over ``latency``
    # (which on geonode is the lower-level handshake time).
    rt: float | None = None
    for field_name in ("responseTime", "response_time", "latency", "speed"):
        val = item.get(field_name)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num > 0 and math.isfinite(num):
            rt = num
            break

    return ProxyEntry(
        protocol=base.protocol,
        host=base.host,
        port=base.port,
        response_time_ms=rt,
        source=source,
    )


def _rank_and_dedupe(entries: list[ProxyEntry]) -> list[ProxyEntry]:
    """Sort entries fastest-first, dedupe, and prefer http(s) over socks.

    Sort keys (lower = earlier):

    1. Has reported ``response_time_ms`` (entries without are tried after
       timed ones, since we have no signal on whether they're alive).
    2. Reported response time itself.
    3. Protocol preference (http < https < socks5 < socks4) -- Playwright
       Chromium handles http(s) most reliably.
    """
    def sort_key(entry: ProxyEntry) -> tuple[int, float, int]:
        has_rt = 0 if entry.response_time_ms is not None else 1
        rt = entry.response_time_ms if entry.response_time_ms is not None else float("inf")
        proto_rank = PROTOCOL_SORT_RANK.get(entry.protocol, 9)
        return (has_rt, rt, proto_rank)

    seen: set[tuple[str, str, int]] = set()
    ranked = sorted(entries, key=sort_key)
    deduped: list[ProxyEntry] = []
    for entry in ranked:
        key = (entry.protocol, entry.host, entry.port)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _parse_text_lines(body: str, *, source: str) -> list[ProxyEntry]:
    """Parse a plain-text proxy list (one entry per line).

    Accepted line shapes:

    * ``host:port`` -- bare; assumes ``http``.
    * ``protocol://host:port`` -- prefer this (roosterkid emits this).
    * ``host:port CC-X-Y-...`` -- spys.me's column format; we keep just
      the first whitespace-delimited token.

    Comment lines (starting with ``#``) and blank lines are ignored.
    """
    entries: list[ProxyEntry] = []
    # roosterkid's HTTPS.txt is HTTPS-only by definition; if the source
    # said so but a line lacks a scheme, default to https.
    default_scheme = "https" if source == "roosterkid" else "http"
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0]  # spys.me appends "CC-H-S-!" metadata.
        if "://" not in token:
            token = f"{default_scheme}://{token}"
        parsed = ProxyEntry.parse(token, source=source)
        if parsed is not None:
            entries.append(parsed)
    return entries


__all__ = [
    "DEFAULT_LIST_URLS",
    "GEONODE_LIST_URL",
    "PROXYSCRAPE_LIST_URL",
    "ROOSTERKID_HTTPS_URL",
    "SPYSME_URL",
    "ProxyEntry",
    "ProxyProvider",
]
