"""Unit tests for tool registration and error translation, with CalDavService mocked."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.config import Settings
from nextcloud_task_mcp.errors import TaskListNotFoundError
from nextcloud_task_mcp.server import build_server


def _run(coro):
    """Run an async tool call from a sync test function (mirrors tests/test_auth.py)."""
    return asyncio.run(coro)


@pytest.fixture
def fake_service() -> MagicMock:
    return MagicMock(spec=CalDavService)


@pytest.fixture
def tools(settings, fake_service):
    mcp = build_server(settings, service=fake_service)
    return asyncio.run(mcp.get_tools())


def test_all_tools_registered(tools):
    assert set(tools) == {
        "list_task_lists",
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "complete_task",
        "delete_task",
    }


def test_create_task_uses_umlaut_parameter_names(tools):
    schema = tools["create_task"].parameters
    assert "fällig_datum" in schema["properties"]
    assert "priorität" in schema["properties"]
    assert "übergeordnete_aufgabe" in schema["properties"]
    assert schema["required"] == ["list_name", "titel"]


def test_update_task_has_felder_leeren_parameter(tools):
    schema = tools["update_task"].parameters
    assert "felder_leeren" in schema["properties"]


def test_get_task_delegates_to_service(tools, fake_service):
    fake_service.get_task.return_value = {"uid": "abc", "titel": "Milch kaufen"}
    result = _run(tools["get_task"].fn("Personal", "abc"))
    assert result == {"uid": "abc", "titel": "Milch kaufen"}
    fake_service.get_task.assert_called_once_with("Personal", "abc")


def test_list_task_lists_delegates_to_service(tools, fake_service):
    fake_service.list_task_lists.return_value = [{"name": "Personal", "url": "https://x/"}]
    result = _run(tools["list_task_lists"].fn())
    assert result == [{"name": "Personal", "url": "https://x/"}]


def test_list_tasks_passes_nur_offene_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(tools["list_tasks"].fn("Personal", nur_offene=False))
    fake_service.list_tasks.assert_called_once_with("Personal", only_open=False)


def test_create_task_maps_german_params_to_service_call(tools, fake_service):
    fake_service.create_task.return_value = "new-uid"
    result = _run(
        tools["create_task"].fn(
            list_name="Personal",
            titel="Neue Aufgabe",
            fällig_datum="2026-07-20",
            priorität="hoch",
            übergeordnete_aufgabe="parent-uid",
        )
    )
    assert result == {"uid": "new-uid"}
    args, _ = fake_service.create_task.call_args
    list_name, fields = args
    assert list_name == "Personal"
    assert fields.titel == "Neue Aufgabe"
    assert fields.faellig_datum == "2026-07-20"
    assert fields.prioritaet == "hoch"
    assert fields.uebergeordnete_aufgabe == "parent-uid"


def test_update_task_returns_uid(tools, fake_service):
    result = _run(tools["update_task"].fn("Personal", "task-uid", titel="Neu"))
    assert result == {"uid": "task-uid"}
    fake_service.update_task.assert_called_once()
    args, _ = fake_service.update_task.call_args
    list_name, task_uid, fields = args
    assert list_name == "Personal"
    assert task_uid == "task-uid"
    assert fields.titel == "Neu"


def test_update_task_passes_felder_leeren_as_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", felder_leeren=["fällig_datum", "ort"]))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ("fällig_datum", "ort")


def test_update_task_without_felder_leeren_has_empty_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", titel="Neu"))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ()


def test_complete_task_delegates(tools, fake_service):
    result = _run(tools["complete_task"].fn("Personal", "task-uid"))
    assert result == {"uid": "task-uid"}
    fake_service.complete_task.assert_called_once_with("Personal", "task-uid")


def test_delete_task_delegates(tools, fake_service):
    result = _run(tools["delete_task"].fn("Personal", "task-uid"))
    assert result == {"uid": "task-uid"}
    fake_service.delete_task.assert_called_once_with("Personal", "task-uid")


def test_task_mcp_error_becomes_clean_tool_error(tools, fake_service):
    fake_service.list_tasks.side_effect = TaskListNotFoundError("Task list 'Foo' was not found.")
    with pytest.raises(ToolError, match="Foo"):
        _run(tools["list_tasks"].fn("Foo"))


def test_unexpected_error_does_not_leak_internals(tools, fake_service):
    fake_service.list_tasks.side_effect = RuntimeError("some internal detail")
    with pytest.raises(ToolError) as exc_info:
        _run(tools["list_tasks"].fn("Personal"))
    assert "some internal detail" not in str(exc_info.value)


# --- Non-blocking tools (A1): a blocked call must not stall a concurrent one ---


def test_concurrent_tool_calls_do_not_block_each_other(tools, fake_service):
    """A slow/blocked CalDavService call must not stall other tool calls.

    Simulates the A1 scenario directly: `list_tasks` blocks on a
    `threading.Event` (standing in for a hung Nextcloud request) while a
    second, independent `list_task_lists` call is issued concurrently. Since
    tool bodies now offload the blocking service call to a worker thread via
    anyio.to_thread.run_sync, the event loop stays free and the second call
    completes well before the first one is unblocked.
    """
    started = threading.Event()
    release = threading.Event()

    def blocking_list_tasks(list_name, only_open=True):
        started.set()
        release.wait(timeout=5)
        return []

    fake_service.list_tasks.side_effect = blocking_list_tasks
    fake_service.list_task_lists.return_value = [{"name": "Personal", "url": "https://x/"}]

    async def scenario():
        blocked_task = asyncio.create_task(tools["list_tasks"].fn("Personal"))
        # Wait until the blocking call has actually started running in its
        # worker thread, then race a second, independent tool call against it.
        await asyncio.to_thread(started.wait, 5)

        second_result = await asyncio.wait_for(tools["list_task_lists"].fn(), timeout=2)
        assert second_result == [{"name": "Personal", "url": "https://x/"}]
        assert not blocked_task.done()

        release.set()
        await asyncio.wait_for(blocked_task, timeout=5)

    asyncio.run(scenario())


# --- Redirect-domain allow-list defaults (D9) ---
#
# PersonalAuthProvider's own vendored default allow-list is
# ["claude.ai", "claude.com", "localhost"]. build_server overrides that
# default (only when the operator hasn't set MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS
# themselves) to drop "localhost" once PUBLIC_BASE_URL is not local, since a
# "localhost" entry can never be reached by a real OAuth redirect against a
# public deployment.


def test_build_server_drops_localhost_when_public_base_url_is_public(settings, fake_service):
    # The `settings` fixture already uses a non-local public_base_url and leaves
    # oauth_allowed_redirect_domains unset (None).
    assert settings.oauth_allowed_redirect_domains is None
    mcp = build_server(settings, service=fake_service)
    assert mcp.auth.allowed_redirect_domains == ["claude.ai", "claude.com"]
    assert "localhost" not in mcp.auth.allowed_redirect_domains


def test_build_server_keeps_vendored_default_when_public_base_url_is_local(fake_service, tmp_path):
    local_settings = Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        public_base_url="http://127.0.0.1:8000",
        oauth_password=None,
        oauth_state_dir=str(tmp_path / "oauth-state"),
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
    )
    mcp = build_server(local_settings, service=fake_service)
    assert mcp.auth.allowed_redirect_domains == ["claude.ai", "claude.com", "localhost"]


def test_build_server_respects_explicitly_configured_redirect_domains(settings, fake_service):
    public_settings = replace(
        settings,
        public_base_url="https://public.example.com",
        oauth_allowed_redirect_domains=["only-this.example.com"],
    )
    mcp = build_server(public_settings, service=fake_service)
    assert mcp.auth.allowed_redirect_domains == ["only-this.example.com"]
