"""Unit tests for CalDavService with the caldav library itself mocked out."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from caldav.lib import error as caldav_error
from icalendar import Todo

from nextcloud_task_mcp import caldav_client as caldav_client_module
from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.caldav_client import CalDavService, _translate
from nextcloud_task_mcp.errors import (
    AuthenticationFailedError,
    ConnectionFailedError,
    InvalidTaskDataError,
    TaskConflictError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)


def _make_calendar(name: str, url: str = "https://cloud.example.com/dav/personal/") -> MagicMock:
    """A MagicMock standing in for a caldav.Calendar with the given display name."""
    calendar = MagicMock()
    calendar.get_display_name.return_value = name
    calendar.url = url
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
            "fällig_datum": None,
            "priorität": None,
            "fortschritt_prozent": 0,
            "status": "offen",
            "ort": None,
            "url": None,
            "tags": [],
            "notizen": None,
            "übergeordnete_uid": None,
            "wiederholung": None,
        }
    ]


def test_list_tasks_list_not_found_raises(service, principal):
    principal.calendars.return_value = []

    with pytest.raises(TaskListNotFoundError):
        service.list_tasks("Nonexistent")


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
