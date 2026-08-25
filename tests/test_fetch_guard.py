"""SSRF guard on the caller-supplied fetch path."""

import pytest

from china_calendar.fetch import BlockedURL, FetchError, guard_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://example.org/",
    "ftp://example.org/x",
])
def test_non_http_schemes_refused(url):
    with pytest.raises(BlockedURL):
        guard_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8810/",
    "http://192.168.1.10:3002/",             # a git forge on the LAN
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://[::1]/",
    "http://[fd00::1]/",                 # IPv6 ULA
])
def test_private_destinations_refused(url):
    with pytest.raises(BlockedURL):
        guard_url(url)


def test_hostname_resolving_to_private_address_refused(monkeypatch):
    """The literal is not the only way in — a name pointing at the LAN is
    the same request with a friendlier face."""
    import socket

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BlockedURL):
        guard_url("https://internal-service.example.org/")


def test_public_address_allowed(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guard_url("https://example.org/page")  # no raise


def test_unresolvable_host_is_a_fetch_error_not_a_block(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError) as exc:
        guard_url("https://nx.example/")
    assert not isinstance(exc.value, BlockedURL)
