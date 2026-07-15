"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from typing import Any, TypeVar

from caldav.collection import Calendar as DAVCalendar
from caldav.collection import Principal as DAVPrincipal
from caldav.davclient import DAVClient
from caldav.elements import dav
from caldav.elements import ical as ical_elements
from caldav.lib import error as caldav_error
from icalendar import Calendar, Event, Todo

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

from . import event_mapping, mapping
from .errors import (
    AuthenticationFailedError,
    CalendarAlreadyExistsError,
    CalendarNotFoundError,
    ConnectionFailedError,
    EventNotFoundError,
    InvalidEventDataError,
    InvalidTaskDataError,
    TaskConflictError,
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Nextcloud stores the calendar color as "#RRGGBB" or "#RRGGBBAA" (the Apple
# calendar-color extension property). Anything else is rejected up front so a
# typo can't end up as an unparseable property on the server.
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")

# Fallback bounds for one-sided time-range queries: CalDAV time-range filters
# technically allow an open side, but caldav's search() + expand handling is
# only well-defined with both ends present, so an omitted bound is widened to
# a range that comfortably covers any real-world calendar instead.
_RANGE_MIN = datetime(1901, 1, 1, tzinfo=timezone.utc)
_RANGE_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)

# The two supported task<->event link semantics, mapped to the RELATED-TO
# RELTYPE written on the *event* (never on the task - a RELATED-TO added to a
# VTODO would make Nextcloud Tasks render the task as a subtask of a
# non-task, garbling its UI; the event side has no such interpretation):
#   "zeitblock":      the event reserves time for the task (event = child).
#   "voraussetzung":  the event must happen before the task (event = parent).
_LINK_RELTYPES: dict[str, str] = {"zeitblock": "PARENT", "voraussetzung": "CHILD"}

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
    # Also a DAVError subclass, so checked before the generic branch. caldav's
    # built-in backoff (rate_limit_handle, see __init__) retries 429/503
    # transparently; this error only surfaces once those retries are
    # exhausted, i.e. the server is enforcing a longer window. Nextcloud does
    # this by design for calendar *creation* (~10 new calendars per user per
    # hour), so the message names waiting as the fix rather than reading like
    # a server defect.
    if isinstance(exc, caldav_error.RateLimitError):
        return TaskMcpError(
            "Nextcloud is rate-limiting these requests (HTTP 429/503). This is "
            "expected after creating many calendars/task lists in a short time "
            "(Nextcloud allows roughly 10 new calendars per hour). Wait a while "
            "and retry."
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
        self._username = username
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
        # calendars by (component, display name) so repeat calls for the same
        # name skip that round-trip entirely (A3). The component is part of
        # the key because Nextcloud keeps task lists (VTODO) and event
        # calendars (VEVENT) in the same DAV namespace, and the same display
        # name may legitimately exist once per kind. Guarded by `_lock`, like
        # everything else that touches CalDAV state.
        self._calendar_cache: dict[tuple[str, str], DAVCalendar] = {}
        # Lazily discovered and cached like the calendar cache above (A3's
        # reasoning applies equally here): the caller's own address(es) don't
        # change during the lifetime of one CalDavService, so there is no
        # reason to re-run the principal PROPFIND(s) on every create_event
        # call that adds attendees. Guarded by `_lock`.
        self._own_organizer_address: str | None = None
        self._own_calendar_user_addresses: list[str] | None = None

    def _get_principal(self) -> DAVPrincipal:
        with self._lock:
            if self._principal is None:
                try:
                    self._principal = self._client.principal()
                except Exception as exc:
                    raise _translate(exc) from exc
            return self._principal

    def _get_own_organizer_address(self) -> str:
        """The caller's own "mailto:..." address, used to fill in ORGANIZER
        the first time attendees are added to an event (see
        `event_mapping.apply_event_fields`'s `own_organizer` parameter).
        """
        with self._lock:
            if self._own_organizer_address is None:
                self._own_organizer_address = self._discover_own_organizer_address()
            return self._own_organizer_address

    def _discover_own_organizer_address(self) -> str:
        """Best-effort discovery of the caller's own scheduling address.

        Tries `principal.get_vcal_address()` first (the caldav library's own
        helper for this, built on calendar-user-address-set), then falls back
        to the mailto entries of `principal.calendar_user_address_set()`
        directly. Both are RFC 6638 properties that a CalDAV server may not
        expose (or may expose empty) - if neither yields anything, this falls
        back to a `mailto:<username>` guess rather than failing outright,
        since the caller's own username is at least a plausible address on
        most Nextcloud instances (username == email is common), and an event
        can still be created without a perfect ORGANIZER.
        """
        principal = self._get_principal()
        try:
            address = str(principal.get_vcal_address()).strip()
            if address:
                return address
        except Exception:
            logger.debug("principal.get_vcal_address() unavailable", exc_info=True)
        try:
            for addr in principal.calendar_user_address_set() or []:
                if addr and str(addr).strip().lower().startswith("mailto:"):
                    return str(addr).strip()
        except Exception:
            logger.debug("principal.calendar_user_address_set() unavailable", exc_info=True)
        return f"mailto:{self._username}"

    def _get_own_calendar_user_addresses(self) -> list[str]:
        """Every CalDAV calendar-user-address of the caller (RFC 6638), used by
        `respond_to_event` to find "my" ATTENDEE entry on an event.
        """
        with self._lock:
            if self._own_calendar_user_addresses is None:
                self._own_calendar_user_addresses = self._discover_own_calendar_user_addresses()
            return self._own_calendar_user_addresses

    def _discover_own_calendar_user_addresses(self) -> list[str]:
        principal = self._get_principal()
        try:
            addresses = [str(a).strip() for a in (principal.calendar_user_address_set() or []) if a]
            if addresses:
                return addresses
        except Exception:
            logger.debug("principal.calendar_user_address_set() unavailable", exc_info=True)
        # No usable address set from the server - the single best-effort
        # organizer address (which has its own mailto:<username> fallback)
        # is at least something to compare ATTENDEEs against.
        return [self._get_own_organizer_address()]

    @staticmethod
    def _supports_component(calendar: DAVCalendar, component: str) -> bool:
        """True if `calendar` supports `component` ("VTODO"/"VEVENT"), or can't tell.

        Nextcloud advertises `supported-calendar-component-set` on every
        calendar, but a collection that doesn't (or whose PROPFIND fails,
        e.g. an external webcal subscription with flaky props) is treated as
        supporting everything - failing open here only means a name shows up
        in one listing too many, while failing closed would make an entire
        calendar silently unreachable.
        """
        try:
            components = calendar.get_supported_components()
        except Exception:
            return True
        return not components or component in components

    @staticmethod
    def _kind_label(component: str) -> str:
        return "calendar" if component == "VEVENT" else "task list"

    def _not_found(self, name: str, component: str) -> TaskMcpError:
        if component == "VEVENT":
            return CalendarNotFoundError(f"Calendar '{name}' was not found.")
        return TaskListNotFoundError(f"Task list '{name}' was not found.")

    def _resolve_collection(self, name: str, component: str) -> DAVCalendar:
        """Resolve a display name to a collection supporting `component`, freshly.

        Nextcloud keeps task lists (VTODO) and event calendars (VEVENT) side
        by side under `/calendars/<user>/`, so resolution filters by
        component support - asking for the task list "Personal" must not
        return an events-only calendar of the same name. Raises the
        kind-specific not-found error if nothing matches, or a generic
        `TaskMcpError` if more than one does - a duplicate display name is
        genuinely ambiguous, so callers are told to rename rather than have
        the server silently pick one (A3).
        """
        try:
            calendars = self._get_principal().calendars()
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        matches = [
            c
            for c in calendars
            if c.get_display_name() == name and self._supports_component(c, component)
        ]
        if not matches:
            raise self._not_found(name, component)
        if len(matches) > 1:
            kind = self._kind_label(component)
            raise TaskMcpError(
                f"Multiple {kind}s are named '{name}', which is ambiguous. "
                f"Rename the {kind}s in Nextcloud so each has a distinct name, or "
                "use a different, unambiguous name."
            )
        return matches[0]

    def _resolve_and_cache(self, name: str, component: str) -> DAVCalendar:
        calendar = self._resolve_collection(name, component)
        self._calendar_cache[(component, name)] = calendar
        return calendar

    def _get_collection(self, name: str, component: str) -> DAVCalendar:
        cached = self._calendar_cache.get((component, name))
        if cached is not None:
            return cached
        return self._resolve_and_cache(name, component)

    def _with_collection(self, name: str, component: str, fn: Callable[[DAVCalendar], _T]) -> _T:
        """Resolve `name`'s (cached) collection and call `fn(calendar)`.

        `fn` should perform raw caldav operations without translating
        `caldav_error.NotFoundError` itself: a cached calendar may have gone
        stale (the collection was deleted/renamed server-side since it was
        cached), which surfaces as that same NotFoundError on the actual
        request. On that specific error, the stale cache entry is dropped
        and resolution is retried exactly once with a fresh
        `principal.calendars()` call before giving up (A3) - this keeps the
        common case cheap while still recovering from a stale cache instead
        of failing (or silently misbehaving) forever.
        """
        calendar = self._get_collection(name, component)
        try:
            return fn(calendar)
        except caldav_error.NotFoundError:
            self._calendar_cache.pop((component, name), None)
            calendar = self._resolve_and_cache(name, component)
            return fn(calendar)

    def _with_calendar(self, list_name: str, fn: Callable[[DAVCalendar], _T]) -> _T:
        """Task-list flavour of `_with_collection`, kept for the VTODO call sites."""
        return self._with_collection(list_name, "VTODO", fn)

    def list_task_lists(self) -> list[dict[str, str]]:
        """Return all VTODO-supporting calendars on the account as {"name", "url"} dicts.

        Event-only calendars (VEVENT, e.g. Nextcloud's default "Personal"
        calendar) are excluded - they live in the same DAV namespace but
        can't hold tasks, so listing them here would invite task operations
        that the server then rejects. `list_calendars` is the event-side
        counterpart.
        """
        with self._lock:
            try:
                calendars = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            calendars = [c for c in calendars if self._supports_component(c, "VTODO")]
            names = [calendar.get_display_name() or str(calendar.url) for calendar in calendars]
            name_counts: dict[str, int] = {}
            for name in names:
                name_counts[name] = name_counts.get(name, 0) + 1
            # Populate the resolution cache opportunistically (A3), but only
            # for names that are actually unambiguous - caching one of
            # several same-named calendars would silently hide the
            # ambiguity that `_resolve_collection` is supposed to surface.
            for calendar, name in zip(calendars, names, strict=True):
                if name_counts[name] == 1:
                    self._calendar_cache[("VTODO", name)] = calendar

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

        A display-name conflict (another list already has this exact name,
        checked proactively via `principal.calendars()` before the
        server-side create) is rejected rather than silently handled,
        mirroring `_resolve_collection`'s "don't guess" stance on ambiguous
        names. A collision of the generated collection *id*, by contrast, is
        dodged automatically - see `_make_collection`.

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

            if any(
                calendar.get_display_name() == display_name
                and self._supports_component(calendar, "VTODO")
                for calendar in existing
            ):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{display_name}' already exists."
                )

            calendar = self._make_collection(
                principal,
                display_name,
                slug,
                component="VTODO",
                conflict_error=TaskListAlreadyExistsError,
                kind="task list",
            )

            self._calendar_cache[("VTODO", display_name)] = calendar
            return {"name": display_name, "url": str(calendar.url)}

    def _make_collection(
        self,
        principal: DAVPrincipal,
        display_name: str,
        slug: str,
        *,
        component: str,
        conflict_error: type[TaskMcpError],
        kind: str,
    ) -> DAVCalendar:
        """MKCALENDAR a new collection, dodging occupied collection ids.

        A 405 (Method Not Allowed) / 409 (Conflict) response means the
        collection URL is already taken - either by a different-named
        collection whose name slugifies to the same id, or by a *deleted*
        collection still sitting in Nextcloud's trashbin, which keeps its URI
        occupied (invisibly - it no longer shows up in listings) until the
        trash is purged. Since the display name is this API's identity and
        the id is internal, the id is not worth failing over: retry with
        "<slug>-2", "<slug>-3", ... before giving up. Display-name conflicts
        are still rejected by the callers, before ever getting here.
        """
        candidates = [slug] + [f"{slug}-{i}" for i in range(2, 7)]
        for cal_id in candidates:
            try:
                return principal.make_calendar(
                    name=display_name,
                    cal_id=cal_id,
                    supported_calendar_component_set=[component],
                )
            except (caldav_error.MkcolError, caldav_error.MkcalendarError) as exc:
                if "405" in str(exc) or "409" in str(exc):
                    continue  # id occupied - try the next candidate
                logger.warning("CalDAV request failed creating %s", kind, exc_info=exc)
                raise TaskMcpError("The CalDAV request failed on the Nextcloud server.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
        raise conflict_error(
            f"Could not create the {kind} '{display_name}': every generated collection id "
            f"('{candidates[0]}' through '{candidates[-1]}') is already taken on the server "
            "(possibly by deleted collections still in the trashbin). "
            "Try a different display name."
        )

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
            self._calendar_cache.pop(("VTODO", list_name), None)

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

            matches = [
                c
                for c in existing
                if c.get_display_name() == list_name and self._supports_component(c, "VTODO")
            ]
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
                c.get_display_name() == new_display_name and self._supports_component(c, "VTODO")
                for c in existing
            ):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{new_display_name}' already exists."
                )

            try:
                calendar.set_properties([dav.DisplayName(new_display_name)])
            except Exception as exc:
                raise _translate(exc) from exc

            self._calendar_cache.pop(("VTODO", list_name), None)
            self._calendar_cache[("VTODO", new_display_name)] = calendar
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

    # ------------------------------------------------------------------
    # Event calendars (VEVENT)
    # ------------------------------------------------------------------

    @staticmethod
    def _range_bound(value: str | None, *, exclusive_end: bool) -> datetime | None:
        """Normalize a `von`/`bis` filter value to a timezone-aware datetime.

        A date-only value expands to the start of that day (for `von`) or the
        start of the *next* day (for `bis`, making a date-only upper bound
        inclusive of the whole day - the resulting datetime is used as the
        exclusive end of a CalDAV time-range filter). Naive datetimes are
        interpreted as UTC, matching `parse_datetime_input` everywhere else.
        """
        if value is None:
            return None
        parsed = mapping.parse_datetime_input(value)
        if isinstance(parsed, datetime):
            return parsed
        day_start = datetime.combine(parsed, time.min, tzinfo=timezone.utc)
        return day_start + timedelta(days=1) if exclusive_end else day_start

    def _event_calendars(self, calendar_names: list[str] | None) -> list[tuple[str, DAVCalendar]]:
        """Return (display name, calendar) pairs for the VEVENT calendars to query.

        With explicit `calendar_names`, each is resolved individually (going
        through the cache); unknown names raise `CalendarNotFoundError`
        instead of being skipped, so a typo can't silently produce an empty
        result. With `None`, every VEVENT-supporting calendar on the account
        is returned, freshly listed.
        """
        if calendar_names is not None:
            return [(name, self._get_collection(name, "VEVENT")) for name in calendar_names]

        try:
            calendars = self._get_principal().calendars()
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc
        result: list[tuple[str, DAVCalendar]] = []
        for calendar in calendars:
            if not self._supports_component(calendar, "VEVENT"):
                continue
            name = calendar.get_display_name() or str(calendar.url)
            result.append((name, calendar))
        return result

    def list_calendars(self) -> list[dict[str, Any]]:
        """Return all VEVENT-supporting calendars as {"name", "url", "farbe", "komponenten"}.

        Task-only lists (VTODO) are excluded - `list_task_lists` is their
        counterpart. `komponenten` reports the full advertised component set
        so a mixed VEVENT+VTODO collection is recognizable as both.
        """
        with self._lock:
            try:
                calendars = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            result: list[dict[str, Any]] = []
            for calendar in calendars:
                try:
                    components = list(calendar.get_supported_components())
                except Exception:
                    components = []
                if components and "VEVENT" not in components:
                    continue
                name = calendar.get_display_name() or str(calendar.url)
                # The color property is cosmetic; a calendar whose PROPFIND
                # for it fails should still be listed rather than error out.
                farbe: str | None
                try:
                    props = calendar.get_properties([ical_elements.CalendarColor()])
                    raw = props.get(ical_elements.CalendarColor.tag)
                    farbe = str(raw) if raw else None
                except Exception:
                    farbe = None
                result.append(
                    {
                        "name": name,
                        "url": str(calendar.url),
                        "farbe": farbe,
                        "komponenten": [str(c) for c in components],
                    }
                )
                if sum(1 for entry in result if entry["name"] == name) == 1:
                    self._calendar_cache[("VEVENT", name)] = calendar
            # Drop cache entries that turned out to be ambiguous after all.
            counts: dict[str, int] = {}
            for entry in result:
                counts[str(entry["name"])] = counts.get(str(entry["name"]), 0) + 1
            for dup_name, count in counts.items():
                if count > 1:
                    self._calendar_cache.pop(("VEVENT", dup_name), None)
            return result

    def create_calendar(self, display_name: str, farbe: str | None = None) -> dict[str, Any]:
        """Create a new VEVENT calendar, optionally with a "#RRGGBB" color.

        Mirrors `create_task_list`'s conflict handling: a display-name clash
        with an existing event calendar, or a collection-id clash on the
        server (405/409 from MKCALENDAR), both fail loudly instead of
        silently reusing an existing calendar.
        """
        if not display_name or not display_name.strip():
            raise InvalidEventDataError("display_name is required to create a calendar.")
        if farbe is not None and not _COLOR_RE.match(farbe):
            raise InvalidEventDataError(
                f"farbe must look like '#RRGGBB' (or '#RRGGBBAA'), got '{farbe}'."
            )

        slug = _slugify(display_name)

        with self._lock:
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            if any(
                calendar.get_display_name() == display_name
                and self._supports_component(calendar, "VEVENT")
                for calendar in existing
            ):
                raise CalendarAlreadyExistsError(
                    f"A calendar named '{display_name}' already exists."
                )

            calendar = self._make_collection(
                principal,
                display_name,
                slug,
                component="VEVENT",
                conflict_error=CalendarAlreadyExistsError,
                kind="calendar",
            )

            if farbe is not None:
                try:
                    calendar.set_properties([ical_elements.CalendarColor(farbe)])
                except Exception as exc:
                    raise _translate(exc) from exc

            self._calendar_cache[("VEVENT", display_name)] = calendar
            return {"name": display_name, "url": str(calendar.url), "farbe": farbe}

    def delete_calendar(self, calendar_name: str) -> None:
        """Permanently delete an event calendar and every event inside it.

        Irreversible from this API's point of view (the server may keep a
        trashbin, but this client can't restore from it) - callers should
        confirm with the user first.
        """

        def op(calendar: DAVCalendar) -> None:
            calendar.delete()

        with self._lock:
            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            self._calendar_cache.pop(("VEVENT", calendar_name), None)

    def update_calendar(
        self,
        calendar_name: str,
        new_display_name: str | None = None,
        farbe: str | None = None,
    ) -> dict[str, Any]:
        """Rename an event calendar and/or set its color (PROPPATCH).

        Only the display name/color change - the collection's URL/id stays
        stable, so clients that reference the calendar by URL are unaffected.
        Renaming to a name another event calendar already has raises
        `CalendarAlreadyExistsError`, mirroring `rename_task_list`.
        """
        if new_display_name is not None and not new_display_name.strip():
            raise InvalidEventDataError("new_display_name must not be empty.")
        if new_display_name is None and farbe is None:
            raise InvalidEventDataError("Nothing to update: give new_display_name and/or farbe.")
        if farbe is not None and not _COLOR_RE.match(farbe):
            raise InvalidEventDataError(
                f"farbe must look like '#RRGGBB' (or '#RRGGBBAA'), got '{farbe}'."
            )

        with self._lock:
            try:
                existing = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            matches = [
                c
                for c in existing
                if c.get_display_name() == calendar_name and self._supports_component(c, "VEVENT")
            ]
            if not matches:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.")
            if len(matches) > 1:
                raise TaskMcpError(
                    f"Multiple calendars are named '{calendar_name}', which is ambiguous. "
                    "Rename the calendars in Nextcloud so each has a distinct name, or "
                    "use a different, unambiguous name."
                )
            calendar = matches[0]

            if (
                new_display_name is not None
                and new_display_name != calendar_name
                and any(
                    c.get_display_name() == new_display_name
                    and self._supports_component(c, "VEVENT")
                    for c in existing
                )
            ):
                raise CalendarAlreadyExistsError(
                    f"A calendar named '{new_display_name}' already exists."
                )

            props: list[Any] = []
            if new_display_name is not None:
                props.append(dav.DisplayName(new_display_name))
            if farbe is not None:
                props.append(ical_elements.CalendarColor(farbe))
            try:
                calendar.set_properties(props)
            except Exception as exc:
                raise _translate(exc) from exc

            final_name = new_display_name if new_display_name is not None else calendar_name
            self._calendar_cache.pop(("VEVENT", calendar_name), None)
            self._calendar_cache[("VEVENT", final_name)] = calendar
            return {"name": final_name, "url": str(calendar.url), "farbe": farbe}

    def list_events(
        self,
        calendar_names: list[str] | None = None,
        von: str | None = None,
        bis: str | None = None,
        suchtext: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        expand: bool = False,
    ) -> list[dict[str, Any]]:
        """Return events across one, several, or all VEVENT calendars, sorted by start.

        `von`/`bis` bound the query server-side (CalDAV time-range REPORT), so
        recurring events that have an occurrence in the window are matched
        even when their master event started long before it. With
        `expand=True`, recurring events are additionally expanded into their
        individual occurrences within the window (requires both bounds).
        `suchtext`/`tag`/`limit` filter the parsed results client-side via
        `event_mapping.filter_events`.
        """
        start_bound = self._range_bound(von, exclusive_end=False)
        end_bound = self._range_bound(bis, exclusive_end=True)
        if expand and (start_bound is None or end_bound is None):
            raise InvalidEventDataError(
                "Expanding recurring events requires both von and bis bounds."
            )

        with self._lock:

            def op(calendar: DAVCalendar):
                if start_bound is None and end_bound is None:
                    return calendar.events()
                # caldav's search/expand path is only well-defined with
                # both ends present; widen an omitted side instead of
                # passing None through (see _RANGE_MIN/_RANGE_MAX).
                return calendar.search(
                    start=start_bound or _RANGE_MIN,
                    end=end_bound or _RANGE_MAX,
                    event=True,
                    expand=expand,
                )

            targets = self._event_calendars(calendar_names)
            events: list[dict[str, Any]] = []
            for name, target_calendar in targets:
                try:
                    if calendar_names is not None:
                        # Named calendars go through the cache-aware path so a
                        # stale cache entry is re-resolved once (A3).
                        objs = self._with_collection(name, "VEVENT", op)
                    else:
                        # The all-calendars case just listed everything fresh;
                        # querying the object directly also keeps two
                        # same-named calendars both reachable here.
                        objs = op(target_calendar)
                except TaskMcpError:
                    raise
                except caldav_error.NotFoundError as exc:
                    raise CalendarNotFoundError(f"Calendar '{name}' was not found.") from exc
                except Exception as exc:
                    raise _translate(exc) from exc

                for obj in objs:
                    parsed = event_mapping.parse_vevent(obj.icalendar_component)
                    parsed["kalender"] = name
                    events.append(parsed)

            return event_mapping.filter_events(events, suchtext=suchtext, tag=tag, limit=limit)

    def get_event(self, calendar_name: str, event_uid: str) -> dict[str, Any]:
        """Return a single event, parsed into the server's German event dict."""
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.event_by_uid(event_uid)

            try:
                event_obj = self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            parsed = event_mapping.parse_vevent(event_obj.icalendar_component)
            parsed["kalender"] = calendar_name
            return parsed

    def create_event(self, calendar_name: str, fields: event_mapping.EventFields) -> str:
        """Create a new event in the given calendar and return its UID.

        If `fields.teilnehmer` adds attendees, ORGANIZER is set to the
        caller's own address (discovered lazily, see
        `_get_own_organizer_address`) - Nextcloud's CalDAV server then does
        server-side scheduling (iMIP invitation mails) once the event is
        saved with both ORGANIZER and ATTENDEEs present.
        """
        if fields.titel is None:
            raise InvalidEventDataError("titel is required to create an event.")
        if fields.start is None:
            raise InvalidEventDataError("start is required to create an event.")
        with self._lock:
            own_organizer = self._get_own_organizer_address() if fields.teilnehmer else None
            new_uid = str(uuid.uuid4())
            event = Event()
            event.add("uid", new_uid)
            event.add("dtstamp", datetime.now(timezone.utc))
            event_mapping.apply_event_fields(event, fields, own_organizer=own_organizer)

            vcal = Calendar()
            vcal.add("prodid", "-//nextcloud-task-mcp//EN")
            vcal.add("version", "2.0")
            vcal.add_component(event)
            ical_text = vcal.to_ical().decode("utf-8")

            def op(calendar: DAVCalendar):
                calendar.save_event(ical=ical_text)

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return new_uid

    def update_event(
        self, calendar_name: str, event_uid: str, fields: event_mapping.EventFields
    ) -> None:
        """Update only the given (non-None) fields of an existing event.

        Same ORGANIZER-on-first-attendee and server-side-scheduling behavior
        as `create_event` when `fields.teilnehmer` sets attendees.
        """
        with self._lock:
            own_organizer = self._get_own_organizer_address() if fields.teilnehmer else None

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.apply_event_fields(
                    event_obj.icalendar_component, fields, own_organizer=own_organizer
                )
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def respond_to_event(
        self,
        calendar_name: str,
        event_uid: str,
        antwort: str,
        kommentar: str | None = None,
    ) -> None:
        """Set the caller's own PARTSTAT on an event they were invited to (RSVP reply).

        The own ATTENDEE entry is found by comparing the event's ATTENDEEs
        against the caller's own CalDAV calendar-user-addresses (case-
        insensitive, "mailto:" ignored on both sides) - see
        `_get_own_calendar_user_addresses`. Raises `InvalidEventDataError` if
        none match (the caller isn't an attendee of this event). Saves the
        event afterwards; Nextcloud's CalDAV server then propagates the reply
        to the organizer as an iMIP/iTIP REPLY, the same server-side
        scheduling mechanism that sends the original invitations (see
        create_event/update_event).
        """
        partstat = event_mapping.response_label_to_partstat(antwort)
        with self._lock:
            own_addresses = self._get_own_calendar_user_addresses()

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.apply_own_attendee_response(
                    event_obj.icalendar_component, own_addresses, partstat, kommentar
                )
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def delete_event(self, calendar_name: str, event_uid: str) -> None:
        """Permanently delete an event."""
        with self._lock:

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_obj.delete()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    # ------------------------------------------------------------------
    # Task <-> event linking and combined views
    # ------------------------------------------------------------------

    def link_task_to_event(
        self,
        list_name: str,
        task_uid: str,
        calendar_name: str,
        event_uid: str,
        beziehung: str = "zeitblock",
    ) -> None:
        """Link a task (VTODO) to an event (VEVENT) via RELATED-TO on the event.

        The RELATED-TO property is written on the *event*, never the task:
        Nextcloud Tasks interprets a task's RELATED-TO as "subtask of", so
        pointing one at an event UID would garble the task tree in its UI,
        while the calendar app simply ignores the property (it round-trips
        as raw data). See `_LINK_RELTYPES` for the two supported semantics.
        """
        reltype = _LINK_RELTYPES.get(beziehung)
        if reltype is None:
            raise InvalidEventDataError(
                f"Unknown beziehung '{beziehung}'. Expected one of: {', '.join(_LINK_RELTYPES)}."
            )

        with self._lock:
            # Verify the task actually exists before writing its UID onto the
            # event - a dangling link would be invisible until someone tried
            # to follow it.
            def check_task(calendar: DAVCalendar):
                calendar.get_todo_by_uid(task_uid)

            try:
                self._with_collection(list_name, "VTODO", check_task)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.add_relation(event_obj.icalendar_component, task_uid, reltype)
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def list_events_for_task(
        self,
        list_name: str,
        task_uid: str,
        calendar_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return events linked to the given task - the task-side counterpart of link_task_to_event.

        The RELATED-TO link is only ever written on the event (see
        `link_task_to_event`'s docstring for why), so there is no CalDAV
        query that starts from a task UID and finds the events pointing at
        it: every event in the queried calendars has to be fetched and its
        parsed `verknuepfte_aufgaben` checked for `task_uid`. Verifies the
        task exists first, same check and error as `link_task_to_event`.
        """
        with self._lock:

            def check_task(calendar: DAVCalendar):
                calendar.get_todo_by_uid(task_uid)

            try:
                self._with_collection(list_name, "VTODO", check_task)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            def op(calendar: DAVCalendar):
                return calendar.events()

            targets = self._event_calendars(calendar_names)
            events: list[dict[str, Any]] = []
            for name, target_calendar in targets:
                try:
                    if calendar_names is not None:
                        # Named calendars go through the cache-aware path so a
                        # stale cache entry is re-resolved once (A3).
                        objs = self._with_collection(name, "VEVENT", op)
                    else:
                        # The all-calendars case just listed everything fresh;
                        # querying the object directly also keeps two
                        # same-named calendars both reachable here.
                        objs = op(target_calendar)
                except TaskMcpError:
                    raise
                except caldav_error.NotFoundError as exc:
                    raise CalendarNotFoundError(f"Calendar '{name}' was not found.") from exc
                except Exception as exc:
                    raise _translate(exc) from exc

                for obj in objs:
                    parsed = event_mapping.parse_vevent(obj.icalendar_component)
                    if any(rel["uid"] == task_uid for rel in parsed["verknuepfte_aufgaben"]):
                        parsed["kalender_name"] = name
                        events.append(parsed)

            events.sort(key=event_mapping._start_sort_key)
            return events

    def create_event_from_task(
        self,
        list_name: str,
        task_uid: str,
        calendar_name: str,
        start: str | None = None,
        dauer_minuten: int = 60,
    ) -> str:
        """Create a calendar event from an existing task (timeboxing) and link them.

        Title, notes, location and tags are copied from the task; the event
        starts at `start` (or, if omitted, the task's due date/time) and runs
        for `dauer_minuten`. A date-only start produces a one-day all-day
        event instead. The new event carries RELATED-TO;RELTYPE=PARENT with
        the task's UID (the "zeitblock" link semantics).
        """
        if dauer_minuten <= 0:
            raise InvalidEventDataError(f"dauer_minuten must be > 0, got {dauer_minuten}.")

        with self._lock:
            task = self.get_task(list_name, task_uid)

            start_spec = start if start is not None else task.get("faellig_datum")
            if start_spec is None:
                raise InvalidEventDataError(
                    "The task has no faellig_datum (due date); pass an explicit start "
                    "for the event instead."
                )

            parsed_start = mapping.parse_datetime_input(start_spec)
            if isinstance(parsed_start, datetime):
                ende = (parsed_start + timedelta(minutes=dauer_minuten)).isoformat()
                start_value = parsed_start.isoformat()
            else:
                # All-day due date -> one-day all-day event (inclusive end).
                start_value = parsed_start.isoformat()
                ende = start_value

            fields = event_mapping.EventFields(
                titel=task.get("titel") or "Aufgabe",
                start=start_value,
                ende=ende,
                beschreibung=task.get("notizen"),
                ort=task.get("ort"),
                tags=task.get("tags") or None,
                verknuepfte_aufgabe=task_uid,
            )
            return self.create_event(calendar_name, fields)

    def get_agenda(
        self,
        datum: str,
        calendar_names: list[str] | None = None,
        list_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one day's events and due tasks together (a combined agenda).

        CalDAV has no single query spanning VEVENTs and VTODOs, so this is
        plain server-side composition: a time-range event query (recurring
        events expanded to that day's occurrences) plus a due-date-filtered
        task listing per VTODO list. `datum` must be a date-only "YYYY-MM-DD"
        string; day boundaries are UTC, consistent with the naive-input-is-UTC
        rule used everywhere else in this server.
        """
        parsed = mapping.parse_datetime_input(datum)
        if isinstance(parsed, datetime):
            raise InvalidEventDataError(
                f"datum must be a date-only 'YYYY-MM-DD' string, got '{datum}'."
            )

        with self._lock:
            termine = self.list_events(
                calendar_names=calendar_names, von=datum, bis=datum, expand=True
            )

            if list_names is None:
                list_names = [entry["name"] for entry in self.list_task_lists()]
            aufgaben: list[dict[str, Any]] = []
            for name in list_names:
                tasks = self.list_tasks(name, only_open=True, due_before=datum, due_after=datum)
                for task in tasks:
                    task["liste"] = name
                    aufgaben.append(task)

            return {"datum": parsed.isoformat(), "termine": termine, "aufgaben": aufgaben}
