"""End-to-end tests for the stdio transport: a real server process, real pipes."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

_TIMEOUT_SECONDS = 30

_REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "stdio-test", "version": "0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


@dataclass
class _Session:
    cwd: Path
    stdout_lines: list[str]
    stderr: str
    returncode: int


def _run_stdio_session(cwd: Path) -> _Session:
    """Act as a client: send the handshake, wait for both answers, then close stdin.

    stdin must stay open until the answers arrive - a server that exits on EOF
    may legitimately drop a request that is still in flight.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MCP_", "NEXTCLOUD_", "PUBLIC_BASE_URL"))
    }
    env.update(
        MCP_TRANSPORT="stdio",
        NEXTCLOUD_BASE_URL="https://cloud.example.com",
        NEXTCLOUD_USERNAME="testuser",
        NEXTCLOUD_APP_PASSWORD="testpass",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "nextcloud_organizer_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None
    stdout = process.stdout
    lines: queue.Queue[str] = queue.Queue()

    def pump_stdout() -> None:
        for line in stdout:
            lines.put(line)

    reader = threading.Thread(target=pump_stdout)
    reader.start()
    stdout_lines: list[str] = []
    try:
        for request in _REQUESTS:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        answered: set[int] = set()
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        while answered < {1, 2}:
            stdout_lines.append(lines.get(timeout=max(deadline - time.monotonic(), 0.01)))
            answered |= {json.loads(stdout_lines[-1]).get("id")} - {None}
        process.stdin.close()
        returncode = process.wait(timeout=_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join()
    stdout_lines.extend(lines.get_nowait() for _ in range(lines.qsize()))
    assert process.stderr is not None
    return _Session(cwd, stdout_lines, process.stderr.read(), returncode)


@pytest.fixture(scope="module")
def stdio_session(tmp_path_factory: pytest.TempPathFactory) -> _Session:
    return _run_stdio_session(tmp_path_factory.mktemp("stdio-cwd"))


def test_stdio_server_exits_cleanly_when_stdin_closes(stdio_session):
    # _run_stdio_session raises TimeoutExpired if the process outlives its stdin.
    assert stdio_session.returncode == 0, stdio_session.stderr


def test_stdio_stdout_contains_only_json_rpc_messages(stdio_session):
    assert stdio_session.stdout_lines, f"no output on stdout; stderr: {stdio_session.stderr}"
    for line in stdio_session.stdout_lines:
        message = json.loads(line)  # any log line or banner on stdout fails here
        assert message["jsonrpc"] == "2.0"


def test_stdio_server_answers_initialize_and_lists_tools(stdio_session):
    by_id = {
        message["id"]: message
        for message in map(json.loads, stdio_session.stdout_lines)
        if "id" in message
    }
    assert by_id[1]["result"]["serverInfo"]["name"] == "nextcloud-organizer-mcp"
    tool_names = {tool["name"] for tool in by_id[2]["result"]["tools"]}
    assert {"list_task_lists", "list_tasks", "create_task"} <= tool_names


def test_stdio_server_creates_no_oauth_state(stdio_session):
    assert list(stdio_session.cwd.iterdir()) == []


def test_stdio_server_logs_only_to_stderr(stdio_session):
    assert stdio_session.stderr.strip(), "expected startup logging on stderr"
