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
    NotizNotFoundError,
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
        "list_notizen",
        "get_notiz",
        "create_notiz",
        "update_notiz",
        "replace_in_notiz",
        "update_notiz_abschnitt",
        "append_notiz",
        "search_notizen",
        "delete_notiz",
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
    assert "faellig_datum" in schema["properties"]
    assert "prioritaet" in schema["properties"]
    assert "uebergeordnete_aufgabe" in schema["properties"]
    assert schema["required"] == ["list_name", "titel"]


def test_update_task_has_felder_leeren_parameter(tools):
    schema = tools["update_task"].parameters
    assert "felder_leeren" in schema["properties"]


def test_get_task_delegates_to_service(tools, fake_service):
    fake_service.get_task.return_value = {"uid": "abc", "titel": "Milch kaufen"}
    result = _run(tools["get_task"].fn("Personal", "abc"))
    assert result == {"uid": "abc", "titel": "Milch kaufen"}
    fake_service.get_task.assert_called_once_with("Personal", "abc")


# --- Notes tools ---


def test_list_notizen_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.list_notes.return_value = [{"id": 1, "titel": "Projekt X"}]
    result = _run(tools["list_notizen"].fn("Arbeit"))
    assert result == [{"id": 1, "titel": "Projekt X"}]
    fake_notes_service.list_notes.assert_called_once_with("Arbeit")


def test_get_notiz_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.get_note.return_value = {"id": 1, "titel": "Projekt X", "inhalt": "..."}
    result = _run(tools["get_notiz"].fn(1))
    assert result == {"id": 1, "titel": "Projekt X", "inhalt": "..."}
    fake_notes_service.get_note.assert_called_once_with(1)


def test_create_notiz_builds_note_fields(tools, fake_notes_service):
    fake_notes_service.create_note.return_value = {"id": 2}
    result = _run(tools["create_notiz"].fn("Projekt X", "Arbeit", "Erste Notiz", True))
    assert result == {"id": 2}
    fields = fake_notes_service.create_note.call_args[0][0]
    assert fields.titel == "Projekt X"
    assert fields.kategorie == "Arbeit"
    assert fields.inhalt == "Erste Notiz"
    assert fields.favorit is True


def test_update_notiz_only_sets_given_fields(tools, fake_notes_service):
    fake_notes_service.update_note.return_value = {"id": 2}
    result = _run(tools["update_notiz"].fn(2, titel="Neuer Titel"))
    assert result == {"id": 2}
    notiz_id, fields = fake_notes_service.update_note.call_args[0]
    assert notiz_id == 2
    assert fields.titel == "Neuer Titel"
    assert fields.kategorie is None
    assert fields.inhalt is None
    assert fields.favorit is None


def test_replace_in_notiz_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.replace_in_note.return_value = {"id": 2, "inhalt": "Neu"}
    result = _run(tools["replace_in_notiz"].fn(2, "Alt", "Neu"))
    assert result == {"id": 2, "inhalt": "Neu"}
    fake_notes_service.replace_in_note.assert_called_once_with(2, "Alt", "Neu")


def test_update_notiz_abschnitt_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.replace_note_section.return_value = {"id": 2, "inhalt": "## A\n\nNeu"}
    result = _run(tools["update_notiz_abschnitt"].fn(2, "## A", "## A\n\nNeu"))
    assert result == {"id": 2, "inhalt": "## A\n\nNeu"}
    fake_notes_service.replace_note_section.assert_called_once_with(2, "## A", "## A\n\nNeu")


def test_append_notiz_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.append_note.return_value = {"id": 2, "inhalt": "Alt\n\nNeu"}
    result = _run(tools["append_notiz"].fn(2, "Neu"))
    assert result == {"id": 2, "inhalt": "Alt\n\nNeu"}
    fake_notes_service.append_note.assert_called_once_with(2, "Neu")


def test_search_notizen_delegates_to_notes_service(tools, fake_notes_service):
    fake_notes_service.search_notes.return_value = [{"id": 1, "titel": "Projekt X"}]
    result = _run(tools["search_notizen"].fn("Projekt", "Arbeit"))
    assert result == [{"id": 1, "titel": "Projekt X"}]
    fake_notes_service.search_notes.assert_called_once_with("Projekt", "Arbeit")


def test_delete_notiz_delegates_to_notes_service(tools, fake_notes_service):
    result = _run(tools["delete_notiz"].fn(2))
    assert result == {"id": 2}
    fake_notes_service.delete_note.assert_called_once_with(2)


def test_notiz_tools_use_ascii_parameter_names(tools):
    for tool_name in (
        "list_notizen",
        "get_notiz",
        "create_notiz",
        "update_notiz",
        "replace_in_notiz",
        "update_notiz_abschnitt",
        "append_notiz",
        "search_notizen",
        "delete_notiz",
    ):
        schema = tools[tool_name].parameters
        for prop_name in schema.get("properties", {}):
            assert prop_name.isascii(), f"{tool_name}.{prop_name} is not ASCII"


def test_create_notiz_requires_only_titel(tools):
    schema = tools["create_notiz"].parameters
    assert schema["required"] == ["titel"]


def test_get_notiz_requires_notiz_id(tools):
    schema = tools["get_notiz"].parameters
    assert schema["required"] == ["notiz_id"]


def test_delete_notiz_requires_notiz_id(tools):
    schema = tools["delete_notiz"].parameters
    assert schema["required"] == ["notiz_id"]


def test_delete_notiz_not_found_becomes_clean_tool_error(tools, fake_notes_service):
    fake_notes_service.delete_note.side_effect = NotizNotFoundError(
        "The requested note was not found."
    )
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["delete_notiz"].fn(999))


def test_notiz_not_found_becomes_clean_tool_error(tools, fake_notes_service):
    fake_notes_service.get_note.side_effect = NotizNotFoundError(
        "The requested note was not found."
    )
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["get_notiz"].fn(999))


def test_notiz_unexpected_exception_becomes_generic_tool_error(tools, fake_notes_service):
    fake_notes_service.get_note.side_effect = RuntimeError("boom")
    with pytest.raises(ToolError, match="unexpected internal error"):
        _run(tools["get_notiz"].fn(1))


def test_get_task_returns_wiederholung_field(tools, fake_service):
    fake_service.get_task.return_value = {"uid": "abc", "wiederholung": "FREQ=WEEKLY"}
    result = _run(tools["get_task"].fn("Personal", "abc"))
    assert result["wiederholung"] == "FREQ=WEEKLY"


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


def test_list_tasks_passes_nur_offene_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(tools["list_tasks"].fn("Personal", nur_offene=False))
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=False,
        due_before=None,
        due_after=None,
        prioritaet=None,
        tag=None,
        suchtext=None,
        ohne_erinnerung=False,
        ohne_sichtbarkeit=False,
        ohne_tags=False,
        uid_regex=None,
        limit=None,
    )


def test_list_tasks_passes_filter_params_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(
        tools["list_tasks"].fn(
            listen_namen=["Personal"],
            faellig_vor="2026-08-01",
            faellig_nach="2026-07-01",
            prioritaet="hoch",
            tag="arbeit",
            suchtext="test",
            limit=5,
        )
    )
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=True,
        due_before="2026-08-01",
        due_after="2026-07-01",
        prioritaet="hoch",
        tag="arbeit",
        suchtext="test",
        ohne_erinnerung=False,
        ohne_sichtbarkeit=False,
        ohne_tags=False,
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
        prioritaet=None,
        tag=None,
        suchtext=None,
        ohne_erinnerung=False,
        ohne_sichtbarkeit=False,
        ohne_tags=False,
        uid_regex=None,
        limit=None,
    )


def test_list_tasks_passes_cleanup_filters_through(tools, fake_service):
    fake_service.list_tasks.return_value = []
    _run(
        tools["list_tasks"].fn(
            listen_namen=["Personal"],
            ohne_erinnerung=True,
            ohne_sichtbarkeit=True,
            ohne_tags=True,
            uid_regex="^[A-F0-9-]+$",
        )
    )
    fake_service.list_tasks.assert_called_once_with(
        list_names=["Personal"],
        only_open=True,
        due_before=None,
        due_after=None,
        prioritaet=None,
        tag=None,
        suchtext=None,
        ohne_erinnerung=True,
        ohne_sichtbarkeit=True,
        ohne_tags=True,
        uid_regex="^[A-F0-9-]+$",
        limit=None,
    )


def test_list_tasks_both_list_name_and_listen_namen_raises_error(tools, fake_service):
    with pytest.raises(ToolError, match="list_name is the deprecated alias of listen_namen"):
        _run(tools["list_tasks"].fn(list_name="Personal", listen_namen=["Arbeit"]))
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

    assert positional == ["listen_namen", "nur_offene", "faellig_vor", "faellig_nach", "limit"]
    assert all(
        params[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in (
            "prioritaet",
            "tag",
            "suchtext",
            "ohne_erinnerung",
            "ohne_sichtbarkeit",
            "ohne_tags",
            "uid_regex",
            "list_name",
        )
    )


def test_list_tasks_tool_still_exposes_every_filter_to_clients(tools):
    """Keyword-only parameters must still reach the MCP schema clients read."""
    properties = tools["list_tasks"].parameters["properties"]

    assert {"listen_namen", "nur_offene", "faellig_vor", "faellig_nach", "limit"} <= set(properties)
    assert {"prioritaet", "tag", "suchtext", "list_name"} <= set(properties)
    assert {"ohne_erinnerung", "ohne_sichtbarkeit", "ohne_tags", "uid_regex"} <= set(properties)


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
            titel="Neue Aufgabe",
            faellig_datum="2026-07-20",
            prioritaet="hoch",
            uebergeordnete_aufgabe="parent-uid",
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


def test_create_task_passes_wiederholung(tools, fake_service):
    fake_service.create_task.return_value = "new-uid"
    _run(
        tools["create_task"].fn(
            list_name="Personal",
            titel="Muell rausbringen",
            faellig_datum="2026-07-20",
            wiederholung="FREQ=WEEKLY;BYDAY=MO",
        )
    )
    args, _ = fake_service.create_task.call_args
    _, fields = args
    assert fields.wiederholung == "FREQ=WEEKLY;BYDAY=MO"


def test_update_task_passes_wiederholung(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", wiederholung="FREQ=DAILY"))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.wiederholung == "FREQ=DAILY"


def test_update_task_passes_status(tools, fake_service):
    """The reopen path has to survive the tool layer, not just the mapping."""
    _run(tools["update_task"].fn("Personal", "task-uid", status="offen"))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.status == "offen"


def test_update_task_passes_felder_leeren_wiederholung_as_clear(tools, fake_service):
    _run(tools["update_task"].fn("Personal", "task-uid", felder_leeren=["wiederholung"]))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ("wiederholung",)


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
    _run(tools["update_task"].fn("Personal", "task-uid", felder_leeren=["faellig_datum", "ort"]))
    args, _ = fake_service.update_task.call_args
    _, _, fields = args
    assert fields.clear == ("faellig_datum", "ort")


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


def test_move_task_delegates(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
    }
    result = _run(
        tools["move_task"].fn(list_name="Privat", task_uid="task-uid", ziel_liste="Arbeit")
    )
    assert result == {
        "uid": "task-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
    }
    fake_service.move_task.assert_called_once_with("Privat", "task-uid", "Arbeit", None, ())


def test_move_task_passes_new_parent_through(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
        "hierarchie": "gesetzt",
    }
    result = _run(
        tools["move_task"].fn(
            list_name="Privat",
            task_uid="task-uid",
            ziel_liste="Arbeit",
            uebergeordnete_aufgabe="parent-uid",
        )
    )
    assert result["hierarchie"] == "gesetzt"
    fake_service.move_task.assert_called_once_with("Privat", "task-uid", "Arbeit", "parent-uid", ())


def test_move_task_passes_felder_leeren_through_as_tuple(tools, fake_service):
    fake_service.move_task.return_value = {
        "uid": "task-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
        "hierarchie": "geleert",
    }
    _run(
        tools["move_task"].fn(
            list_name="Privat",
            task_uid="task-uid",
            ziel_liste="Arbeit",
            felder_leeren=["uebergeordnete_aufgabe"],
        )
    )
    fake_service.move_task.assert_called_once_with(
        "Privat", "task-uid", "Arbeit", None, ("uebergeordnete_aufgabe",)
    )


def test_move_event_delegates(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "kopiert",
    }
    result = _run(
        tools["move_event"].fn(
            kalender_name="Privat", event_uid="event-uid", ziel_kalender="Arbeit"
        )
    )
    assert result == {
        "uid": "event-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "kopiert",
    }
    fake_service.move_event.assert_called_once_with("Privat", "event-uid", "Arbeit", None, ())


def test_move_event_passes_linked_task_through(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
        "hierarchie": "gesetzt",
    }
    result = _run(
        tools["move_event"].fn(
            kalender_name="Privat",
            event_uid="event-uid",
            ziel_kalender="Arbeit",
            verknuepfte_aufgabe="task-uid",
        )
    )
    assert result["hierarchie"] == "gesetzt"
    fake_service.move_event.assert_called_once_with("Privat", "event-uid", "Arbeit", "task-uid", ())


def test_move_event_passes_felder_leeren_through_as_tuple(tools, fake_service):
    fake_service.move_event.return_value = {
        "uid": "event-uid",
        "von": "Privat",
        "nach": "Arbeit",
        "methode": "MOVE",
        "hierarchie": "geleert",
    }
    _run(
        tools["move_event"].fn(
            kalender_name="Privat",
            event_uid="event-uid",
            ziel_kalender="Arbeit",
            felder_leeren=["verknuepfte_aufgabe"],
        )
    )
    fake_service.move_event.assert_called_once_with(
        "Privat", "event-uid", "Arbeit", None, ("verknuepfte_aufgabe",)
    )


# --- Calendar/event tools ---


def test_create_event_schema(tools):
    schema = tools["create_event"].parameters
    assert set(schema["required"]) == {"kalender_name", "titel", "start"}
    assert "wiederholung" in schema["properties"]
    assert "ausnahme_daten" in schema["properties"]
    assert "erinnerungen" in schema["properties"]
    assert "verknuepfte_aufgabe" in schema["properties"]
    assert "teilnehmer" in schema["properties"]


def test_respond_to_event_schema(tools):
    schema = tools["respond_to_event"].parameters
    assert set(schema["required"]) == {"kalender_name", "event_uid", "antwort"}
    assert "kommentar" in schema["properties"]


def test_get_free_busy_schema(tools):
    schema = tools["get_free_busy"].parameters
    assert set(schema["required"]) == {"von", "bis"}
    assert "benutzer" in schema["properties"]


def test_update_event_has_felder_leeren_parameter(tools):
    schema = tools["update_event"].parameters
    assert "felder_leeren" in schema["properties"]
    assert set(schema["required"]) == {"kalender_name", "event_uid"}


def test_list_events_schema(tools):
    schema = tools["list_events"].parameters
    assert set(schema["properties"]) == {
        "kalender_namen",
        "von",
        "bis",
        "suchtext",
        "tag",
        "limit",
        "wiederholungen_aufloesen",
        "ohne_erinnerung",
        "ohne_sichtbarkeit",
        "ohne_tags",
        "uid_regex",
        "felder",
        "kompakt",
    }
    assert schema.get("required", []) == []


def test_list_events_delegates_with_filters(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(
        tools["list_events"].fn(
            kalender_namen=["Termine"],
            von="2026-07-01",
            bis="2026-07-31",
            suchtext="Zahnarzt",
            tag="Privat",
            limit=10,
            wiederholungen_aufloesen=True,
        )
    )
    fake_service.list_events.assert_called_once_with(
        calendar_names=["Termine"],
        von="2026-07-01",
        bis="2026-07-31",
        suchtext="Zahnarzt",
        tag="Privat",
        limit=10,
        expand=True,
        ohne_erinnerung=False,
        ohne_sichtbarkeit=False,
        ohne_tags=False,
        uid_regex=None,
    )


def test_list_events_passes_cleanup_filters_through(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(
        tools["list_events"].fn(
            kalender_namen=["Termine"],
            ohne_erinnerung=True,
            ohne_sichtbarkeit=True,
            ohne_tags=True,
            uid_regex="^[A-F0-9-]+$",
        )
    )
    fake_service.list_events.assert_called_once_with(
        calendar_names=["Termine"],
        von=None,
        bis=None,
        suchtext=None,
        tag=None,
        limit=None,
        expand=False,
        ohne_erinnerung=True,
        ohne_sichtbarkeit=True,
        ohne_tags=True,
        uid_regex="^[A-F0-9-]+$",
    )


def test_create_event_builds_event_fields(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    expected_event = {"uid": "new-uid", "titel": "Meeting", "start": "2026-07-20T14:00:00"}
    fake_service.get_event.return_value = expected_event
    result = _run(
        tools["create_event"].fn(
            kalender_name="Termine",
            titel="Meeting",
            start="2026-07-20T14:00:00",
            ende="2026-07-20T15:00:00",
            status="bestätigt",
        )
    )
    assert result == expected_event
    (cal_name, fields), _ = fake_service.create_event.call_args
    assert cal_name == "Termine"
    assert fields.titel == "Meeting"
    assert fields.status == "bestätigt"
    assert fields.clear == ()
    fake_service.get_event.assert_called_once_with("Termine", "new-uid")


# --- Birthdays (create_birthday) ---


def test_create_birthday_schema(tools):
    schema = tools["create_birthday"].parameters
    assert set(schema["properties"]) == {"name", "datum", "jahr", "kalender"}
    assert schema["required"] == ["name", "datum"]
    assert schema["properties"]["kalender"]["default"] == "Geburtstage"


def test_create_birthday_builds_the_convention_and_returns_the_event(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    expected_event = {"uid": "new-uid", "titel": "🎂 Papa (1975)"}
    fake_service.get_event.return_value = expected_event

    result = _run(tools["create_birthday"].fn(name="Papa", datum="07-04", jahr=1975))

    assert result == expected_event
    (cal_name, fields), _ = fake_service.create_event.call_args
    assert cal_name == "Geburtstage"
    assert fields.titel == "🎂 Papa (1975)"
    assert (fields.start, fields.ende) == ("1975-07-04", "1975-07-04")
    assert fields.wiederholung == "FREQ=YEARLY"
    assert fields.tags == ["Geburtstag"]
    assert fields.sichtbarkeit == "privat"
    assert fields.erinnerungen == ["-PT0M", "-P1D"]
    fake_service.get_event.assert_called_once_with("Geburtstage", "new-uid")


def test_create_birthday_writes_to_the_given_calendar(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"

    _run(tools["create_birthday"].fn(name="Papa", datum="07-04", kalender="Familie"))

    (cal_name, _), _ = fake_service.create_event.call_args
    assert cal_name == "Familie"


def test_create_birthday_invalid_datum_becomes_clean_tool_error(tools, fake_service):
    with pytest.raises(ToolError, match="Could not parse datum"):
        _run(tools["create_birthday"].fn(name="Papa", datum="4.7."))
    fake_service.create_event.assert_not_called()


def test_update_event_passes_clear_fields(tools, fake_service):
    expected_event = {"uid": "event-1", "titel": "Meeting", "ort": None}
    fake_service.get_event.return_value = expected_event
    result = _run(
        tools["update_event"].fn(
            kalender_name="Termine",
            event_uid="event-1",
            felder_leeren=["ende", "ort"],
        )
    )
    assert result == expected_event
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.clear == ("ende", "ort")
    fake_service.get_event.assert_called_once_with("Termine", "event-1")


def test_list_events_compacts_large_exdate_list(tools, fake_service):
    exdates_15 = [f"2026-08-{i:02d}" for i in range(1, 16)]
    fake_service.list_events.return_value = [
        {"uid": "e1", "ausnahme_daten": exdates_15},
        {"uid": "e2", "ausnahme_daten": ["2026-08-01", "2026-08-02", "2026-08-03"]},
        {"uid": "e3", "ausnahme_daten": []},
        {"uid": "e4", "ausnahme_daten": None},
    ]
    results = _run(tools["list_events"].fn(kalender_namen=["Termine"]))
    assert results[0]["ausnahme_daten"] == {
        "anzahl": 15,
        "erste": exdates_15[:5],
        "hinweis": "gekürzt - vollständige Liste über get_event abrufen",
    }
    assert results[1]["ausnahme_daten"] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert results[2]["ausnahme_daten"] == []
    assert results[3]["ausnahme_daten"] is None


# --- Payload slimming (felder whitelist / kompakt mode) and default window ---


def test_felder_whitelists_track_the_keys_the_parsers_actually_produce():
    """`felder` rejects any name not in the whitelist, so the two must not drift.

    A key added to `parse_vtodo`/`parse_vevent` but forgotten here becomes a
    field that listings return yet `felder` refuses to select - which is how
    `sichtbarkeit` arrived on the task side. Deriving both sets from the
    parsers makes the next such addition fail here instead of in a client.
    """
    from icalendar import Event, Todo

    from nextcloud_task_mcp.server import _EVENT_RESULT_KEYS, _TASK_RESULT_KEYS

    todo = Todo()
    todo.add("uid", "t1")
    # "liste"/"liste_url" are stamped on by the client layer, not the parser.
    assert set(mapping.parse_vtodo(todo)) | {"liste", "liste_url"} == set(_TASK_RESULT_KEYS)

    event = Event()
    event.add("uid", "e1")
    # "kalender" likewise; "quelle_url" is stripped before list_events returns.
    assert set(event_mapping.parse_vevent(event)) | {"kalender"} == set(_EVENT_RESULT_KEYS)


def _sample_event() -> dict:
    return {
        "uid": "e1",
        "titel": "Meeting",
        "start": "2026-07-20T14:00:00+02:00",
        "ende": "2026-07-20T15:00:00+02:00",
        "ganztaegig": False,
        "ort": None,
        "beschreibung": "x" * 250,
        "tags": [],
        "erinnerungen": [],
        "status": None,
        "sichtbarkeit": None,
        "wiederholung": None,
        "ausnahme_daten": [],
        "url": None,
        "verknuepfte_aufgaben": [],
        "wiederholung_von": None,
        "kalender": "Termine",
        "organisator": None,
        "teilnehmer": [],
    }


def test_list_events_kompakt_drops_empty_fields_and_truncates_description(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(kalender_namen=["Termine"], kompakt=True))
    assert set(event) == {"uid", "titel", "start", "ende", "ganztaegig", "beschreibung", "kalender"}
    assert event["ganztaegig"] is False  # False is a value, not "empty"
    assert event["beschreibung"].startswith("x" * 200 + "…")
    assert "gekürzt von 250 Zeichen" in event["beschreibung"]
    assert "get_event" in event["beschreibung"]


def test_list_events_kompakt_keeps_short_description_untouched(tools, fake_service):
    event = _sample_event()
    event["beschreibung"] = "kurz"
    fake_service.list_events.return_value = [event]
    (result,) = _run(tools["list_events"].fn(kalender_namen=["Termine"], kompakt=True))
    assert result["beschreibung"] == "kurz"


def test_list_events_felder_whitelist_filters_keys(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(
        tools["list_events"].fn(kalender_namen=["Termine"], felder=["uid", "titel", "start"])
    )
    assert event == {"uid": "e1", "titel": "Meeting", "start": "2026-07-20T14:00:00+02:00"}


def test_list_events_felder_accepts_bare_string(tools, fake_service):
    # Same lenience as listen_namen: a single name instead of a list works.
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(kalender_namen=["Termine"], felder="uid"))
    assert event == {"uid": "e1"}


def test_list_events_unknown_felder_entry_raises(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    with pytest.raises(ToolError, match="Unbekannte felder-Einträge: summary"):
        _run(tools["list_events"].fn(kalender_namen=["Termine"], felder=["uid", "summary"]))


def test_list_events_empty_felder_means_no_whitelist(tools, fake_service):
    """`[]` is what MCP clients send for an unset array - it must not blank the row.

    Deliberately unlike `listen_namen=[]` (an empty scope, which returns no
    rows): an empty *field* whitelist can only otherwise mean "a row of
    nothing", which is never what a caller wants.
    """
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(tools["list_events"].fn(kalender_namen=["Termine"], felder=[]))
    assert event == _sample_event()


def test_list_events_kompakt_keeps_exdate_summary_dict(tools, fake_service):
    """The >10-EXDATE summary is a dict, so `kompakt` must not prune it as "empty"."""
    event = _sample_event()
    event["ausnahme_daten"] = [f"2026-08-{i:02d}" for i in range(1, 16)]
    fake_service.list_events.return_value = [event]
    (result,) = _run(tools["list_events"].fn(kalender_namen=["Termine"], kompakt=True))
    assert result["ausnahme_daten"]["anzahl"] == 15


def test_list_events_felder_combines_with_kompakt(tools, fake_service):
    fake_service.list_events.return_value = [_sample_event()]
    (event,) = _run(
        tools["list_events"].fn(
            kalender_namen=["Termine"], felder=["uid", "titel", "ort"], kompakt=True
        )
    )
    # `ort` is whitelisted but None, so kompakt still drops it.
    assert event == {"uid": "e1", "titel": "Meeting"}


def test_list_events_without_calendars_and_window_defaults_to_90_days(tools, fake_service):
    from datetime import date, datetime, timedelta

    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn())
    _, kwargs = fake_service.list_events.call_args
    today = datetime.now(mapping.get_default_timezone()).date()
    assert date.fromisoformat(kwargs["von"]) == today - timedelta(days=90)
    assert date.fromisoformat(kwargs["bis"]) == today + timedelta(days=90)


def test_list_events_default_window_not_applied_when_scoped(tools, fake_service):
    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(kalender_namen=["Termine"]))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["von"] is None and kwargs["bis"] is None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(von="2026-07-01"))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["von"] == "2026-07-01" and kwargs["bis"] is None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(bis="2026-07-31"))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["von"] is None and kwargs["bis"] == "2026-07-31"


def test_list_events_cleanup_filters_do_not_disable_the_default_window(tools, fake_service):
    """A cleanup sweep narrows the default window rather than escaping it.

    The `ohne_*`/`uid_regex` filters run client-side on whatever the query
    returned, so a bare sweep only ever sees today ±90 days - phone-created
    events older than that need `von`/`bis` (or a calendar name) as well.
    Pinned because "find every hand-made event" reads like it should scan
    everything, and it does not.
    """
    from datetime import date, datetime, timedelta

    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(ohne_erinnerung=True, uid_regex="^[A-F0-9-]+$"))
    _, kwargs = fake_service.list_events.call_args
    today = datetime.now(mapping.get_default_timezone()).date()
    assert date.fromisoformat(kwargs["von"]) == today - timedelta(days=90)
    assert date.fromisoformat(kwargs["bis"]) == today + timedelta(days=90)
    assert kwargs["ohne_erinnerung"] is True and kwargs["uid_regex"] == "^[A-F0-9-]+$"


def test_list_events_default_window_lets_unscoped_expansion_work(tools, fake_service):
    """Unscoped expansion gets the default bounds; a named calendar still needs its own.

    `expand=True` without both bounds is refused one layer down
    (`_collect_events`), so the default window is what makes a bare
    `wiederholungen_aufloesen=True` call viable at all. Naming a calendar is a
    scoping decision and turns the default off, so that call still arrives
    unbounded and is refused as before - pinned here so the asymmetry is a
    choice rather than a surprise.
    """
    fake_service.list_events.return_value = []
    _run(tools["list_events"].fn(wiederholungen_aufloesen=True))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["expand"] is True
    assert kwargs["von"] is not None and kwargs["bis"] is not None

    fake_service.list_events.reset_mock()
    _run(tools["list_events"].fn(kalender_namen=["Termine"], wiederholungen_aufloesen=True))
    _, kwargs = fake_service.list_events.call_args
    assert kwargs["von"] is None and kwargs["bis"] is None


def _sample_task() -> dict:
    return {
        "uid": "t1",
        "titel": "Aufgabe",
        "start_datum": None,
        "faellig_datum": "2026-07-20",
        "prioritaet": None,
        "fortschritt_prozent": 0,
        "status": "offen",
        "ort": None,
        "url": None,
        "tags": [],
        "erinnerungen": [],
        "notizen": "n" * 300,
        "uebergeordnete_uid": None,
        "wiederholung": None,
        "ausnahme_daten": [],
        "wiederholung_von": None,
        "serie_uid": None,
        "liste": "Personal",
        "liste_url": "https://cloud.example.com/remote.php/dav/calendars/demo/personal/",
    }


def test_list_tasks_kompakt_drops_empty_fields_and_liste_url(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(tools["list_tasks"].fn(listen_namen=["Personal"], kompakt=True))
    assert set(task) == {
        "uid",
        "titel",
        "faellig_datum",
        "fortschritt_prozent",
        "status",
        "notizen",
        "liste",
    }
    assert task["fortschritt_prozent"] == 0  # 0 is a value, not "empty"
    assert task["notizen"].startswith("n" * 200 + "…")
    assert "gekürzt von 300 Zeichen" in task["notizen"]
    assert "get_task" in task["notizen"]


def test_list_tasks_felder_whitelist_filters_keys_and_validates(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(
        tools["list_tasks"].fn(listen_namen=["Personal"], felder=["uid", "titel", "faellig_datum"])
    )
    assert task == {"uid": "t1", "titel": "Aufgabe", "faellig_datum": "2026-07-20"}

    with pytest.raises(ToolError, match="Unbekannte felder-Einträge: beschreibung"):
        _run(tools["list_tasks"].fn(listen_namen=["Personal"], felder=["beschreibung"]))


def test_list_tasks_kompakt_keeps_liste_url_when_whitelisted(tools, fake_service):
    fake_service.list_tasks.return_value = [_sample_task()]
    (task,) = _run(
        tools["list_tasks"].fn(listen_namen=["Personal"], felder=["uid", "liste_url"], kompakt=True)
    )
    assert task == {"uid": "t1", "liste_url": _sample_task()["liste_url"]}


# --- Attendees (teilnehmer) ---


def test_create_event_passes_teilnehmer_through(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    teilnehmer = [{"email": "a@example.com", "rolle": "optional"}]
    _run(
        tools["create_event"].fn(
            kalender_name="Termine",
            titel="Meeting",
            start="2026-07-20T14:00:00",
            teilnehmer=teilnehmer,
        )
    )
    (_, fields), _ = fake_service.create_event.call_args
    assert fields.teilnehmer == teilnehmer


def test_create_event_teilnehmer_defaults_to_none(tools, fake_service):
    fake_service.create_event.return_value = "new-uid"
    _run(
        tools["create_event"].fn(
            kalender_name="Termine", titel="Meeting", start="2026-07-20T14:00:00"
        )
    )
    (_, fields), _ = fake_service.create_event.call_args
    assert fields.teilnehmer is None


def test_update_event_passes_teilnehmer_through(tools, fake_service):
    teilnehmer = [{"email": "b@example.com"}]
    _run(
        tools["update_event"].fn(
            kalender_name="Termine", event_uid="event-1", teilnehmer=teilnehmer
        )
    )
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.teilnehmer == teilnehmer


def test_update_event_can_clear_teilnehmer(tools, fake_service):
    _run(
        tools["update_event"].fn(
            kalender_name="Termine", event_uid="event-1", felder_leeren=["teilnehmer"]
        )
    )
    (_, _, fields), _ = fake_service.update_event.call_args
    assert fields.clear == ("teilnehmer",)


def test_update_events_delegates(tools, fake_service):
    expected_res = {
        "kalender_name": "Termine",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "ergebnisse": [{"uid": "u1", "status": "ok"}, {"uid": "u2", "status": "ok"}],
    }
    fake_service.update_events.return_value = expected_res
    res = _run(
        tools["update_events"].fn(
            kalender_name="Termine",
            event_uids=["u1", "u2"],
            ort="Büro",
            felder_leeren=["beschreibung"],
        )
    )
    assert res == expected_res
    (cal_name, uids, fields), _ = fake_service.update_events.call_args
    assert cal_name == "Termine"
    assert uids == ["u1", "u2"]
    assert fields.ort == "Büro"
    assert fields.clear == ("beschreibung",)


def test_update_exdates_delegates(tools, fake_service):
    fake_service.change_exdates.return_value = {
        "kalender_name": "Termine",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "ergebnisse": [
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
            calendar_name="Termine",
            event_uids=["u1", "u2"],
            add=["2026-07-27"],
        )
    )

    # The tool's surface is English end to end, unlike the German batch shape
    # `_batch_over_events` returns underneath it.
    assert res["calendar_name"] == "Termine"
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
    assert args == ("Termine", ["u1", "u2"], ["2026-07-27"], None, True)


def test_update_exdates_renames_failed_entries(tools, fake_service):
    fake_service.change_exdates.return_value = {
        "kalender_name": "Termine",
        "erfolgreich": 0,
        "fehlgeschlagen": 1,
        "ergebnisse": [{"uid": "u1", "status": "fehler", "fehler": "Event 'u1' was not found."}],
    }

    res = _run(
        tools["update_exdates"].fn(
            calendar_name="Termine", event_uids=["u1"], remove=["2026-07-27"]
        )
    )

    assert res["results"] == [
        {"uid": "u1", "status": "error", "error": "Event 'u1' was not found."}
    ]


def test_delete_events_delegates(tools, fake_service):
    expected_res = {
        "kalender_name": "Termine",
        "erfolgreich": 2,
        "fehlgeschlagen": 0,
        "ergebnisse": [{"uid": "u1", "status": "ok"}, {"uid": "u2", "status": "ok"}],
    }
    fake_service.delete_events.return_value = expected_res
    res = _run(tools["delete_events"].fn(kalender_name="Termine", event_uids=["u1", "u2"]))
    assert res == expected_res
    fake_service.delete_events.assert_called_once_with("Termine", ["u1", "u2"])


# --- respond_to_event ---


def test_respond_to_event_delegates(tools, fake_service):
    result = _run(
        tools["respond_to_event"].fn(
            kalender_name="Termine",
            event_uid="event-1",
            antwort="zugesagt",
            kommentar="Bin dabei",
        )
    )
    fake_service.respond_to_event.assert_called_once_with(
        "Termine", "event-1", "zugesagt", "Bin dabei"
    )
    assert result == {"uid": "event-1", "antwort": "zugesagt"}


def test_respond_to_event_kommentar_defaults_to_none(tools, fake_service):
    _run(
        tools["respond_to_event"].fn(
            kalender_name="Termine", event_uid="event-1", antwort="abgesagt"
        )
    )
    fake_service.respond_to_event.assert_called_once_with("Termine", "event-1", "abgesagt", None)


def test_respond_to_event_not_an_attendee_becomes_clean_tool_error(tools, fake_service):
    from nextcloud_task_mcp.errors import InvalidEventDataError

    fake_service.respond_to_event.side_effect = InvalidEventDataError(
        "You are not listed as an attendee of this event, so there is nothing to respond to."
    )
    with pytest.raises(ToolError, match="not listed as an attendee"):
        _run(
            tools["respond_to_event"].fn(
                kalender_name="Termine", event_uid="event-1", antwort="zugesagt"
            )
        )


# --- get_free_busy ---


def test_get_free_busy_delegates_own_availability(tools, fake_service):
    fake_service.get_free_busy.return_value = {
        "von": "2026-07-20T00:00:00+00:00",
        "bis": "2026-07-21T00:00:00+00:00",
        "benutzer": None,
        "belegt": [],
    }
    result = _run(tools["get_free_busy"].fn(von="2026-07-20", bis="2026-07-21"))
    fake_service.get_free_busy.assert_called_once_with("2026-07-20", "2026-07-21", None)
    assert result["belegt"] == []


def test_get_free_busy_passes_benutzer_through(tools, fake_service):
    fake_service.get_free_busy.return_value = {
        "von": "2026-07-20T00:00:00+00:00",
        "bis": "2026-07-21T00:00:00+00:00",
        "benutzer": "bob@example.com",
        "belegt": [],
    }
    _run(tools["get_free_busy"].fn(von="2026-07-20", bis="2026-07-21", benutzer="bob@example.com"))
    fake_service.get_free_busy.assert_called_once_with(
        "2026-07-20", "2026-07-21", "bob@example.com"
    )


# --- share_calendar / unshare_calendar / list_calendar_shares ---


def test_share_calendar_delegates(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "kalender_name": "Privat",
        "empfaenger": "bob",
        "schreibzugriff": True,
    }
    result = _run(
        tools["share_calendar"].fn(kalender_name="Privat", empfaenger="bob", schreibzugriff=True)
    )
    fake_service.share_calendar.assert_called_once_with("Privat", "bob", False, True)
    assert result == {"kalender_name": "Privat", "empfaenger": "bob", "schreibzugriff": True}


def test_share_calendar_defaults_gruppe_and_schreibzugriff_false(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "kalender_name": "Privat",
        "empfaenger": "team",
        "schreibzugriff": False,
    }
    _run(tools["share_calendar"].fn(kalender_name="Privat", empfaenger="team"))
    fake_service.share_calendar.assert_called_once_with("Privat", "team", False, False)


def test_share_calendar_passes_gruppe_through(tools, fake_service):
    fake_service.share_calendar.return_value = {
        "kalender_name": "Privat",
        "empfaenger": "team",
        "schreibzugriff": False,
    }
    _run(tools["share_calendar"].fn(kalender_name="Privat", empfaenger="team", gruppe=True))
    fake_service.share_calendar.assert_called_once_with("Privat", "team", True, False)


def test_unshare_calendar_delegates(tools, fake_service):
    result = _run(
        tools["unshare_calendar"].fn(kalender_name="Privat", empfaenger="bob", gruppe=False)
    )
    fake_service.unshare_calendar.assert_called_once_with("Privat", "bob", False)
    assert result == {"kalender_name": "Privat", "empfaenger": "bob"}


def test_list_calendar_shares_delegates(tools, fake_service):
    fake_service.list_calendar_shares.return_value = [
        {"empfaenger": "bob", "typ": "benutzer", "schreibzugriff": True, "status": "akzeptiert"}
    ]
    result = _run(tools["list_calendar_shares"].fn(kalender_name="Privat"))
    fake_service.list_calendar_shares.assert_called_once_with("Privat")
    assert result == [
        {"empfaenger": "bob", "typ": "benutzer", "schreibzugriff": True, "status": "akzeptiert"}
    ]


# --- list_trash / restore_from_trash ---


def test_list_trash_delegates(tools, fake_service):
    fake_service.list_trash.return_value = [
        {
            "id": "42.ics",
            "titel": "Einkaufen",
            "typ": "aufgabe",
            "kalender": "personal",
            "geloescht_am": "2026-07-10T12:00:00+00:00",
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
        "kalender_name": "Privat",
        "ics": "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
    }
    result = _run(tools["export_calendar"].fn(kalender_name="Privat"))
    fake_service.export_calendar.assert_called_once_with("Privat")
    assert result["ics"].startswith("BEGIN:VCALENDAR")


def test_import_ics_delegates(tools, fake_service):
    fake_service.import_ics.return_value = {
        "kalender_name": "Privat",
        "importiert": 2,
        "uebersprungen": 1,
    }
    ics_text = "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    result = _run(tools["import_ics"].fn(kalender_name="Privat", ics=ics_text))
    fake_service.import_ics.assert_called_once_with("Privat", ics_text)
    assert result == {"kalender_name": "Privat", "importiert": 2, "uebersprungen": 1}


def test_link_task_to_event_defaults_to_zeitblock(tools, fake_service):
    result = _run(
        tools["link_task_to_event"].fn(
            list_name="Privat",
            task_uid="task-1",
            kalender_name="Termine",
            event_uid="event-1",
        )
    )
    fake_service.link_task_to_event.assert_called_once_with(
        "Privat", "task-1", "Termine", "event-1", "zeitblock"
    )
    assert result == {"task_uid": "task-1", "event_uid": "event-1", "beziehung": "zeitblock"}


def test_list_events_for_task_delegates(tools, fake_service):
    fake_service.list_events_for_task.return_value = [
        {"uid": "event-1", "kalender_name": "Termine"}
    ]
    result = _run(tools["list_events_for_task"].fn(list_name="Privat", task_uid="task-1"))
    fake_service.list_events_for_task.assert_called_once_with(
        "Privat", "task-1", calendar_names=None
    )
    assert result == [{"uid": "event-1", "kalender_name": "Termine"}]


def test_list_events_for_task_passes_kalender_namen_through(tools, fake_service):
    fake_service.list_events_for_task.return_value = []
    _run(
        tools["list_events_for_task"].fn(
            list_name="Privat", task_uid="task-1", kalender_namen=["Termine"]
        )
    )
    fake_service.list_events_for_task.assert_called_once_with(
        "Privat", "task-1", calendar_names=["Termine"]
    )


def test_create_event_from_task_delegates(tools, fake_service):
    fake_service.create_event_from_task.return_value = "event-uid"
    result = _run(
        tools["create_event_from_task"].fn(
            list_name="Privat",
            task_uid="task-1",
            kalender_name="Termine",
            dauer_minuten=30,
        )
    )
    fake_service.create_event_from_task.assert_called_once_with(
        "Privat", "task-1", "Termine", None, 30, None, None, None, None
    )
    assert result == {"uid": "event-uid", "task_uid": "task-1"}


def test_create_event_from_task_passes_new_fields(tools, fake_service):
    fake_service.create_event_from_task.return_value = "event-uid"
    _run(
        tools["create_event_from_task"].fn(
            list_name="Privat",
            task_uid="task-1",
            kalender_name="Termine",
            start="2026-07-20T14:00:00",
            ende="2026-07-20T16:00:00",
            beschreibung="",
            erinnerungen=["-PT30M"],
            sichtbarkeit="privat",
        )
    )
    fake_service.create_event_from_task.assert_called_once_with(
        "Privat",
        "task-1",
        "Termine",
        "2026-07-20T14:00:00",
        None,
        "2026-07-20T16:00:00",
        "",
        ["-PT30M"],
        "privat",
    )


def test_get_agenda_delegates(tools, fake_service):
    fake_service.get_agenda.return_value = {"datum": "2026-07-20", "termine": [], "aufgaben": []}
    result = _run(tools["get_agenda"].fn(datum="2026-07-20"))
    fake_service.get_agenda.assert_called_once_with(
        "2026-07-20", calendar_names=None, list_names=None
    )
    assert result["datum"] == "2026-07-20"


def test_list_tags_delegates(tools, fake_service):
    fake_service.list_tags.return_value = [{"tag": "Arbeit", "anzahl": 3}]
    result = _run(tools["list_tags"].fn(kalender_namen=["Cal1"], listen_namen=["List1"]))
    fake_service.list_tags.assert_called_once_with(calendar_names=["Cal1"], list_names=["List1"])
    assert result == [{"tag": "Arbeit", "anzahl": 3}]


def test_calendar_not_found_becomes_clean_tool_error(tools, fake_service):
    fake_service.get_event.side_effect = CalendarNotFoundError("Calendar 'X' was not found.")
    with pytest.raises(ToolError, match="was not found"):
        _run(tools["get_event"].fn(kalender_name="X", event_uid="e1"))


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
        prioritaet=None,
        tag=None,
        suchtext=None,
        ohne_erinnerung=False,
        ohne_sichtbarkeit=False,
        ohne_tags=False,
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
    "list_notizen",
    "get_notiz",
    "search_notizen",
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
    "update_notiz",
    "replace_in_notiz",
    "update_notiz_abschnitt",
    "delete_notiz",
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
