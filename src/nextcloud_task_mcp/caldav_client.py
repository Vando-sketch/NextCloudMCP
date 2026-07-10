"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from caldav.collection import Calendar as DAVCalendar
from caldav.collection import Principal as DAVPrincipal
from caldav.davclient import DAVClient
from caldav.lib import error as caldav_error
from icalendar import Calendar, Todo

# caldav's top-level `caldav.DAVClient`/`DAVPrincipal`/`DAVCalendar`
# are exposed via PEP 562 module-level lazy imports (see caldav/__init__.py),
# which mypy cannot resolve as concrete classes usable in annotations
# ("Variable is not valid as a type"). Importing the same classes directly
# from their defining submodules sidesteps that - same runtime objects,
# just statically resolvable.

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
    InvalidTaskDataError,
    TaskConflictError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


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
        self._client = DAVClient(url=url, username=username, password=password, timeout=timeout)
        self._principal: DAVPrincipal | None = None
        # A1 moves CalDavService calls onto worker threads (via
        # anyio.to_thread.run_sync in server.py) so they no longer block the
        # asyncio event loop - but that means calls can now genuinely run
        # concurrently against the single shared DAVClient/HTTP session. This
        # lock serializes the actual CalDAV operations (folding in the old
        # principal-only lock) to keep that access correct; it intentionally
        # trades away parallel Nextcloud access for correctness, while still
        # keeping the event loop itself free to serve other requests. It also
        # guards `_calendar_cache` below (A3).
        self._lock = threading.RLock()
        # Resolving a calendar by display name costs a full PROPFIND + linear
        # scan (`principal.calendars()`) on every call. Cache resolved
        # calendars by display name so repeat calls for the same list_name
        # skip that round-trip entirely (A3). Guarded by `_lock`, like
        # everything else that touches CalDAV state.
        self._calendar_cache: dict[str, DAVCalendar] = {}

    def _get_principal(self) -> DAVPrincipal:
        with self._lock:
            if self._principal is None:
                try:
                    self._principal = self._client.principal()
                except Exception as exc:
                    raise _translate(exc) from exc
            return self._principal

    def _resolve_calendar(self, list_name: str) -> DAVCalendar:
        """Resolve `list_name` to a calendar via a fresh `principal.calendars()` call.

        Raises `TaskListNotFoundError` if no calendar has that display name,
        or a generic `TaskMcpError` if more than one does - a duplicate
        display name is genuinely ambiguous, so callers are told to rename
        their lists rather than have the server silently pick one (A3).
        """
        try:
            calendars = self._get_principal().calendars()
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        matches = [c for c in calendars if c.get_display_name() == list_name]
        if not matches:
            raise TaskListNotFoundError(f"Task list '{list_name}' was not found.")
        if len(matches) > 1:
            raise TaskMcpError(
                f"Multiple task lists are named '{list_name}', which is ambiguous. "
                "Rename the task lists in Nextcloud so each has a distinct name, or "
                "use a different, unambiguous list name."
            )
        return matches[0]

    def _resolve_and_cache(self, list_name: str) -> DAVCalendar:
        calendar = self._resolve_calendar(list_name)
        self._calendar_cache[list_name] = calendar
        return calendar

    def _get_calendar(self, list_name: str) -> DAVCalendar:
        cached = self._calendar_cache.get(list_name)
        if cached is not None:
            return cached
        return self._resolve_and_cache(list_name)

    def _with_calendar(self, list_name: str, fn: Callable[[DAVCalendar], _T]) -> _T:
        """Resolve `list_name`'s (cached) calendar and call `fn(calendar)`.

        `fn` should perform raw caldav operations without translating
        `caldav_error.NotFoundError` itself: a cached calendar may have gone
        stale (the list was deleted/renamed server-side since it was
        cached), which surfaces as that same NotFoundError on the actual
        request. On that specific error, the stale cache entry is dropped
        and resolution is retried exactly once with a fresh
        `principal.calendars()` call before giving up (A3) - this keeps the
        common case cheap while still recovering from a stale cache instead
        of failing (or silently misbehaving) forever.
        """
        calendar = self._get_calendar(list_name)
        try:
            return fn(calendar)
        except caldav_error.NotFoundError:
            self._calendar_cache.pop(list_name, None)
            calendar = self._resolve_and_cache(list_name)
            return fn(calendar)

    def list_task_lists(self) -> list[dict[str, str]]:
        """Return all calendars on the account as {"name", "url"} dicts."""
        with self._lock:
            try:
                calendars = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            names = [calendar.get_display_name() or str(calendar.url) for calendar in calendars]
            name_counts: dict[str, int] = {}
            for name in names:
                name_counts[name] = name_counts.get(name, 0) + 1
            # Populate the resolution cache opportunistically (A3), but only
            # for names that are actually unambiguous - caching one of
            # several same-named calendars would silently hide the
            # ambiguity that `_resolve_calendar` is supposed to surface.
            for calendar, name in zip(calendars, names, strict=True):
                if name_counts[name] == 1:
                    self._calendar_cache[name] = calendar

            return [
                {"name": name, "url": str(calendar.url)}
                for calendar, name in zip(calendars, names, strict=True)
            ]

    def list_tasks(self, list_name: str, only_open: bool = True) -> list[dict[str, Any]]:
        """Return all tasks in the given list, parsed into German task dicts."""
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.todos(include_completed=not only_open)

            try:
                todos = self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return [mapping.parse_vtodo(todo.icalendar_component) for todo in todos]

    def create_task(self, list_name: str, fields: mapping.TaskFields) -> str:
        """Create a new task in the given list and return its UID."""
        if fields.titel is None:
            raise InvalidTaskDataError("titel is required to create a task.")
        with self._lock:
            new_uid = str(uuid.uuid4())
            todo = Todo()
            todo.add("uid", new_uid)
            todo.add("dtstamp", datetime.now(timezone.utc))
            mapping.apply_task_fields(todo, fields)

            vcal = Calendar()
            vcal.add("prodid", "-//nextcloud-task-mcp//EN")
            vcal.add("version", "2.0")
            vcal.add_component(todo)
            ical_text = vcal.to_ical().decode("utf-8")

            def op(calendar: DAVCalendar):
                calendar.save_todo(ical=ical_text)

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return new_uid

    def update_task(self, list_name: str, task_uid: str, fields: mapping.TaskFields) -> None:
        """Update only the given (non-None) fields of an existing task."""
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                mapping.apply_task_fields(todo_obj.icalendar_component, fields)
                todo_obj.save()

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def get_task(self, list_name: str, task_uid: str) -> dict[str, Any]:
        """Return a single task, parsed into the server's German task dict."""
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.get_todo_by_uid(task_uid)

            try:
                todo_obj = self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return mapping.parse_vtodo(todo_obj.icalendar_component)

    def complete_task(self, list_name: str, task_uid: str) -> None:
        """Mark a task as completed (STATUS, PERCENT-COMPLETE, COMPLETED timestamp)."""
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                mapping.mark_completed(todo_obj.icalendar_component)
                todo_obj.save()

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def delete_task(self, list_name: str, task_uid: str) -> None:
        """Permanently delete a task."""
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                todo_obj.delete()

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
