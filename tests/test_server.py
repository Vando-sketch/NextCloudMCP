"""Unit tests for tool registration and error translation, with CalDavService mocked."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from nextcloud_task_mcp.caldav_client import CalDavService
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
