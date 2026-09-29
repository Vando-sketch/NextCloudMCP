# FastMCP 4 Migration (0.2.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move nextcloud-organizer-mcp from `fastmcp>=2.9,<3` to `fastmcp>=4,<5` and ship it as 0.2.0, proving the Claude-connector OAuth flow still works end to end.

**Architecture:** Test-first. New-behaviour tests (FastMCP 4 API, dependency guards, a real-HTTP OAuth end-to-end suite) are written and committed *red* against the current 2.14.7 lockfile; one dependency-bump commit turns them green. The vendored `personal_auth.py` is not modified; the end-to-end suite is what proves it still works on the MCP SDK 2.x it now runs on.

**Tech Stack:** Python 3.10+/3.12, FastMCP 4.0.x (MCP SDK 2.x), uv, pytest, uvicorn, httpx, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-29-fastmcp-4-migration-design.md`

## Global Constraints

- Dependency range is exactly `fastmcp>=4,<5`; FastMCP 3.x is not supported. Version becomes `0.2.0`.
- Work on branch `feat/fastmcp-4`; PR into `main`. Dependabot PR #54 is closed once this PR supersedes it.
- CI must stay green on Python 3.10 and 3.12: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-report=term-missing --cov-fail-under=90`.
- `src/nextcloud_organizer_mcp/personal_auth.py` gets no functional change and stays excluded from ruff, mypy and coverage.
- Keep the `anyio>=4.14.2` and `cryptography>=50.0.0` floors in `pyproject.toml`.
- `uv.lock` must contain no `diskcache`.
- Do not push, open/comment on/close PRs, tag or publish without the maintainer's explicit go-ahead at that step. Nothing is released before the maintainer's manual smoke test.
- No new deployment or config changes are documented unless one is discovered (none found so far); the changelog states this explicitly. `docs/deployment.md` is not edited.
- Code and commit messages: normal prose, no tool narration. End every commit message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Failure modes the spec implies but that no unit test would catch; each has a test in the task named:

1. **Claude's connector speaking the previous MCP protocol (2025-06-18, session-based).** FastMCP 4 negotiates the newest protocol per connection and `fastmcp.Client` always picks the new one, so a raw `2025-06-18` handshake over `/mcp` is pinned in Task 3.
2. **Tool-approval hints on the wire.** Claude decides approval prompts from `readOnlyHint`/`destructiveHint`; the Python attribute names changed to snake_case but the JSON must stay camelCase. Pinned on the raw `tools/list` response in Task 3, and on the Python side in Task 2.
3. **Already-connected users.** An `oauth_tokens.json` written by 0.1.1 must still authenticate `/mcp` and refresh, or every user has to re-authorize. Pinned in Task 3.
4. **Invalid, replayed, or mis-verified grants.** FastMCP 4 answers `401 invalid_grant` where 2.x answered `400`; wrong PKCE verifier, code replay and stale refresh token must all be rejected with exactly that. Pinned in Task 3.
5. **Silent kwarg drift in `main()`.** `test_main_disables_uvicorn_access_log_and_passes_host_port` mocks `FastMCP.run`, so a renamed `uvicorn_config` would go unnoticed and the access log (which would record single-use `/consent` keys) could turn back on. Pinned by a signature-binding test in Task 2 and a real-process smoke check in Task 8.

---

### Task 1: Record the FastMCP 4.0.0 release-notes findings in the spec

The spec listed "primary 4.0.0 release notes not read" as a risk. They have now been read (`gh release view v4.0.0 -R PrefectHQ/fastmcp`); record what they change.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-fastmcp-4-migration-design.md` (the "Not verified" paragraph under Evidence; the "Unread primary 4.0 changelog" bullet under Risks)

**Interfaces:**
- Consumes: nothing.
- Produces: an accurate spec that Tasks 3 and 7 refer to.

- [ ] **Step 1: Replace the "Not verified" paragraph**

Replace the paragraph starting `Not verified: primary FastMCP 4.0.0 release notes were not obtained` with:

```markdown
Release notes: the FastMCP v4.0.0 and v3.0.0 GitHub release bodies were read
after the spike (`gh release view v4.0.0 -R PrefectHQ/fastmcp`). Relevant
findings:

- 4.0 is built on MCP protocol revision `2026-07-28` and MCP SDK 2.x. A server
  negotiates the best protocol per connection; older clients keep working. The
  end-to-end tests therefore include a raw `2025-06-18` session handshake, since
  `fastmcp.Client` always negotiates the newest protocol.
- "MCP model fields are snake_case (with a warning compatibility bridge for the
  old names)": explains the `ToolAnnotations` change; the JSON on the wire keeps
  camelCase, which the end-to-end tests assert.
- "Honor OAuth application_type in DCR (SEP-837)": covered by a DCR test that
  registers with `application_type` `web` and `native`.
- Server-initiated sampling/roots and 3.x deprecated APIs are removed; none are
  used here. OAuth hardening in the notes concerns `OAuthProxy`, which this
  project does not use.
- FastMCP performs an update check against pypi.org at startup in both 2.14 and
  4.x (`FASTMCP_CHECK_FOR_UPDATES` controls it); not a change, so not documented
  as one.

An independent review via `agy` failed twice (no output beyond a preamble) and
was skipped at the maintainer's direction.
```

- [ ] **Step 2: Replace the "Unread primary 4.0 changelog" risk bullet**

Replace the bullet beginning `- Unread primary 4.0 changelog:` with:

```markdown
- Protocol-era negotiation (see Release notes above): a connector that speaks the
  previous protocol version must keep working; covered by a raw `2025-06-18`
  handshake test.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-29-fastmcp-4-migration-design.md
git commit -m "docs: record FastMCP 4.0.0 release-notes findings in the migration spec

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Red tests for the FastMCP 4 API and dependency set

**Files:**
- Create: `tests/test_dependencies.py`
- Modify: `tests/test_server.py` (imports; `tools` fixture at line 45-47; annotation tests at lines 2020-2065; new test after `test_main_disables_uvicorn_access_log_and_passes_host_port`)

**Interfaces:**
- Consumes: `build_server(settings, service=..., notes_service=...)` and `main()` from `nextcloud_organizer_mcp.server`.
- Produces: the `tools` fixture now maps tool name to a FastMCP `Tool` (from `await mcp.list_tools()`); nothing else changes for later tasks.

- [ ] **Step 1: Install the current (2.14.7) lockfile**

Run: `uv sync --locked`
Expected: succeeds; `uv run python -c "import fastmcp; print(fastmcp.__version__)"` prints `2.14.7`.

- [ ] **Step 2: Create `tests/test_dependencies.py`**

```python
"""Guards on the resolved dependency set (security-motivated, see CHANGELOG 0.2.0)."""

from __future__ import annotations

import importlib.util
from importlib.metadata import version


def test_fastmcp_is_version_4_or_newer():
    assert int(version("fastmcp").split(".")[0]) >= 4


def test_diskcache_is_not_installed():
    # diskcache has an unfixed pickle-deserialization advisory. It used to come in
    # via fastmcp[disk] -> py-key-value-aio; FastMCP 4 no longer pulls it in.
    assert importlib.util.find_spec("diskcache") is None
```

- [ ] **Step 3: Switch the `tools` fixture to the FastMCP 4 API**

In `tests/test_server.py`, replace

```python
    return asyncio.run(mcp.get_tools())
```

with

```python
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
```

(`get_tools()` was replaced by `list_tools()`, which returns a list instead of a dict.)

- [ ] **Step 4: Read the annotations under their snake_case names**

Run:

```bash
sed -i \
  -e 's/annotations\.readOnlyHint/annotations.read_only_hint/g' \
  -e 's/annotations\.destructiveHint/annotations.destructive_hint/g' \
  -e 's/annotations\.openWorldHint/annotations.open_world_hint/g' \
  tests/test_server.py
grep -n "annotations\.[a-z_]*Hint" tests/test_server.py
```

Expected: the `grep` prints nothing (only the assertion *messages* such as `readOnlyHint=True` in f-strings keep the camelCase spelling, and they do not match `annotations.`). Leave those messages alone: they name the wire-format keys.

- [ ] **Step 5: Add the `FastMCP` import**

In the third-party import block of `tests/test_server.py`, change

```python
import pytest
from fastmcp.exceptions import ToolError
```

to

```python
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
```

- [ ] **Step 6: Add the `run_http_async` signature guard**

Immediately after `test_main_disables_uvicorn_access_log_and_passes_host_port`, add:

```python
def test_main_kwargs_are_accepted_by_run_http_async(settings):
    with (
        patch("nextcloud_organizer_mcp.server.Settings.from_env", return_value=settings),
        patch("nextcloud_organizer_mcp.server.FastMCP.run") as fastmcp_run,
    ):
        main()

    # The test above mocks run() away, so it can't notice FastMCP renaming or
    # dropping one of these parameters (uvicorn_config keeps the access log off).
    # Binding raises TypeError if run_http_async no longer accepts them.
    inspect.signature(FastMCP.run_http_async).bind(None, **fastmcp_run.call_args.kwargs)
```

- [ ] **Step 7: Run and confirm they fail for the right reason**

Run: `uv run pytest tests/test_dependencies.py tests/test_server.py -q -p no:cacheprovider 2>&1 | tail -8`
Expected (FastMCP 2.14.7 still locked):
- `tests/test_dependencies.py::test_fastmcp_is_version_4_or_newer` FAILS with `assert 2 >= 4`
- `tests/test_dependencies.py::test_diskcache_is_not_installed` FAILS (a `ModuleSpec(name='diskcache', ...)` is not None)
- 137 errors in `tests/test_server.py`, each `AttributeError: 'FastMCP' object has no attribute 'list_tools'`
- `test_main_kwargs_are_accepted_by_run_http_async` PASSES (it is a guard against future drift, valid on both versions)

If any of the first three do not fail, stop and investigate before continuing.

- [ ] **Step 8: Commit (red on purpose)**

```bash
git add tests/test_dependencies.py tests/test_server.py
git commit -m "test: expect the FastMCP 4 API and dependency set (red until the bump)

These fail against fastmcp 2.14.7 by design: list_tools() replaces
get_tools(), annotation attributes are snake_case, and fastmcp>=4 without
diskcache is asserted. The bump commit turns them green.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Red end-to-end OAuth tests over real HTTP

**Files:**
- Create: `tests/fixtures/oauth_tokens_0_1_1.json`
- Create: `tests/test_oauth_e2e.py`

**Interfaces:**
- Consumes: the `settings` fixture and `TEST_OAUTH_PASSWORD` from `tests/conftest.py`; `build_server` from `nextcloud_organizer_mcp.server`.
- Produces: `running_server(settings)`, a context manager that serves the real app on an ephemeral `127.0.0.1` port and yields its base URL. It rewrites `public_base_url`, sets `host="127.0.0.1"` and restricts redirect domains to `["claude.ai"]`. Later tasks do not depend on it; Task 6 uses the tests as a mutation target.

- [ ] **Step 1: Create the legacy state-file fixture**

`tests/fixtures/oauth_tokens_0_1_1.json` - the shape 0.1.1 (FastMCP 2.14 / MCP SDK 1.28) wrote, with obviously fake tokens that expire in 2100:

```json
{
  "clients": {
    "legacy-client": {
      "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
      "token_endpoint_auth_method": "none",
      "grant_types": ["authorization_code", "refresh_token"],
      "response_types": ["code"],
      "scope": null,
      "client_name": "Claude",
      "client_uri": null,
      "logo_uri": null,
      "contacts": null,
      "tos_uri": null,
      "policy_uri": null,
      "jwks_uri": null,
      "jwks": null,
      "software_id": null,
      "software_version": null,
      "client_id": "legacy-client",
      "client_secret": null,
      "client_id_issued_at": null,
      "client_secret_expires_at": null
    }
  },
  "access_tokens": {
    "pat_legacy_access_token": {
      "token": "pat_legacy_access_token",
      "client_id": "legacy-client",
      "scopes": [],
      "expires_at": 4102444800,
      "resource": null
    }
  },
  "refresh_tokens": {
    "prt_legacy_refresh_token": {
      "token": "prt_legacy_refresh_token",
      "client_id": "legacy-client",
      "scopes": [],
      "expires_at": 4102444800
    }
  },
  "a2r": {"pat_legacy_access_token": "prt_legacy_refresh_token"},
  "r2a": {"prt_legacy_refresh_token": "pat_legacy_access_token"}
}
```

- [ ] **Step 2: Create `tests/test_oauth_e2e.py`**

```python
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


def test_discovery_advertises_the_flow_endpoints(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        meta = http.get("/.well-known/oauth-authorization-server").json()
        assert meta["registration_endpoint"].endswith("/register")
        assert meta["authorization_endpoint"].endswith("/authorize")
        assert meta["token_endpoint"].endswith("/token")
        assert "S256" in meta["code_challenge_methods_supported"]


def test_full_flow_ends_in_an_authenticated_mcp_session(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        _, token = _full_flow(http)
        assert token["token_type"].lower() == "bearer"
        assert "list_task_lists" in _list_tool_names(base, token["access_token"])


def test_wrong_password_is_rejected_and_the_right_one_still_works(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        wrong = _consent(http, pending, "not-the-password")
        assert wrong.status_code == 401
        assert "location" not in wrong.headers
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 302


def test_pending_key_is_single_use(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 302
        assert _consent(http, pending, TEST_OAUTH_PASSWORD).status_code == 400


def test_wrong_pkce_verifier_is_rejected(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        client_id = _register(http)
        _, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        consent = _consent(http, pending, TEST_OAUTH_PASSWORD)
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        response = _exchange_code(http, client_id, code, "not-the-verifier" * 4)
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_grant"


def test_authorization_code_cannot_be_replayed(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        client_id = _register(http)
        verifier, challenge = _pkce()
        pending = _authorize(http, client_id, challenge)
        consent = _consent(http, pending, TEST_OAUTH_PASSWORD)
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        assert _exchange_code(http, client_id, code, verifier).status_code == 200
        assert _exchange_code(http, client_id, code, verifier).status_code == 401


def test_refresh_rotates_the_refresh_token(settings):
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
        client_id, first = _full_flow(http)
        refreshed = _refresh(http, client_id, first["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        second = refreshed.json()
        assert second["refresh_token"] != first["refresh_token"]
        assert "list_task_lists" in _list_tool_names(base, second["access_token"])
        assert _refresh(http, client_id, first["refresh_token"]).status_code == 401


def test_invalid_refresh_token_gets_401_invalid_grant(settings):
    # New with FastMCP 4: an invalid/expired grant is a 401 (MCP spec), not the
    # SDK's 400 that FastMCP 2.x answered - every invalid_grant assertion in this
    # module expects 401.
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
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


def test_previous_protocol_version_session_still_works(settings):
    # FastMCP 4 negotiates the newest protocol per connection; Claude's connector
    # may still speak the 2025-06-18 session-based one. A raw handshake pins that.
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
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
def test_dcr_accepts_an_application_type(settings, application_type):
    # SEP-837: FastMCP 4 honors OAuth application_type during registration.
    with running_server(settings) as base, httpx.Client(base_url=base) as http:
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
```

- [ ] **Step 3: Run against 2.14.7 and confirm the split**

Run: `uv run pytest tests/test_oauth_e2e.py -q -p no:cacheprovider -rA 2>&1 | grep -E "^(PASSED|FAILED)"`
Expected:

```
FAILED tests/test_oauth_e2e.py::test_wrong_pkce_verifier_is_rejected
FAILED tests/test_oauth_e2e.py::test_authorization_code_cannot_be_replayed
FAILED tests/test_oauth_e2e.py::test_refresh_rotates_the_refresh_token
FAILED tests/test_oauth_e2e.py::test_invalid_refresh_token_gets_401_invalid_grant
```

and the other nine PASS. The four failures are the FastMCP 4 behaviour change (`invalid_grant` is `401`, was `400`), each failing on a status assertion that got `400` where `401` is expected. The nine passes are deliberate characterization: they show the harness drives the real flow correctly on the *old* stack, so a later failure means the migration broke something, not the test. If a "PASS" test fails here, or a "FAILED" one passes, stop and investigate.

- [ ] **Step 4: Lint and type-check the new files**

Run: `uv run ruff check tests && uv run ruff format --check tests && uv run mypy src tests`
Expected: all clean (mypy may still report the `ToolAnnotations` errors in `server.py` that Task 4 fixes; nothing in `tests/test_oauth_e2e.py`).

- [ ] **Step 5: Commit (four tests red on purpose)**

```bash
git add tests/fixtures/oauth_tokens_0_1_1.json tests/test_oauth_e2e.py
git commit -m "test: add real-HTTP OAuth end-to-end suite

Drives discovery, DCR, /authorize + PKCE, the password-gated /consent page,
/token, refresh rotation and an authenticated /mcp session (new and
2025-06-18 protocol) against uvicorn on an ephemeral port, plus restart
persistence and a 0.1.1-format oauth_tokens.json. The four invalid_grant
assertions expect FastMCP 4's 401 and fail on 2.14.7 until the bump.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Bump to FastMCP 4 (turns the red tests green)

**Files:**
- Modify: `pyproject.toml:19` (fastmcp range) and the comment above `"httpx>=0.27,<1"`
- Modify: `uv.lock` (regenerated)
- Modify: `src/nextcloud_organizer_mcp/server.py:157-190` (the four `ToolAnnotations` constants)

**Interfaces:**
- Consumes: the red tests from Tasks 2-3.
- Produces: `_READ_ONLY`, `_CREATE`, `_MODIFY`, `_ADD` remain module-level `ToolAnnotations` constants with the same meaning, now built with snake_case keywords.

- [ ] **Step 1: Change the fastmcp range**

In `pyproject.toml` replace `"fastmcp>=2.9,<3",` with `"fastmcp>=4,<5",`.

- [ ] **Step 2: Correct the httpx comment**

Replace the comment block above `"httpx>=0.27,<1",`

```toml
    # Already an indirect dependency via fastmcp/mcp; imported directly here
    # too for the Notes app's plain JSON REST API (notes_client.py), which has
    # nothing to do with CalDAV/the caldav library.
```

with

```toml
    # Imported directly for the Notes app's plain JSON REST API
    # (notes_client.py), which has nothing to do with CalDAV/the caldav
    # library. FastMCP 4 and the MCP SDK 2 moved to httpx2, so it is no longer
    # pulled in by them.
```

- [ ] **Step 3: Regenerate the lockfile and inspect it**

```bash
uv lock --upgrade-package fastmcp
uv sync --locked
uv run python -c "import fastmcp, importlib.metadata as m; print(fastmcp.__version__, m.version('mcp'))"
grep -c diskcache uv.lock
grep -n -A1 '^name = "\(anyio\|cryptography\|mcp\|fastmcp\)"$' uv.lock
```

Expected: prints `4.0.x 2.x` (4.0.10 or newer 4.0.x), `grep -c` prints `0`, and the locked `anyio` is >= 4.14.2 and `cryptography` >= 50.0.0. If `uv lock` picks a fastmcp older than 4.0.10, stop and investigate.

- [ ] **Step 4: Use the MCP SDK 2 keyword names in `server.py`**

Replace the four annotation constants (currently lines 164-190) with:

```python
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

#: Creates something new. Additive, so re-running adds another copy rather than
#: clobbering anything - hence not destructive, but not idempotent either.
_CREATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

#: Overwrites or removes existing state. Re-running with the same arguments
#: lands on the same end state, so idempotent, but the original is gone.
_MODIFY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

#: Adds to existing state without discarding any of it (a share, a link, a
#: restore), and converges on the same end state when repeated.
_ADD = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
```

Keep the `#:` doc comments already present above `_READ_ONLY`. In the explanatory comment block above them, the mentions of `openWorldHint` (wire-format key names) stay as they are.

- [ ] **Step 5: Run the full CI command set**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests \
  && uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-report=term-missing --cov-fail-under=90
```

Expected: ruff clean, `Success: no issues found`, all tests pass (about 1210 passed, 18 skipped), coverage about 95%, no `FastMCPDeprecationWarning` from the annotation tests. The four Task 3 failures and both Task 2 failures are now green. If anything else fails, use superpowers:systematic-debugging before changing tests.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/nextcloud_organizer_mcp/server.py
git commit -m "feat!: require FastMCP 4 (MCP SDK 2)

Replaces fastmcp>=2.9,<3 with fastmcp>=4,<5. ToolAnnotations takes snake_case
keywords in MCP SDK 2 (the wire format stays camelCase). The lockfile drops
diskcache and py-key-value-aio[disk]. personal_auth.py is unchanged; the new
end-to-end suite passes against it on the new stack.

Supersedes #54.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Retire the PEP 563 workaround comments

Measured on the scratch copies: with `from __future__ import annotations` added to `server.py`, `test_no_tool_param_with_a_default_is_required_in_the_schema` fails on FastMCP 2.14.7 and passes on 4.0.10, so the bug is gone. The rule "no future import" is obsolete; the test stays as a cheap regression guard.

**Files:**
- Modify: `src/nextcloud_organizer_mcp/server.py:3-6` (header comment)
- Modify: `tests/test_server.py` (docstring of `test_no_tool_param_with_a_default_is_required_in_the_schema`, currently around line 490)

- [ ] **Step 1: Confirm the claim still holds on the locked version**

```bash
cp src/nextcloud_organizer_mcp/server.py /tmp/server.py.bak
sed -i '0,/^import functools/s//from __future__ import annotations\n\nimport functools/' src/nextcloud_organizer_mcp/server.py
uv run pytest tests/test_server.py -q -p no:cacheprovider -k no_tool_param_with_a_default
cp /tmp/server.py.bak src/nextcloud_organizer_mcp/server.py
git status --short
```

Expected: `1 passed`; `git status --short` prints nothing (file restored). If it fails, keep the old comments (reword only the `(<3)` version), skip Steps 2-3 and note it in the PR.

- [ ] **Step 2: Reword the `server.py` header comment**

Replace

```python
# No `from __future__ import annotations` here: with PEP 563 string annotations,
# fastmcp (<3) rebuilds each tool function to resolve them and drops
# `__kwdefaults__` in the process, so every keyword-only parameter loses its
# default and is marked required in the MCP schema clients see.
```

with

```python
# FastMCP 2.x dropped `__kwdefaults__` when it rebuilt tool functions to resolve
# PEP 563 string annotations (`from __future__ import annotations`), marking
# every keyword-only parameter required in the MCP schema clients see. FastMCP 4
# no longer does; the schema test in tests/test_server.py still guards it.
```

- [ ] **Step 3: Reword the test docstring**

In `tests/test_server.py`, replace the second paragraph of the docstring

```python
    fastmcp (<3) rebuilds tool functions whose annotations are PEP 563 strings
    (`from __future__ import annotations`) and loses `__kwdefaults__` doing it,
    so every keyword-only parameter turns required-but-nullable - and clients
    that then pass an explicit null can trip over it. server.py therefore must
    not use the future import; this test fails on every affected tool at once
    if it comes back.
```

with

```python
    FastMCP 2.x rebuilt tool functions whose annotations are PEP 563 strings
    (`from __future__ import annotations`) and lost `__kwdefaults__` doing it,
    so every keyword-only parameter turned required-but-nullable - and clients
    that then passed an explicit null could trip over it. FastMCP 4 fixed this;
    this test fails on every affected tool at once if it ever comes back.
```

- [ ] **Step 4: Run and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -q
git add src/nextcloud_organizer_mcp/server.py tests/test_server.py
git commit -m "docs: retire the FastMCP <3 PEP 563 workaround comments

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

Expected: all green.

---

### Task 6: Prove the end-to-end suite bites (mutation check, nothing committed)

The spec requires that the suite fails if the consent gate breaks. Verify it.

**Files:**
- Temporarily modify, then restore: `src/nextcloud_organizer_mcp/personal_auth.py`

- [ ] **Step 1: Disable the consent password check**

```bash
python3 - <<'EOF'
p = "src/nextcloud_organizer_mcp/personal_auth.py"
s = open(p).read()
old = "password_ok = bool(self.password) and secrets.compare_digest("
assert old in s
open(p, "w").write(s.replace(old, "password_ok = True or bool(self.password) and secrets.compare_digest("))
EOF
uv run pytest tests/test_oauth_e2e.py -q -p no:cacheprovider 2>&1 | grep -E "^FAILED|passed|failed"
```

Expected: `FAILED tests/test_oauth_e2e.py::test_wrong_password_is_rejected_and_the_right_one_still_works` and `1 failed, 12 passed`.

- [ ] **Step 2: Restore the file and verify**

```bash
git checkout -- src/nextcloud_organizer_mcp/personal_auth.py
git status --short
uv run pytest tests/test_oauth_e2e.py -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: `git status --short` prints nothing; `13 passed`.

---

### Task 7: Changelog and docs

**Files:**
- Modify: `CHANGELOG.md` (under `## [Unreleased]`)
- Modify: `docs/architecture.md` (the "Auth tests" bullet, currently lines 146-153)
- Modify: `tests/test_auth.py` (module docstring, first two paragraphs)

- [ ] **Step 1: Changelog**

Replace the empty `## [Unreleased]` section with:

```markdown
## [Unreleased]

### Changed

- **Requires FastMCP 4** (`fastmcp>=4,<5`, MCP Python SDK 2.x). The previous
  range was `fastmcp>=2.9,<3`. Tool behaviour, the OAuth 2.1 flow (Dynamic
  Client Registration, PKCE, the password-gated `/consent` page) and all
  environment variables are unchanged. **No configuration or deployment change
  is needed:** update as usual (`git pull`, `uv sync --locked --no-dev`,
  restart), and connectors that are already connected in Claude stay connected -
  the persisted `oauth_tokens.json` from 0.1.x keeps working. To stay on
  FastMCP 2.x, pin `nextcloud-organizer-mcp<0.2`.
- OAuth token errors for an invalid, expired or replayed grant (`invalid_grant`)
  now return HTTP `401` instead of `400`, as the MCP specification requires
  (behaviour of FastMCP 4).
- Tool annotations are declared with the MCP SDK 2 keyword names
  (`read_only_hint`, ...); what clients receive on the wire is unchanged.

### Security

- Clears the FastMCP advisories that affect `fastmcp < 3.2.0` (OpenAPI provider
  SSRF, OAuth proxy confused-deputy, Gemini CLI command injection) from this
  package's dependency range. The affected features are not used by this
  server, but installs no longer resolve a vulnerable version.
- `diskcache` (unfixed pickle-deserialization advisory) is no longer installed;
  it was pulled in by `fastmcp[disk]` on FastMCP 2.x.

### Added

- Real-HTTP OAuth end-to-end tests (`tests/test_oauth_e2e.py`) covering
  discovery, registration, PKCE, the consent page, token exchange and refresh,
  an authenticated MCP session on both the current and the previous (2025-06-18)
  protocol version, restart persistence, and a state file written by 0.1.1.
```

- [ ] **Step 2: Architecture doc**

In `docs/architecture.md`, replace the last three lines of the "Auth tests" bullet

```markdown
  Driving the full interactive OAuth+PKCE flow end-to-end isn't practical in an
  automated test (it requires a browser redirect round-trip), so these tests target the
  middleware boundary instead - see the module docstring for the full rationale.
```

with

```markdown
  The full flow is covered separately by `tests/test_oauth_e2e.py`, which serves the
  real app with uvicorn on an ephemeral port and drives discovery, DCR, `/authorize` with
  PKCE, the `/consent` page, `/token`, refresh rotation and an authenticated `/mcp`
  session (current and 2025-06-18 protocol), plus restart persistence and a state file
  in the 0.1.1 format.
```

- [ ] **Step 3: `test_auth.py` docstring**

In the module docstring of `tests/test_auth.py`, replace

```python
Registration flow (see nextcloud_organizer_mcp.personal_auth, vendored from
crumrine/fastmcp-personal-auth). Driving that flow end-to-end - a real
client registering, opening a browser at /authorize, completing a redirect,
exchanging a code with a PKCE verifier - has no stable, automatable surface
in a unit test; it's normally exercised by an interactive OAuth client (e.g.
Claude.ai) or FastMCP's own upstream test suite for the underlying
InMemoryOAuthProvider machinery.

What we test instead, at the ASGI/middleware level via the real app FastMCP
builds from `auth=...`:
```

with

```python
Registration flow (see nextcloud_organizer_mcp.personal_auth, vendored from
crumrine/fastmcp-personal-auth). The complete flow over real HTTP (register,
authorize, consent, token, refresh, an authenticated /mcp session) lives in
tests/test_oauth_e2e.py; this module checks the layer piecewise at the
ASGI/middleware level via the real app FastMCP builds from `auth=...`:
```

- [ ] **Step 4: Run and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -q
git add CHANGELOG.md docs/architecture.md tests/test_auth.py
git commit -m "docs: changelog and architecture notes for the FastMCP 4 migration

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

Expected: all green.

---

### Task 8: Full verification on both Pythons and the real entrypoint

**Files:** none modified.

- [ ] **Step 1: Python 3.10 (a separate environment; do not touch `.venv`)**

```bash
export UV_PROJECT_ENVIRONMENT=/tmp/venv-py310
uv sync --locked --python 3.10
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests \
  && uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-report=term-missing --cov-fail-under=90
unset UV_PROJECT_ENVIRONMENT
```

Expected: everything passes on 3.10, including `tests/test_oauth_e2e.py`. This closes the "Python 3.10 resolution of `mcp` 2.x is unverified" risk from the spec; if resolution or a test fails only on 3.10, report it instead of patching around it.

- [ ] **Step 2: Python 3.12 (the default `.venv`)**

```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests \
  && uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-report=term-missing --cov-fail-under=90
```

Expected: all green.

- [ ] **Step 3: Real-process smoke test of the console script**

```bash
PORT=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])")
rm -rf /tmp/smoke-state
( NEXTCLOUD_CALDAV_URL=http://127.0.0.1:1/remote.php/dav/ NEXTCLOUD_BASE_URL=http://127.0.0.1:1 \
  NEXTCLOUD_USERNAME=u NEXTCLOUD_APP_PASSWORD=p PUBLIC_BASE_URL=http://127.0.0.1:$PORT \
  MCP_HOST=127.0.0.1 MCP_PORT=$PORT MCP_OAUTH_STATE_DIR=/tmp/smoke-state \
  timeout 9 uv run nextcloud-organizer-mcp > /tmp/smoke.log 2>&1 & )
sleep 4
curl -s -o /dev/null -w "discovery %{http_code}\n" http://127.0.0.1:$PORT/.well-known/oauth-authorization-server
curl -s -o /dev/null -w "mcp-without-token %{http_code}\n" -X POST http://127.0.0.1:$PORT/mcp
sleep 6
echo "uvicorn access-log lines: $(grep -cE '"(GET|POST) /' /tmp/smoke.log)"
grep -o "FastMCP [0-9.]*" /tmp/smoke.log | head -1
```

Expected: `discovery 200`, `mcp-without-token 401`, `uvicorn access-log lines: 0`, `FastMCP 4.0.x`. A `pypi.org` line from FastMCP's own update check in the log is expected (it happens on 2.14 as well) and is not an access-log line.

- [ ] **Step 4: Audit the lockfile for known advisories**

```bash
uv export --locked --no-hashes --no-emit-project -o /tmp/requirements-audit.txt
uvx pip-audit -r /tmp/requirements-audit.txt --no-deps --disable-pip
```

Expected: `No known vulnerabilities found` (verified on the scratch copy with fastmcp 4.0.10). Any finding is a release blocker: report it, do not suppress it.

---

### Task 9: Publish the PR (needs the maintainer's explicit go-ahead)

**Files:** none modified.

- [ ] **Step 1: Ask the maintainer to confirm** pushing `feat/fastmcp-4` and opening the PR. Do not proceed without a yes.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin feat/fastmcp-4
gh pr create --base main --head feat/fastmcp-4 \
  --title "feat!: migrate to FastMCP 4 (0.2.0)" \
  --body "$(cat <<'EOF'
## Summary
- Requires `fastmcp>=4,<5` (MCP SDK 2.x), replacing `>=2.9,<3`; supersedes #54.
- Clears the fastmcp < 3.2.0 advisories from the dependency range and drops `diskcache`.
- `personal_auth.py` unchanged. New `tests/test_oauth_e2e.py` drives the whole OAuth flow over real HTTP (both protocol versions, restart persistence, a 0.1.1 `oauth_tokens.json`).
- No config or deployment change; connected Claude connectors stay connected. `invalid_grant` is now HTTP 401 (FastMCP 4).

## Test plan
- [x] ruff, ruff format, mypy, pytest with coverage >= 90% on Python 3.10 and 3.12
- [x] New tests written first and confirmed red on 2.14.7 (6 failing), green after the bump
- [x] Mutation check: disabling the consent password check fails the E2E suite
- [x] Real `nextcloud-organizer-mcp` process: discovery 200, /mcp without token 401, no uvicorn access log
- [ ] Manual: fresh Claude custom connector against the real Funnel URL
- [ ] Manual: reconnect an existing connector; run one read and one write tool

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Register the PR with the thread** using the `link_pull_request` tool (full PR URL), then call `list_thread_pull_requests` to confirm.

- [ ] **Step 4: Comment on #54 that this PR supersedes it** (`gh pr comment 54 --body "Superseded by #<new PR>, which also carries the required code changes."`). Close #54 (`gh pr close 54`) only after the maintainer confirms, normally once the new PR is merged.

---

### Task 10: Release 0.2.0 (maintainer-gated)

**Files:**
- Modify: `pyproject.toml` (`version = "0.2.0"`)
- Modify: `uv.lock` (project version)
- Modify: `CHANGELOG.md` (`## [Unreleased]` becomes `## [0.2.0] - <release date>`; add a fresh empty `## [Unreleased]` above it)

- [ ] **Step 1: Manual smoke test (maintainer).** Run the PR build behind the real Tailscale Funnel URL. (a) Add it as a *new* Claude custom connector and complete the consent page; (b) reconnect an *existing* connector without re-authorizing; (c) run one read tool and one write tool. This is the only check of real claude.ai behaviour (including the `401 invalid_grant` change). If any step fails, stop; do not release.

- [ ] **Step 2: Release commit** (only after Step 1 passes and the maintainer says go):

```bash
sed -i 's/^version = "0.1.1"/version = "0.2.0"/' pyproject.toml
uv lock
# then edit CHANGELOG.md as described above
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: release 0.2.0

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Publish.** The maintainer merges the PR and publishes a GitHub Release `v0.2.0`; the existing release workflow uploads to PyPI via trusted publishing. Nothing is tagged or published by the agent.
