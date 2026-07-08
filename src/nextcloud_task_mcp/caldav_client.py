"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import caldav
from caldav.lib import error as caldav_error
from icalendar import Calendar, Todo

try:
    # caldav 3.x uses niquests (a requests-API-compatible client) by default,
    # falling back to requests if niquests isn't installed.
    from niquests import exceptions as _http_errors
except ImportError:  # pragma: no cover - depends on caldav's installed backend
    from requests import exceptions as _http_errors  # type: ignore[no-redef]

from . import mapping
from .errors import (
    AuthenticationFailedError,
    ConnectionFailedError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)


def _translate(exc: Exception) -> TaskMcpError:
    """Convert a caldav/requests exception into a clean, user-facing TaskMcpError."""
    if isinstance(exc, caldav_error.AuthorizationError):
        return AuthenticationFailedError(
            "Nextcloud rejected the CalDAV credentials (check username/app password)."
        )
    if isinstance(exc, caldav_error.NotFoundError):
        return TaskMcpError("The requested resource was not found.")
    if isinstance(exc, caldav_error.DAVError):
        return TaskMcpError(f"CalDAV request failed: {exc}")
    if isinstance(exc, (_http_errors.ConnectionError, _http_errors.Timeout)):
        return ConnectionFailedError(
            "Could not reach the Nextcloud server (connection refused or timed out)."
        )
    if isinstance(exc, _http_errors.RequestException):
        return ConnectionFailedError(f"CalDAV network request failed: {exc}")
    return TaskMcpError(f"Unexpected error talking to Nextcloud: {exc}")


class CalDavService:
    """Holds one reused CalDAV connection and exposes task CRUD operations on it."""

    def __init__(self, url: str, username: str, password: str) -> None:
        self._client = caldav.DAVClient(url=url, username=username, password=password)
        self._principal: caldav.Principal | None = None
        self._principal_lock = threading.Lock()

    def _get_principal(self) -> caldav.Principal:
        with self._principal_lock:
            if self._principal is None:
                try:
                    self._principal = self._client.principal()
                except Exception as exc:
                    raise _translate(exc) from exc
            return self._principal

    def _get_calendar(self, list_name: str) -> caldav.Calendar:
        try:
            return self._get_principal().calendar(name=list_name)
        except caldav_error.NotFoundError as exc:
            raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

    def _get_todo(self, calendar: caldav.Calendar, task_uid: str):
        try:
            return calendar.get_todo_by_uid(task_uid)
        except caldav_error.NotFoundError as exc:
            raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
        except Exception as exc:
            raise _translate(exc) from exc

    def list_task_lists(self) -> list[dict[str, str]]:
        """Return all calendars on the account as {"name", "url"} dicts."""
        try:
            calendars = self._get_principal().calendars()
            return [
                {"name": calendar.get_display_name() or str(calendar.url), "url": str(calendar.url)}
                for calendar in calendars
            ]
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

    def list_tasks(self, list_name: str, only_open: bool = True) -> list[dict[str, Any]]:
        """Return all tasks in the given list, parsed into German task dicts."""
        calendar = self._get_calendar(list_name)
        try:
            todos = calendar.todos(include_completed=not only_open)
        except Exception as exc:
            raise _translate(exc) from exc
        return [mapping.parse_vtodo(todo.icalendar_component) for todo in todos]

    def create_task(
        self,
        list_name: str,
        *,
        titel: str,
        start_datum: str | None = None,
        faellig_datum: str | None = None,
        prioritaet: str | None = None,
        fortschritt_prozent: int | None = None,
        ort: str | None = None,
        url: str | None = None,
        tags: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        notizen: str | None = None,
        sichtbarkeit: str | None = None,
        uebergeordnete_aufgabe: str | None = None,
    ) -> str:
        """Create a new task in the given list and return its UID."""
        calendar = self._get_calendar(list_name)

        new_uid = str(uuid.uuid4())
        todo = Todo()
        todo.add("uid", new_uid)
        todo.add("dtstamp", datetime.now(timezone.utc))
        mapping.apply_task_fields(
            todo,
            titel=titel,
            start_datum=start_datum,
            faellig_datum=faellig_datum,
            prioritaet=prioritaet,
            fortschritt_prozent=fortschritt_prozent,
            ort=ort,
            url=url,
            tags=tags,
            erinnerungen=erinnerungen,
            notizen=notizen,
            sichtbarkeit=sichtbarkeit,
            uebergeordnete_aufgabe=uebergeordnete_aufgabe,
        )

        vcal = Calendar()
        vcal.add("prodid", "-//nextcloud-task-mcp//EN")
        vcal.add("version", "2.0")
        vcal.add_component(todo)

        try:
            calendar.save_todo(ical=vcal.to_ical().decode("utf-8"))
        except Exception as exc:
            raise _translate(exc) from exc
        return new_uid

    def update_task(
        self,
        list_name: str,
        task_uid: str,
        *,
        titel: str | None = None,
        start_datum: str | None = None,
        faellig_datum: str | None = None,
        prioritaet: str | None = None,
        fortschritt_prozent: int | None = None,
        ort: str | None = None,
        url: str | None = None,
        tags: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        notizen: str | None = None,
        sichtbarkeit: str | None = None,
        uebergeordnete_aufgabe: str | None = None,
    ) -> None:
        """Update only the given (non-None) fields of an existing task."""
        calendar = self._get_calendar(list_name)
        todo_obj = self._get_todo(calendar, task_uid)
        mapping.apply_task_fields(
            todo_obj.icalendar_component,
            titel=titel,
            start_datum=start_datum,
            faellig_datum=faellig_datum,
            prioritaet=prioritaet,
            fortschritt_prozent=fortschritt_prozent,
            ort=ort,
            url=url,
            tags=tags,
            erinnerungen=erinnerungen,
            notizen=notizen,
            sichtbarkeit=sichtbarkeit,
            uebergeordnete_aufgabe=uebergeordnete_aufgabe,
        )
        try:
            todo_obj.save()
        except Exception as exc:
            raise _translate(exc) from exc

    def complete_task(self, list_name: str, task_uid: str) -> None:
        """Mark a task as completed (STATUS, PERCENT-COMPLETE, COMPLETED timestamp)."""
        calendar = self._get_calendar(list_name)
        todo_obj = self._get_todo(calendar, task_uid)
        mapping.mark_completed(todo_obj.icalendar_component)
        try:
            todo_obj.save()
        except Exception as exc:
            raise _translate(exc) from exc

    def delete_task(self, list_name: str, task_uid: str) -> None:
        """Permanently delete a task."""
        calendar = self._get_calendar(list_name)
        todo_obj = self._get_todo(calendar, task_uid)
        try:
            todo_obj.delete()
        except Exception as exc:
            raise _translate(exc) from exc
