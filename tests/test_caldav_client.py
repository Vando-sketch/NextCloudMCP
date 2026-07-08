"""Unit tests for CalDavService with the caldav library itself mocked out."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from caldav.lib import error as caldav_error
from icalendar import Todo

from nextcloud_task_mcp import caldav_client as caldav_client_module
from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.errors import (
    AuthenticationFailedError,
    ConnectionFailedError,
    TaskListNotFoundError,
    TaskNotFoundError,
)


@pytest.fixture
def mock_dav_client():
    with patch("nextcloud_task_mcp.caldav_client.caldav.DAVClient") as mock_cls:
        yield mock_cls


@pytest.fixture
def service(mock_dav_client) -> CalDavService:
    return CalDavService(url="https://cloud.example.com/dav/", username="u", password="p")


@pytest.fixture
def principal(mock_dav_client):
    return mock_dav_client.return_value.principal.return_value


def test_list_task_lists_returns_names_and_urls(service, principal):
    cal1 = MagicMock()
    cal1.get_display_name.return_value = "Personal"
    cal1.url = "https://cloud.example.com/dav/personal/"
    cal2 = MagicMock()
    cal2.get_display_name.return_value = "Arbeit"
    cal2.url = "https://cloud.example.com/dav/arbeit/"
    principal.calendars.return_value = [cal1, cal2]

    result = service.list_task_lists()

    assert result == [
        {"name": "Personal", "url": "https://cloud.example.com/dav/personal/"},
        {"name": "Arbeit", "url": "https://cloud.example.com/dav/arbeit/"},
    ]


def test_list_tasks_parses_todos(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("summary", "Milch kaufen")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.todos.return_value = [todo_obj]

    result = service.list_tasks("Personal", only_open=True)

    calendar.todos.assert_called_once_with(include_completed=False)
    assert result == [
        {
            "uid": "abc",
            "titel": "Milch kaufen",
            "start_datum": None,
            "fällig_datum": None,
            "priorität": None,
            "fortschritt_prozent": 0,
            "status": "offen",
            "ort": None,
            "url": None,
            "tags": [],
            "notizen": None,
            "übergeordnete_uid": None,
        }
    ]


def test_list_tasks_list_not_found_raises(service, principal):
    principal.calendar.side_effect = caldav_error.NotFoundError("no such calendar")

    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Nonexistent")


def test_create_task_saves_ical_and_returns_uid(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar

    uid = service.create_task("Personal", titel="Neue Aufgabe")

    calendar.save_todo.assert_called_once()
    _, kwargs = calendar.save_todo.call_args
    assert "BEGIN:VTODO" in kwargs["ical"]
    assert uid in kwargs["ical"]
    assert "Neue Aufgabe" in kwargs["ical"]


def test_update_task_applies_fields_and_saves(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("summary", "Alt")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.update_task("Personal", "abc", titel="Neu")

    todo_obj.save.assert_called_once()
    assert str(todo.get("summary")) == "Neu"


def test_update_task_not_found_raises(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar
    calendar.get_todo_by_uid.side_effect = caldav_error.NotFoundError("no such task")

    with pytest.raises(TaskNotFoundError):
        service.update_task("Personal", "missing-uid", titel="x")


def test_complete_task_marks_completed(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar

    todo = Todo()
    todo.add("uid", "abc")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.complete_task("Personal", "abc")

    todo_obj.save.assert_called_once()
    assert str(todo.get("status")) == "COMPLETED"
    assert str(todo.get("percent-complete")) == "100"


def test_delete_task_calls_delete(service, principal):
    calendar = MagicMock()
    principal.calendar.return_value = calendar
    todo_obj = MagicMock()
    calendar.get_todo_by_uid.return_value = todo_obj

    service.delete_task("Personal", "abc")

    todo_obj.delete.assert_called_once()


def test_authorization_error_translated(service, mock_dav_client):
    mock_dav_client.return_value.principal.side_effect = caldav_error.AuthorizationError(
        "bad creds"
    )

    with pytest.raises(AuthenticationFailedError):
        service.list_task_lists()


def test_connection_error_translated(service, mock_dav_client):
    mock_dav_client.return_value.principal.side_effect = (
        caldav_client_module._http_errors.ConnectionError("refused")
    )

    with pytest.raises(ConnectionFailedError):
        service.list_task_lists()
