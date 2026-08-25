"""Polite fetch layer.

Conditional requests (ETag / If-Modified-Since) against per-source state,
robots.txt respected, real User-Agent with contact address, no retries within
a run — a failing source is recorded in its SourceState and picked up again
next sweep, never hammered.

`fetch_raw` is the UNTRUSTED path: the URL comes from a dashboard
form or an MCP caller, so it is SSRF-guarded — scheme allowlist, no private
destinations, every redirect hop re-checked, response size capped. `get` is
the trusted path (URLs come from sources.yaml on disk) and is not guarded.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import Config
from .models import SourceState, utcnow
from .store import Store


class FetchError(Exception):
    pass


class NoContent(FetchError):
    """HTTP 204. An error for most sources, but some APIs answer it for a
    legitimately empty result — the EP publishes one part-session's draft
    agenda at a time and 204s every sitting beyond it (#64). Subclasses
    FetchError so existing handlers are unchanged; callers that know 204 is
    normal catch this instead."""


class RobotsDisallowed(FetchError):
    pass


class BlockedURL(FetchError):
    """Caller-supplied URL points somewhere we refuse to fetch."""


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def guard_url(url: str) -> None:
    """Refuse non-HTTP schemes and destinations inside the LAN or the compose
    network. Every resolved address is checked, so a hostname pointing at
    192.168.x.x is refused like the literal. Not rebinding-proof: this lookup
    and httpx's are separate, which would need pinned-IP connections to close.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"only http/https may be fetched, not {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise BlockedURL(f"no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"{url}: cannot resolve {host} ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BlockedURL(
                f"{host} resolves to the non-public address {ip}; refusing to fetch")


@dataclass
class FetchResult:
    content: bytes
    status: int
    not_modified: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={"User-Agent": config.user_agent},
            follow_redirects=True,
            timeout=30,
        )

    def _robots_allows(self, url: str) -> bool:
        host = "{0}://{1}".format(*urlsplit(url)[:2])
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(f"{host}/robots.txt")
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                    self._robots[host] = parser
                else:
                    # No robots.txt (or it errors out server-side): allowed.
                    self._robots[host] = None
            except httpx.HTTPError:
                # Can't even fetch robots.txt — the main fetch will fail the
                # same way and get recorded; don't treat it as a disallow.
                self._robots[host] = None
        parser = self._robots[host]
        return parser is None or parser.can_fetch(self.config.user_agent, url)

    def _guarded_get(self, url: str) -> bytes:
        """GET with every redirect hop re-guarded and the body size capped.
        Redirects are followed by hand because httpx's own follower would
        chase a 302 into the LAN without consulting `guard_url` again."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            guard_url(current)
            try:
                with self._client.stream("GET", current, follow_redirects=False) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(f"HTTP {resp.status_code} without Location for {current}")
                        current = str(resp.url.join(location))
                        continue
                    if resp.status_code == 204:
                        raise NoContent(f"HTTP 204 for {current}")
                    if resp.status_code != 200:
                        raise FetchError(f"HTTP {resp.status_code} for {current}")
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise FetchError(
                                f"response from {current} exceeds {MAX_RESPONSE_BYTES} bytes")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise FetchError(f"{current}: {exc}") from exc
        raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url}")

    def fetch_raw(self, url: str, ignore_robots: bool | None = None) -> FetchResult:
        """One-off fetch with no SourceState involvement (verification pass).

        SSRF-guarded: the URL is caller-supplied. Robots handling defaults to
        the same answer the sweep would give for that host — left
        to itself, verifying a URL the sweep fetches nightly would fail with
        RobotsDisallowed, and a disabled source behind a disallow would probe
        unreachable forever.
        """
        guard_url(url)  # before robots.txt — the probe must not reach the LAN either
        if ignore_robots is None:
            from .sources.base import robots_exempt
            ignore_robots = robots_exempt(url)
        if not ignore_robots and not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return FetchResult(content=self._guarded_get(url), status=200)

    def get(self, source_id: str, url: str, force: bool = False,
            ignore_robots: bool = False) -> FetchResult:
        """Conditional GET. Raises FetchError on failure; updates SourceState
        cache validators on success. Does not touch item counts — that is the
        sweep's job, since it knows how parsing went."""
        if not ignore_robots and not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        state = self.store.source_state(source_id)
        headers = {}
        if not force:
            if state.etag:
                headers["If-None-Match"] = state.etag
            if state.last_modified:
                headers["If-Modified-Since"] = state.last_modified

        try:
            resp = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            state.last_run = utcnow()
            state.last_error = f"{type(exc).__name__}: {exc}"
            self.store.save_source_state(state)
            raise FetchError(f"{source_id}: {exc}") from exc

        state.last_run = utcnow()
        if resp.status_code == 304:
            state.last_error = None
            self.store.save_source_state(state)
            return FetchResult(content=b"", status=304, not_modified=True)
        if resp.status_code == 204:
            # Not an error, and deliberately not recorded as one: a source that
            # legitimately 204s (next year's EP sittings, every day until the
            # EP loads them) would otherwise look permanently sick.
            state.last_error = None
            self.store.save_source_state(state)
            raise NoContent(f"{source_id}: HTTP 204 for {url}")
        if resp.status_code != 200:
            state.last_error = f"HTTP {resp.status_code}"
            self.store.save_source_state(state)
            raise FetchError(f"{source_id}: HTTP {resp.status_code} for {url}")

        state.etag = resp.headers.get("etag")
        state.last_modified = resp.headers.get("last-modified")
        state.last_error = None
        self.store.save_source_state(state)
        return FetchResult(content=resp.content, status=200)

    def close(self) -> None:
        self._client.close()
