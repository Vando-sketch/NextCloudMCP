"""User-facing exceptions raised by this server.

These are deliberately separate from caldav/requests exceptions so that
tool code never leaks raw stack traces or library internals to the MCP
client - see :mod:`nextcloud_task_mcp.caldav_client` for the translation
layer that converts library exceptions into these.
"""

from __future__ import annotations


class TaskMcpError(Exception):
    """Base class for all user-facing errors raised by this server."""


class ConnectionFailedError(TaskMcpError):
    """Raised when the CalDAV server can't be reached or times out."""


class AuthenticationFailedError(TaskMcpError):
    """Raised when Nextcloud rejects the configured CalDAV credentials."""


class TaskListNotFoundError(TaskMcpError):
    """Raised when the requested task list does not exist."""


class TaskListAlreadyExistsError(TaskMcpError):
    """Raised when creating a task list whose display name (or generated
    collection id) collides with one that already exists on the server."""


class TaskNotFoundError(TaskMcpError):
    """Raised when the requested task UID does not exist in the given list."""


class InvalidTaskDataError(TaskMcpError):
    """Raised when task field values can't be mapped to valid iCalendar data."""


class TaskConflictError(TaskMcpError):
    """Raised when a task was modified by another client since it was last read.

    The underlying CalDAV etag no longer matches (HTTP 412), so the write was
    rejected. Callers should re-fetch the current task and retry the change.
    """


class TransientServerError(TaskMcpError):
    """Raised when Nextcloud (or a proxy in front of it) gave no real answer.

    A 502/503/504 is not a decision about the request - it says the request
    never reached one, so it may or may not have been carried out. Batch
    operations retry these a few times before one can ever reach a caller;
    a message that gets through says exactly that much and no more.
    """


class ObjectMoveError(TaskMcpError):
    """Raised when moving one task/event fails for a reason that concerns only it.

    Distinct from the errors that say the whole call is broken (bad
    credentials, unreachable server, missing collection): a batch move records
    one of these against the UID it happened on and keeps going, while the
    others abort the batch. The message always names what state the object was
    left in, since the copy fallback has several halfway points.
    """


class CalendarNotFoundError(TaskMcpError):
    """Raised when the requested event calendar does not exist (or supports no VEVENTs)."""


class CalendarAlreadyExistsError(TaskMcpError):
    """Raised when creating a calendar whose display name (or generated
    collection id) collides with one that already exists on the server."""


class EventNotFoundError(TaskMcpError):
    """Raised when the requested event UID does not exist in the given calendar."""


class InvalidEventDataError(TaskMcpError):
    """Raised when event field values can't be mapped to valid iCalendar data."""


class InvalidIcsDataError(TaskMcpError):
    """Raised when ICS text passed to import_ics isn't a parseable VCALENDAR
    containing at least one VEVENT or VTODO."""


class NotizNotFoundError(TaskMcpError):
    """Raised when the requested note id does not exist."""


class InvalidNotizDataError(TaskMcpError):
    """Raised when note field values are rejected by the Notes API, or when
    a note tool call is missing data it needs (e.g. no fields to update)."""
