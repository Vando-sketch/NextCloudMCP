"""Unit tests for CalDavService with the caldav library itself mocked out."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from caldav.elements import dav
from caldav.lib import error as caldav_error
from caldav.lib.url import URL
from icalendar import Calendar, Event, FreeBusy, Timezone, Todo
from lxml import etree

from nextcloud_task_mcp import caldav_client as caldav_client_module
from nextcloud_task_mcp import event_mapping, mapping
from nextcloud_task_mcp.caldav_client import CalDavService, _translate
from nextcloud_task_mcp.errors import (
    AuthenticationFailedError,
    CalendarAlreadyExistsError,
    CalendarNotFoundError,
    ConnectionFailedError,
    EventNotFoundError,
    InvalidEventDataError,
    InvalidIcsDataError,
    InvalidTaskDataError,
    TaskConflictError,
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)


def _make_calendar(
    name: str,
    url: str = "https://cloud.example.com/dav/personal/",
    components: list[str] | None = None,
) -> MagicMock:
    """A MagicMock standing in for a caldav.Calendar with the given display name.

    `components` is what `get_supported_components()` reports; it defaults to
    VTODO-only (a plain Nextcloud task list) since most tests here exercise
    the task side. Event-calendar tests pass ["VEVENT"] explicitly.
    """
    calendar = MagicMock()
    calendar.get_display_name.return_value = name
    calendar.url = url
    calendar.get_supported_components.return_value = (
        components if components is not None else ["VTODO"]
    )
    return calendar


@pytest.fixture
def mock_dav_client():
    with patch("nextcloud_task_mcp.caldav_client.DAVClient") as mock_cls:
        yield mock_cls


@pytest.fixture
def service(mock_dav_client) -> CalDavService:
    return CalDavService(url="https://cloud.example.com/dav/", username="u", password="p")


# --- HTTP timeout (A2) ---


def test_default_timeout_passed_to_dav_client(mock_dav_client):
    CalDavService(url="https://cloud.example.com/dav/", username="u", password="p")
    _, kwargs = mock_dav_client.call_args
    assert kwargs["timeout"] == 30


def test_custom_timeout_passed_to_dav_client(mock_dav_client):
    CalDavService(url="https://cloud.example.com/dav/", username="u", password="p", timeout=5)
    _, kwargs = mock_dav_client.call_args
    assert kwargs["timeout"] == 5


# --- Rate-limit backoff on 429/503 (A5) ---


def test_rate_limit_handling_enabled_by_default(mock_dav_client):
    CalDavService(url="https://cloud.example.com/dav/", username="u", password="p")
    _, kwargs = mock_dav_client.call_args
    assert kwargs["rate_limit_handle"] is True
    assert isinstance(kwargs["rate_limit_default_sleep"], int)
    assert isinstance(kwargs["rate_limit_max_sleep"], int)
    assert kwargs["rate_limit_default_sleep"] > 0
    assert kwargs["rate_limit_max_sleep"] >= kwargs["rate_limit_default_sleep"]


@pytest.fixture
def principal(mock_dav_client):
    return mock_dav_client.return_value.principal.return_value


def test_list_task_lists_returns_names_and_urls(service, principal):
    cal1 = _make_calendar("Personal", "https://cloud.example.com/dav/personal/")
    cal2 = _make_calendar("Arbeit", "https://cloud.example.com/dav/arbeit/")
    principal.calendars.return_value = [cal1, cal2]

    result = service.list_task_lists()

    assert result == [
        {"name": "Personal", "url": "https://cloud.example.com/dav/personal/"},
        {"name": "Arbeit", "url": "https://cloud.example.com/dav/arbeit/"},
    ]


# --- create_task_list ---


def test_create_task_list_creates_and_returns_info(service, principal):
    principal.calendars.return_value = []
    new_calendar = _make_calendar(
        "Groceries", "https://cloud.example.com/dav/calendars/u/groceries/"
    )
    principal.make_calendar.return_value = new_calendar

    result = service.create_task_list("Groceries")

    principal.make_calendar.assert_called_once_with(
        name="Groceries", cal_id="groceries", supported_calendar_component_set=["VTODO"]
    )
    assert result == {
        "name": "Groceries",
        "url": "https://cloud.example.com/dav/calendars/u/groceries/",
    }


def test_create_task_list_slugifies_display_name(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.return_value = _make_calendar("Grocery List!")

    service.create_task_list("Grocery List!")

    _, kwargs = principal.make_calendar.call_args
    assert kwargs["cal_id"] == "grocery-list"


def test_create_task_list_slugifies_with_no_ascii_alnum_falls_back(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.return_value = _make_calendar("日本語")

    service.create_task_list("日本語")

    _, kwargs = principal.make_calendar.call_args
    assert kwargs["cal_id"].startswith("list-")
    assert len(kwargs["cal_id"]) > len("list-")


def test_create_task_list_populates_cache(service, principal):
    principal.calendars.return_value = []
    new_calendar = _make_calendar("Groceries")
    principal.make_calendar.return_value = new_calendar

    service.create_task_list("Groceries")
    new_calendar.todos.return_value = []

    service.list_tasks("Groceries")

    # No second principal.calendars() PROPFIND - the newly-created calendar
    # was cached directly instead of requiring a fresh resolution.
    assert principal.calendars.call_count == 1


def test_create_task_list_requires_display_name(service):
    with pytest.raises(InvalidTaskDataError):
        service.create_task_list("")


def test_create_task_list_requires_non_whitespace_display_name(service):
    with pytest.raises(InvalidTaskDataError):
        service.create_task_list("   ")


def test_create_task_list_raises_when_display_name_already_exists(service, principal):
    existing = _make_calendar("Groceries")
    principal.calendars.return_value = [existing]

    with pytest.raises(TaskListAlreadyExistsError):
        service.create_task_list("Groceries")

    principal.make_calendar.assert_not_called()


def test_create_task_list_raises_when_collection_id_conflicts(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.side_effect = caldav_error.MkcolError("405 Method Not Allowed")

    with pytest.raises(TaskListAlreadyExistsError):
        service.create_task_list("Groceries")


def test_create_task_list_raises_when_collection_id_conflicts_409(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.side_effect = caldav_error.MkcalendarError("409 Conflict")

    with pytest.raises(TaskListAlreadyExistsError):
        service.create_task_list("Groceries")


def test_create_task_list_reraises_unrelated_mkcol_error_as_generic(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.side_effect = caldav_error.MkcolError("403 Forbidden")

    with pytest.raises(TaskMcpError) as exc_info:
        service.create_task_list("Groceries")
    assert not isinstance(exc_info.value, TaskListAlreadyExistsError)


def test_create_task_list_translates_generic_exception(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.create_task_list("Groceries")


def test_create_task_list_translates_generic_exception_from_calendars_lookup(service, principal):
    principal.calendars.side_effect = caldav_client_module._http_errors.ConnectionError("down")

    with pytest.raises(ConnectionFailedError):
        service.create_task_list("Groceries")


def test_create_task_list_reraises_task_mcp_error_from_get_principal(service, mock_dav_client):
    mock_dav_client.return_value.principal.side_effect = caldav_error.AuthorizationError(
        "bad creds"
    )

    with pytest.raises(AuthenticationFailedError):
        service.create_task_list("Groceries")


# --- delete_task_list ---


def test_delete_task_list_deletes_calendar(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]

    service.delete_task_list("Groceries")

    calendar.delete.assert_called_once_with()


def test_delete_task_list_evicts_cache_entry(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]

    service.delete_task_list("Groceries")

    # The deleted list must no longer be served from the cache - a later
    # lookup has to hit principal.calendars() again, see it's really gone,
    # and raise not-found rather than reusing the deleted calendar object.
    principal.calendars.return_value = []
    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Groceries")
    assert principal.calendars.call_count == 2


def test_delete_task_list_not_found_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskListNotFoundError):
        service.delete_task_list("Nonexistent")


def test_delete_task_list_stale_cache_entry_is_invalidated_and_retried(service, principal):
    stale_calendar = _make_calendar("Groceries", "https://cloud.example.com/dav/old/")
    fresh_calendar = _make_calendar("Groceries", "https://cloud.example.com/dav/new/")

    principal.calendars.return_value = [stale_calendar]
    service.list_task_lists()
    assert principal.calendars.call_count == 1

    stale_calendar.delete.side_effect = caldav_error.NotFoundError("gone")
    principal.calendars.return_value = [fresh_calendar]

    service.delete_task_list("Groceries")

    assert principal.calendars.call_count == 2
    fresh_calendar.delete.assert_called_once_with()


def test_delete_task_list_stale_cache_entry_gives_up_after_one_retry(service, principal):
    stale_calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [stale_calendar]
    service.list_task_lists()

    stale_calendar.delete.side_effect = caldav_error.NotFoundError("gone")

    with pytest.raises(TaskListNotFoundError):
        service.delete_task_list("Groceries")
    assert principal.calendars.call_count == 2


def test_delete_task_list_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]
    calendar.delete.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.delete_task_list("Groceries")


def test_delete_task_list_reraises_task_mcp_error_from_get_principal(service, mock_dav_client):
    mock_dav_client.return_value.principal.side_effect = caldav_error.AuthorizationError(
        "bad creds"
    )

    with pytest.raises(AuthenticationFailedError):
        service.delete_task_list("Groceries")


def test_delete_task_list_ambiguous_name_reraises_as_task_mcp_error(service, principal):
    cal1 = _make_calendar("Groceries", "https://cloud.example.com/dav/g1/")
    cal2 = _make_calendar("Groceries", "https://cloud.example.com/dav/g2/")
    principal.calendars.return_value = [cal1, cal2]

    with pytest.raises(TaskMcpError, match="ambiguous"):
        service.delete_task_list("Groceries")


# --- rename_task_list ---


def test_rename_task_list_sets_display_name_and_returns_info(service, principal):
    calendar = _make_calendar("Groceries", "https://cloud.example.com/dav/groceries/")
    principal.calendars.return_value = [calendar]

    result = service.rename_task_list("Groceries", "Shopping")

    calendar.set_properties.assert_called_once()
    (props,), _ = calendar.set_properties.call_args
    assert len(props) == 1
    assert str(props[0]) == str(dav.DisplayName("Shopping"))
    assert result == {
        "name": "Shopping",
        "url": "https://cloud.example.com/dav/groceries/",
    }


def test_rename_task_list_updates_cache(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]

    service.rename_task_list("Groceries", "Shopping")

    # New name is served from the cache without a fresh PROPFIND...
    calendar.get_display_name.return_value = "Shopping"
    calendar.todos.return_value = []
    service.list_tasks("Shopping")
    assert principal.calendars.call_count == 1

    # ...and the old name is gone from the cache, so it has to resolve fresh
    # (and fail, since no calendar is named "Groceries" anymore).
    principal.calendars.return_value = []
    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Groceries")
    assert principal.calendars.call_count == 2


def test_rename_task_list_requires_new_display_name(service):
    with pytest.raises(InvalidTaskDataError):
        service.rename_task_list("Groceries", "")


def test_rename_task_list_requires_non_whitespace_new_display_name(service):
    with pytest.raises(InvalidTaskDataError):
        service.rename_task_list("Groceries", "   ")


def test_rename_task_list_not_found_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskListNotFoundError):
        service.rename_task_list("Nonexistent", "Shopping")


def test_rename_task_list_ambiguous_list_name_reraises_as_task_mcp_error(service, principal):
    cal1 = _make_calendar("Groceries", "https://cloud.example.com/dav/g1/")
    cal2 = _make_calendar("Groceries", "https://cloud.example.com/dav/g2/")
    principal.calendars.return_value = [cal1, cal2]

    with pytest.raises(TaskMcpError, match="ambiguous") as exc_info:
        service.rename_task_list("Groceries", "Shopping")
    assert not isinstance(exc_info.value, TaskListNotFoundError)


def test_rename_task_list_raises_when_new_name_already_exists(service, principal):
    calendar = _make_calendar("Groceries")
    other = _make_calendar("Shopping")
    principal.calendars.return_value = [calendar, other]

    with pytest.raises(TaskListAlreadyExistsError):
        service.rename_task_list("Groceries", "Shopping")

    calendar.set_properties.assert_not_called()


def test_rename_task_list_to_same_name_is_not_a_self_conflict(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]

    result = service.rename_task_list("Groceries", "Groceries")

    calendar.set_properties.assert_called_once()
    assert result["name"] == "Groceries"


def test_rename_task_list_translates_generic_exception_from_set_properties(service, principal):
    calendar = _make_calendar("Groceries")
    principal.calendars.return_value = [calendar]
    calendar.set_properties.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.rename_task_list("Groceries", "Shopping")


def test_rename_task_list_translates_generic_exception_from_calendars_lookup(service, principal):
    principal.calendars.side_effect = caldav_client_module._http_errors.ConnectionError("down")

    with pytest.raises(ConnectionFailedError):
        service.rename_task_list("Groceries", "Shopping")


def test_rename_task_list_reraises_task_mcp_error_from_get_principal(service, mock_dav_client):
    mock_dav_client.return_value.principal.side_effect = caldav_error.AuthorizationError(
        "bad creds"
    )

    with pytest.raises(AuthenticationFailedError):
        service.rename_task_list("Groceries", "Shopping")


def test_list_tasks_parses_todos(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

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
            "faellig_datum": None,
            "prioritaet": None,
            "fortschritt_prozent": 0,
            "status": "offen",
            "ort": None,
            "url": None,
            "tags": [],
            "erinnerungen": [],
            "notizen": None,
            "uebergeordnete_uid": None,
            "wiederholung": None,
            "liste": "Personal",
        }
    ]


def test_list_tasks_list_not_found_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Nonexistent")


def test_list_tasks_no_arguments_queries_all_lists_and_sets_liste(service, principal):
    cal1 = _make_calendar("List1")
    cal2 = _make_calendar("List2")
    principal.calendars.return_value = [cal1, cal2]

    todo1 = Todo()
    todo1.add("uid", "t1")
    todo1.add("summary", "Task 1")
    todo1.add("due", date(2026, 8, 1))

    todo2 = Todo()
    todo2.add("uid", "t2")
    todo2.add("summary", "Task 2")
    todo2.add("due", date(2026, 8, 2))

    t1_obj = MagicMock()
    t1_obj.icalendar_component = todo1
    t2_obj = MagicMock()
    t2_obj.icalendar_component = todo2

    cal1.todos.return_value = [t1_obj]
    cal2.todos.return_value = [t2_obj]

    result = service.list_tasks(list_names=None)
    assert len(result) == 2
    assert result[0]["uid"] == "t1"
    assert result[0]["liste"] == "List1"
    assert result[1]["uid"] == "t2"
    assert result[1]["liste"] == "List2"


def test_list_tasks_empty_list_names_returns_empty_without_request(service, principal):
    result = service.list_tasks(list_names=[])
    assert result == []
    principal.calendars.assert_not_called()


def test_list_tasks_limit_cuts_after_merge_across_lists(service, principal):
    cal1 = _make_calendar("List1")
    cal2 = _make_calendar("List2")
    principal.calendars.return_value = [cal1, cal2]

    todo1 = Todo()
    todo1.add("uid", "t1-later")
    todo1.add("summary", "Task 1")
    todo1.add("due", date(2026, 8, 10))

    todo2 = Todo()
    todo2.add("uid", "t2-earlier")
    todo2.add("summary", "Task 2")
    todo2.add("due", date(2026, 8, 1))

    t1_obj = MagicMock()
    t1_obj.icalendar_component = todo1
    t2_obj = MagicMock()
    t2_obj.icalendar_component = todo2

    cal1.todos.return_value = [t1_obj]
    cal2.todos.return_value = [t2_obj]

    result = service.list_tasks(list_names=None, limit=1)
    assert len(result) == 1
    assert result[0]["uid"] == "t2-earlier"
    assert result[0]["liste"] == "List2"


def test_list_tasks_all_lists_reaches_both_lists_sharing_a_display_name(service, principal):
    """Two lists may legitimately carry the same display name.

    The all-lists branch queries the resolved collection objects directly
    instead of resolving by name, which is the only reason both stay
    reachable - resolving "Dup" by name is ambiguous on purpose. Pinned here
    because "simplifying" that branch back to the name-based path would
    silently turn a full listing into an error.
    """
    dup_a = _make_calendar("Dup", "https://cloud.example.com/dav/dup-a/")
    dup_b = _make_calendar("Dup", "https://cloud.example.com/dav/dup-b/")
    dup_a.todos.return_value = [_todo_obj("in-a", titel="A", faellig_datum="2026-08-01")]
    dup_b.todos.return_value = [_todo_obj("in-b", titel="B", faellig_datum="2026-08-02")]
    principal.calendars.return_value = [dup_a, dup_b]

    result = service.list_tasks(list_names=None)

    assert [t["uid"] for t in result] == ["in-a", "in-b"]
    assert [t["liste"] for t in result] == ["Dup", "Dup"]


def test_list_tasks_named_duplicate_list_is_still_ambiguous(service, principal):
    dup_a = _make_calendar("Dup", "https://cloud.example.com/dav/dup-a/")
    dup_b = _make_calendar("Dup", "https://cloud.example.com/dav/dup-b/")
    principal.calendars.return_value = [dup_a, dup_b]

    with pytest.raises(TaskMcpError, match="ambiguous"):
        service.list_tasks(list_names=["Dup"])


def test_get_agenda_sorts_tasks_by_due_time(service, principal):
    """Agenda tasks inherit list_tasks' sort - they are no longer in server order."""
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.todos.return_value = [
        _todo_obj("abend", titel="Abends", faellig_datum="2026-07-20T18:00:00"),
        _todo_obj("frueh", titel="Früh", faellig_datum="2026-07-20T06:00:00"),
    ]
    principal.calendars.return_value = [event_cal, todo_cal]

    result = service.get_agenda("2026-07-20")

    assert [t["uid"] for t in result["aufgaben"]] == ["frueh", "abend"]
    assert {t["liste"] for t in result["aufgaben"]} == {"Privat"}


def test_create_task_saves_ical_and_returns_uid(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    uid = service.create_task("Personal", mapping.TaskFields(titel="Neue Aufgabe"))

    calendar.save_todo.assert_called_once()
    _, kwargs = calendar.save_todo.call_args
    assert "BEGIN:VTODO" in kwargs["ical"]
    assert uid in kwargs["ical"]
    assert "Neue Aufgabe" in kwargs["ical"]


def test_create_task_without_titel_raises(service):
    with pytest.raises(InvalidTaskDataError):
        service.create_task("Personal", mapping.TaskFields())


def test_update_task_applies_fields_and_saves(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("summary", "Alt")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.update_task("Personal", "abc", mapping.TaskFields(titel="Neu"))

    todo_obj.save.assert_called_once()
    assert str(todo.get("summary")) == "Neu"


def test_update_task_not_found_raises(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = caldav_error.NotFoundError("no such task")

    with pytest.raises(TaskNotFoundError):
        service.update_task("Personal", "missing-uid", mapping.TaskFields(titel="x"))


# --- wiederholung (RRULE), now writable ---


def test_create_task_saves_rrule(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    service.create_task(
        "Personal",
        mapping.TaskFields(
            titel="Muell rausbringen",
            faellig_datum="2026-07-20",
            wiederholung="FREQ=WEEKLY;BYDAY=MO",
        ),
    )

    calendar.save_todo.assert_called_once()
    _, kwargs = calendar.save_todo.call_args
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in kwargs["ical"]


def test_create_task_invalid_rrule_raises_and_does_not_save(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    with pytest.raises(InvalidTaskDataError, match="RRULE"):
        service.create_task(
            "Personal",
            mapping.TaskFields(titel="T", faellig_datum="2026-07-20", wiederholung="kaputt"),
        )
    calendar.save_todo.assert_not_called()


def test_create_task_rrule_without_anchor_raises_and_does_not_save(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    with pytest.raises(InvalidTaskDataError, match="start_datum|faellig_datum"):
        service.create_task("Personal", mapping.TaskFields(titel="T", wiederholung="FREQ=DAILY"))
    calendar.save_todo.assert_not_called()


def test_update_task_sets_rrule_on_task_with_existing_anchor(service, principal):
    """The RRULE anchor check runs against the final component state, so a
    call that only sets wiederholung succeeds when faellig_datum was already
    on the stored task, not part of this call's fields."""
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("due", date(2026, 7, 20))
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.update_task("Personal", "abc", mapping.TaskFields(wiederholung="FREQ=WEEKLY"))

    todo_obj.save.assert_called_once()
    assert mapping.parse_vtodo(todo)["wiederholung"] == "FREQ=WEEKLY"


def test_update_task_invalid_rrule_raises_and_does_not_save(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("due", date(2026, 7, 20))
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    with pytest.raises(InvalidTaskDataError, match="RRULE"):
        service.update_task("Personal", "abc", mapping.TaskFields(wiederholung="kaputt"))
    todo_obj.save.assert_not_called()


def test_update_task_rrule_without_any_anchor_raises_and_does_not_save(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    with pytest.raises(InvalidTaskDataError, match="start_datum|faellig_datum"):
        service.update_task("Personal", "abc", mapping.TaskFields(wiederholung="FREQ=DAILY"))
    todo_obj.save.assert_not_called()


def test_update_task_clears_wiederholung(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("due", date(2026, 7, 20))
    todo.add("rrule", {"FREQ": "DAILY"})
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.update_task("Personal", "abc", mapping.TaskFields(clear=("wiederholung",)))

    todo_obj.save.assert_called_once()
    assert mapping.parse_vtodo(todo)["wiederholung"] is None


def test_complete_task_leaves_rrule_intact(service, principal):
    """Pins the observed behaviour documented in docs/tools.md: complete_task
    only sets STATUS/PERCENT-COMPLETE/COMPLETED - it does not roll a
    recurring task's series forward to a next occurrence."""
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("due", date(2026, 7, 20))
    todo.add("rrule", {"FREQ": "DAILY"})
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    service.complete_task("Personal", "abc")

    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "erledigt"
    assert parsed["wiederholung"] == "FREQ=DAILY"


def test_get_task_returns_parsed_task(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

    todo = Todo()
    todo.add("uid", "abc")
    todo.add("summary", "Milch kaufen")
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    calendar.get_todo_by_uid.return_value = todo_obj

    result = service.get_task("Personal", "abc")

    calendar.get_todo_by_uid.assert_called_once_with("abc")
    assert result["uid"] == "abc"
    assert result["titel"] == "Milch kaufen"


def test_get_task_not_found_raises(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = caldav_error.NotFoundError("no such task")

    with pytest.raises(TaskNotFoundError):
        service.get_task("Personal", "missing-uid")


def test_get_task_list_not_found_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskListNotFoundError):
        service.get_task("Nonexistent", "abc")


def test_complete_task_marks_completed(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]

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
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
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


# --- Calendar cache and duplicate-name detection (A3) ---


def test_get_calendar_is_cached_across_calls(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.todos.return_value = []

    service.list_tasks("Personal")
    service.list_tasks("Personal")

    # Only the first call should have needed a fresh principal.calendars()
    # PROPFIND; the second is served from the cache (A3).
    assert principal.calendars.call_count == 1


def test_list_task_lists_populates_cache_opportunistically(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.todos.return_value = []

    service.list_task_lists()
    service.list_tasks("Personal")

    assert principal.calendars.call_count == 1


def test_duplicate_display_names_across_calls_are_not_cached(service, principal):
    """A name that's ambiguous when populated must not silently cache one of the matches."""
    cal1 = _make_calendar("Personal", "https://cloud.example.com/dav/p1/")
    cal2 = _make_calendar("Personal", "https://cloud.example.com/dav/p2/")
    principal.calendars.return_value = [cal1, cal2]

    service.list_task_lists()

    with pytest.raises(TaskMcpError, match="ambiguous"):
        service.list_tasks("Personal")


def test_duplicate_display_name_raises_ambiguous_error(service, principal):
    cal1 = _make_calendar("Personal", "https://cloud.example.com/dav/p1/")
    cal2 = _make_calendar("Personal", "https://cloud.example.com/dav/p2/")
    principal.calendars.return_value = [cal1, cal2]

    with pytest.raises(TaskMcpError, match="ambiguous") as exc_info:
        service.list_tasks("Personal")
    assert not isinstance(exc_info.value, TaskListNotFoundError)


def test_stale_cache_entry_is_invalidated_and_retried(service, principal):
    """A cached calendar that 404s on use (deleted/renamed server-side) is retried once."""
    stale_calendar = _make_calendar("Personal", "https://cloud.example.com/dav/old/")
    fresh_calendar = _make_calendar("Personal", "https://cloud.example.com/dav/new/")

    # First resolution returns the (soon to be stale) calendar and populates the cache.
    principal.calendars.return_value = [stale_calendar]
    service.list_task_lists()
    assert principal.calendars.call_count == 1

    # Using the cached calendar now 404s (as if it were deleted/recreated
    # server-side); a fresh principal.calendars() call finds it again under a
    # new URL.
    stale_calendar.todos.side_effect = caldav_error.NotFoundError("gone")
    principal.calendars.return_value = [fresh_calendar]
    fresh_calendar.todos.return_value = []

    result = service.list_tasks("Personal")

    assert result == []
    assert principal.calendars.call_count == 2
    fresh_calendar.todos.assert_called_once()


def test_stale_cache_entry_gives_up_after_one_retry(service, principal):
    stale_calendar = _make_calendar("Personal")
    principal.calendars.return_value = [stale_calendar]
    service.list_task_lists()

    # Every call to .todos() (both the initial attempt and the retry) 404s -
    # the list is genuinely gone, not just cached-stale.
    stale_calendar.todos.side_effect = caldav_error.NotFoundError("gone")

    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Personal")
    # Resolved once initially (list_task_lists) + once more on retry.
    assert principal.calendars.call_count == 2


# --- _translate: every branch (A4, D7, E4) ---
#
# _translate is a pure function, so we exercise it directly rather than
# through CalDavService. For the branches D7 identifies as previously
# embedding raw exception text (generic DAVError, generic RequestException,
# and the final catch-all), we assert both the resulting error type AND that
# the sensitive marker text from the original exception does NOT appear in
# the translated message - only a categorized generic message should.

_SECRET_MARKER = "super-secret-internal-detail-xyz"

_http_errors = caldav_client_module._http_errors

_TRANSLATE_CASES = [
    pytest.param(
        caldav_error.AuthorizationError(_SECRET_MARKER),
        AuthenticationFailedError,
        False,
        id="authorization_error",
    ),
    pytest.param(
        caldav_error.NotFoundError(_SECRET_MARKER),
        TaskMcpError,
        False,
        id="not_found_error",
    ),
    pytest.param(
        caldav_error.ETagMismatchError(_SECRET_MARKER),
        TaskConflictError,
        False,
        id="etag_mismatch_conflict",
    ),
    pytest.param(
        caldav_error.DAVError(_SECRET_MARKER),
        TaskMcpError,
        True,
        id="generic_dav_error",
    ),
    pytest.param(
        _http_errors.ConnectionError(_SECRET_MARKER),
        ConnectionFailedError,
        False,
        id="connection_error",
    ),
    pytest.param(
        _http_errors.Timeout(_SECRET_MARKER),
        ConnectionFailedError,
        False,
        id="timeout",
    ),
    pytest.param(
        _http_errors.RequestException(_SECRET_MARKER),
        ConnectionFailedError,
        True,
        id="generic_request_exception",
    ),
    pytest.param(
        RuntimeError(_SECRET_MARKER),
        TaskMcpError,
        True,
        id="arbitrary_exception_catch_all",
    ),
]


@pytest.mark.parametrize(("exc", "expected_type", "must_be_scrubbed"), _TRANSLATE_CASES)
def test_translate_every_branch(exc, expected_type, must_be_scrubbed):
    result = _translate(exc)

    assert isinstance(result, expected_type)
    if must_be_scrubbed:
        assert _SECRET_MARKER not in str(result)


def test_translate_etag_mismatch_message_mentions_retry():
    result = _translate(caldav_error.ETagMismatchError("412 precondition failed"))
    assert isinstance(result, TaskConflictError)
    message = str(result).lower()
    assert "modified" in message or "conflict" in message
    assert "retry" in message or "re-fetch" in message


def test_translate_authorization_error_403_is_not_reported_as_credentials():
    # caldav collapses both 401 and 403 into AuthorizationError, but the HTTP
    # reason phrase ("Forbidden" vs "Unauthorized") still survives on
    # `.reason` and distinguishes them - a 403 (e.g. Nextcloud's "Calendar
    # limit reached") must not be misreported as a bad-credentials problem.
    result = _translate(caldav_error.AuthorizationError(url="irrelevant", reason="Forbidden"))
    assert not isinstance(result, AuthenticationFailedError)
    assert isinstance(result, TaskMcpError)
    message = str(result).lower()
    assert "rejected the caldav credentials" not in message
    assert "forbidden" in message


def test_translate_authorization_error_401_still_reports_credentials():
    result = _translate(caldav_error.AuthorizationError(url="irrelevant", reason="Unauthorized"))
    assert isinstance(result, AuthenticationFailedError)


# --- Generic (non-TaskMcpError, non-NotFoundError) exceptions through every
# --- public CalDavService method (E4 remainder: outer except-Exception
# --- branches, and _resolve_calendar's own except-Exception branch). ---


def test_resolve_calendar_translates_generic_exception_from_principal_calendars(service, principal):
    # Hits `_resolve_calendar`'s own `except Exception` branch (not the outer
    # per-method one): the very first, uncached resolution of "Personal" asks
    # `principal.calendars()` directly, which here raises something that is
    # neither a TaskMcpError nor a caldav NotFoundError.
    principal.calendars.side_effect = caldav_client_module._http_errors.ConnectionError("down")

    with pytest.raises(ConnectionFailedError):
        service.list_tasks("Personal")


def test_list_task_lists_translates_generic_exception(service, principal):
    principal.calendars.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.list_task_lists()


def test_list_tasks_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.todos.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.list_tasks("Personal")


def test_create_task_list_not_found_raises(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.save_todo.side_effect = caldav_error.NotFoundError("no such list")

    with pytest.raises(TaskListNotFoundError):
        service.create_task("Personal", mapping.TaskFields(titel="x"))


def test_create_task_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.save_todo.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.create_task("Personal", mapping.TaskFields(titel="x"))


def test_update_task_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.update_task("Personal", "abc", mapping.TaskFields(titel="x"))


def test_get_task_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.get_task("Personal", "abc")


def test_complete_task_not_found_raises(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = caldav_error.NotFoundError("no such task")

    with pytest.raises(TaskNotFoundError):
        service.complete_task("Personal", "missing-uid")


def test_complete_task_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.complete_task("Personal", "abc")


def test_delete_task_not_found_raises(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = caldav_error.NotFoundError("no such task")

    with pytest.raises(TaskNotFoundError):
        service.delete_task("Personal", "missing-uid")


def test_delete_task_translates_generic_exception_from_op(service, principal):
    calendar = _make_calendar("Personal")
    principal.calendars.return_value = [calendar]
    calendar.get_todo_by_uid.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.delete_task("Personal", "abc")


def test_resolve_calendar_reraises_task_mcp_error_from_get_principal(service, mock_dav_client):
    # `_resolve_calendar`'s own `except TaskMcpError: raise` branch: the
    # failure happens resolving the *principal* itself (already translated to
    # a TaskMcpError by `_get_principal`), not in `.calendars()`.
    mock_dav_client.return_value.principal.side_effect = caldav_error.AuthorizationError(
        "bad creds"
    )

    with pytest.raises(AuthenticationFailedError):
        service.list_tasks("Personal")


@pytest.mark.parametrize(
    "call",
    [
        lambda service: service.create_task("Personal", mapping.TaskFields(titel="x")),
        lambda service: service.update_task("Personal", "abc", mapping.TaskFields(titel="x")),
        lambda service: service.complete_task("Personal", "abc"),
        lambda service: service.delete_task("Personal", "abc"),
    ],
    ids=["create_task", "update_task", "complete_task", "delete_task"],
)
def test_ambiguous_list_name_reraises_as_task_mcp_error(service, principal, call):
    # Each mutating method's own `except TaskMcpError: raise` branch: the
    # ambiguity is detected during calendar *resolution* (_resolve_calendar),
    # before the method's own CalDAV operation ever runs.
    cal1 = _make_calendar("Personal", "https://cloud.example.com/dav/p1/")
    cal2 = _make_calendar("Personal", "https://cloud.example.com/dav/p2/")
    principal.calendars.return_value = [cal1, cal2]

    with pytest.raises(TaskMcpError, match="ambiguous"):
        call(service)


def test_translate_scrubbed_branches_log_the_real_exception(caplog):
    with caplog.at_level(logging.WARNING, logger="nextcloud_task_mcp.caldav_client"):
        _translate(caldav_error.DAVError(_SECRET_MARKER))
        _translate(_http_errors.RequestException(_SECRET_MARKER))
        _translate(RuntimeError(_SECRET_MARKER))

    # The raw detail must still be visible server-side (in the logs), even
    # though it's scrubbed from the user-facing message.
    logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert len(caplog.records) == 3
    for record in caplog.records:
        assert record.levelno == logging.WARNING
    # exc_info was attached so the traceback (and the secret marker within
    # it) ends up in the formatted log output, not just the bare message.
    formatted = "\n".join(caplog.text.splitlines())
    assert _SECRET_MARKER in formatted or _SECRET_MARKER in logged_text


# ======================================================================
# Event calendars (VEVENT)
# ======================================================================


def _make_event_obj(component=None) -> MagicMock:
    """A MagicMock standing in for a caldav Event object wrapping a real component."""
    obj = MagicMock()
    obj.icalendar_component = component if component is not None else _make_vevent()
    return obj


def _make_vevent(uid: str = "event-1", summary: str = "Meeting") -> Event:
    event = Event()
    event.add("uid", uid)
    event.add("summary", summary)
    event.add("dtstart", datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))
    return event


# --- component-aware resolution ---


def test_list_task_lists_excludes_event_only_calendars(service, principal):
    todo_cal = _make_calendar("Privat", "https://cloud.example.com/dav/privat/")
    event_cal = _make_calendar(
        "Personal", "https://cloud.example.com/dav/personal/", components=["VEVENT"]
    )
    principal.calendars.return_value = [todo_cal, event_cal]

    result = service.list_task_lists()

    assert result == [{"name": "Privat", "url": "https://cloud.example.com/dav/privat/"}]


def test_task_resolution_skips_event_calendar_with_same_name(service, principal):
    event_cal = _make_calendar("Personal", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Personal")


def test_event_resolution_skips_task_list_with_same_name(service, principal):
    todo_cal = _make_calendar("Personal", components=["VTODO"])
    principal.calendars.return_value = [todo_cal]

    with pytest.raises(CalendarNotFoundError):
        service.get_event("Personal", "event-1")


def test_same_name_todo_and_event_calendars_are_not_ambiguous(service, principal):
    """One VTODO list and one VEVENT calendar sharing a name resolve per kind."""
    todo_cal = _make_calendar("Personal", components=["VTODO"])
    event_cal = _make_calendar("Personal", components=["VEVENT"])
    todo_cal.todos.return_value = []
    event_cal.events.return_value = []
    principal.calendars.return_value = [todo_cal, event_cal]

    assert service.list_tasks("Personal") == []
    assert service.list_events(calendar_names=["Personal"]) == []


def test_mixed_component_calendar_is_reachable_from_both_sides(service, principal):
    mixed = _make_calendar("Alles", components=["VEVENT", "VTODO"])
    mixed.todos.return_value = []
    mixed.events.return_value = []
    principal.calendars.return_value = [mixed]

    assert service.list_tasks("Alles") == []
    assert service.list_events(calendar_names=["Alles"]) == []


# --- list_calendars ---


def test_list_calendars_returns_color_and_components(service, principal):
    event_cal = _make_calendar(
        "Termine", "https://cloud.example.com/dav/termine/", components=["VEVENT"]
    )
    event_cal.get_properties.return_value = {
        caldav_client_module.ical_elements.CalendarColor.tag: "#00679e"
    }
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    principal.calendars.return_value = [event_cal, todo_cal]

    result = service.list_calendars()

    assert result == [
        {
            "name": "Termine",
            "url": "https://cloud.example.com/dav/termine/",
            "farbe": "#00679e",
            "komponenten": ["VEVENT"],
        }
    ]


def test_list_calendars_survives_color_propfind_failure(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.get_properties.side_effect = RuntimeError("boom")
    principal.calendars.return_value = [event_cal]

    result = service.list_calendars()

    assert result[0]["farbe"] is None


# --- create/update/delete calendar ---


def test_create_calendar_passes_vevent_component_set(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.return_value = _make_calendar(
        "Termine", "https://cloud.example.com/dav/termine/", components=["VEVENT"]
    )

    result = service.create_calendar("Termine")

    principal.make_calendar.assert_called_once_with(
        name="Termine", cal_id="termine", supported_calendar_component_set=["VEVENT"]
    )
    assert result == {
        "name": "Termine",
        "url": "https://cloud.example.com/dav/termine/",
        "farbe": None,
    }


def test_create_calendar_sets_color(service, principal):
    principal.calendars.return_value = []
    new_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.make_calendar.return_value = new_cal

    service.create_calendar("Termine", farbe="#FF7A66")

    new_cal.set_properties.assert_called_once()


def test_create_calendar_rejects_invalid_color(service):
    with pytest.raises(InvalidEventDataError, match="farbe"):
        service.create_calendar("Termine", farbe="rot")


def test_create_calendar_name_conflict(service, principal):
    principal.calendars.return_value = [_make_calendar("Termine", components=["VEVENT"])]

    with pytest.raises(CalendarAlreadyExistsError):
        service.create_calendar("Termine")


def test_create_calendar_does_not_conflict_with_task_list_of_same_name(service, principal):
    principal.calendars.return_value = [_make_calendar("Termine", components=["VTODO"])]
    principal.make_calendar.return_value = _make_calendar("Termine", components=["VEVENT"])

    result = service.create_calendar("Termine")

    assert result["name"] == "Termine"


def test_delete_calendar_deletes(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.delete_calendar("Termine")

    event_cal.delete.assert_called_once_with()


def test_delete_calendar_not_found(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(CalendarNotFoundError):
        service.delete_calendar("Nonexistent")


def test_update_calendar_renames_and_recolors(service, principal):
    event_cal = _make_calendar(
        "Termine", "https://cloud.example.com/dav/termine/", components=["VEVENT"]
    )
    principal.calendars.return_value = [event_cal]

    result = service.update_calendar("Termine", new_display_name="Arbeit", farbe="#00679e")

    event_cal.set_properties.assert_called_once()
    (props,), _ = event_cal.set_properties.call_args
    assert len(props) == 2
    assert result["name"] == "Arbeit"


def test_update_calendar_requires_something_to_update(service):
    with pytest.raises(InvalidEventDataError, match="Nothing to update"):
        service.update_calendar("Termine")


def test_update_calendar_name_conflict(service, principal):
    principal.calendars.return_value = [
        _make_calendar("Termine", components=["VEVENT"]),
        _make_calendar("Arbeit", components=["VEVENT"]),
    ]

    with pytest.raises(CalendarAlreadyExistsError):
        service.update_calendar("Termine", new_display_name="Arbeit")


# --- event CRUD ---


def test_create_event_requires_titel_and_start(service):
    with pytest.raises(InvalidEventDataError, match="titel"):
        service.create_event("Termine", event_mapping.EventFields(start="2026-07-20T14:00:00"))
    with pytest.raises(InvalidEventDataError, match="start"):
        service.create_event("Termine", event_mapping.EventFields(titel="Meeting"))


def test_create_event_saves_serialized_vevent(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    uid = service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="Meeting", start="2026-07-20T14:00:00", ende="2026-07-20T15:00:00"
        ),
    )

    event_cal.save_event.assert_called_once()
    _, kwargs = event_cal.save_event.call_args
    ical_text = kwargs["ical"]
    assert "BEGIN:VEVENT" in ical_text
    assert "SUMMARY:Meeting" in ical_text
    assert uid in ical_text


def test_create_event_with_explicit_offset_has_no_vtimezone(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(titel="Meeting", start="2026-07-20T14:00:00+00:00"),
    )

    _, kwargs = event_cal.save_event.call_args
    assert "VTIMEZONE" not in kwargs["ical"]


def test_create_event_with_naive_start_adds_default_zone_vtimezone(service, principal):
    """A naive start now means "local time", not UTC - so it needs a VTIMEZONE too.

    Without this the naive path would be uncovered: the explicit-offset test
    above deliberately asserts the *absence* of a VTIMEZONE, which a
    regression in `keep_zone` handling would satisfy just as happily.
    """
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(titel="Meeting", start="2026-07-20T14:00:00"),
    )

    _, kwargs = event_cal.save_event.call_args
    ical_text = kwargs["ical"]
    assert "BEGIN:VTIMEZONE" in ical_text
    assert "TZID:Europe/Berlin" in ical_text
    assert "DTSTART;TZID=Europe/Berlin:20260720T140000" in ical_text


def test_create_event_with_named_zone_adds_matching_vtimezone(service, principal):
    """Regression test: a named-zone DTSTART needs its own VTIMEZONE on the
    wire (RFC 5545 3.6.5), or the TZID it references is dangling."""
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="Meeting",
            start="2026-07-20T09:00:00 Europe/Berlin",
            ende="2026-07-20T10:00:00 Europe/Berlin",
        ),
    )

    _, kwargs = event_cal.save_event.call_args
    ical_text = kwargs["ical"]
    assert "BEGIN:VTIMEZONE" in ical_text
    assert "TZID:Europe/Berlin" in ical_text
    assert "DTSTART;TZID=Europe/Berlin:20260720T090000" in ical_text
    assert ical_text.index("BEGIN:VTIMEZONE") < ical_text.index("BEGIN:VEVENT")


def test_get_event_parses_and_annotates_calendar(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = _make_event_obj()
    principal.calendars.return_value = [event_cal]

    result = service.get_event("Termine", "event-1")

    assert result["uid"] == "event-1"
    assert result["titel"] == "Meeting"
    assert result["kalender"] == "Termine"


def test_get_event_not_found(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.side_effect = caldav_error.NotFoundError("nope")
    principal.calendars.return_value = [event_cal]

    with pytest.raises(EventNotFoundError):
        service.get_event("Termine", "missing")


def test_update_event_applies_fields_and_saves(service, principal):
    component = _make_vevent()
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.update_event("Termine", "event-1", event_mapping.EventFields(ort="Büro"))

    assert str(component["location"]) == "Büro"
    event_obj.save.assert_called_once_with()


def test_update_event_with_named_zone_adds_matching_vtimezone(service, principal):
    component = _make_vevent()
    instance = Calendar()
    instance.add_component(component)
    event_obj = _make_event_obj(component)
    event_obj.icalendar_instance = instance
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.update_event(
        "Termine",
        "event-1",
        event_mapping.EventFields(start="2026-07-20T09:00:00 Europe/Berlin"),
    )

    vtimezones = [c for c in instance.subcomponents if c.name == "VTIMEZONE"]
    assert len(vtimezones) == 1
    assert str(vtimezones[0]["TZID"]) == "Europe/Berlin"
    assert instance.subcomponents.index(vtimezones[0]) < instance.subcomponents.index(component)


def test_update_event_dedupes_existing_vtimezone(service, principal):
    component = _make_vevent()
    instance = Calendar()
    instance.add_component(Timezone.from_tzinfo(ZoneInfo("Europe/Berlin"), tzid="Europe/Berlin"))
    instance.add_component(component)
    event_obj = _make_event_obj(component)
    event_obj.icalendar_instance = instance
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.update_event(
        "Termine",
        "event-1",
        event_mapping.EventFields(start="2026-07-20T09:00:00 Europe/Berlin"),
    )

    vtimezones = [c for c in instance.subcomponents if c.name == "VTIMEZONE"]
    assert len(vtimezones) == 1


def test_delete_event_deletes(service, principal):
    event_obj = _make_event_obj()
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.delete_event("Termine", "event-1")

    event_obj.delete.assert_called_once_with()


# --- list_events ---


def test_list_events_without_bounds_lists_all(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.events.return_value = [_make_event_obj()]
    principal.calendars.return_value = [event_cal]

    result = service.list_events()

    event_cal.events.assert_called_once_with()
    assert len(result) == 1
    assert result[0]["kalender"] == "Termine"


def test_list_events_with_bounds_uses_time_range_search(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    principal.calendars.return_value = [event_cal]

    service.list_events(von="2026-07-01", bis="2026-07-31")

    _, kwargs = event_cal.search.call_args
    assert kwargs["start"] == datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    # date-only `bis` is inclusive: the exclusive filter end is the next day.
    assert kwargs["end"] == datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Berlin"))
    assert kwargs["event"] is True
    assert kwargs["expand"] is False


def test_list_events_expand_requires_both_bounds(service):
    with pytest.raises(InvalidEventDataError, match="von and bis"):
        service.list_events(von="2026-07-01", expand=True)


def test_list_events_unknown_calendar_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(CalendarNotFoundError):
        service.list_events(calendar_names=["Nonexistent"])


def test_list_events_filters_by_suchtext_across_calendars(service, principal):
    cal1 = _make_calendar("Arbeit", "https://cloud.example.com/dav/a/", components=["VEVENT"])
    cal2 = _make_calendar("Privat", "https://cloud.example.com/dav/p/", components=["VEVENT"])
    cal1.events.return_value = [_make_event_obj(_make_vevent("e1", "Zahnarzt"))]
    cal2.events.return_value = [_make_event_obj(_make_vevent("e2", "Kino"))]
    principal.calendars.return_value = [cal1, cal2]

    result = service.list_events(suchtext="zahnarzt")

    assert [e["uid"] for e in result] == ["e1"]


# --- task <-> event linking ---


def test_link_task_to_event_rejects_unknown_relation(service):
    with pytest.raises(InvalidEventDataError, match="beziehung"):
        service.link_task_to_event("Privat", "t1", "Termine", "e1", beziehung="egal")


def test_link_task_to_event_writes_relation_on_event(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    component = _make_vevent()
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [todo_cal, event_cal]

    service.link_task_to_event("Privat", "task-9", "Termine", "event-1", beziehung="zeitblock")

    todo_cal.get_todo_by_uid.assert_called_once_with("task-9")
    parsed = event_mapping.parse_vevent(component)
    assert parsed["verknuepfte_aufgaben"] == [{"uid": "task-9", "beziehung": "zeitblock"}]
    event_obj.save.assert_called_once_with()


def test_link_task_to_event_missing_task_raises_before_touching_event(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.get_todo_by_uid.side_effect = caldav_error.NotFoundError("nope")
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [todo_cal, event_cal]

    with pytest.raises(TaskNotFoundError):
        service.link_task_to_event("Privat", "missing", "Termine", "event-1")
    event_cal.event_by_uid.assert_not_called()


# --- list_events_for_task ---


def _make_related_vevent(uid: str, task_uid: str | None, reltype: str = "PARENT") -> Event:
    event = _make_vevent(uid)
    if task_uid is not None:
        event.add("related-to", task_uid, parameters={"RELTYPE": reltype})
    return event


def test_list_events_for_task_returns_only_linked_events(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    linked = _make_event_obj(_make_related_vevent("event-linked", "task-1"))
    unlinked = _make_event_obj(_make_related_vevent("event-unlinked", None))
    event_cal.events.return_value = [linked, unlinked]
    principal.calendars.return_value = [todo_cal, event_cal]

    result = service.list_events_for_task("Privat", "task-1")

    todo_cal.get_todo_by_uid.assert_called_once_with("task-1")
    assert [e["uid"] for e in result] == ["event-linked"]
    assert result[0]["verknuepfte_aufgaben"] == [{"uid": "task-1", "beziehung": "zeitblock"}]
    assert result[0]["kalender_name"] == "Termine"


def test_list_events_for_task_matches_any_reltype(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.events.return_value = [
        _make_event_obj(_make_related_vevent("event-1", "task-1", reltype="CHILD"))
    ]
    principal.calendars.return_value = [todo_cal, event_cal]

    result = service.list_events_for_task("Privat", "task-1")

    assert [e["uid"] for e in result] == ["event-1"]
    assert result[0]["verknuepfte_aufgaben"] == [{"uid": "task-1", "beziehung": "voraussetzung"}]


def test_list_events_for_task_missing_task_raises(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.get_todo_by_uid.side_effect = caldav_error.NotFoundError("nope")
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [todo_cal, event_cal]

    with pytest.raises(TaskNotFoundError):
        service.list_events_for_task("Privat", "missing")
    event_cal.events.assert_not_called()


def test_list_events_for_task_searches_only_named_calendars(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    cal1 = _make_calendar("Arbeit", "https://cloud.example.com/dav/a/", components=["VEVENT"])
    cal2 = _make_calendar(
        "Privatkalender", "https://cloud.example.com/dav/p/", components=["VEVENT"]
    )
    cal1.events.return_value = [_make_event_obj(_make_related_vevent("e1", "task-1"))]
    cal2.events.return_value = [_make_event_obj(_make_related_vevent("e2", "task-1"))]
    principal.calendars.return_value = [todo_cal, cal1, cal2]

    result = service.list_events_for_task("Privat", "task-1", calendar_names=["Arbeit"])

    assert [e["uid"] for e in result] == ["e1"]
    cal2.events.assert_not_called()


def test_list_events_for_task_unknown_calendar_raises(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    principal.calendars.return_value = [todo_cal]

    with pytest.raises(CalendarNotFoundError):
        service.list_events_for_task("Privat", "task-1", calendar_names=["Nonexistent"])


def test_list_events_for_task_sorted_by_start(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    later = _make_related_vevent("event-later", "task-1")
    del later["dtstart"]
    later.add("dtstart", datetime(2026, 8, 1, tzinfo=timezone.utc))
    earlier = _make_related_vevent("event-earlier", "task-1")
    del earlier["dtstart"]
    earlier.add("dtstart", datetime(2026, 7, 1, tzinfo=timezone.utc))
    event_cal.events.return_value = [_make_event_obj(later), _make_event_obj(earlier)]
    principal.calendars.return_value = [todo_cal, event_cal]

    result = service.list_events_for_task("Privat", "task-1")

    assert [e["uid"] for e in result] == ["event-earlier", "event-later"]


# --- create_event_from_task ---


def _todo_obj(uid: str = "task-1", **fields) -> MagicMock:
    todo = Todo()
    todo.add("uid", uid)
    mapping.apply_task_fields(todo, mapping.TaskFields(**fields))
    obj = MagicMock()
    obj.icalendar_component = todo
    return obj


def test_create_event_from_task_uses_due_datetime(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.get_todo_by_uid.return_value = _todo_obj(
        titel="Steuer", faellig_datum="2026-07-20T14:00:00", notizen="Belege", ort="Zuhause"
    )
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [todo_cal, event_cal]

    uid = service.create_event_from_task("Privat", "task-1", "Termine", dauer_minuten=30)

    _, kwargs = event_cal.save_event.call_args
    ical_text = kwargs["ical"]
    assert "SUMMARY:Steuer" in ical_text
    assert "DTSTART:20260720T120000Z" in ical_text
    assert "DTEND:20260720T123000Z" in ical_text
    assert "RELATED-TO;RELTYPE=PARENT:task-1" in ical_text
    assert uid


def test_create_event_from_task_all_day_due_date(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.get_todo_by_uid.return_value = _todo_obj(titel="Steuer", faellig_datum="2026-07-20")
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [todo_cal, event_cal]

    service.create_event_from_task("Privat", "task-1", "Termine")

    _, kwargs = event_cal.save_event.call_args
    assert "DTSTART;VALUE=DATE:20260720" in kwargs["ical"]


def test_create_event_from_task_without_due_or_start_raises(service, principal):
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.get_todo_by_uid.return_value = _todo_obj(titel="Steuer")
    principal.calendars.return_value = [todo_cal]

    with pytest.raises(InvalidEventDataError, match="faellig_datum"):
        service.create_event_from_task("Privat", "task-1", "Termine")


def test_create_event_from_task_rejects_nonpositive_duration(service):
    with pytest.raises(InvalidEventDataError, match="dauer_minuten"):
        service.create_event_from_task("Privat", "task-1", "Termine", dauer_minuten=0)


# --- get_agenda ---


def test_get_agenda_requires_date_only(service):
    with pytest.raises(InvalidEventDataError, match="date-only"):
        service.get_agenda("2026-07-20T14:00:00")


def test_get_agenda_combines_events_and_due_tasks(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = [_make_event_obj()]
    todo = Todo()
    todo.add("uid", "task-1")
    todo.add("summary", "Steuer")
    todo.add("due", datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc))
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.todos.return_value = [todo_obj]
    principal.calendars.return_value = [event_cal, todo_cal]

    result = service.get_agenda("2026-07-20")

    assert result["datum"] == "2026-07-20"
    assert [e["uid"] for e in result["termine"]] == ["event-1"]
    assert [t["uid"] for t in result["aufgaben"]] == ["task-1"]
    assert result["aufgaben"][0]["liste"] == "Privat"
    # Events were queried with expand=True over exactly that day in default timezone.
    _, kwargs = event_cal.search.call_args
    assert kwargs["expand"] is True
    assert kwargs["start"] == datetime(2026, 7, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    assert kwargs["end"] == datetime(2026, 7, 21, tzinfo=ZoneInfo("Europe/Berlin"))


def test_get_agenda_excludes_tasks_due_other_days(service, principal):
    todo = Todo()
    todo.add("uid", "task-1")
    todo.add("summary", "Steuer")
    todo.add("due", datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc))
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.todos.return_value = [todo_obj]
    principal.calendars.return_value = [todo_cal]

    result = service.get_agenda("2026-07-20")

    assert result["termine"] == []
    assert result["aufgaben"] == []


def test_get_agenda_day_window_local_timezone_bounds(service, principal):
    """A task due at 00:30 local is included in that day's agenda, and a 23:30 local
    event on the previous day does not leak."""
    # 2026-07-20 00:30 Europe/Berlin = 2026-07-19 22:30 UTC
    todo = Todo()
    todo.add("uid", "task-early-local")
    todo.add("summary", "Early local task")
    todo.add("due", datetime(2026, 7, 19, 22, 30, tzinfo=timezone.utc))
    todo_obj = MagicMock()
    todo_obj.icalendar_component = todo
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.todos.return_value = [todo_obj]

    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    principal.calendars.return_value = [event_cal, todo_cal]

    result = service.get_agenda("2026-07-20")
    assert [t["uid"] for t in result["aufgaben"]] == ["task-early-local"]


def test_get_agenda_day_window_spans_dst_transition(service, principal):
    """The event query window is a real local day, 25 hours long on 2026-10-25.

    Building it by adding a fixed 24 hours would cut the last local hour off
    the fall-back day - the class of off-by-an-hour bug this whole change is
    about.
    """
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    todo_cal = _make_calendar("Privat", components=["VTODO"])
    todo_cal.todos.return_value = []
    principal.calendars.return_value = [event_cal, todo_cal]

    service.get_agenda("2026-10-25")

    _, kwargs = event_cal.search.call_args
    start, end = kwargs["start"], kwargs["end"]
    assert start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert end.isoformat() == "2026-10-26T00:00:00+01:00"
    # Subtracting two datetimes that share a tzinfo gives the *wall-clock*
    # difference (24h here), so the real length has to be measured in UTC.
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=25)


# ======================================================================
# Attendees / organizer discovery (Part A)
# ======================================================================


def test_create_event_with_teilnehmer_sets_organizer_and_attendee(service, principal):
    principal.get_vcal_address.return_value = "mailto:me@example.com"
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="Meeting",
            start="2026-07-20T14:00:00",
            teilnehmer=[{"email": "a@example.com", "name": "Alice"}],
        ),
    )

    _, kwargs = event_cal.save_event.call_args
    ical_text = kwargs["ical"]
    assert "ORGANIZER:mailto:me@example.com" in ical_text
    assert "ATTENDEE" in ical_text
    assert "mailto:a@example.com" in ical_text


def test_create_event_without_teilnehmer_does_not_discover_own_address(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine", event_mapping.EventFields(titel="Meeting", start="2026-07-20T14:00:00")
    )

    principal.get_vcal_address.assert_not_called()
    principal.calendar_user_address_set.assert_not_called()


def test_own_organizer_address_falls_back_to_calendar_user_address_set(service, principal):
    principal.get_vcal_address.side_effect = RuntimeError("not supported")
    principal.calendar_user_address_set.return_value = ["mailto:fallback@example.com"]
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="Meeting",
            start="2026-07-20T14:00:00",
            teilnehmer=[{"email": "a@example.com"}],
        ),
    )

    _, kwargs = event_cal.save_event.call_args
    assert "ORGANIZER:mailto:fallback@example.com" in kwargs["ical"]


def test_own_organizer_address_falls_back_to_username_when_everything_fails(
    mock_dav_client, principal
):
    service = CalDavService(url="https://cloud.example.com/dav/", username="alice", password="p")
    principal.get_vcal_address.side_effect = RuntimeError("nope")
    principal.calendar_user_address_set.side_effect = RuntimeError("nope")
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="Meeting",
            start="2026-07-20T14:00:00",
            teilnehmer=[{"email": "a@example.com"}],
        ),
    )

    _, kwargs = event_cal.save_event.call_args
    assert "ORGANIZER:mailto:alice" in kwargs["ical"]


def test_own_organizer_address_is_cached_across_calls(service, principal):
    principal.get_vcal_address.return_value = "mailto:me@example.com"
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [event_cal]

    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="A", start="2026-07-20T14:00:00", teilnehmer=[{"email": "a@example.com"}]
        ),
    )
    service.create_event(
        "Termine",
        event_mapping.EventFields(
            titel="B", start="2026-07-21T14:00:00", teilnehmer=[{"email": "b@example.com"}]
        ),
    )

    assert principal.get_vcal_address.call_count == 1


def test_update_event_with_teilnehmer_sets_organizer_when_absent(service, principal):
    principal.get_vcal_address.return_value = "mailto:me@example.com"
    component = _make_vevent()
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.update_event(
        "Termine",
        "event-1",
        event_mapping.EventFields(teilnehmer=[{"email": "a@example.com"}]),
    )

    assert str(component["organizer"]) == "mailto:me@example.com"
    event_obj.save.assert_called_once_with()


# ======================================================================
# respond_to_event
# ======================================================================


def _vevent_with_attendee(
    uid: str = "event-1", email: str = "me@example.com", partstat: str = "NEEDS-ACTION"
) -> Event:
    event = _make_vevent(uid)
    event.add("attendee", f"mailto:{email}", parameters={"PARTSTAT": partstat})
    return event


def test_respond_to_event_sets_partstat_and_saves(service, principal):
    principal.calendar_user_address_set.return_value = ["mailto:me@example.com"]
    component = _vevent_with_attendee()
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.respond_to_event("Termine", "event-1", "zugesagt")

    parsed = event_mapping.parse_vevent(component)
    assert parsed["teilnehmer"][0]["status"] == "zugesagt"
    event_obj.save.assert_called_once_with()


def test_respond_to_event_writes_comment(service, principal):
    principal.calendar_user_address_set.return_value = ["mailto:me@example.com"]
    component = _vevent_with_attendee()
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    service.respond_to_event("Termine", "event-1", "abgesagt", kommentar="Leider nicht")

    assert str(component.get("comment")) == "Leider nicht"


def test_respond_to_event_not_an_attendee_raises(service, principal):
    principal.calendar_user_address_set.return_value = ["mailto:me@example.com"]
    component = _vevent_with_attendee(email="other@example.com")
    event_obj = _make_event_obj(component)
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.return_value = event_obj
    principal.calendars.return_value = [event_cal]

    with pytest.raises(InvalidEventDataError, match="not listed as an attendee"):
        service.respond_to_event("Termine", "event-1", "zugesagt")

    event_obj.save.assert_not_called()


def test_respond_to_event_unknown_antwort_rejected(service):
    with pytest.raises(InvalidEventDataError, match="antwort"):
        service.respond_to_event("Termine", "event-1", "vielleicht")


def test_respond_to_event_event_not_found(service, principal):
    principal.calendar_user_address_set.return_value = ["mailto:me@example.com"]
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.event_by_uid.side_effect = caldav_error.NotFoundError("nope")
    principal.calendars.return_value = [event_cal]

    with pytest.raises(EventNotFoundError):
        service.respond_to_event("Termine", "missing", "zugesagt")


# ======================================================================
# get_free_busy (Part B)
# ======================================================================


def _make_freebusy_obj(component) -> MagicMock:
    obj = MagicMock()
    obj.icalendar_component = component
    return obj


def test_get_free_busy_own_availability_aggregates_and_merges(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    busy_event = _make_vevent("event-1")
    busy_event.add("dtend", datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc))
    cancelled_event = _make_vevent("event-2")
    cancelled_event.add("status", "CANCELLED")
    event_cal.search.return_value = [
        _make_event_obj(busy_event),
        _make_event_obj(cancelled_event),
    ]
    principal.calendars.return_value = [event_cal]

    result = service.get_free_busy("2026-07-20", "2026-07-21")

    assert result["benutzer"] is None
    assert result["belegt"] == [
        {"von": "2026-07-20T16:00:00+02:00", "bis": "2026-07-20T17:00:00+02:00"}
    ]


def test_get_free_busy_own_availability_queries_with_bounds(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    principal.calendars.return_value = [event_cal]

    service.get_free_busy("2026-07-20", "2026-07-21")

    _, kwargs = event_cal.search.call_args
    assert kwargs["start"] == datetime(2026, 7, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    # date-only `bis` is inclusive of the whole day, so the exclusive filter
    # end is the start of the *next* day (same convention as list_events).
    assert kwargs["end"] == datetime(2026, 7, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    assert kwargs["event"] is True


def test_get_free_busy_returns_normalized_bounds(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.return_value = []
    principal.calendars.return_value = [event_cal]

    result = service.get_free_busy("2026-07-20", "2026-07-21")

    assert result["von"] == "2026-07-20T00:00:00+02:00"
    assert result["bis"] == "2026-07-22T00:00:00+02:00"


def test_get_free_busy_own_availability_translates_generic_exception(service, principal):
    event_cal = _make_calendar("Termine", components=["VEVENT"])
    event_cal.search.side_effect = RuntimeError("boom")
    principal.calendars.return_value = [event_cal]

    with pytest.raises(TaskMcpError):
        service.get_free_busy("2026-07-20", "2026-07-21")


def test_get_free_busy_for_other_user_queries_scheduling_outbox(service, principal):
    vfb = FreeBusy()
    vfb.add(
        "freebusy",
        [
            (
                datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
            )
        ],
        parameters={"FBTYPE": "BUSY"},
    )
    principal.freebusy_request.return_value = {"mailto:bob@example.com": _make_freebusy_obj(vfb)}

    result = service.get_free_busy("2026-07-20", "2026-07-21", benutzer="bob@example.com")

    args, _ = principal.freebusy_request.call_args
    assert args[0] == datetime(2026, 7, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    assert args[1] == datetime(2026, 7, 22, tzinfo=ZoneInfo("Europe/Berlin"))
    assert args[2] == ["mailto:bob@example.com"]
    assert result["benutzer"] == "bob@example.com"
    assert result["belegt"] == [
        {"von": "2026-07-20T11:00:00+02:00", "bis": "2026-07-20T12:00:00+02:00"}
    ]


def test_get_free_busy_for_other_user_accepts_mailto_prefixed_benutzer(service, principal):
    vfb = FreeBusy()
    principal.freebusy_request.return_value = {"mailto:bob@example.com": _make_freebusy_obj(vfb)}

    service.get_free_busy("2026-07-20", "2026-07-21", benutzer="mailto:bob@example.com")

    args, _ = principal.freebusy_request.call_args
    assert args[2] == ["mailto:bob@example.com"]


def test_get_free_busy_for_other_user_bare_key_response(service, principal):
    vfb = FreeBusy()
    principal.freebusy_request.return_value = {"bob@example.com": _make_freebusy_obj(vfb)}

    result = service.get_free_busy("2026-07-20", "2026-07-21", benutzer="bob@example.com")

    assert result["belegt"] == []


def test_get_free_busy_for_other_user_error_response_raises_clean_error(service, principal):
    principal.freebusy_request.return_value = {
        "errors": {"mailto:bob@example.com": "3.7;Invalid Calendar User"}
    }

    with pytest.raises(TaskMcpError, match="bob@example.com"):
        service.get_free_busy("2026-07-20", "2026-07-21", benutzer="bob@example.com")


def test_get_free_busy_for_other_user_empty_response_raises(service, principal):
    principal.freebusy_request.return_value = {}

    with pytest.raises(TaskMcpError):
        service.get_free_busy("2026-07-20", "2026-07-21", benutzer="bob@example.com")


def test_get_free_busy_for_other_user_translates_generic_exception(service, principal):
    principal.freebusy_request.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.get_free_busy("2026-07-20", "2026-07-21", benutzer="bob@example.com")


# --- occupied collection ids are dodged (Nextcloud trashbin) ---


def test_create_task_list_retries_with_suffixed_id_when_slug_occupied(service, principal):
    """A trashbin remnant occupying the slug URI must not block re-creation."""
    principal.calendars.return_value = []
    new_calendar = _make_calendar("Groceries")
    principal.make_calendar.side_effect = [
        caldav_error.MkcolError("405 Method Not Allowed"),
        new_calendar,
    ]

    result = service.create_task_list("Groceries")

    assert result["name"] == "Groceries"
    assert principal.make_calendar.call_count == 2
    _, kwargs = principal.make_calendar.call_args
    assert kwargs["cal_id"] == "groceries-2"


def test_create_calendar_retries_with_suffixed_id_when_slug_occupied(service, principal):
    principal.calendars.return_value = []
    new_calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.make_calendar.side_effect = [
        caldav_error.MkcalendarError("409 Conflict"),
        caldav_error.MkcalendarError("409 Conflict"),
        new_calendar,
    ]

    result = service.create_calendar("Termine")

    assert result["name"] == "Termine"
    _, kwargs = principal.make_calendar.call_args
    assert kwargs["cal_id"] == "termine-3"


def test_create_task_list_gives_up_when_all_candidate_ids_occupied(service, principal):
    principal.calendars.return_value = []
    principal.make_calendar.side_effect = caldav_error.MkcolError("405 Method Not Allowed")

    with pytest.raises(TaskListAlreadyExistsError, match="collection id"):
        service.create_task_list("Groceries")


def test_translate_rate_limit_error_names_waiting_as_fix():
    translated = _translate(
        caldav_error.RateLimitError("RateLimitError at 'https://x/', reason ...")
    )
    assert isinstance(translated, TaskMcpError)
    assert "rate-limit" in str(translated).lower() or "rate limit" in str(translated).lower()
    assert "retry" in str(translated).lower()
    # The raw URL/exception text must not leak into the client-facing message.
    assert "https://x/" not in str(translated)


# ======================================================================
# Calendar sharing (Nextcloud DAV extension)
# ======================================================================


def _dav_response(status: int, xml: str | None = None) -> SimpleNamespace:
    """A stand-in for `caldav.response.DAVResponse` - `_dav_request`'s callers
    only ever look at `.status` and `.tree`."""
    tree = etree.fromstring(xml.encode("utf-8")) if xml else None
    return SimpleNamespace(status=status, tree=tree)


@pytest.fixture
def dav_client(service, mock_dav_client) -> MagicMock:
    """`service._client` itself, with a real `.url` set (the mock's default
    auto-generated attribute doesn't behave like a caldav URL object, but
    `_trashbin_objects_url`/`_trashbin_restore_url` need `.join()` on it)."""
    client = mock_dav_client.return_value
    client.url = URL.objectify("https://cloud.example.com/dav/")
    return client


_INVITE_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/privat/</d:href>
    <d:propstat>
      <d:prop>
        <oc:invite>
          <oc:user>
            <d:href>principal:principals/users/bob</d:href>
            <oc:common-name>Bob</oc:common-name>
            <oc:invite-accepted/>
            <oc:access><oc:read-write/></oc:access>
          </oc:user>
          <oc:user>
            <d:href>principal:principals/groups/team</d:href>
            <oc:invite-noresponse/>
            <oc:access><oc:read/></oc:access>
          </oc:user>
          <oc:organizer>
            <d:href>principal:principals/users/owner</d:href>
          </oc:organizer>
        </oc:invite>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_share_calendar_posts_share_xml_with_read_write(service, principal, dav_client):
    calendar = _make_calendar(
        "Privat", "https://cloud.example.com/dav/privat/", components=["VEVENT"]
    )
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(200)

    result = service.share_calendar("Privat", "bob", schreibzugriff=True)

    assert result == {"kalender_name": "Privat", "empfaenger": "bob", "schreibzugriff": True}
    args, _ = dav_client.request.call_args
    url, method, body, headers = args
    assert url == "https://cloud.example.com/dav/privat/"
    assert method == "POST"
    assert headers["Content-Type"].startswith("application/xml")
    tree = etree.fromstring(body.encode("utf-8"))
    assert tree.find(".//{DAV:}href").text == "principal:principals/users/bob"
    assert tree.find(".//{http://owncloud.org/ns}read-write") is not None
    assert tree.find(".//{http://owncloud.org/ns}set") is not None


def test_share_calendar_read_only_omits_read_write_element(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(200)

    service.share_calendar("Privat", "bob")

    args, _ = dav_client.request.call_args
    tree = etree.fromstring(args[2].encode("utf-8"))
    assert tree.find(".//{http://owncloud.org/ns}read-write") is None


def test_share_calendar_group_uses_groups_principal_href(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(200)

    service.share_calendar("Privat", "team", gruppe=True)

    args, _ = dav_client.request.call_args
    tree = etree.fromstring(args[2].encode("utf-8"))
    assert tree.find(".//{DAV:}href").text == "principal:principals/groups/team"


def test_share_calendar_resolves_task_lists_too(service, principal, dav_client):
    calendar = _make_calendar("Aufgaben", components=["VTODO"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(200)

    result = service.share_calendar("Aufgaben", "bob")

    assert result["kalender_name"] == "Aufgaben"


def test_share_calendar_requires_empfaenger(service, principal, dav_client):
    with pytest.raises(TaskMcpError, match="empfaenger is required"):
        service.share_calendar("Privat", "")


def test_share_calendar_not_found_across_both_kinds(service, principal, dav_client):
    principal.calendars.return_value = []

    with pytest.raises(TaskMcpError, match="was not found"):
        service.share_calendar("Ghost", "bob")


def test_share_calendar_unknown_recipient_404_raises_clean_error(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(404)

    with pytest.raises(TaskMcpError, match="could not find user/group 'ghost'"):
        service.share_calendar("Privat", "ghost")


def test_share_calendar_forbidden_raises_clean_permission_error(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.side_effect = caldav_error.AuthorizationError("403 Forbidden")

    with pytest.raises(TaskMcpError, match="permission denied"):
        service.share_calendar("Privat", "bob")


def test_share_calendar_unexpected_status_raises_clean_error(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(500)

    with pytest.raises(TaskMcpError, match="HTTP 500"):
        service.share_calendar("Privat", "bob")


def test_share_calendar_invalid_request_400_raises_clean_error(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(400)

    with pytest.raises(TaskMcpError, match="invalid"):
        service.share_calendar("Privat", "not a valid id!!")


def test_unshare_calendar_posts_remove_xml(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(200)

    service.unshare_calendar("Privat", "bob")

    args, _ = dav_client.request.call_args
    tree = etree.fromstring(args[2].encode("utf-8"))
    assert tree.find(".//{http://owncloud.org/ns}remove") is not None
    assert tree.find(".//{DAV:}href").text == "principal:principals/users/bob"
    # A remove element carries no access/summary children.
    assert tree.find(".//{http://owncloud.org/ns}read-write") is None


def test_unshare_calendar_requires_empfaenger(service, principal, dav_client):
    with pytest.raises(TaskMcpError, match="empfaenger is required"):
        service.unshare_calendar("Privat", "")


def test_list_calendar_shares_parses_users_and_groups(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(207, _INVITE_XML)

    result = service.list_calendar_shares("Privat")

    assert result == [
        {"empfaenger": "bob", "typ": "benutzer", "schreibzugriff": True, "status": "akzeptiert"},
        {"empfaenger": "team", "typ": "gruppe", "schreibzugriff": False, "status": "ausstehend"},
    ]


def test_list_calendar_shares_unknown_invite_status_falls_back_to_raw_lowercase(
    service, principal, dav_client
):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/privat/</d:href>
    <d:propstat>
      <d:prop>
        <oc:invite>
          <oc:user>
            <d:href>principal:principals/users/bob</d:href>
            <oc:invite-mystery/>
            <oc:access><oc:read/></oc:access>
          </oc:user>
        </oc:invite>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(207, xml)

    result = service.list_calendar_shares("Privat")

    assert result == [
        {"empfaenger": "bob", "typ": "benutzer", "schreibzugriff": False, "status": "mystery"}
    ]


def test_list_calendar_shares_no_invitees_returns_empty_list(service, principal, dav_client):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/privat/</d:href>
    <d:propstat>
      <d:prop><oc:invite/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(207, xml)

    assert service.list_calendar_shares("Privat") == []


def test_list_calendar_shares_unexpected_status_raises_clean_error(service, principal, dav_client):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    dav_client.request.return_value = _dav_response(500)

    with pytest.raises(TaskMcpError, match="unexpected error"):
        service.list_calendar_shares("Privat")


# ======================================================================
# Trash bin (Nextcloud calendar-trashbin DAV plugin)
# ======================================================================


_TRASHED_TODO_ICS = (
    "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VTODO\nUID:t1\nSUMMARY:Einkaufen\n"
    "END:VTODO\nEND:VCALENDAR\n"
)

_TRASHBIN_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:nc="http://nextcloud.com/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/trashbin/objects/</d:href>
    <d:propstat>
      <d:prop/>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/u/trashbin/objects/42.ics</d:href>
    <d:propstat>
      <d:prop>
        <nc:deleted-at>1752000000</nc:deleted-at>
        <nc:calendar-uri>personal</nc:calendar-uri>
        <c:calendar-data>{_TRASHED_TODO_ICS}</c:calendar-data>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_list_trash_parses_items_including_deleted_at_and_type(service, dav_client):
    dav_client.request.return_value = _dav_response(207, _TRASHBIN_XML)

    result = service.list_trash()

    assert result == [
        {
            "id": "42.ics",
            "titel": "Einkaufen",
            "typ": "aufgabe",
            "kalender": "personal",
            "geloescht_am": mapping.format_datetime_output(
                datetime.fromtimestamp(1752000000, tz=timezone.utc)
            ),
        }
    ]
    args, _ = dav_client.request.call_args
    url, method, body, headers = args
    assert url == "https://cloud.example.com/dav/calendars/u/trashbin/objects/"
    # A calendar-query REPORT, not PROPFIND: Nextcloud answers a Depth-1
    # PROPFIND on trashbin/objects/ with 501 Not Implemented (issue #13).
    assert method == "REPORT"
    assert headers["Depth"] == "1"
    assert "calendar-query" in body
    assert 'comp-filter name="VCALENDAR"' in body


def test_list_trash_missing_props_default_to_none(service, dav_client):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:nc="http://nextcloud.com/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/trashbin/objects/7.ics</d:href>
    <d:propstat>
      <d:prop/>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    dav_client.request.return_value = _dav_response(207, xml)

    result = service.list_trash()

    assert result == [
        {"id": "7.ics", "titel": None, "typ": None, "kalender": None, "geloescht_am": None}
    ]


def test_list_trash_falls_back_to_displayname_when_no_calendar_data(service, dav_client):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:nc="http://nextcloud.com/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/trashbin/objects/8.ics</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Einkaufen (trashed)</d:displayname>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    dav_client.request.return_value = _dav_response(207, xml)

    result = service.list_trash()

    assert result[0]["titel"] == "Einkaufen (trashed)"
    assert result[0]["typ"] is None


def test_list_trash_deleted_at_accepts_iso8601_too(service, dav_client):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:nc="http://nextcloud.com/ns">
  <d:response>
    <d:href>/remote.php/dav/calendars/u/trashbin/objects/9.ics</d:href>
    <d:propstat>
      <d:prop>
        <nc:deleted-at>2026-07-10T12:00:00+00:00</nc:deleted-at>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""
    dav_client.request.return_value = _dav_response(207, xml)

    result = service.list_trash()

    assert result[0]["geloescht_am"] == "2026-07-10T14:00:00+02:00"


def test_list_trash_not_available_translates_404_to_clean_error(service, dav_client):
    dav_client.request.return_value = _dav_response(404)

    with pytest.raises(TaskMcpError, match="not available on this server"):
        service.list_trash()


def test_list_trash_405_also_translates_to_not_available(service, dav_client):
    dav_client.request.return_value = _dav_response(405)

    with pytest.raises(TaskMcpError, match="not available on this server"):
        service.list_trash()


def test_list_trash_unexpected_status_raises_clean_error(service, dav_client):
    dav_client.request.return_value = _dav_response(500)

    with pytest.raises(TaskMcpError, match="unexpected error"):
        service.list_trash()


def test_restore_from_trash_moves_with_destination_header(service, dav_client):
    dav_client.request.return_value = _dav_response(204)

    result = service.restore_from_trash("42.ics")

    assert result is None
    args, _ = dav_client.request.call_args
    url, method, _, headers = args
    assert url == "https://cloud.example.com/dav/calendars/u/trashbin/objects/42.ics"
    assert method == "MOVE"
    assert (
        headers["Destination"]
        == "https://cloud.example.com/dav/calendars/u/trashbin/restore/42.ics"
    )


def test_restore_from_trash_not_found_raises_clean_error(service, dav_client):
    dav_client.request.return_value = _dav_response(404)

    with pytest.raises(TaskMcpError, match="was not found in the trash bin"):
        service.restore_from_trash("999.ics")


def test_restore_from_trash_not_available_translates_405(service, dav_client):
    dav_client.request.return_value = _dav_response(405)

    with pytest.raises(TaskMcpError, match="not available on this server"):
        service.restore_from_trash("42.ics")


def test_restore_from_trash_unexpected_status_raises_clean_error(service, dav_client):
    dav_client.request.return_value = _dav_response(500)

    with pytest.raises(TaskMcpError, match="HTTP 500"):
        service.restore_from_trash("42.ics")


def test_restore_from_trash_requires_id(service, dav_client):
    with pytest.raises(TaskMcpError, match="id is required"):
        service.restore_from_trash("")


# ======================================================================
# ICS import / export
# ======================================================================


def _make_calendar_obj(instance: Calendar) -> MagicMock:
    """A MagicMock standing in for a caldav CalendarObjectResource whose
    `icalendar_instance` is the full VCALENDAR (event/todo + any VTIMEZONEs
    and recurrence overrides sharing its URL), matching what the real caldav
    library returns for `.events()`/`.todos()` entries."""
    obj = MagicMock()
    obj.icalendar_instance = instance
    return obj


def _wrap_in_vcalendar(*components: Any) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//test//")
    cal.add("version", "2.0")
    for component in components:
        cal.add_component(component)
    return cal


_ICS_WITH_TZ = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
DTSTART;TZID=Europe/Berlin:20260720T140000
DTEND;TZID=Europe/Berlin:20260720T150000
END:VEVENT
END:VCALENDAR
"""


def test_export_calendar_merges_events_and_todos_into_one_vcalendar(service, principal):
    calendar = _make_calendar("Privat", components=["VEVENT", "VTODO"])
    principal.calendars.return_value = [calendar]

    event = _make_vevent("event-1", "Meeting")
    todo = Todo()
    todo.add("uid", "task-1")
    todo.add("summary", "Einkaufen")
    calendar.events.return_value = [_make_calendar_obj(_wrap_in_vcalendar(event))]
    calendar.todos.return_value = [_make_calendar_obj(_wrap_in_vcalendar(todo))]

    result = service.export_calendar("Privat")

    assert result["kalender_name"] == "Privat"
    parsed = Calendar.from_ical(result["ics"])
    assert parsed.name == "VCALENDAR"
    assert str(parsed.get("version")) == "2.0"
    kinds = sorted(str(c.name) for c in parsed.subcomponents)
    assert kinds == ["VEVENT", "VTODO"]
    calendar.todos.assert_called_once_with(include_completed=True)


def test_export_calendar_only_queries_supported_components(service, principal):
    calendar = _make_calendar("Aufgaben", components=["VTODO"])
    principal.calendars.return_value = [calendar]
    todo = Todo()
    todo.add("uid", "task-1")
    todo.add("summary", "Einkaufen")
    calendar.todos.return_value = [_make_calendar_obj(_wrap_in_vcalendar(todo))]

    result = service.export_calendar("Aufgaben")

    calendar.events.assert_not_called()
    parsed = Calendar.from_ical(result["ics"])
    assert [c.name for c in parsed.subcomponents] == ["VTODO"]


def test_export_calendar_dedups_vtimezone_by_tzid(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    calendar.events.return_value = [
        _make_calendar_obj(Calendar.from_ical(_ICS_WITH_TZ.format(uid="e1", summary="Eins"))),
        _make_calendar_obj(Calendar.from_ical(_ICS_WITH_TZ.format(uid="e2", summary="Zwei"))),
    ]
    calendar.todos.return_value = []

    result = service.export_calendar("Termine")

    parsed = Calendar.from_ical(result["ics"])
    tz_components = [c for c in parsed.subcomponents if c.name == "VTIMEZONE"]
    event_components = [c for c in parsed.subcomponents if c.name == "VEVENT"]
    assert len(tz_components) == 1
    assert len(event_components) == 2


def test_export_calendar_not_found_across_both_kinds(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskMcpError, match="was not found"):
        service.export_calendar("Ghost")


def test_export_calendar_events_not_found_becomes_clean_error(service, principal):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    calendar.events.side_effect = caldav_error.NotFoundError("gone")

    with pytest.raises(TaskMcpError, match="was not found"):
        service.export_calendar("Privat")


def test_export_calendar_unexpected_error_is_translated(service, principal):
    calendar = _make_calendar("Privat", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    calendar.events.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.export_calendar("Privat")


def test_import_ics_saves_one_calendar_object_with_its_timezone(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [calendar]

    result = service.import_ics("Termine", _ICS_WITH_TZ.format(uid="e1", summary="Eins"))

    assert result == {"kalender_name": "Termine", "importiert": 1, "uebersprungen": 0}
    calendar.save_event.assert_called_once()
    _, kwargs = calendar.save_event.call_args
    assert "BEGIN:VEVENT" in kwargs["ical"]
    assert "BEGIN:VTIMEZONE" in kwargs["ical"]


def test_import_ics_recurring_overrides_share_one_calendar_object(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//
BEGIN:VEVENT
UID:series-1
SUMMARY:Weekly
DTSTART:20260720T140000Z
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:series-1
SUMMARY:Weekly (moved)
DTSTART:20260727T160000Z
RECURRENCE-ID:20260727T140000Z
END:VEVENT
END:VCALENDAR
"""

    result = service.import_ics("Termine", ics)

    assert result == {"kalender_name": "Termine", "importiert": 1, "uebersprungen": 0}
    calendar.save_event.assert_called_once()
    _, kwargs = calendar.save_event.call_args
    assert kwargs["ical"].count("BEGIN:VEVENT") == 2


def test_import_ics_skips_unsupported_component_kind(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])  # no VTODO support
    principal.calendars.return_value = [calendar]
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//
BEGIN:VTODO
UID:task-1
SUMMARY:Einkaufen
END:VTODO
END:VCALENDAR
"""

    result = service.import_ics("Termine", ics)

    assert result == {"kalender_name": "Termine", "importiert": 0, "uebersprungen": 1}
    calendar.save_event.assert_not_called()
    calendar.save_todo.assert_not_called()


def test_import_ics_mixed_kinds_partially_skipped(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//
BEGIN:VEVENT
UID:e1
SUMMARY:Meeting
DTSTART:20260720T140000Z
END:VEVENT
BEGIN:VTODO
UID:t1
SUMMARY:Einkaufen
END:VTODO
END:VCALENDAR
"""

    result = service.import_ics("Termine", ics)

    assert result == {"kalender_name": "Termine", "importiert": 1, "uebersprungen": 1}
    calendar.save_event.assert_called_once()
    calendar.save_todo.assert_not_called()


def test_import_ics_saves_vtodo_into_a_task_list(service, principal):
    calendar = _make_calendar("Aufgaben", components=["VTODO"])
    principal.calendars.return_value = [calendar]
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//
BEGIN:VTODO
UID:t1
SUMMARY:Einkaufen
END:VTODO
END:VCALENDAR
"""

    result = service.import_ics("Aufgaben", ics)

    assert result == {"kalender_name": "Aufgaben", "importiert": 1, "uebersprungen": 0}
    calendar.save_todo.assert_called_once()
    _, kwargs = calendar.save_todo.call_args
    assert "BEGIN:VTODO" in kwargs["ical"]


def test_import_ics_save_error_is_translated(service, principal):
    calendar = _make_calendar("Termine", components=["VEVENT"])
    principal.calendars.return_value = [calendar]
    calendar.save_event.side_effect = RuntimeError("boom")

    with pytest.raises(TaskMcpError):
        service.import_ics("Termine", _ICS_WITH_TZ.format(uid="e1", summary="Eins"))


def test_import_ics_invalid_ics_raises_clean_error_with_parse_detail(service, principal):
    with pytest.raises(InvalidIcsDataError, match="Could not parse ics"):
        service.import_ics("Termine", "not a valid ics at all {{{")


def test_import_ics_requires_at_least_one_event_or_todo(service, principal):
    ics = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//\nEND:VCALENDAR\n"

    with pytest.raises(InvalidIcsDataError, match="at least one VEVENT or VTODO"):
        service.import_ics("Termine", ics)


def test_import_ics_rejects_non_vcalendar_top_level(service, principal):
    with pytest.raises(InvalidIcsDataError, match="VCALENDAR"):
        service.import_ics("Termine", "BEGIN:VEVENT\nUID:x\nEND:VEVENT\n")


def test_import_ics_empty_string_raises_clean_error(service, principal):
    with pytest.raises(InvalidIcsDataError, match="required"):
        service.import_ics("Termine", "")


def test_import_ics_calendar_not_found_across_both_kinds(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskMcpError, match="was not found"):
        service.import_ics("Ghost", _ICS_WITH_TZ.format(uid="e1", summary="Eins"))


# ======================================================================
# Batched per-collection metadata (supported-component-set + color)
#
# Regression guard for the per-tool-call latency fix: `_supports_component`
# and `list_calendars`'s color lookup must read from ONE Depth-1 PROPFIND
# over the calendar-home-set, not a PROPFIND per calendar (caldav's
# `get_supported_components()` / color `get_properties()`).
# ======================================================================


_COLLECTION_META_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
               xmlns:ical="http://apple.com/ns/ical/">
  <d:response>
    <d:href>/dav/calendars/u/personal/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Personal</d:displayname>
        <c:supported-calendar-component-set>
          <c:comp name="VTODO"/>
        </c:supported-calendar-component-set>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/calendars/u/arbeit/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Arbeit</d:displayname>
        <c:supported-calendar-component-set>
          <c:comp name="VEVENT"/>
        </c:supported-calendar-component-set>
        <ical:calendar-color>#FF0000FF</ical:calendar-color>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def _personal_and_arbeit() -> tuple[MagicMock, MagicMock]:
    personal = _make_calendar("Personal", "https://cloud.example.com/dav/calendars/u/personal/")
    arbeit = _make_calendar(
        "Arbeit", "https://cloud.example.com/dav/calendars/u/arbeit/", components=["VEVENT"]
    )
    return personal, arbeit


def test_list_task_lists_uses_batched_metadata_not_per_calendar_propfind(
    service, principal, dav_client
):
    personal, arbeit = _personal_and_arbeit()
    principal.calendars.return_value = [personal, arbeit]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)

    result = service.list_task_lists()

    # Only the VTODO collection is returned, resolved from the batch.
    assert result == [
        {"name": "Personal", "url": "https://cloud.example.com/dav/calendars/u/personal/"}
    ]
    # The component support came from the single batched PROPFIND, not from a
    # per-calendar caldav lookup.
    personal.get_supported_components.assert_not_called()
    arbeit.get_supported_components.assert_not_called()
    # Exactly one PROPFIND, over the calendar-home-set, at Depth 1.
    assert dav_client.request.call_count == 1
    args, _ = dav_client.request.call_args
    assert args[0] == "https://cloud.example.com/dav/calendars/u/"
    assert args[1] == "PROPFIND"
    assert args[3]["Depth"] == "1"


def test_list_calendars_reads_color_from_batched_metadata(service, principal, dav_client):
    personal, arbeit = _personal_and_arbeit()
    principal.calendars.return_value = [personal, arbeit]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)

    result = service.list_calendars()

    assert result == [
        {
            "name": "Arbeit",
            "url": "https://cloud.example.com/dav/calendars/u/arbeit/",
            "farbe": "#FF0000FF",
            "komponenten": ["VEVENT"],
        }
    ]
    # Color came from the batch, not a per-calendar CalendarColor PROPFIND.
    arbeit.get_properties.assert_not_called()
    personal.get_supported_components.assert_not_called()
    arbeit.get_supported_components.assert_not_called()


def test_collection_metadata_is_cached_across_calls(service, principal, dav_client):
    personal, arbeit = _personal_and_arbeit()
    principal.calendars.return_value = [personal, arbeit]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)

    service.list_task_lists()
    service.list_task_lists()

    # The metadata PROPFIND runs once for the process, not once per call.
    assert dav_client.request.call_count == 1


def test_collection_metadata_invalidated_after_create(service, principal, dav_client):
    personal, arbeit = _personal_and_arbeit()
    principal.calendars.return_value = [personal, arbeit]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)

    service.list_task_lists()
    assert dav_client.request.call_count == 1

    principal.make_calendar.return_value = _make_calendar(
        "Groceries", "https://cloud.example.com/dav/calendars/u/groceries/"
    )
    service.create_task_list("Groceries")

    # Creating a collection drops the cache, so the next listing re-fetches.
    service.list_task_lists()
    assert dav_client.request.call_count == 2


def test_supports_component_falls_back_when_calendar_absent_from_batch(
    service, principal, dav_client
):
    # A calendar whose href isn't in the batched response (e.g. a subscription
    # collection elsewhere) still resolves via caldav's per-calendar lookup.
    stray = _make_calendar(
        "Extern", "https://other.example.com/dav/feeds/holidays/", components=["VEVENT"]
    )
    stray.get_properties.return_value = {}
    principal.calendars.return_value = [stray]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)

    result = service.list_calendars()

    assert result == [
        {
            "name": "Extern",
            "url": "https://other.example.com/dav/feeds/holidays/",
            "farbe": None,
            "komponenten": ["VEVENT"],
        }
    ]
    stray.get_supported_components.assert_called()


def test_collection_list_is_cached_across_calls(service, principal):
    principal.calendars.return_value = [_make_calendar("Personal")]

    service.list_task_lists()
    service.list_task_lists()

    # `principal.calendars()` (two PROPFINDs in caldav) runs once, not per call.
    assert principal.calendars.call_count == 1


def test_collection_list_refetched_after_create(service, principal):
    principal.calendars.return_value = [_make_calendar("Personal")]

    service.list_task_lists()
    service.list_task_lists()
    assert principal.calendars.call_count == 1

    principal.make_calendar.return_value = _make_calendar(
        "Groceries", "https://cloud.example.com/dav/calendars/u/groceries/"
    )
    service.create_task_list("Groceries")  # fresh list for the conflict check
    service.list_task_lists()  # cache was invalidated by the create

    assert principal.calendars.call_count == 3


def test_collection_list_refetched_after_rename(service, principal):
    cal = _make_calendar("Alt", "https://cloud.example.com/dav/calendars/u/alt/")
    principal.calendars.return_value = [cal]

    service.list_task_lists()
    assert principal.calendars.call_count == 1

    service.rename_task_list("Alt", "Neu")  # fresh list + invalidation
    service.list_task_lists()

    assert principal.calendars.call_count == 3


# ======================================================================
# Collection cache TTL - bounded staleness for out-of-band changes
#
# `_collections`/`_collection_meta` are invalidated immediately when *this*
# process creates/renames/deletes a collection (covered above), but a
# rename/delete made through the Nextcloud web UI (or any other client) is
# invisible to that invalidation. `_COLLECTION_CACHE_TTL_SECONDS` bounds how
# long such a change can go unnoticed. The clock is driven via monkeypatch
# (never time.sleep) by replacing the `monotonic` name `caldav_client`
# imported into its own module namespace.
# ======================================================================


def test_collection_list_reused_within_ttl_even_as_clock_advances(service, principal, monkeypatch):
    principal.calendars.return_value = [_make_calendar("Personal")]
    fake_now = 1_000.0
    monkeypatch.setattr(caldav_client_module, "monotonic", lambda: fake_now)

    service.list_task_lists()
    fake_now += caldav_client_module._COLLECTION_CACHE_TTL_SECONDS - 1
    monkeypatch.setattr(caldav_client_module, "monotonic", lambda: fake_now)
    service.list_task_lists()

    # Still within the TTL - no second principal.calendars() PROPFIND.
    assert principal.calendars.call_count == 1


def test_collection_renamed_server_side_stops_being_served_under_old_name_after_ttl(
    service, principal, monkeypatch
):
    """A collection renamed outside this process (e.g. in the Nextcloud web UI)
    is still served under its old name while the cache is fresh, but the next
    access past the TTL re-fetches and sees the rename - this is what stops a
    vanished/renamed project from being served forever (the reported bug's
    root cause)."""
    old = _make_calendar("CSGO", "https://cloud.example.com/dav/csgo/")
    fake_now = 10_000.0
    monkeypatch.setattr(caldav_client_module, "monotonic", lambda: fake_now)
    principal.calendars.return_value = [old]

    assert service.list_task_lists() == [
        {"name": "CSGO", "url": "https://cloud.example.com/dav/csgo/"}
    ]
    assert principal.calendars.call_count == 1

    # Renamed server-side (outside this process) - same URL, new name.
    renamed = _make_calendar("Esports-Archiv", "https://cloud.example.com/dav/csgo/")
    principal.calendars.return_value = [renamed]

    # Still inside the TTL window: the stale listing is reused.
    fake_now += caldav_client_module._COLLECTION_CACHE_TTL_SECONDS - 1
    assert service.list_task_lists() == [
        {"name": "CSGO", "url": "https://cloud.example.com/dav/csgo/"}
    ]
    assert principal.calendars.call_count == 1

    # Past the TTL: the next access re-fetches and the rename is visible.
    fake_now += 2
    assert service.list_task_lists() == [
        {"name": "Esports-Archiv", "url": "https://cloud.example.com/dav/csgo/"}
    ]
    assert principal.calendars.call_count == 2


def test_reused_display_name_stops_hitting_the_old_collection_after_ttl(
    service, principal, monkeypatch
):
    """The nastiest staleness: a freed-up name handed to a different collection.

    A cache hit on `_calendar_cache` short-circuits resolution entirely, so
    the collection-list TTL never gets a say. And because the old collection
    still exists (it was renamed, not deleted), nothing 404s and the
    stale-cache retry in `_with_collection` never fires either - so before
    this entry had a TTL of its own, every lookup of the reused name kept
    answering from the wrong collection for the life of the process.
    """
    old = _make_calendar("CSGO", "https://cloud.example.com/dav/csgo-old/")
    old.todos.return_value = [_todo_obj("from-old", titel="Alt")]
    fake_now = 20_000.0
    monkeypatch.setattr(caldav_client_module, "monotonic", lambda: fake_now)
    principal.calendars.return_value = [old]

    assert [t["uid"] for t in service.list_tasks(list_names=["CSGO"])] == ["from-old"]

    # Web UI: the old list is renamed away, and a brand-new list takes the
    # name it just vacated.
    old.get_display_name.return_value = "CSGO-Archiv"
    new = _make_calendar("CSGO", "https://cloud.example.com/dav/csgo-new/")
    new.todos.return_value = [_todo_obj("from-new", titel="Neu")]
    principal.calendars.return_value = [old, new]

    # Inside the TTL the old entry is still served - bounded staleness.
    fake_now += caldav_client_module._COLLECTION_CACHE_TTL_SECONDS - 1
    assert [t["uid"] for t in service.list_tasks(list_names=["CSGO"])] == ["from-old"]

    # Past it, the name resolves to the collection that actually bears it now.
    fake_now += 2
    assert [t["uid"] for t in service.list_tasks(list_names=["CSGO"])] == ["from-new"]


def test_collection_metadata_reused_within_ttl_then_refetched_after(
    service, principal, dav_client, monkeypatch
):
    personal, arbeit = _personal_and_arbeit()
    principal.calendars.return_value = [personal, arbeit]
    dav_client.request.return_value = _dav_response(207, _COLLECTION_META_XML)
    fake_now = 5_000.0
    monkeypatch.setattr(caldav_client_module, "monotonic", lambda: fake_now)

    service.list_task_lists()
    assert dav_client.request.call_count == 1

    fake_now += caldav_client_module._COLLECTION_CACHE_TTL_SECONDS - 1
    service.list_task_lists()
    assert dav_client.request.call_count == 1

    fake_now += 2
    service.list_task_lists()
    assert dav_client.request.call_count == 2


# ======================================================================
# get_agenda / list_events / list_tasks / export_calendar cross-check
#
# Regression guard for the reported bug: `get_agenda` returned entries for a
# project ("CSGO") that no other tool (`list_tasks`, `get_task`,
# `export_calendar`) could find, and on the same day silently dropped a real
# task due exactly then. Builds an account where a task list and an
# (unrelated) event calendar share the display name "CSGO" - Nextcloud does
# not enforce cross-collection name uniqueness, and this is the exact shape
# of the original incident - then checks `get_agenda` never disagrees with
# `list_events`/`list_tasks` called directly for the same day, and that every
# entry is traceable to a real, currently-existing collection.
# ======================================================================


def test_get_agenda_matches_list_events_and_list_tasks_for_duplicate_named_collections(
    service, principal
):
    day = "2026-07-20"

    csgo_tasks = _make_calendar(
        "CSGO", "https://cloud.example.com/dav/csgo-tasks/", components=["VTODO"]
    )
    csgo_events = _make_calendar(
        "CSGO", "https://cloud.example.com/dav/csgo-events/", components=["VEVENT"]
    )
    principal.calendars.return_value = [csgo_tasks, csgo_events]

    # A task due at the very start of the local day - the class of task the
    # reported bug silently dropped from the agenda.
    early_task = _todo_obj("task-early", titel="Frueh faellig", faellig_datum="2026-07-20T00:30:00")
    csgo_tasks.todos.return_value = [early_task]
    csgo_tasks.get_todo_by_uid.return_value = early_task

    all_day = Event()
    all_day.add("uid", "event-all-day")
    event_mapping.apply_event_fields(
        all_day, event_mapping.EventFields(titel="Feiertag", start="2026-07-20", ende="2026-07-20")
    )

    recurring = Event()
    recurring.add("uid", "event-recurring")
    event_mapping.apply_event_fields(
        recurring,
        event_mapping.EventFields(
            titel="Weekly Sync",
            start="2026-07-20T09:00:00",
            ende="2026-07-20T10:00:00",
            wiederholung="FREQ=WEEKLY",
            ausnahme_daten=["2026-07-27T09:00:00"],
        ),
    )
    csgo_events.search.return_value = [_make_event_obj(all_day), _make_event_obj(recurring)]
    # export_calendar fetches unfiltered via .events() (not the time-range
    # .search() above), reading each object's full `icalendar_instance` (not
    # just `icalendar_component`, see `_make_calendar_obj`) - both must
    # reflect the same two events for the traceability check below.
    csgo_events.events.return_value = [
        _make_calendar_obj(_wrap_in_vcalendar(all_day)),
        _make_calendar_obj(_wrap_in_vcalendar(recurring)),
    ]

    agenda = service.get_agenda(day)
    direct_events = service.list_events(von=day, bis=day, expand=True)
    direct_tasks = service.list_tasks(due_before=day, due_after=day, only_open=True)

    # Same UIDs, whichever way they're queried.
    assert (
        {e["uid"] for e in agenda["termine"]}
        == {e["uid"] for e in direct_events}
        == {
            "event-all-day",
            "event-recurring",
        }
    )
    assert (
        {t["uid"] for t in agenda["aufgaben"]} == {t["uid"] for t in direct_tasks} == {"task-early"}
    )

    # Both queries used the exact same day window - not just coincidentally
    # matching return values.
    calls = csgo_events.search.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["start"] == calls[1].kwargs["start"]
    assert calls[0].kwargs["end"] == calls[1].kwargs["end"]
    assert calls[0].kwargs["expand"] is True and calls[1].kwargs["expand"] is True

    # get_agenda (unlike list_events/list_tasks) adds quelle_url, naming the
    # exact collection each entry came from - what makes a duplicate-named
    # collection's entries traceable instead of a guess.
    for event in agenda["termine"]:
        assert event["kalender"] == "CSGO"
        assert event["quelle_url"] == "https://cloud.example.com/dav/csgo-events/"
    for task in agenda["aufgaben"]:
        assert task["liste"] == "CSGO"
        assert task["quelle_url"] == "https://cloud.example.com/dav/csgo-tasks/"

    # quelle_url is agenda-only - list_events/list_tasks keep their existing
    # return shape.
    assert all("quelle_url" not in e for e in direct_events)
    assert all("quelle_url" not in t for t in direct_tasks)

    # Every event UID get_agenda returned really exists in the named
    # calendar's own export - not a phantom. (export_calendar resolves a
    # shared display name to its VEVENT collection first, see
    # `_resolve_collection_any`, so this reaches "CSGO"'s event calendar
    # specifically, not the task list of the same name.)
    exported_events = service.export_calendar("CSGO")["ics"]
    for event in agenda["termine"]:
        assert event["uid"] in exported_events

    # The task side is independently confirmed the same way `get_task` would
    # be used to chase down a suspicious agenda entry: resolving "CSGO" as a
    # task list (component-specific, so the same-named event calendar next to
    # it is never in play) and finding the exact task get_agenda reported.
    for task in agenda["aufgaben"]:
        found = service.get_task("CSGO", task["uid"])
        assert found["uid"] == task["uid"]
