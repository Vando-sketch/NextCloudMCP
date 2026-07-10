"""Unit tests for tool registration and error translation, with CalDavService mocked."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.config import Settings
from nextcloud_task_mcp.errors import TaskListNotFoundError
from nextcloud_task_mcp.server import build_server


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
    assert schema["required"] == ["liste", "titel"]


def test_list_task_lists_delegates_to_service(tools, fake_service):
    fake_service.list_task_lists.return_value = [{"name": "Personal", "url": "https://x/"}]
    result = tools["list_task_lists"].fn()
    assert result == [{"name": "Personal", "url": "https://x/"}]


def test_list_tasks_passes_nur_offene_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    tools["list_tasks"].fn("Personal", nur_offene=False)
    fake_service.list_tasks.assert_called_once_with("Personal", only_open=False)


def test_create_task_maps_german_params_to_service_call(tools, fake_service):
    fake_service.create_task.return_value = "new-uid"
    result = tools["create_task"].fn(
        liste="Personal",
        titel="Neue Aufgabe",
        fällig_datum="2026-07-20",
        priorität="hoch",
        übergeordnete_aufgabe="parent-uid",
    )
    assert result == {"uid": "new-uid"}
    _, kwargs = fake_service.create_task.call_args
    assert kwargs["titel"] == "Neue Aufgabe"
    assert kwargs["faellig_datum"] == "2026-07-20"
    assert kwargs["prioritaet"] == "hoch"
    assert kwargs["uebergeordnete_aufgabe"] == "parent-uid"


def test_update_task_returns_uid(tools, fake_service):
    result = tools["update_task"].fn("Personal", "task-uid", titel="Neu")
    assert result == {"uid": "task-uid"}
    fake_service.update_task.assert_called_once()


def test_complete_task_delegates(tools, fake_service):
    result = tools["complete_task"].fn("Personal", "task-uid")
    assert result == {"uid": "task-uid"}
    fake_service.complete_task.assert_called_once_with("Personal", "task-uid")


def test_delete_task_delegates(tools, fake_service):
    result = tools["delete_task"].fn("Personal", "task-uid")
    assert result == {"uid": "task-uid"}
    fake_service.delete_task.assert_called_once_with("Personal", "task-uid")


def test_task_mcp_error_becomes_clean_tool_error(tools, fake_service):
    fake_service.list_tasks.side_effect = TaskListNotFoundError("Task list 'Foo' was not found.")
    with pytest.raises(ToolError, match="Foo"):
        tools["list_tasks"].fn("Foo")


def test_unexpected_error_does_not_leak_internals(tools, fake_service):
    fake_service.list_tasks.side_effect = RuntimeError("some internal detail")
    with pytest.raises(ToolError) as exc_info:
        tools["list_tasks"].fn("Personal")
    assert "some internal detail" not in str(exc_info.value)


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
