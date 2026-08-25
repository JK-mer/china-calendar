"""Auth composition (#12): bearer-only, OAuth proxy, and both side by side.
The OAuth path is exercised with a faked discovery document — no network."""

import importlib

import pytest

FAKE_IDP = {
    "issuer": "https://nextcloud.example.org",
    "authorization_endpoint": "https://nextcloud.example.org/apps/oidc/authorize",
    "token_endpoint": "https://nextcloud.example.org/apps/oidc/token",
    "jwks_uri": "https://nextcloud.example.org/apps/oidc/jwks",
}


@pytest.fixture
def server_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_STORE_DIR", str(tmp_path / "store"))
    for var in ("PC_MCP_TOKEN", "PC_OIDC_CLIENT_ID", "PC_OIDC_CLIENT_SECRET",
                "PC_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    import china_calendar.mcp_server as mod
    importlib.reload(mod)
    return mod


class FakeResponse:
    def raise_for_status(self):
        return self

    def json(self):
        return FAKE_IDP


def test_no_env_no_auth(server_mod):
    assert server_mod.build_auth() is None


def test_bearer_only(server_mod, monkeypatch):
    monkeypatch.setenv("PC_MCP_TOKEN", "sekrit")
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    assert isinstance(server_mod.build_auth(), StaticTokenVerifier)


def test_oauth_only(server_mod, monkeypatch):
    monkeypatch.setenv("PC_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("PC_OIDC_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("PC_PUBLIC_URL", "https://pcal-mcp.example.org")
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    auth = server_mod.build_auth()
    assert isinstance(auth, OAuthProxy)


def test_bearer_plus_oauth_is_multiauth(server_mod, monkeypatch):
    monkeypatch.setenv("PC_MCP_TOKEN", "sekrit")
    monkeypatch.setenv("PC_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("PC_OIDC_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("PC_PUBLIC_URL", "https://pcal-mcp.example.org")
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())
    from fastmcp.server.auth import MultiAuth
    assert isinstance(server_mod.build_auth(), MultiAuth)


def test_discovery_failure_falls_back_to_bearer(server_mod, monkeypatch):
    monkeypatch.setenv("PC_MCP_TOKEN", "sekrit")
    monkeypatch.setenv("PC_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("PC_OIDC_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("PC_PUBLIC_URL", "https://pcal-mcp.example.org")
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("idp down")

    monkeypatch.setattr(httpx, "get", boom)
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    assert isinstance(server_mod.build_auth(), StaticTokenVerifier)
