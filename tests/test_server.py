"""Unit tests for tool registration and error translation, with CalDavService mocked."""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastmcp.exceptions import ToolError

from nextcloud_task_mcp import event_mapping, mapping
from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.config import Settings
from nextcloud_task_mcp.errors import (
    CalendarNotFoundError,
    NoteNotFoundError,
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
)
from nextcloud_task_mcp.notes_client import NotesService
from nextcloud_task_mcp.personal_auth import PersonalAuthProvider
from nextcloud_task_mcp.server import build_server, main


def _run(coro):
    """Run an async tool call from a sync test function (mirrors tests/test_auth.py)."""
    return asyncio.run(coro)


@pytest.fixture
def fake_service() -> MagicMock:
    return MagicMock(spec=CalDavService)


@pytest.fixture
def fake_notes_service() -> MagicMock:
    return MagicMock(spec=NotesService)


@pytest.fixture
def tools(settings, fake_service, fake_notes_service):
    mcp = build_server(settings, service=fake_service, notes_service=fake_notes_service)
    return asyncio.run(mcp.get_tools())


def test_all_tools_registered(tools):
    assert set(tools) == {
        "list_task_lists",
        "create_task_list",
        "delete_task_list",
        "rename_task_list",
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "complete_task",
        "delete_task",
        "move_task",
        "list_calendars",
        "create_calendar",
        "delete_calendar",
        "update_calendar",
        "list_events",
        "get_event",
        "create_event",
        "create_birthday",
        "update_event",
        "update_events",
        "update_exdates",
        "delete_event",
        "delete_events",
        "move_event",
        "respond_to_event",
        "link_task_to_event",
        "list_events_for_task",
        "create_event_from_task",
        "get_agenda",
        "list_tags",
        "get_free_busy",
        "share_calendar",
        "unshare_calendar",
        "list_calendar_shares",
        "list_trash",
        "restore_from_trash",
        "export_calendar",
        "import_ics",
        "list_notes",
        "get_note",
        "create_note",
        "update_note",
        "replace_in_note",
        "update_note_section",
        "append_to_note",
        "search_notes",
        "delete_note",
    }


def test_all_tools_use_ascii_parameter_names(tools):
    """The Anthropic API rejects non-ASCII schema property names (see commit 14d742d)."""
    for tool_name, tool in tools.items():
        for prop_name in tool.parameters.get("properties", {}):
            assert prop_name.isascii(), f"{tool_name}.{prop_name} is not ASCII"


def test_create_task_list_uses_ascii_parameter_names(tools):
    schema = tools["create_task_list"].parameters
    assert set(schema["properties"]) == {"display_name"}
    assert schema["required"] == ["display_name"]


def test_delete_task_list_uses_ascii_parameter_names(tools):
    schema = tools["delete_task_list"].parameters
    assert set(schema["properties"]) == {"list_name"}
    assert schema["required"] == ["list_name"]


def test_rename_task_list_uses_ascii_parameter_names(tools):
    schema = tools["rename_task_list"].parameters
    assert set(schema["properties"]) == {"list_name", "new_display_name"}
    assert set(schema["required"]) == {"list_name", "new_display_name"}


def test_create_task_uses_umlaut_parameter_names(tools):
    schema = tools["create_task"].parameters
    assert "due_date" in schema["properties"]
    assert "priority" in schema["properties"]
    assert "parent_task" in schema["properties"]
    assert schema["required"] == ["list_name", "title"]


def test_update_task_has_clear_fields_parameter(tools):
    schema = tools["update_task"].parameters
    assert "clear_fields" in schema["properties"]


def test_get_task_delegates_to_service(tools, fake_service):
    fake_service.get_task.return_value = {"uid": "abc", "title": "Buy milk"}
    result = _run(tools["get_task"].fn("Personal", "abc"))
    assert result == {"uid": "abc", "title": "Buy milk"}
    fake_service.get_task.assert_called_once_with("Personal", "abc")


# --- Notes tools ---


def test_list_notes_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.list_notes.return_value = [{"id": 1, "title": "Project X"}]
    result = _run(tools["list_notes"].fn("Work"))
    assert result == [{"id": 1, "title": "Project X"}]
    fake_notes_service.list_notes.assert_called_once_with("Work")


def test_get_note_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.get_note.return_value = {"id": 1, "title": "Project X", "content": "..."}
    result = _run(tools["get_note"].fn(1))
    assert result == {"id": 1, "title": "Project X", "content": "..."}
    fake_notes_service.get_note.assert_called_once_with(1)


def test_create_note_builds_note_fields(tools, fake_notes_service):
    fake_notes_service.create_note.return_value = {"id": 2}
    result = _run(tools["create_note"].fn("Project X", "Work", "First Note", True))
    assert result == {"id": 2}
    fields = fake_notes_service.create_note.call_args[0][0]
    assert fields.title == "Project X"
    assert fields.category == "Work"
    assert fields.content == "First Note"
    assert fields.favorite is True


def test_update_note_only_sets_given_fields(tools, fake_notes_service):
    fake_notes_service.update_note.return_value = {"id": 2}
    result = _run(tools["update_note"].fn(2, title="Neuer Title"))
    assert result == {"id": 2}
    note_id, fields = fake_notes_service.update_note.call_args[0]
    assert note_id == 2
    assert fields.title == "Neuer Title"
    assert fields.category is None
    assert fields.content is None
    assert fields.favorite is None


def test_replace_in_note_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.replace_in_note.return_value = {"id": 2, "content": "New_text"}
    result = _run(tools["replace_in_note"].fn(2, "Old_text", "New_text"))
    assert result == {"id": 2, "content": "New_text"}
    fake_notes_service.replace_in_note.assert_called_once_with(2, "Old_text", "New_text")


def test_update_note_section_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.replace_note_section.return_value = {"id": 2, "content": "## A\n\nNeu"}
    result = _run(tools["update_note_section"].fn(2, "## A", "## A\n\nNeu"))
    assert result == {"id": 2, "content": "## A\n\nNeu"}
    fake_notes_service.replace_note_section.assert_called_once_with(2, "## A", "## A\n\nNeu")


def test_append_to_note_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.append_note.return_value = {"id": 2, "content": "Old_text\n\nNeu"}
    result = _run(tools["append_to_note"].fn(2, "New_text"))
    assert result == {"id": 2, "content": "Old_text\n\nNeu"}
    fake_notes_service.append_note.assert_called_once_with(2, "New_text")


def test_search_notes_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.search_notes.return_value = [{"id": 1, "title": "Project X"}]
    result = _run(tools["search_notes"].fn("Projekt", "Work"))
    assert result == [{"id": 1, "title": "Project X"}]
    fake_notes_service.search_notes.assert_called_once_with("Projekt", "Work")


def test_delete_note_delegates_to_notes_service(tools, fake_notes_service):
    result = _run(tools["delete_note"].fn(2))
    assert result == {"id": 2}
    fake_notes_service.delete_note.assert_called_once_with(2)


def test_note_tools_use_ascii_parameter_names(tools):
    for tool_name in (
        "list_notes",
        "get_note",
        "create_note",
        "update_note",
        "replace_in_note",
        "update_note_section",
        "append_to_note",
        "search_notes",
        "delete_note",
    ):
        schema = tools[tool_name].parameters
        for prop_name in schema.get("properties", {}):
            assert prop_name.isascii(), f"{tool_name}.{prop_name} is not ASCII"


def test_create_note_requires_only_title(tools):
    schema = tools["create_note"].parameters
    assert schema["required"] == ["title"]


def test_get_note_requires_note_id(tools):
    schema = tools["get_note"].parameters
    assert schema["required"] == ["note_id"]


def test_delete_note_requires_note_id(tools):
    schema = tools["delete_note"].parameters
    assert schema["required"] == ["note_id"]


def test_delete_note_not_found_becomes_clean_tool_error(tools, fake_notes_service):
    fake_notes_service.delete_note.side_effect = NoteNotFoundError(
        "The requested note was not found."
    )
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["delete_note"].fn(999))


def test_note_not_found_becomes_clean_tool_error(tools, fake_notes_service):
    fake_notes_service.get_note.side_effect = NoteNotFoundError("The requested note was not found.")
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["get_note"].fn(999))


def test_note_unexpected_exception_becomes_generic_tool_error(tools, fake_notes_service):
    fake_notes_service.get_note.side_effect = RuntimeError("boom")
    with pytest.raises(ToolError, match="unexpected internal error"):
        _run(tools["get_note"].fn(1))


def test_get_task_returns_recurrence_field(tools, fake_service):
    fake_service.get_task.return_value = {"uid": "abc", "recurrence": "FREQ=WEEKLY"}
    result = _run(tools["get_task"].fn("Personal", "abc"))
    assert result["recurrence"] == "FREQ=WEEKLY"


def test_list_task_lists_delegates_to_service(tools, fake_service):
    fake_service.list_task_lists.return_value = [{"name": "Personal", "url": "https://x/"}]
    result = _run(tools["list_task_lists"].fn())
    assert result == [{"name": "Personal", "url": "https://x/"}]


def test_create_task_list_delegates_to_service(tools, fake_service):
    fake_service.create_task_list.return_value = {
        "name": "Groceries",
        "url": "https://x/groceries/",
    }
    result = _run(tools["create_task_list"].fn(display_name="Groceries"))
    assert result == {"name": "Groceries", "url": "https://x/groceries/"}
    fake_service.create_task_list.assert_called_once_with("Groceries")


def test_create_task_list_conflict_becomes_clean_tool_error(tools, fake_service):
    fake_service.create_task_list.side_effect = TaskListAlreadyExistsError(
        "A task list named 'Groceries' already exists."
    )
    with pytest.raises(ToolError, match="Groceries"):
        _run(tools["create_task_list"].fn(display_name="Groceries"))


def test_delete_task_list_delegates_to_service(tools, fake_service):
    result = _run(tools["delete_task_list"].fn(list_name="Groceries"))
    assert result == {"list_name": "Groceries"}
    fake_service.delete_task_list.assert_called_once_with("Groceries")


def test_delete_task_list_not_found_becomes_clean_tool_error(tools, fake_service):
    fake_service.delete_task_list.side_effect = TaskListNotFoundError(
        "Task list 'Groceries' was not found."
    )
    with pytest.raises(ToolError, match="Groceries"):
        _run(tools["delete_task_list"].fn(list_name="Groceries"))


def test_rename_task_list_delegates_to_service(tools, fake_service):
    fake_service.rename_task_list.return_value = {
        "name": "Shopping",
        "url": "https://x/groceries/",
    }
    result = _run(tools["rename_task_list"].fn(list_name="Groceries", new_display_name="Shopping"))
    assert result == {"name": "Shopping", "url": "https://x/groceries/"}
    fake_service.rename_task_list.assert_called_once_with("Groceries", "Shopping")


def test_rename_task_list_conflict_becomes_clean_tool_error(tools, fake_service):
    fake_service.rename_task_list.side_effect = TaskListAlreadyExistsError(
        "A task list named 'Shopping' already exists."
    )
    with pytest.raises(ToolError, match="Shopping"):
        _run(tools["rename_task_list"].fn(list_name="Groceries", new_display_name="Shopping"))


def test_rename_task_list_not_found_becomes_clean_tool_error(tools, fake_service):
    fake_service.rename_task_list.side_effect = TaskListNotFoundError(
        "Task list 'Groceries' was not found."
    )
    with pytest.raises(ToolError, match="Groceries"):
        _run(tools["rename_task_list"].fn(list_name="Groceries", new_display_name="Shopping"))


def test_list_tasks_passes_only_open_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(tools["list_tasks"].fn("Personal", only_open=False))
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=False,
        due_before=None,
        due_after=None,
        priority=None,
        tag=None,
        search_text=None,
        without_reminder=False,
        without_visibility=False,
        without_tags=False,
        uid_regex=None,
        limit=None,
    )


def test_list_tasks_passes_filter_params_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(
        tools["list_tasks"].fn(
            list_names=["Personal"],
            due_before="2026-08-01",
            due_after="2026-07-01",
            priority="high",
            tag="work",
            search_text="test",
            limit=5,
        )
    )
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=True,
        due_before="2026-08-01",
        due_after="2026-07-01",
        priority="high",
        tag="work",
        search_text="test",
        without_reminder=False,
        without_visibility=False,
        without_tags=False,
        uid_regex=None,
        limit=5,
    )


def test_list_tasks_deprecated_list_name_alias_works(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(tools["list_tasks"].fn(list_name="Personal"))
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=True,
        due_before=None,
        due_after=None,
        priority=None,
        tag=None,
        search_text=None,
        without_reminder=False,
        without_visibility=False,
        without_tags=False,
        uid_regex=None,
        limit=None,
    )


def test_list_tasks_passes_cleanup_filters_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(
        tools["list_tasks"].fn(
            list_names=["Personal"],
            without_reminder=True,
            without_visibility=True,
            without_tags=True,
            uid_regex="^[A-F0-9-]+$",
        )
    )
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=True,
        due_before=None,
        due_after=None,
        priority=None,
        tag=None,
        search_text=None,
        without_reminder=True,
        without_visibility=True,
        without_tags=True,
        uid_regex="^[A-F0-9-]+$",
        limit=None,
    )


def test_list_tasks_both_list_name_and_list_names_raises_error(tools, fake_service):
    with pytest.raises(ToolError, match="list_name is the deprecated alias of list_names"):
        _run(tools["list_tasks"].fn(list_name="Personal", list_names=["Work"]))
    # The conflict is refused, not resolved: neither argument silently wins.
    fake_service.list_tasks.assert_not_called()


def test_list_tasks_tool_filters_added_after_limit_are_keyword_only(tools):
    """Same positional prefix as `CalDavService.list_tasks`, for the same reason.

    A filter inserted before `limit` rebinds a positional caller's value
    without a word; keeping everything after `limit` keyword-only makes that
    impossible.
    """
    params = inspect.signature(tools["list_tasks"].fn).parameters
    positional = [
        name for name, p in params.items() if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]

    assert positional == ["list_names", "only_open", "due_before", "due_after", "limit"]
    assert all(
        params[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in (
            "priority",
            "tag",
            "search_text",
            "without_reminder",
            "without_visibility",
            "without_tags",
            "uid_regex",
            "list_name",
        )
    )


def test_list_tasks_tool_still_exposes_every_filter_to_clients(tools):
    """Keyword-only parameters must still reach the MCP schema clients read."""
    properties = tools["list_tasks"].parameters["properties"]

    assert {"list_names", "only_open", "due_before", "due_after", "limit"} <= set(properties)
    assert {"priority", "tag", "search_text", "list_name"} <= set(properties)
    assert {"without_reminder", "without_visibility", "without_tags", "uid_regex"} <= set(
        properties
    )


def test_no_tool_param_with_a_default_is_required_in_the_schema(tools):
    """A parameter with a Python default must be optional in the client schema.

    fastmcp (<3) rebuilds tool functions whose annotations are PEP 563 strings
    (`from __future__ import annotations`) and loses `__kwdefaults__` doing it,
    so every keyword-only parameter turns required-but-nullable - and clients
    that then pass an explicit null can trip over it. server.py therefore must
    not use the future import; this test fails on every affected tool at once
    if it comes back.
    """
    offenders = []
    for tool_name, tool in tools.items():
        required = set(tool.parameters.get("required") or [])
        for param_name, param in inspect.signature(tool.fn).parameters.items():
            if param.default is not inspect.Parameter.empty and param_name in required:
                offenders.append(f"{tool_name}.{param_name}")

    assert offenders == []


def test_create_task_maps_german_params_to_service_call(tools, fake_service):
    fake_service.create_task.return_value = "new-uid"
    result = _run(
        tools["create_task"].fn(
            list_name="Personal",
            title="New task",
            due_date="2026-07-20",
            priority="high",
            parent_task="parent-uid",
        )
    )
    assert result == {"uid": "new-uid"}
    args, _ = fake_service.create_task.call_args
    list_name, fields = args
    assert list_name == "Personal"
    assert fields.title == "New task"
    assert fields.due_date == "2026-07-20"
    assert fields.priority == "high"
    assert fields.parent_task == "parent-uid"


def test_create_task_passes_recurrence(tools, fake_service):
    fake_service.create_task.return_value = "new-uid"
    _run(
        tools["create_task"].fn(
            list_name="Personal",
            title="Muell rausbringen",
            due_date="2026-07-20",
            recurrence="FREQ=WEEKLY;BYDAY=MO",
        )
    )
    args, _ = fake_service.create_task.call_args
    _, fields = args
    assert fields.recurrence == "FREQ=WEEKLY;BYDAY=MO"


def test_update_task_passes_recurrence(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", recurrence="FREQ=DAILY"))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.recurrence == "FREQ=DAILY"


def test_update_task_passes_status(tools, fake_service):
    """The reopen path has to survive the tool layer, not just the mapping."""
    _run(tools["update_task"].fn("Personal", "task-uid", status="open"))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.status == "open"


def test_update_task_passes_clear_fields_recurrence_as_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", clear_fields=["recurrence"]))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ("recurrence",)


def test_update_task_returns_uid(tools, fake_service):
    result = _run(tools["update_task"].fn("Personal", "task-uid", title="New_text"))
    assert result == {"uid": "task-uid"}
    fake_service.update_task.assert_called_once()
    args, _ = fake_service.update_task.call_args
    list_name, task_uid, fields = args
    assert list_name == "Personal"
    assert task_uid == "task-uid"
    assert fields.title == "New_text"


def test_update_task_passes_clear_fields_as_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", clear_fields=["due_date", "location"]))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ("due_date", "location")


def test_update_task_without_clear_fields_has_empty_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", title="New_text"))
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


def test_move_task_delegates(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
    }
    result = _run(
        tools["move_task"].fn(list_name="Private", task_uid="task-uid", target_list="Work")
    )
    assert result == {
        "uid": "task-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
    }
    fake_service.move_task.assert_called_once_with("Private", "task-uid", "Work", None, ())


def test_move_task_passes_new_parent_through(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
        "hierarchy": "set",
    }
    result = _run(
        tools["move_task"].fn(
            list_name="Private",
            task_uid="task-uid",
            target_list="Work",
            parent_task="parent-uid",
        )
    )
    assert result["hierarchy"] == "set"
    fake_service.move_task.assert_called_once_with("Private", "task-uid", "Work", "parent-uid", ())


def test_move_task_passes_clear_fields_through_as_tuple(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
        "hierarchy": "cleared",
    }
    _run(
        tools["move_task"].fn(
            list_name="Private",
            task_uid="task-uid",
            target_list="Work",
            clear_fields=["parent_task"],
        )
    )
    fake_service.move_task.assert_called_once_with(
        "Private", "task-uid", "Work", None, ("parent_task",)
    )


def test_move_event_delegates(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "from": "Private",
        "to": "Work",
        "method": "copied",
    }
    result = _run(
        tools["move_event"].fn(
            calendar_name="Private", event_uid="event-uid", target_calendar="Work"
        )
    )
    assert result == {
        "uid": "event-uid",
        "from": "Private",
        "to": "Work",
        "method": "copied",
    }
    fake_service.move_event.assert_called_once_with("Private", "event-uid", "Work", None, ())


def test_move_event_passes_linked_task_through(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
        "hierarchy": "set",
    }
    result = _run(
        tools["move_event"].fn(
            calendar_name="Private",
            event_uid="event-uid",
            target_calendar="Work",
            linked_task="task-uid",
        )
    )
    assert result["hierarchy"] == "set"
    fake_service.move_event.assert_called_once_with("Private", "event-uid", "Work", "task-uid", ())


def test_move_event_passes_clear_fields_through_as_tuple(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "from": "Private",
        "to": "Work",
        "method": "MOVE",
        "hierarchy": "cleared",
    }
    _run(
        tools["move_event"].fn(
            calendar_name="Private",
            event_uid="event-uid",
            target_calendar="Work",
            clear_fields=["linked_task"],
        )
    )
    fake_service.move_event.assert_called_once_with(
        "Private", "event-uid", "Work", None, ("linked_task",)
    )


# --- Calendar/event tools ---


def test_create_event_schema(tools):
    schema = tools["create_event"].parameters
    assert set(schema["required"]) == {"calendar_name", "title", "start"}
    assert "recurrence" in schema["properties"]
    assert "exception_dates" in schema["properties"]
    assert "reminders" in schema["properties"]
    assert "linked_task" in schema["properties"]
    assert "attendees" in schema["properties"]


def test_respond_to_event_schema(tools):
    schema = tools["respond_to_event"].parameters
    assert set(schema["required"]) == {"calendar_name", "event_uid", "response"}
    assert "comment" in schema["properties"]


def test_get_free_busy_schema(tools):
    schema = tools["get_free_busy"].parameters
    assert set(schema["required"]) == {"start", "end"}
    assert "user" in schema["properties"]


def test_update_event_has_clear_fields_parameter(tools):
    schema = tools["update_event"].parameters
    assert "clear_fields" in schema["properties"]
    assert set(schema["required"]) == {"calendar_name", "event_uid"}


def test_list_events_schema(tools):
    schema = tools["list_events"].parameters
    assert set(schema["properties"]) == {
        "calendar_names",
        "start",
        "end",
        "search_text",
        "tag",
        "limit",
        "expand_recurrences",
        "without_reminder",
        "without_visibility",
        "without_tags",
        "uid_regex",
        "fields",
        "compact",
    }
    assert schema.get("required", []) == []


def test_list_events_delegates_with_filters(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(
        tools["list_events"].fn(
            calendar_names=["Events"],
            start="2026-07-01",
            end="2026-07-31",
            search_text="Dentist",
            tag="Private",
            limit=10,
            expand_recurrences=True,
        )
    )
    fake_service.list_events.assert_called_once_with(
        calendar_names=["Events"],
        start="2026-07-01",
        end="2026-07-31",
        search_text="Dentist",
        tag="Private",
        limit=10,
        expand=True,
        without_reminder=False,
        without_visibility=False,
        without_tags=False,
        uid_regex=None,
    )


def test_list_events_passes_cleanup_filters_through(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(
        tools["list_events"].fn(
            calendar_names=["Events"],
            without_reminder=True,
            without_visibility=True,
            without_tags=True,
            uid_regex="^[A-F0-9-]+$",
        )
    )
    fake_service.list_events.assert_called_once_with(
        calendar_names=["Events"],
        start=None,
        end=None,
        search_text=None,
        tag=None,
        limit=None,
        expand=False,
        without_reminder=True,
        without_visibility=True,
        without_tags=True,
        uid_regex="^[A-F0-9-]+$",
    )


def test_create_event_builds_event_fields(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    expected_event = {"uid": "new-uid", "title": "Meeting", "start": "2026-07-20T14:00:00"}
    fake_service.get_event.return_value = expected_event
    result = _run(
        tools["create_event"].fn(
            calendar_name="Events",
            title="Meeting",
            start="2026-07-20T14:00:00",
            end="2026-07-20T15:00:00",
            status="confirmed",
        )
    )
    assert result == expected_event
    (cal_name, fields), _ = fake_service.create_event.call_args
    assert cal_name == "Events"
    assert fields.title == "Meeting"
    assert fields.status == "confirmed"
    assert fields.clear == ()
    fake_service.get_event.assert_called_once_with("Events", "new-uid")


# --- Birthdays (create_birthday) ---


def test_create_birthday_schema(tools):
    schema = tools["create_birthday"].parameters
    assert set(schema["properties"]) == {"name", "date", "year", "calendar"}
    assert schema["required"] == ["name", "date"]
    assert schema["properties"]["calendar"]["default"] == "Birthdays"


def test_create_birthday_builds_the_convention_and_returns_the_event(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    expected_event = {"uid": "new-uid", "title": "🎂 Papa (1975)"}
    fake_service.get_event.return_value = expected_event

    result = _run(tools["create_birthday"].fn(name="Papa", date="07-04", year=1975))

    assert result == expected_event
    (cal_name, fields), _ = fake_service.create_event.call_args
    assert cal_name == "Birthdays"
    assert fields.title == "🎂 Papa (1975)"
    assert (fields.start, fields.end) == ("1975-07-04", "1975-07-04")
    assert fields.recurrence == "FREQ=YEARLY"
    assert fields.tags == ["Birthday"]
    assert fields.visibility == "private"
    assert fields.reminders == ["-PT0M", "-P1D"]
    fake_service.get_event.assert_called_once_with("Birthdays", "new-uid")


def test_create_birthday_writes_to_the_given_calendar(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"

    _run(tools["create_birthday"].fn(name="Papa", date="07-04", calendar="Familie"))

    (cal_name, _), _ = fake_service.create_event.call_args
    assert cal_name == "Familie"


def test_create_birthday_invalid_date_becomes_clean_tool_error(tools, fake_service):
    with pytest.raises(ToolError, match="Could not parse date"):
        _run(tools["create_birthday"].fn(name="Papa", date="4.7."))
    fake_service.create_event.assert_not_called()


def test_update_event_passes_clear_fields(tools, fake_service):
    expected_event = {"uid": "event-1", "title": "Meeting", "location": None}
    fake_service.get_event.return_value = expected_event
    result = _run(
        tools["update_event"].fn(
            calendar_name="Events",
            event_uid="event-1",
            clear_fields=["end", "location"],
        )
    )
    assert result == expected_event
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.clear == ("end", "location")
    fake_service.get_event.assert_called_once_with("Events", "event-1")


def test_list_events_compacts_large_exdate_list(tools, fake_service):
    exdates_15 = [f"2026-08-{i:02d}" for i in range(1, 16)]
    fake_service.list_events.return_value = [
        {"uid": "e1", "exception_dates": exdates_15},
        {"uid": "e2", "exception_dates": ["2026-08-01", "2026-08-02", "2026-08-03"]},
        {"uid": "e3", "exception_dates": []},
        {"uid": "e4", "exception_dates": None},
    ]
    results = _run(tools["list_events"].fn(calendar_names=["Events"]))
    assert results[0]["exception_dates"] == {
        "count": 15,
        "first": exdates_15[:5],
        "note": "truncated - full List über get_event abrufen",
    }
    assert results[1]["exception_dates"] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert results[2]["exception_dates"] == []
    assert results[3]["exception_dates"] is None


# --- Payload slimming (fields whitelist / compact mode) and default window ---


def test_fields_whitelists_track_the_keys_the_parsers_actually_produce():
    """`fields` rejects any name not in the whitelist, so the two must not drift.

    A key added to `parse_vtodo`/`parse_vevent` but forgotten here becomes a
    field that listings return yet `fields` refuses to select - which is how
    `visibility` arrived on the task side. Deriving both sets from the
    parsers makes the next such addition fail here instead of in a client.
    """
    from icalendar import Event, Todo

    from nextcloud_task_mcp.server import _EVENT_RESULT_KEYS, _TASK_RESULT_KEYS

    todo = Todo()
    todo.add("uid", "t1")
    # "list"/"list_url" are stamped on by the client layer, not the parser.
    assert set(mapping.parse_vtodo(todo)) | {"list", "list_url"} == set(_TASK_RESULT_KEYS)

    event = Event()
    event.add("uid", "e1")
    # "calendar" likewise; "source_url" is stripped before list_events returns.
    assert set(event_mapping.parse_vevent(event)) | {"calendar"} == set(_EVENT_RESULT_KEYS)


def _sample_event() -> dict:
    return {
        "uid": "e1",
        "title": "Meeting",
        "start": "2026-07-20T14:00:00+02:00",
        "end": "2026-07-20T15:00:00+02:00",
        "all_day": False,
        "location": None,
        "description": "x" * 250,
        "tags": [],
        "reminders": [],
        "status": None,
        "visibility": None,
        "recurrence": None,
        "exception_dates": [],
        "url": None,
        "linked_tasks": [],
        "recurrence_id": None,
        "calendar": "Events",
        "organizer": None,
        "attendees": [],
    }


def test_list_events_compact_drops_empty_fields_and_truncates_description(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(calendar_names=["Events"], compact=True))
    assert set(event) == {"uid", "title", "start", "end", "all_day", "description", "calendar"}
    assert event["all_day"] is False  # False is a value, not "empty"
    assert event["description"].startswith("x" * 200 + "…")
    assert "truncated start 250 Characters" in event["description"]
    assert "get_event" in event["description"]


def test_list_events_compact_keeps_short_description_untouched(tools, fake_service):
    event = _sample_event()
    event["description"] = "kurz"
    fake_service.list_events.return_value = [event]
    (result,) = _run(tools["list_events"].fn(calendar_names=["Events"], compact=True))
    assert result["description"] == "kurz"


def test_list_events_fields_whitelist_filters_keys(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(
        tools["list_events"].fn(calendar_names=["Events"], fields=["uid", "title", "start"])
    )
    assert event == {"uid": "e1", "title": "Meeting", "start": "2026-07-20T14:00:00+02:00"}


def test_list_events_fields_accepts_bare_string(tools, fake_service):
    # Same lenience as list_names: a single name instead of a list works.
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(calendar_names=["Events"], fields="uid"))
    assert event == {"uid": "e1"}


def test_list_events_unknown_fields_entry_raises(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    with pytest.raises(ToolError, match="Unknown fields-Entries: summary"):
        _run(tools["list_events"].fn(calendar_names=["Events"], fields=["uid", "summary"]))


def test_list_events_empty_fields_means_no_whitelist(tools, fake_service):
    """`[]` is what MCP clients send for an unset array - it must not blank the row.

    Deliberately unlike `list_names=[]` (an empty scope, which returns no
    rows): an empty *field* whitelist can only otherwise mean "a row of
    nothing", which is never what a caller wants.
    """
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(calendar_names=["Events"], fields=[]))
    assert event == _sample_event()


def test_list_events_compact_keeps_exdate_summary_dict(tools, fake_service):
    """The >10-EXDATE summary is a dict, so `compact` must not prune it as "empty"."""
    event = _sample_event()
    event["exception_dates"] = [f"2026-08-{i:02d}" for i in range(1, 16)]
    fake_service.list_events.return_value = [event]
    (result,) = _run(tools["list_events"].fn(calendar_names=["Events"], compact=True))
    assert result["exception_dates"]["count"] == 15


def test_list_events_fields_combines_with_compact(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(
        tools["list_events"].fn(
            calendar_names=["Events"], fields=["uid", "title", "location"], compact=True
        )
    )
    # `location` is whitelisted but None, so compact still drops it.
    assert event == {"uid": "e1", "title": "Meeting"}


def test_list_events_without_calendars_and_window_defaults_to_90_days(tools, fake_service):
    from datetime import date, datetime, timedelta

    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn())
    _, kwargs = fake_service.list_events.call_args
    today = datetime.now(mapping.get_default_timezone()).date()
    assert date.fromisoformat(kwargs["start"]) == today - timedelta(days=90)
    assert date.fromisoformat(kwargs["end"]) == today + timedelta(days=90)


def test_list_events_default_window_not_applied_when_scoped(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(calendar_names=["Events"]))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["start"] is None and kwargs["end"] is None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(start="2026-07-01"))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["start"] == "2026-07-01" and kwargs["end"] is None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(end="2026-07-31"))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["start"] is None and kwargs["end"] == "2026-07-31"


def test_list_events_cleanup_filters_do_not_disable_the_default_window(tools, fake_service):
    """A cleanup sweep narrows the default window rather than escaping it.

    The `ohne_*`/`uid_regex` filters run client-side on whatever the query
    returned, so a bare sweep only ever sees today ±90 days - phone-created
    events older than that need `start`/`end` (or a calendar name) as well.
    Pinned because "find every hand-made event" reads like it should scan
    everything, and it does not.
    """
    from datetime import date, datetime, timedelta

    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(without_reminder=True, uid_regex="^[A-F0-9-]+$"))
    _, kwargs = fake_service.list_events.call_args
    today = datetime.now(mapping.get_default_timezone()).date()
    assert date.fromisoformat(kwargs["start"]) == today - timedelta(days=90)
    assert date.fromisoformat(kwargs["end"]) == today + timedelta(days=90)
    assert kwargs["without_reminder"] is True and kwargs["uid_regex"] == "^[A-F0-9-]+$"


def test_list_events_default_window_lets_unscoped_expansion_work(tools, fake_service):
    """Unscoped expansion gets the default bounds; a named calendar still needs its own.

    `expand=True` without both bounds is refused one layer down
    (`_collect_events`), so the default window is what makes a bare
    `expand_recurrences=True` call viable at all. Naming a calendar is a
    scoping decision and turns the default off, so that call still arrives
    unbounded and is refused as before - pinned here so the asymmetry is a
    choice rather than a surprise.
    """
    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(expand_recurrences=True))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["expand"] is True
    assert kwargs["start"] is not None and kwargs["end"] is not None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(calendar_names=["Events"], expand_recurrences=True))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["start"] is None and kwargs["end"] is None


def _sample_task() -> dict:
    return {
        "uid": "t1",
        "title": "Task",
        "start_date": None,
        "due_date": "2026-07-20",
        "priority": None,
        "progress_percent": 0,
        "status": "open",
        "location": None,
        "url": None,
        "tags": [],
        "reminders": [],
        "notes": "n" * 300,
        "parent_uid": None,
        "recurrence": None,
        "exception_dates": [],
        "recurrence_id": None,
        "series_uid": None,
        "list": "Personal",
        "list_url": "https://cloud.example.com/remote.php/dav/calendars/demo/personal/",
    }


def test_list_tasks_compact_drops_empty_fields_and_list_url(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(tools["list_tasks"].fn(list_names=["Personal"], compact=True))
    assert set(task) == {
        "uid",
        "title",
        "due_date",
        "progress_percent",
        "status",
        "notes",
        "list",
    }
    assert task["progress_percent"] == 0  # 0 is a value, not "empty"
    assert task["notes"].startswith("n" * 200 + "…")
    assert "truncated start 300 Characters" in task["notes"]
    assert "get_task" in task["notes"]


def test_list_tasks_fields_whitelist_filters_keys_and_validates(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(
        tools["list_tasks"].fn(list_names=["Personal"], fields=["uid", "title", "due_date"])
    )
    assert task == {"uid": "t1", "title": "Task", "due_date": "2026-07-20"}

    with pytest.raises(ToolError, match="Unknown fields-Entries: description"):
        _run(tools["list_tasks"].fn(list_names=["Personal"], fields=["description"]))


def test_list_tasks_compact_keeps_list_url_when_whitelisted(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(
        tools["list_tasks"].fn(list_names=["Personal"], fields=["uid", "list_url"], compact=True)
    )
    assert task == {"uid": "t1", "list_url": _sample_task()["list_url"]}


# --- Attendees (attendees) ---


def test_create_event_passes_attendees_through(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    attendees = [{"email": "a@example.com", "role": "optional"}]
    _run(
        tools["create_event"].fn(
            calendar_name="Events",
            title="Meeting",
            start="2026-07-20T14:00:00",
            attendees=attendees,
        )
    )
    (_, fields), _ = fake_service.create_event.call_args
    assert fields.attendees == attendees


def test_create_event_attendees_defaults_to_none(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    _run(
        tools["create_event"].fn(
            calendar_name="Events", title="Meeting", start="2026-07-20T14:00:00"
        )
    )
    (_, fields), _ = fake_service.create_event.call_args
    assert fields.attendees is None


def test_update_event_passes_attendees_through(tools, fake_service):
    attendees = [{"email": "b@example.com"}]
    _run(tools["update_event"].fn(calendar_name="Events", event_uid="event-1", attendees=attendees))
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.attendees == attendees


def test_update_event_can_clear_attendees(tools, fake_service):
    _run(
        tools["update_event"].fn(
            calendar_name="Events", event_uid="event-1", clear_fields=["attendees"]
        )
    )
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.clear == ("attendees",)


def test_update_events_delegates(tools, fake_service):
    expected_res = {
        "calendar_name": "Events",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "results": [{"uid": "u1", "status": "ok"}, {"uid": "u2", "status": "ok"}],
    }
    fake_service.update_events.return_value = expected_res
    res = _run(
        tools["update_events"].fn(
            calendar_name="Events",
            event_uids=["u1", "u2"],
            location="Büro",
            clear_fields=["description"],
        )
    )
    assert res == expected_res
    (cal_name, uids, fields), _ = fake_service.update_events.call_args
    assert cal_name == "Events"
    assert uids == ["u1", "u2"]
    assert fields.location == "Büro"
    assert fields.clear == ("description",)


def test_update_exdates_delegates(tools, fake_service):
    fake_service.change_exdates.return_value = {
        "calendar_name": "Events",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "results": [
            {"uid": "u1", "status": "ok", "added": 1, "removed": 0, "total": 7, "skipped": []},
            {
                "uid": "u2",
                "status": "ok",
                "added": 0,
                "removed": 0,
                "total": 3,
                "skipped": [{"value": "2026-07-27", "reason": "no occurrence"}],
            },
        ],
    }

    res = _run(
        tools["update_exdates"].fn(
            calendar_name="Events",
            event_uids=["u1", "u2"],
            add=["2026-07-27"],
        )
    )

    # The tool's surface is English end to end, unlike the German batch shape
    # `_batch_over_events` returns underneath it.
    assert res["calendar_name"] == "Events"
    assert (res["succeeded"], res["failed"]) == (2, 0)
    assert res["results"][0] == {
        "uid": "u1",
        "status": "ok",
        "added": 1,
        "removed": 0,
        "total": 7,
        "skipped": [],
    }
    assert res["results"][1]["skipped"] == [{"value": "2026-07-27", "reason": "no occurrence"}]
    args, _ = fake_service.change_exdates.call_args
    assert args == ("Events", ["u1", "u2"], ["2026-07-27"], None, True)


def test_update_exdates_renames_failed_entries(tools, fake_service):
    fake_service.change_exdates.return_value = {
        "calendar_name": "Events",
        "erfolgreich": 0,
        "fehlgeschlagen": 1,
        "results": [{"uid": "u1", "status": "error", "error": "Event 'u1' was not found."}],
    }

    res = _run(
        tools["update_exdates"].fn(calendar_name="Events", event_uids=["u1"], remove=["2026-07-27"])
    )

    assert res["results"] == [
        {"uid": "u1", "status": "error", "error": "Event 'u1' was not found."}
    ]


def test_delete_events_delegates(tools, fake_service):
    expected_res = {
        "calendar_name": "Events",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "results": [{"uid": "u1", "status": "ok"}, {"uid": "u2", "status": "ok"}],
    }
    fake_service.delete_events.return_value = expected_res
    res = _run(tools["delete_events"].fn(calendar_name="Events", event_uids=["u1", "u2"]))
    assert res == expected_res
    fake_service.delete_events.assert_called_once_with("Events", ["u1", "u2"])


# --- respond_to_event ---


def test_respond_to_event_delegates(tools, fake_service):
    result = _run(
        tools["respond_to_event"].fn(
            calendar_name="Events",
            event_uid="event-1",
            response="accepted",
            comment="Bin dabei",
        )
    )
    fake_service.respond_to_event.assert_called_once_with(
        "Events", "event-1", "accepted", "Bin dabei"
    )
    assert result == {"uid": "event-1", "response": "accepted"}


def test_respond_to_event_comment_defaults_to_none(tools, fake_service):
    _run(
        tools["respond_to_event"].fn(
            calendar_name="Events", event_uid="event-1", response="cancelled"
        )
    )
    fake_service.respond_to_event.assert_called_once_with("Events", "event-1", "cancelled", None)


def test_respond_to_event_not_an_attendee_becomes_clean_tool_error(tools, fake_service):
    from nextcloud_task_mcp.errors import InvalidEventDataError

    fake_service.respond_to_event.side_effect = InvalidEventDataError(
        "You are not listed as an attendee of this event, so there is nothing to respond to."
    )
    with pytest.raises(ToolError, match="not listed as an attendee"):
        _run(
            tools["respond_to_event"].fn(
                calendar_name="Events", event_uid="event-1", response="accepted"
            )
        )


# --- get_free_busy ---


def test_get_free_busy_delegates_own_availability(tools, fake_service):
    fake_service.get_free_busy.return_value = {
        "start": "2026-07-20T00:00:00+00:00",
        "end": "2026-07-21T00:00:00+00:00",
        "user": None,
        "belegt": [],
    }
    result = _run(tools["get_free_busy"].fn(start="2026-07-20", end="2026-07-21"))
    fake_service.get_free_busy.assert_called_once_with("2026-07-20", "2026-07-21", None)
    assert result["belegt"] == []


def test_get_free_busy_passes_user_through(tools, fake_service):
    fake_service.get_free_busy.return_value = {
        "start": "2026-07-20T00:00:00+00:00",
        "end": "2026-07-21T00:00:00+00:00",
        "user": "bob@example.com",
        "belegt": [],
    }
    _run(tools["get_free_busy"].fn(start="2026-07-20", end="2026-07-21", user="bob@example.com"))
    fake_service.get_free_busy.assert_called_once_with(
        "2026-07-20", "2026-07-21", "bob@example.com"
    )


# --- share_calendar / unshare_calendar / list_calendar_shares ---


def test_share_calendar_delegates(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "calendar_name": "Private",
        "recipient": "bob",
        "write_access": True,
    }
    result = _run(
        tools["share_calendar"].fn(calendar_name="Private", recipient="bob", write_access=True)
    )
    fake_service.share_calendar.assert_called_once_with("Private", "bob", False, True)
    assert result == {"calendar_name": "Private", "recipient": "bob", "write_access": True}


def test_share_calendar_defaults_group_and_write_access_false(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "calendar_name": "Private",
        "recipient": "team",
        "write_access": False,
    }
    _run(tools["share_calendar"].fn(calendar_name="Private", recipient="team"))
    fake_service.share_calendar.assert_called_once_with("Private", "team", False, False)


def test_share_calendar_passes_group_through(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "calendar_name": "Private",
        "recipient": "team",
        "write_access": False,
    }
    _run(tools["share_calendar"].fn(calendar_name="Private", recipient="team", group=True))
    fake_service.share_calendar.assert_called_once_with("Private", "team", True, False)


def test_unshare_calendar_delegates(tools, fake_service):
    result = _run(
        tools["unshare_calendar"].fn(calendar_name="Private", recipient="bob", group=False)
    )
    fake_service.unshare_calendar.assert_called_once_with("Private", "bob", False)
    assert result == {"calendar_name": "Private", "recipient": "bob"}


def test_list_calendar_shares_delegates(tools, fake_service):
    fake_service.list_calendar_shares.return_value = [
        {"recipient": "bob", "type": "user", "write_access": True, "status": "akzeptiert"}
    ]
    result = _run(tools["list_calendar_shares"].fn(calendar_name="Private"))
    fake_service.list_calendar_shares.assert_called_once_with("Private")
    assert result == [
        {"recipient": "bob", "type": "user", "write_access": True, "status": "akzeptiert"}
    ]


# --- list_trash / restore_from_trash ---


def test_list_trash_delegates(tools, fake_service):
    fake_service.list_trash.return_value = [
        {
            "id": "42.ics",
            "title": "Shopping",
            "type": "task",
            "calendar": "personal",
            "deleted_at": "2026-07-10T12:00:00+00:00",
        }
    ]
    result = _run(tools["list_trash"].fn())
    fake_service.list_trash.assert_called_once_with()
    assert result[0]["id"] == "42.ics"


def test_restore_from_trash_delegates(tools, fake_service):
    result = _run(tools["restore_from_trash"].fn(id="42.ics"))
    fake_service.restore_from_trash.assert_called_once_with("42.ics")
    assert result == {"id": "42.ics"}


# --- export_calendar / import_ics ---


def test_export_calendar_delegates(tools, fake_service):
    fake_service.export_calendar.return_value = {
        "calendar_name": "Private",
        "ics": "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
    }
    result = _run(tools["export_calendar"].fn(calendar_name="Private"))
    fake_service.export_calendar.assert_called_once_with("Private")
    assert result["ics"].startswith("BEGIN:VCALENDAR")


def test_import_ics_delegates(tools, fake_service):
    fake_service.import_ics.return_value = {
        "calendar_name": "Private",
        "importiert": 2,
        "uebersprungen": 1,
    }
    ics_text = "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    result = _run(tools["import_ics"].fn(calendar_name="Private", ics=ics_text))
    fake_service.import_ics.assert_called_once_with("Private", ics_text)
    assert result == {"calendar_name": "Private", "importiert": 2, "uebersprungen": 1}


def test_link_task_to_event_defaults_to_time_block(tools, fake_service):
    result = _run(
        tools["link_task_to_event"].fn(
            list_name="Private",
            task_uid="task-1",
            calendar_name="Events",
            event_uid="event-1",
        )
    )
    fake_service.link_task_to_event.assert_called_once_with(
        "Private", "task-1", "Events", "event-1", "time_block"
    )
    assert result == {"task_uid": "task-1", "event_uid": "event-1", "relation": "time_block"}


def test_list_events_for_task_delegates(tools, fake_service):
    fake_service.list_events_for_task.return_value = [{"uid": "event-1", "calendar_name": "Events"}]
    result = _run(tools["list_events_for_task"].fn(list_name="Private", task_uid="task-1"))
    fake_service.list_events_for_task.assert_called_once_with(
        "Private", "task-1", calendar_names=None
    )
    assert result == [{"uid": "event-1", "calendar_name": "Events"}]


def test_list_events_for_task_passes_calendar_names_through(tools, fake_service):
    fake_service.list_events_for_task.return_value = []
    _run(
        tools["list_events_for_task"].fn(
            list_name="Private", task_uid="task-1", calendar_names=["Events"]
        )
    )
    fake_service.list_events_for_task.assert_called_once_with(
        "Private", "task-1", calendar_names=["Events"]
    )


def test_create_event_from_task_delegates(tools, fake_service):
    fake_service.create_event_from_task.return_value = "event-uid"
    result = _run(
        tools["create_event_from_task"].fn(
            list_name="Private",
            task_uid="task-1",
            calendar_name="Events",
            duration_minutes=30,
        )
    )
    fake_service.create_event_from_task.assert_called_once_with(
        "Private", "task-1", "Events", None, 30, None, None, None, None
    )
    assert result == {"uid": "event-uid", "task_uid": "task-1"}


def test_create_event_from_task_passes_new_fields(tools, fake_service):
    fake_service.create_event_from_task.return_value = "event-uid"
    _run(
        tools["create_event_from_task"].fn(
            list_name="Private",
            task_uid="task-1",
            calendar_name="Events",
            start="2026-07-20T14:00:00",
            end="2026-07-20T16:00:00",
            description="",
            reminders=["-PT30M"],
            visibility="private",
        )
    )
    fake_service.create_event_from_task.assert_called_once_with(
        "Private",
        "task-1",
        "Events",
        "2026-07-20T14:00:00",
        None,
        "2026-07-20T16:00:00",
        "",
        ["-PT30M"],
        "private",
    )


def test_get_agenda_delegates(tools, fake_service):
    fake_service.get_agenda.return_value = {"date": "2026-07-20", "events": [], "tasks": []}
    result = _run(tools["get_agenda"].fn(date="2026-07-20"))
    fake_service.get_agenda.assert_called_once_with(
        "2026-07-20", calendar_names=None, list_names=None
    )
    assert result["date"] == "2026-07-20"


def test_list_tags_delegates(tools, fake_service):
    fake_service.list_tags.return_value = [{"tag": "Work", "count": 3}]
    result = _run(tools["list_tags"].fn(calendar_names=["Cal1"], list_names=["List1"]))
    fake_service.list_tags.assert_called_once_with(calendar_names=["Cal1"], list_names=["List1"])
    assert result == [{"tag": "Work", "count": 3}]


def test_calendar_not_found_becomes_clean_tool_error(tools, fake_service):
    fake_service.get_event.side_effect = CalendarNotFoundError("Calendar 'X' was not found.")
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["get_event"].fn(calendar_name="X", event_uid="e1"))


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

    def blocking_list_tasks(
        list_names=None,
        only_open=True,
        due_before=None,
        due_after=None,
        limit=None,
        *,
        priority=None,
        tag=None,
        search_text=None,
        without_reminder=False,
        without_visibility=False,
        without_tags=False,
        uid_regex=None,
    ):
        # Spelled out rather than (*args, **kwargs) on purpose: this is the
        # one place a test would notice the tool and the service drifting
        # apart on how list_tasks is called.
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


def _allowed_redirect_domains(mcp) -> list[str]:
    # `mcp.auth` is typed as fastmcp's generic `AuthProvider | None`, which
    # doesn't know about `allowed_redirect_domains` (specific to the vendored
    # PersonalAuthProvider) - narrow it for both mypy and as a runtime check
    # that build_server actually wired up our auth provider.
    assert isinstance(mcp.auth, PersonalAuthProvider)
    return mcp.auth.allowed_redirect_domains


def test_build_server_drops_localhost_when_public_base_url_is_public(settings, fake_service):
    # The `settings` fixture already uses a non-local public_base_url and leaves
    # oauth_allowed_redirect_domains unset (None).
    assert settings.oauth_allowed_redirect_domains is None
    mcp = build_server(settings, service=fake_service)
    assert _allowed_redirect_domains(mcp) == ["claude.ai", "claude.com"]
    assert "localhost" not in _allowed_redirect_domains(mcp)


def test_build_server_keeps_vendored_default_when_public_base_url_is_local(fake_service, tmp_path):
    local_settings = Settings(
        caldav_url="https://cloud.example.com/remote.php/dav/",
        caldav_username="testuser",
        caldav_password="testpass",
        notes_base_url="https://cloud.example.com",
        public_base_url="http://127.0.0.1:8000",
        oauth_password=None,
        oauth_state_dir=str(tmp_path / "oauth-state"),
        oauth_allowed_redirect_domains=None,
        oauth_access_token_expiry_seconds=30 * 24 * 60 * 60,
        host="127.0.0.1",
        port=8000,
    )
    mcp = build_server(local_settings, service=fake_service)
    assert _allowed_redirect_domains(mcp) == ["claude.ai", "claude.com", "localhost"]


def test_build_server_respects_explicitly_configured_redirect_domains(settings, fake_service):
    public_settings = replace(
        settings,
        public_base_url="https://public.example.com",
        oauth_allowed_redirect_domains=["only-this.example.com"],
    )
    mcp = build_server(public_settings, service=fake_service)
    assert _allowed_redirect_domains(mcp) == ["only-this.example.com"]


# --- Token expiry settings wired through to PersonalAuthProvider (D5) ---


def test_build_server_wires_refresh_token_expiry_seconds_through(settings, fake_service):
    public_settings = replace(settings, oauth_refresh_token_expiry_seconds=1234)
    mcp = build_server(public_settings, service=fake_service)
    assert isinstance(mcp.auth, PersonalAuthProvider)
    assert mcp.auth.refresh_token_expiry_seconds == 1234


def test_build_server_wires_access_token_expiry_seconds_through(settings, fake_service):
    public_settings = replace(settings, oauth_access_token_expiry_seconds=5678)
    mcp = build_server(public_settings, service=fake_service)
    assert isinstance(mcp.auth, PersonalAuthProvider)
    assert mcp.auth.access_token_expiry_seconds == 5678


# --- Default timezone wired through to the mapping layer ---
#
# `build_server` is the only place that connects the configured zone to the
# mapping modules, and every other test runs with the shipped default - so
# without this test, deleting that one line would leave the suite green while
# MCP_DEFAULT_TIMEZONE silently stopped working in production.


def test_build_server_wires_default_timezone_through(settings, fake_service):
    public_settings = replace(settings, default_timezone="America/New_York")
    build_server(public_settings, service=fake_service)
    assert mapping.get_default_timezone() == ZoneInfo("America/New_York")


def test_build_server_applies_shipped_default_timezone(settings, fake_service):
    mapping.set_default_timezone("UTC")
    build_server(settings, service=fake_service)
    assert mapping.get_default_timezone() == ZoneInfo(Settings.default_timezone)


# --- main(): access-log-disabled security control must not silently regress (E7) ---
#
# Uvicorn's default access log records the full request path including the
# query string, which is where PersonalAuthProvider's /authorize gate reads
# MCP_OAUTH_PASSWORD from (see the comment in `main()`) - so `access_log`
# staying disabled is a load-bearing security control, not a style choice.
# This test guards it against a silent regression by a future refactor.


def test_main_disables_uvicorn_access_log_and_passes_host_port(settings):
    with (
        patch("nextcloud_task_mcp.server.Settings.from_env", return_value=settings) as from_env,
        patch("nextcloud_task_mcp.server.FastMCP.run") as fastmcp_run,
    ):
        main()

    from_env.assert_called_once()
    fastmcp_run.assert_called_once_with(
        transport="http",
        host=settings.host,
        port=settings.port,
        uvicorn_config={"access_log": False},
    )


# ---------------------------------------------------------------------------
# Tool annotations
#
# Every tool must carry MCP `annotations`. Without them a client has no way to
# tell a plain listing from a destructive write, so it has to gate *every* call
# on a human clicking "Allow" - and when that prompt is never answered the call
# dies client-side with "No approval received", which the server never sees.
# ---------------------------------------------------------------------------

#: Tools that only ever read. These must advertise readOnlyHint=True so clients
#: can run them without an approval round-trip.
READ_ONLY_TOOLS = {
    "list_task_lists",
    "list_tasks",
    "get_task",
    "list_calendars",
    "list_events",
    "get_event",
    "list_events_for_task",
    "get_agenda",
    "list_tags",
    "get_free_busy",
    "list_calendar_shares",
    "list_trash",
    "export_calendar",
    "list_notes",
    "get_note",
    "search_notes",
}

#: Tools that modify Nextcloud state in a way that is not trivially undone.
DESTRUCTIVE_TOOLS = {
    "delete_task_list",
    "rename_task_list",
    "update_task",
    "complete_task",
    "delete_task",
    "move_task",
    "delete_calendar",
    "update_calendar",
    "update_event",
    "delete_event",
    "update_events",
    "update_exdates",
    "delete_events",
    "move_event",
    "respond_to_event",
    "unshare_calendar",
    "update_note",
    "replace_in_note",
    "update_note_section",
    "delete_note",
}


def test_every_tool_carries_annotations(tools):
    missing = sorted(name for name, tool in tools.items() if tool.annotations is None)
    assert missing == [], f"tools without MCP annotations: {missing}"


def test_read_only_tools_are_marked_read_only(tools):
    wrong = sorted(
        name for name in READ_ONLY_TOOLS if tools[name].annotations.readOnlyHint is not True
    )
    assert wrong == [], f"read-only tools missing readOnlyHint=True: {wrong}"


def test_writing_tools_are_not_marked_read_only(tools):
    writers = set(tools) - READ_ONLY_TOOLS
    wrong = sorted(name for name in writers if tools[name].annotations.readOnlyHint is not False)
    assert wrong == [], f"writing tools not marked readOnlyHint=False: {wrong}"


def test_destructive_tools_are_marked_destructive(tools):
    wrong = sorted(
        name for name in DESTRUCTIVE_TOOLS if tools[name].annotations.destructiveHint is not True
    )
    assert wrong == [], f"destructive tools missing destructiveHint=True: {wrong}"


def test_additive_writers_are_not_marked_destructive(tools):
    additive = set(tools) - READ_ONLY_TOOLS - DESTRUCTIVE_TOOLS
    wrong = sorted(
        name for name in additive if tools[name].annotations.destructiveHint is not False
    )
    assert wrong == [], f"additive tools wrongly marked destructive: {wrong}"


def test_all_tools_are_open_world(tools):
    # Every tool ultimately talks to a remote Nextcloud instance.
    wrong = sorted(
        name for name, tool in tools.items() if tool.annotations.openWorldHint is not True
    )
    assert wrong == [], f"tools missing openWorldHint=True: {wrong}"


def test_annotations_survive_the_mcp_wire_format(tools):
    # FastMCP only forwards annotations to clients via to_mcp_tool(); a tool
    # object carrying them is not enough.
    mcp_tool = tools["list_events"].to_mcp_tool(name="list_events")
    assert mcp_tool.annotations is not None
    assert mcp_tool.annotations.readOnlyHint is True
