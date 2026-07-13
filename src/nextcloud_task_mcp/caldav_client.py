"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from caldav.collection import Calendar as DAVCalendar
from caldav.collection import Principal as DAVPrincipal
from caldav.davclient import DAVClient
from caldav.elements import dav
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
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Runs of anything that isn't an ASCII letter/digit collapse to a single
# hyphen, so "Groceries & Errands!" -> "groceries-errands" (leading/trailing
# hyphens stripped separately, below).
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(display_name: str) -> str:
    """Derive a URL-safe CalDAV collection id from a task list's display name.

    Lowercases, collapses runs of non-alphanumeric characters to a single
    hyphen, and strips leading/trailing hyphens. Falls back to a random id
    if that leaves nothing usable (e.g. a name that's all emoji/CJK/etc. -
    non-ASCII scripts have no case-folded alphanumeric equivalent here).
    """
    slug = _SLUG_INVALID_CHARS.sub("-", display_name.strip().lower()).strip("-")
    return slug or f"list-{uuid.uuid4().hex[:8]}"


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
        self._client = DAVClient(
            url=url,
            username=username,
            password=password,
            timeout=timeout,
            # A5: without this, caldav 3.2.1 raises RateLimitError immediately
            # on a 429/503 response instead of backing off - a transient,
            # server-side "slow down" turns into a hard failure for every
            # caller. `rate_limit_handle=True` makes it sleep (honoring the
            # server's Retry-After header when present, falling back to
            # `rate_limit_default_sleep` otherwise) and retry instead;
            # `rate_limit_max_sleep` caps how long any single wait can be, so
            # a server asking for an extreme Retry-After can't stall a tool
            # call indefinitely. These are caldav's own built-in retry
            # mechanism - deliberately not reimplemented here.
            rate_limit_handle=True,
            rate_limit_default_sleep=5,
            rate_limit_max_sleep=60,
        )
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

    def create_task_list(self, display_name: str) -> dict[str, str]:
        """Create a new Nextcloud task list (a CalDAV calendar collection supporting VTODO).

        The collection id (the last path segment of its URL) is derived from
        `display_name` via `_slugify` rather than left to caldav's own
        default (a random UUID) - a human still has to look at this URL in
        Nextcloud's web UI or a CalDAV client, so a readable id is worth
        generating deliberately.

        Two conflict cases are both rejected rather than silently handled,
        mirroring `_resolve_calendar`'s "don't guess" stance on ambiguous
        names: another list already has this exact display name (checked
        proactively via `principal.calendars()`, before ever attempting the
        server-side create), or the generated collection id happens to
        collide with an existing collection (surfaces as a 405/409 from the
        MKCOL/MKCALENDAR request itself, since two different display names
        can slugify to the same id).

        Returns:
            {"name": display name, "url": internal CalDAV URL} for the new
            list, matching one entry of `list_task_lists`'s return value.
        """
        if not display_name or not display_name.strip():
            raise InvalidTaskDataError("display_name is required to create a task list.")

        slug = _slugify(display_name)

        with self._lock:
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            if any(calendar.get_display_name() == display_name for calendar in existing):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{display_name}' already exists."
                )

            try:
                calendar = principal.make_calendar(
                    name=display_name,
                    cal_id=slug,
                    supported_calendar_component_set=["VTODO"],
                )
            except (caldav_error.MkcolError, caldav_error.MkcalendarError) as exc:
                # 405 (Method Not Allowed) is what a MKCOL/MKCALENDAR against
                # an already-existing collection URL returns; 409 (Conflict)
                # is the same idea from servers that respond differently.
                # Anything else here is a genuine, unrelated CalDAV failure.
                if "405" in str(exc) or "409" in str(exc):
                    raise TaskListAlreadyExistsError(
                        f"A task list with id '{slug}' already exists on the server. "
                        "Try a different display name."
                    ) from exc
                logger.warning("CalDAV request failed creating task list", exc_info=exc)
                raise TaskMcpError("The CalDAV request failed on the Nextcloud server.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            self._calendar_cache[display_name] = calendar
            return {"name": display_name, "url": str(calendar.url)}

    def delete_task_list(self, list_name: str) -> None:
        """Permanently delete a Nextcloud task list and every task inside it.

        This is irreversible from this API's point of view: deleting the
        underlying CalDAV calendar collection deletes all VTODOs it contains
        along with it (the server may retain them in a trashbin, but this
        client has no way to recover them). Callers should confirm with the
        user before calling this.

        Resolution goes through the same (cached) `_with_calendar` path as
        `delete_task`/`update_task`/etc., so a `list_name` that isn't
        currently cached costs one `principal.calendars()` PROPFIND, and a
        stale cache entry (list already deleted/recreated server-side) is
        retried once against a fresh resolution before giving up (A3).
        """

        def op(calendar: DAVCalendar) -> None:
            calendar.delete()

        with self._lock:
            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            # The list is gone - drop it from the cache so a later call
            # with this name resolves fresh instead of reusing a deleted
            # calendar's (now-invalid) object.
            self._calendar_cache.pop(list_name, None)

    def rename_task_list(self, list_name: str, new_display_name: str) -> dict[str, str]:
        """Rename a Nextcloud task list (change its CalDAV displayname property).

        Only the display name changes - the collection's URL/id is left
        alone, so any client that referenced the list by URL is unaffected.

        Mirrors `create_task_list`'s "don't guess" stance on name conflicts:
        `principal.calendars()` is fetched fresh (not from the cache) so both
        resolving `list_name` and checking `new_display_name` for conflicts
        see the current server state in one round-trip. Renaming a list to
        the name it already has is a no-op success rather than a
        self-conflict; renaming it to a name some *other* list already has
        raises `TaskListAlreadyExistsError` instead of silently producing two
        identically-named lists (which `_resolve_calendar` would then report
        as ambiguous).

        Returns:
            {"name": new display name, "url": internal CalDAV URL} for the
            renamed list, matching one entry of `list_task_lists`'s return
            value.
        """
        if not new_display_name or not new_display_name.strip():
            raise InvalidTaskDataError("new_display_name is required to rename a task list.")

        with self._lock:
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            matches = [c for c in existing if c.get_display_name() == list_name]
            if not matches:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.")
            if len(matches) > 1:
                raise TaskMcpError(
                    f"Multiple task lists are named '{list_name}', which is ambiguous. "
                    "Rename the task lists in Nextcloud so each has a distinct name, or "
                    "use a different, unambiguous list name."
                )
            calendar = matches[0]

            if new_display_name != list_name and any(
                c.get_display_name() == new_display_name for c in existing
            ):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{new_display_name}' already exists."
                )

            try:
                calendar.set_properties([dav.DisplayName(new_display_name)])
            except Exception as exc:
                raise _translate(exc) from exc

            self._calendar_cache.pop(list_name, None)
            self._calendar_cache[new_display_name] = calendar
            return {"name": new_display_name, "url": str(calendar.url)}

    def list_tasks(
        self,
        list_name: str,
        only_open: bool = True,
        due_before: str | None = None,
        due_after: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return tasks in the given list, parsed into German task dicts.

        `due_before`/`due_after`/`limit` filter the already-parsed results via
        `mapping.filter_tasks` (C4) - see its docstring for the exact
        semantics (date-vs-datetime bound normalization, no-due-date
        exclusion, and `limit` validation).
        """
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
            tasks = [mapping.parse_vtodo(todo.icalendar_component) for todo in todos]
            return mapping.filter_tasks(
                tasks, due_before=due_before, due_after=due_after, limit=limit
            )

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
