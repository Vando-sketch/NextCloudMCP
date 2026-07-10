"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import logging
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
    TaskConflictError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)


def _translate(exc: Exception) -> TaskMcpError:
    """Convert a caldav/requests exception into a clean, user-facing TaskMcpError.

    Messages returned here are forwarded verbatim to MCP clients (see
    `server.py`'s `_call`), so they must never embed raw exception text that
    could leak library/server internals (D7). Branches below that can't be
    reduced to an already-safe, specific message log the real exception
    server-side (`exc_info=True`) and return a categorized generic message
    instead.
    """
    if isinstance(exc, caldav_error.AuthorizationError):
        return AuthenticationFailedError(
            "Nextcloud rejected the CalDAV credentials (check username/app password)."
        )
    if isinstance(exc, caldav_error.NotFoundError):
        return TaskMcpError("The requested resource was not found.")
    # Must be checked before the generic DAVError branch below, since
    # ETagMismatchError is a DAVError subclass (A4). caldav sends `If-Match`
    # and raises this on HTTP 412 when the task changed since it was last
    # read - that's an actionable, distinct condition, not a generic failure.
    if isinstance(exc, caldav_error.ETagMismatchError):
        return TaskConflictError(
            "The task was modified by another client since it was last read "
            "(conflicting edit). Re-fetch the task and retry."
        )
    if isinstance(exc, caldav_error.DAVError):
        logger.warning("CalDAV request failed", exc_info=exc)
        return TaskMcpError("The CalDAV request failed on the Nextcloud server.")
    if isinstance(exc, (_http_errors.ConnectionError, _http_errors.Timeout)):
        return ConnectionFailedError(
            "Could not reach the Nextcloud server (connection refused or timed out)."
        )
    if isinstance(exc, _http_errors.RequestException):
        logger.warning("CalDAV network request failed", exc_info=exc)
        return ConnectionFailedError("A network error occurred talking to Nextcloud.")
    logger.warning("Unexpected error talking to Nextcloud", exc_info=exc)
    return TaskMcpError("An unexpected error occurred talking to Nextcloud.")


class CalDavService:
    """Holds one reused CalDAV connection and exposes task CRUD operations on it."""

    def __init__(self, url: str, username: str, password: str, timeout: int = 30) -> None:
        self._client = caldav.DAVClient(
            url=url, username=username, password=password, timeout=timeout
        )
        self._principal: caldav.Principal | None = None
        # A1 moves CalDavService calls onto worker threads (via
        # anyio.to_thread.run_sync in server.py) so they no longer block the
        # asyncio event loop - but that means calls can now genuinely run
        # concurrently against the single shared DAVClient/HTTP session. This
        # lock serializes the actual CalDAV operations (folding in the old
        # principal-only lock) to keep that access correct; it intentionally
        # trades away parallel Nextcloud access for correctness, while still
        # keeping the event loop itself free to serve other requests.
        self._lock = threading.RLock()

    def _get_principal(self) -> caldav.Principal:
        with self._lock:
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
        with self._lock:
            try:
                calendars = self._get_principal().calendars()
                return [
                    {
                        "name": calendar.get_display_name() or str(calendar.url),
                        "url": str(calendar.url),
                    }
                    for calendar in calendars
                ]
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

    def list_tasks(self, list_name: str, only_open: bool = True) -> list[dict[str, Any]]:
        """Return all tasks in the given list, parsed into German task dicts."""
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            calendar = self._get_calendar(list_name)
            todo_obj = self._get_todo(calendar, task_uid)
            mapping.mark_completed(todo_obj.icalendar_component)
            try:
                todo_obj.save()
            except Exception as exc:
                raise _translate(exc) from exc

    def delete_task(self, list_name: str, task_uid: str) -> None:
        """Permanently delete a task."""
        with self._lock:
            calendar = self._get_calendar(list_name)
            todo_obj = self._get_todo(calendar, task_uid)
            try:
                todo_obj.delete()
            except Exception as exc:
                raise _translate(exc) from exc
