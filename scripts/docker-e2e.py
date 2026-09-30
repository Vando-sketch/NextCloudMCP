"""End-to-end check of a running server: the full OAuth flow, then real tool calls.

Acts as an MCP client would (Dynamic Client Registration, PKCE, the password
consent page, token exchange), then calls tools against the Nextcloud behind the
server. Used by the Docker variant of .github/workflows/integration.yml; also
handy to check a deployment by hand:

    MCP_OAUTH_PASSWORD=... uv run python scripts/docker-e2e.py http://127.0.0.1:8000 Test

Arguments: the server URL as the client reaches it (must equal PUBLIC_BASE_URL)
and the name of a task list that exists in Nextcloud. Exit code 0 means every
step worked.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp import Client

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def step(message: str) -> None:
    print(f"- {message}", flush=True)


def obtain_access_token(base_url: str, password: str) -> str:
    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=30) as http:
        step("register a client (Dynamic Client Registration)")
        registration = http.post(
            "/register",
            json={
                "client_name": "docker-e2e",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        registration.raise_for_status()
        client_id = registration.json()["client_id"]

        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(16)

        step("authorize; expect a redirect to the consent page")
        authorize = http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            },
        )
        assert authorize.status_code in (302, 307), authorize.status_code
        location = urlparse(authorize.headers["location"])
        assert location.path == "/consent", location.path
        pending = parse_qs(location.query)["pending"][0]

        step("submit a wrong password; expect it to be rejected")
        wrong = http.post("/consent", data={"pending": pending, "password": "not-the-password"})
        assert wrong.status_code == 401, wrong.status_code

        step("submit the right password; expect a redirect back with a code")
        consent = http.post("/consent", data={"pending": pending, "password": password})
        assert consent.status_code == 302, consent.status_code
        callback = urlparse(consent.headers["location"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT_URI
        params = parse_qs(callback.query)
        assert params["state"] == [state], "state was not echoed back"
        code = params["code"][0]

        step("exchange the code for an access token")
        token = http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        return str(token.json()["access_token"])


async def call_tools(base_url: str, access_token: str, list_name: str) -> None:
    mcp_url = f"{base_url.rstrip('/')}/mcp"

    step("call a tool without a token; expect 401")
    async with httpx.AsyncClient(timeout=30) as http:
        assert (await http.post(mcp_url)).status_code == 401

    async with Client(mcp_url, auth=access_token) as client:
        step("list_task_lists includes the test list")
        lists = (await client.call_tool("list_task_lists", {})).data
        assert any(item["name"] == list_name for item in lists), lists

        step("create a task, read it back, delete it")
        title = f"docker-e2e {uuid.uuid4()}"
        created = (
            await client.call_tool("create_task", {"list_name": list_name, "title": title})
        ).data
        task_uid = created["uid"]
        try:
            tasks = (await client.call_tool("list_tasks", {"list_names": [list_name]})).data
            assert any(task["uid"] == task_uid for task in tasks), "created task not listed"
        finally:
            await client.call_tool("delete_task", {"list_name": list_name, "task_uid": task_uid})


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    base_url, list_name = sys.argv[1].rstrip("/"), sys.argv[2]
    password = os.environ["MCP_OAUTH_PASSWORD"]
    access_token = obtain_access_token(base_url, password)
    asyncio.run(call_tools(base_url, access_token, list_name))
    print("End-to-end check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
