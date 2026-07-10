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
- the MCP_OAUTH_PASSWORD gate is the interactive /consent page (LOCAL PATCH 5
  in nextcloud_task_mcp/personal_auth.py - live testing against production
  claude.ai showed the old `state`-carries-the-password check could never be
  satisfied by a real client): /authorize parks the request and redirects to
  the form without minting a code; the form enforces the password, a
  per-pending-key attempt limit, a per-IP failure rate limit, and a TTL on
  parked requests; and nothing submitted to the form is ever logged.

Tool logic remains tested independently of this auth layer in
tests/test_server.py, which calls registered tool functions directly and
never goes through HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

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


CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
#: Shape of the `state` value production claude.ai actually sends (captured
#: live 2026-07-10): its own CSRF token - it never contains the password.
CLAUDE_STATE = "AfGKaeD8ijS45GgSdUH0KLgD0AAitxmZJozNMHVOTLo"


async def _start_authorization(client: httpx.AsyncClient, *, state: str = CLAUDE_STATE):
    """Register a client with an *allowed* redirect domain the way claude.ai
    does (DCR, then GET /authorize with its own CSRF token as `state`) and
    return the raw /authorize response."""
    register_response = await client.post(
        "/register",
        json={
            "client_name": "consent-flow-test-client",
            "redirect_uris": [CLAUDE_REDIRECT_URI],
        },
    )
    assert register_response.status_code == 201
    client_id = register_response.json()["client_id"]

    return await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "response_type": "code",
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "state": state,
        },
    )


def _consent_path(authorize_response, settings) -> str:
    """Assert the /authorize response is the redirect to the consent page and
    return its path+query, ready to request against the test app."""
    assert authorize_response.status_code == 302
    location = authorize_response.headers["location"]
    assert location.startswith(f"{settings.public_base_url}/consent?pending=")
    assert "code=" not in location
    parsed = urlparse(location)
    return f"{parsed.path}?{parsed.query}"


def _pending_key(consent_path: str) -> str:
    return parse_qs(urlparse(consent_path).query)["pending"][0]


def _http_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_authorize_redirects_to_consent_page_without_issuing_code(app, settings):
    # claude.ai's real flow: `state` is Claude's own CSRF token, nothing more.
    # /authorize must neither reject it (the pre-patch-5 behavior, which made
    # connector setup impossible) nor mint a code before the password step.
    async def scenario():
        async with _http_client(app) as client:
            return await _start_authorization(client)

    _consent_path(_run(scenario()), settings)


def test_authorize_ignores_password_smuggled_in_state(app, settings):
    # The old delivery channel must be fully dead: even a state that *does*
    # contain the password gets the consent redirect, never a direct code.
    async def scenario():
        async with _http_client(app) as client:
            return await _start_authorization(client, state=TEST_OAUTH_PASSWORD)

    _consent_path(_run(scenario()), settings)


def test_consent_form_is_served_for_valid_pending_key(app, settings):
    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            return path, await client.get(path)

    path, response = _run(scenario())
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert f'name="pending" value="{_pending_key(path)}"' in response.text
    # The pending key must not end up in caches or referrer headers.
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_consent_form_rejects_unknown_pending_key(app):
    async def scenario():
        async with _http_client(app) as client:
            return await client.get("/consent", params={"pending": "no-such-key"})

    response = _run(scenario())
    assert response.status_code == 400
    assert 'name="password"' not in response.text


def test_consent_with_correct_password_redirects_to_client_with_code(app, settings):
    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            return await client.post(
                "/consent",
                data={"pending": _pending_key(path), "password": TEST_OAUTH_PASSWORD},
            )

    response = _run(scenario())
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(CLAUDE_REDIRECT_URI)
    query = parse_qs(urlparse(location).query)
    assert query["code"]
    # The client's own CSRF state must round-trip unchanged.
    assert query["state"] == [CLAUDE_STATE]


def test_consent_wrong_password_rerenders_form_and_keeps_key_valid(app, settings, caplog):
    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            key = _pending_key(path)
            wrong = await client.post(
                "/consent", data={"pending": key, "password": "wrong-password"}
            )
            retry = await client.post(
                "/consent", data={"pending": key, "password": TEST_OAUTH_PASSWORD}
            )
            return wrong, retry

    with caplog.at_level(logging.DEBUG):
        wrong, retry = _run(scenario())

    assert wrong.status_code == 401
    assert "Wrong password" in wrong.text
    assert 'name="password"' in wrong.text  # form is re-rendered
    # A failed attempt must not consume the pending key (below the limit) ...
    assert retry.status_code == 302
    assert "code=" in retry.headers["location"]
    # ... and nothing the user typed may reach any log (README > Authentication).
    assert "wrong-password" not in caplog.text
    assert TEST_OAUTH_PASSWORD not in caplog.text


def test_consent_pending_key_is_single_use(app, settings):
    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            key = _pending_key(path)
            first = await client.post(
                "/consent", data={"pending": key, "password": TEST_OAUTH_PASSWORD}
            )
            replay = await client.post(
                "/consent", data={"pending": key, "password": TEST_OAUTH_PASSWORD}
            )
            return first, replay

    first, replay = _run(scenario())
    assert first.status_code == 302
    assert replay.status_code == 400


def test_consent_attempt_limit_invalidates_pending_key(app, settings):
    from nextcloud_task_mcp.personal_auth import CONSENT_MAX_ATTEMPTS_PER_KEY

    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            key = _pending_key(path)
            responses = [
                await client.post("/consent", data={"pending": key, "password": f"wrong-{i}"})
                for i in range(CONSENT_MAX_ATTEMPTS_PER_KEY)
            ]
            after_limit = await client.post(
                "/consent", data={"pending": key, "password": TEST_OAUTH_PASSWORD}
            )
            return responses, after_limit

    responses, after_limit = _run(scenario())
    # Attempts below the limit re-render the form; the one that hits the limit
    # hard-rejects and burns the key ...
    assert [r.status_code for r in responses[:-1]] == [401] * (len(responses) - 1)
    assert responses[-1].status_code == 403
    # ... so even the correct password can no longer redeem it.
    assert after_limit.status_code == 400


def test_consent_ip_rate_limit_hard_rejects_even_correct_password(app, settings):
    # Fresh pending keys are free to mint via /authorize, so the per-key
    # attempt limit alone would not stop password guessing - the per-IP
    # failure budget is the real backstop. httpx's ASGITransport presents all
    # requests from one client address, which is exactly what we need here.
    from nextcloud_task_mcp.personal_auth import CONSENT_MAX_FAILURES_PER_IP

    async def scenario():
        async with _http_client(app) as client:
            failures = 0
            while failures < CONSENT_MAX_FAILURES_PER_IP:
                path = _consent_path(await _start_authorization(client), settings)
                key = _pending_key(path)
                response = await client.post("/consent", data={"pending": key, "password": "wrong"})
                assert response.status_code in (401, 403)
                failures += 1

            blocked_path = _consent_path(await _start_authorization(client), settings)
            return await client.post(
                "/consent",
                data={
                    "pending": _pending_key(blocked_path),
                    "password": TEST_OAUTH_PASSWORD,
                },
            )

    response = _run(scenario())
    assert response.status_code == 429


def test_consent_pending_key_expires_after_ttl(app, settings, monkeypatch):
    from types import SimpleNamespace

    from nextcloud_task_mcp import personal_auth
    from nextcloud_task_mcp.personal_auth import CONSENT_PENDING_TTL_SECONDS

    real_time = time.time()

    async def scenario():
        async with _http_client(app) as client:
            path = _consent_path(await _start_authorization(client), settings)
            key = _pending_key(path)

            # Jump the module's clock past the TTL. Patching the module's
            # `time` attribute (not the global time module) keeps the fake
            # clock scoped to personal_auth.
            monkeypatch.setattr(
                personal_auth,
                "time",
                SimpleNamespace(time=lambda: real_time + CONSENT_PENDING_TTL_SECONDS + 1),
            )

            form_after_ttl = await client.get(path)
            submit_after_ttl = await client.post(
                "/consent", data={"pending": key, "password": TEST_OAUTH_PASSWORD}
            )
            return form_after_ttl, submit_after_ttl

    form_after_ttl, submit_after_ttl = _run(scenario())
    assert form_after_ttl.status_code == 400
    assert submit_after_ttl.status_code == 400


# --- State-file/dir permissions (D4, LOCAL PATCH 3 in personal_auth.py) ---


def test_oauth_state_dir_and_file_have_restrictive_permissions(app, settings):
    # Registering a client is enough to trigger PersonalAuthProvider._save_state()
    # (it writes oauth_tokens.json after every register_client/authorize/token
    # call), and the state dir itself is created (and chmod'd) in __init__.
    async def scenario():
        async with _http_client(app) as client:
            await _start_authorization(client)

    _run(scenario())

    state_dir = Path(settings.oauth_state_dir)
    state_file = state_dir / "oauth_tokens.json"

    assert state_file.exists()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_oauth_state_dir_permissions_enforced_even_if_dir_preexists(settings, tmp_path):
    # Path.mkdir(mode=...) is masked by the process umask and does not fix an
    # already-existing directory's permissions - PersonalAuthProvider must
    # chmod explicitly, not just pass mode= to mkdir.
    from nextcloud_task_mcp.personal_auth import PersonalAuthProvider

    state_dir = Path(settings.oauth_state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o755)
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o755

    PersonalAuthProvider(
        base_url=settings.public_base_url,
        password=settings.oauth_password,
        state_dir=str(state_dir),
    )

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
