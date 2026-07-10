"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import Response

from nextcloud_task_mcp.config import Settings
from nextcloud_task_mcp.personal_auth import PersonalAuthProvider

#: Matches the value baked into the `settings` fixture below - tests that need
#: to exercise the OAuth password gate reference this directly.
TEST_OAUTH_PASSWORD = "test-oauth-password"


def run_async(coro: Any) -> Any:
    """`asyncio.run` wrapper shared by tests that drive PersonalAuthProvider's
    async methods directly (it has no sync API - it's built for an ASGI app)."""
    return asyncio.run(coro)


async def register_oauth_client(
    provider: PersonalAuthProvider,
    *,
    redirect_uri: str = "https://claude.ai/api/mcp/auth_callback",
) -> OAuthClientInformationFull:
    """Register a client directly against the provider (bypassing the /register
    HTTP endpoint - same effect, since PersonalAuthProvider.register_client is
    what that endpoint calls)."""
    client = OAuthClientInformationFull(
        client_id=f"client-{secrets.token_hex(8)}",
        # pydantic coerces the str to AnyUrl at validation time; mypy only
        # sees the declared field type.
        redirect_uris=[redirect_uri],  # type: ignore[list-item]
    )
    await provider.register_client(client)
    return client


def make_auth_params(
    client: OAuthClientInformationFull, *, state: str | None = "client-csrf-state"
) -> AuthorizationParams:
    """The AuthorizationParams the framework's AuthorizationHandler would build
    from a valid /authorize request. `state` defaults to an opaque client CSRF
    token, which is all real claude.ai ever sends there."""
    assert client.redirect_uris
    return AuthorizationParams(
        state=state,
        scopes=[],
        code_challenge="x" * 43,
        redirect_uri=client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
    )


async def submit_consent(
    provider: PersonalAuthProvider,
    pending_key: str,
    password: str,
    *,
    client_host: str = "127.0.0.1",
) -> Response:
    """POST the consent form the way a browser would, driving the provider's
    real route endpoint with a hand-built Starlette request (no ASGI app
    needed for provider-level tests)."""
    body = urlencode({"pending": pending_key, "password": password}).encode()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/consent",
        "raw_path": b"/consent",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": (client_host, 52000),
        "server": ("test.example.com", 443),
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return await provider.handle_consent_submission(Request(scope, receive))


async def authorize_and_get_code(
    provider: PersonalAuthProvider,
    client: OAuthClientInformationFull,
) -> AuthorizationCode:
    """Drive `authorize()` plus - when a password is configured - the consent
    form it now redirects to, and return the `AuthorizationCode` object the
    provider stored for the code in the final client redirect, exactly as the
    framework would fetch it before calling `exchange_authorization_code`."""
    redirect_url = await provider.authorize(client, make_auth_params(client))
    if provider.password:
        pending_key = parse_qs(urlparse(redirect_url).query)["pending"][0]
        response = await submit_consent(provider, pending_key, provider.password)
        assert response.status_code == 302
        redirect_url = response.headers["location"]
    code = parse_qs(urlparse(redirect_url).query)["code"][0]
    return provider.auth_codes[code]


async def issue_token(
    provider: PersonalAuthProvider,
) -> tuple[OAuthClientInformationFull, OAuthToken]:
    """Full happy path: register a client, authorize it (completing the
    consent form when a password is set), and exchange the resulting code for
    an access/refresh token pair."""
    client = await register_oauth_client(provider)
    auth_code = await authorize_and_get_code(provider, client)
    token = await provider.exchange_authorization_code(client, auth_code)
    return client, token


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A Settings instance with dummy values, no environment variables required.

    `oauth_state_dir` points at a per-test tmp_path so PersonalAuthProvider's
    token persistence never touches the repo or leaks state between tests.
    `public_base_url` is deliberately non-local (mirrors a real deployment) so
    `oauth_password` must be set too - Settings enforces that pairing itself.
    """
    return Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        public_base_url="https://test.example.com",
        oauth_password=TEST_OAUTH_PASSWORD,
        oauth_state_dir=str(tmp_path / "oauth-state"),
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
    )
