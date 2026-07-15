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
