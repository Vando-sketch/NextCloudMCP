"""End-to-end OAuth 2.1 flow against the real app served over real HTTP.

tests/test_auth.py checks the auth layer piecewise (ASGI transport, direct
provider calls). This module drives the whole chain a Claude custom connector
performs - discovery, Dynamic Client Registration, /authorize with PKCE, the
password-gated /consent page, /token, refresh rotation, then an authenticated
MCP session on /mcp - against a real uvicorn server on an ephemeral port, so a
FastMCP/MCP SDK upgrade that breaks the flow cannot pass CI unnoticed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from conftest import TEST_OAUTH_PASSWORD
from fastmcp import Client

from nextcloud_organizer_mcp.caldav_client import CalDavService
from nextcloud_organizer_mcp.config import Settings
from nextcloud_organizer_mcp.server import build_server

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "oauth_tokens_0_1_1.json"


@contextmanager
def running_server(settings: Settings) -> Iterator[str]:
    """Serve the real app on an ephemeral 127.0.0.1 port; yield its base URL.

    The port is bound before the server is built because the provider's issuer
    URL (`public_base_url`) has to match the URL the client actually talks to.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    local = replace(
        settings,
        public_base_url=base_url,
        host="127.0.0.1",
        oauth_allowed_redirect_domains=["claude.ai"],
    )
    app = build_server(local, service=MagicMock(spec=CalDavService)).http_app()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve([sock])), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture(scope="module")
def shared_base(tmp_path_factory) -> Iterator[str]:
    """One server for the tests that don't restart it or seed its state dir.

    Starting and stopping uvicorn costs ~0.25 s, which dominated this module.
    Every test sharing this server registers its own client and codes, so none
    depends on another's leftovers; the two tests that need a fresh or
    restarted server keep using `running_server(settings)` directly.
    """
    module_settings = Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        notes_base_url="https://cloud.example.com",
        public_base_url="https://test.example.com",
        oauth_password=TEST_OAUTH_PASSWORD,
        oauth_state_dir=str(tmp_path_factory.mktemp("oauth-state")),
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
    )
    with running_server(module_settings) as base:
        yield base


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _register(http: httpx.Client) -> str:
    response = http.post(
        "/register",
        json={
            "client_name": "e2e",
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize(http: httpx.Client, client_id: str, challenge: str) -> str:
    """GET /authorize; return the pending key from the redirect to /consent."""
    response = http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "csrf-state",
        },
    )
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert location.path == "/consent"
    assert "code" not in parse_qs(location.query), "code minted before the password"
    return parse_qs(location.query)["pending"][0]


def _consent(http: httpx.Client, pending: str, password: str) -> httpx.Response:
    return http.post("/consent", data={"pending": pending, "password": password})


def _exchange_code(http: httpx.Client, client_id: str, code: str, verifier: str):
    return http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def _refresh(http: httpx.Client, client_id: str, refresh_token: str):
    return http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def _full_flow(http: httpx.Client) -> tuple[str, dict]:
    """Register, authorize, pass the consent page, exchange the code."""
    client_id = _register(http)
    verifier, challenge = _pkce()
    pending = _authorize(http, client_id, challenge)
    consent = _consent(http, pending, TEST_OAUTH_PASSWORD)
    assert consent.status_code == 302
    redirect = urlparse(consent.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT_URI
    query = parse_qs(redirect.query)
    assert query["state"] == ["csrf-state"]
    token = _exchange_code(http, client_id, query["code"][0], verifier)
    assert token.status_code == 200, token.text
    return client_id, token.json()


def _list_tool_names(base_url: str, access_token: str) -> set[str]:
    async def go() -> set[str]:
        async with Client(f"{base_url}/mcp", auth=access_token) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(go())


def test_discovery_advertises_the_flow_endpoints(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        meta = http.get("/.well-known/oauth-authorization-server").json()
        assert meta["registration_endpoint"].endswith("/register")
        assert meta["authorization_endpoint"].endswith("/authorize")
        assert meta["token_endpoint"].endswith("/token")
        assert "S256" in meta["code_challenge_methods_supported"]


def test_full_flow_ends_in_an_authenticated_mcp_session(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        _, token = _full_flow(http)
        assert token["token_type"].lower() == "bearer"
        assert "list_task_lists" in _list_tool_names(shared_base, token["access_token"])


def test_wrong_password_is_rejected_and_the_right_one_still_works(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        wrong = _consent(http, pending, "not-the-password")
        assert wrong.status_code == 401
        assert "location" not in wrong.headers
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 302


def test_pending_key_is_single_use(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 302
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 400


def test_wrong_pkce_verifier_is_rejected(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        consent = _consent(http, pending, TEST_OAUTH_PASSWORD)
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        response = _exchange_code(http, client_id, code, "not-the-verifier" * 4)
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_grant"


def test_authorization_code_cannot_be_replayed(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        client_id = _register(http)
        verifier, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        consent = _consent(http, pending, TEST_OAUTH_PASSWORD)
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        assert _exchange_code(http, client_id, code, verifier).status_code == 200
        assert _exchange_code(http, client_id, code, verifier).status_code == 401


def test_refresh_rotates_the_refresh_token(shared_base):
    with httpx.Client(base_url=shared_base) as http:
        client_id, first = _full_flow(http)
        refreshed = _refresh(http, client_id, first["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        second = refreshed.json()
        assert second["refresh_token"] != first["refresh_token"]
        assert "list_task_lists" in _list_tool_names(shared_base, second["access_token"])
        assert _refresh(http, client_id, first["refresh_token"]).status_code == 401


def test_invalid_refresh_token_gets_401_invalid_grant(shared_base):
    # New with FastMCP 4: an invalid/expired grant is a 401 (MCP spec), not the
    # SDK's 400 that FastMCP 2.x answered - every invalid_grant assertion in this
    # module expects 401.
    with httpx.Client(base_url=shared_base) as http:
        client_id = _register(http)
        response = _refresh(http, client_id, "prt_does_not_exist")
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_grant"


def test_issued_token_survives_a_server_restart(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        _, token = _full_flow(http)
    with running_server(settings) as base:
        assert "list_task_lists" in _list_tool_names(base, token["access_token"])


def test_state_file_written_by_0_1_1_still_loads(settings):
    # oauth_tokens.json as written by 0.1.1 (FastMCP 2.14 / MCP SDK 1.28):
    # already-connected Claude connectors must not need to re-authorize.
    state_dir = Path(settings.oauth_state_dir)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copy(LEGACY_FIXTURE, state_dir / "oauth_tokens.json")
    legacy = json.loads(LEGACY_FIXTURE.read_text())
    access = next(iter(legacy["access_tokens"]))
    refresh = next(iter(legacy["refresh_tokens"]))
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        assert "list_task_lists" in _list_tool_names(base, access)
        rotated = _refresh(http, "legacy-client", refresh)
        assert rotated.status_code == 200, rotated.text


def _mcp_post(http: httpx.Client, token: str, body: dict, session: str | None = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    return http.post("/mcp", json=body, headers=headers)


def _rpc_result(response: httpx.Response) -> dict:
    """The JSON-RPC message in a Streamable HTTP response (plain JSON or one SSE event)."""
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in response.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


def test_previous_protocol_version_session_still_works(shared_base):
    # FastMCP 4 negotiates the newest protocol per connection; Claude's connector
    # may still speak the 2025-06-18 session-based one. A raw handshake pins that.
    with httpx.Client(base_url=shared_base) as http:
        _, token = _full_flow(http)
        init = _mcp_post(
            http,
            token["access_token"],
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-e2e", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert _rpc_result(init)["result"]["protocolVersion"] == "2025-06-18"
        session = init.headers.get("mcp-session-id")
        _mcp_post(
            http,
            token["access_token"],
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session,
        )
        listing = _mcp_post(
            http,
            token["access_token"],
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            session,
        )
        assert listing.status_code == 200, listing.text
        tools = {tool["name"]: tool for tool in _rpc_result(listing)["result"]["tools"]}
        assert "list_task_lists" in tools
        # Claude decides approval prompts from these hints; on the wire the keys
        # are camelCase whatever the Python attribute names are.
        assert tools["list_task_lists"]["annotations"]["readOnlyHint"] is True
        assert tools["delete_task"]["annotations"]["destructiveHint"] is True


@pytest.mark.parametrize("application_type", ["web", "native"])
def test_dcr_accepts_an_application_type(shared_base, application_type):
    # SEP-837: FastMCP 4 honors OAuth application_type during registration.
    with httpx.Client(base_url=shared_base) as http:
        response = http.post(
            "/register",
            json={
                "client_name": "claude-like",
                "redirect_uris": [REDIRECT_URI],
                "application_type": application_type,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert response.status_code == 201, response.text
