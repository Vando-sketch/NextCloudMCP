"""HTTP-level tests for the OAuth 2.1 auth layer (PersonalAuthProvider).

PersonalAuthProvider implements a full OAuth 2.1 + PKCE + Dynamic Client
Registration flow (see nextcloud_task_mcp.personal_auth, vendored from
crumrine/fastmcp-personal-auth). Driving that flow end-to-end - a real
client registering, opening a browser at /authorize, completing a redirect,
exchanging a code with a PKCE verifier - has no stable, automatable surface
in a unit test; it's normally exercised by an interactive OAuth client (e.g.
Claude.ai) or FastMCP's own upstream test suite for the underlying
InMemoryOAuthProvider machinery.

What we test instead, at the ASGI/middleware level via the real app FastMCP
builds from `auth=...`:
- unauthenticated and invalid-token requests to /mcp are rejected before any
  tool logic runs;
- the OAuth discovery and Dynamic Client Registration endpoints Claude's
  connector flow depends on are present and open (DCR is intentionally open
  by design - see PersonalAuthProvider's docstring);
- the redirect-domain allow-list in /authorize rejects a disallowed redirect
  URI;
- the MCP_OAUTH_PASSWORD gate in /authorize actually enforces the password
  (this codebase carries a local patch removing an upstream dead-code bug
  that made the password check unconditionally pass - see the "LOCAL PATCH"
  note in nextcloud_task_mcp/personal_auth.py - so this is a regression test
  for that fix, not just a feature test).

Tool logic remains tested independently of this auth layer in
tests/test_server.py, which calls registered tool functions directly and
never goes through HTTP.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest
from conftest import TEST_OAUTH_PASSWORD

from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.server import build_server


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def app(settings):
    mcp = build_server(settings, service=MagicMock(spec=CalDavService))
    return mcp.http_app()


async def _request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def test_mcp_endpoint_rejects_missing_token(app):
    response = _run(
        _request(app, "POST", "/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 401


def test_mcp_endpoint_rejects_invalid_token(app):
    response = _run(
        _request(
            app,
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        )
    )
    assert response.status_code == 401


def test_oauth_discovery_endpoint_exposed(app):
    response = _run(_request(app, "GET", "/.well-known/oauth-authorization-server"))
    assert response.status_code == 200
    body = response.json()
    assert "registration_endpoint" in body
    assert "authorization_endpoint" in body
    assert "token_endpoint" in body


def test_dynamic_client_registration_is_open(app):
    response = _run(
        _request(
            app,
            "POST",
            "/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            },
        )
    )
    assert response.status_code == 201
    assert "client_id" in response.json()


def test_authorize_rejects_disallowed_redirect_domain(app):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            register_response = await client.post(
                "/register",
                json={
                    "client_name": "malicious-client",
                    "redirect_uris": ["https://attacker.example.com/callback"],
                },
            )
            assert register_response.status_code == 201
            client_id = register_response.json()["client_id"]

            return await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "https://attacker.example.com/callback",
                    "response_type": "code",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                },
            )

    # AuthorizeError is surfaced as a redirect back to the (unvalidated) client
    # redirect_uri carrying an OAuth error, per the MCP SDK's standard error
    # handling - not a 4xx. The security guarantee we care about is that no
    # authorization code is ever issued for a disallowed redirect domain.
    authorize_response = _run(scenario())
    assert authorize_response.status_code == 302
    location = authorize_response.headers["location"]
    assert "error=access_denied" in location
    assert "code=" not in location


async def _register_and_authorize(app, *, state: str):
    """Register a client with an *allowed* redirect domain, then hit /authorize
    with the given `state` (used to smuggle the password, per PersonalAuthProvider's
    own design) and return the raw response."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        register_response = await client.post(
            "/register",
            json={
                "client_name": "password-gate-test-client",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            },
        )
        assert register_response.status_code == 201
        client_id = register_response.json()["client_id"]

        return await client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
                "state": state,
            },
        )


def test_authorize_rejects_missing_password(app):
    # Allowed redirect domain, but the state/scope never carries the configured
    # MCP_OAUTH_PASSWORD. Before the local patch, PersonalAuthProvider's dead
    # "auto-approve for allowed redirect domains" fallback made this succeed
    # regardless of the password - this is the regression test for that bug.
    response = _run(_register_and_authorize(app, state="not-the-password"))
    assert response.status_code == 302
    location = response.headers["location"]
    assert "error=access_denied" in location
    assert "code=" not in location


def test_authorize_succeeds_with_correct_password(app):
    response = _run(_register_and_authorize(app, state=TEST_OAUTH_PASSWORD))
    assert response.status_code == 302
    location = response.headers["location"]
    assert "code=" in location
    assert "error=" not in location


def test_authorize_rejects_password_sent_via_scope(app):
    # Regression test for LOCAL PATCH note 2 in personal_auth.py: the password
    # is only accepted via `state` (never persisted). `scope` is persisted
    # verbatim onto every token issued and would leak the password to disk for
    # the token's lifetime, so that channel was removed - a password sent only
    # via `scope` must now be rejected, even though `scope` is itself valid.
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # A real attacker following this path would register with the
            # password as its scope up front, exactly as reproduced here.
            register_response = await client.post(
                "/register",
                json={
                    "client_name": "scope-password-test-client",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                    "scope": TEST_OAUTH_PASSWORD,
                },
            )
            assert register_response.status_code == 201
            client_id = register_response.json()["client_id"]

            return await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "response_type": "code",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                    "scope": TEST_OAUTH_PASSWORD,
                    "state": "unrelated-state-value",
                },
            )

    response = _run(scenario())
    assert response.status_code == 302
    location = response.headers["location"]
    assert "error=" in location
    assert "code=" not in location
