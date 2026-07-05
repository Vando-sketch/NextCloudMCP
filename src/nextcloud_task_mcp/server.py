"""FastMCP server exposing Nextcloud Tasks (CalDAV) as MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .auth import BearerTokenMiddleware
from .caldav_client import CalDavService
from .config import Settings
from .errors import TaskMcpError

logger = logging.getLogger(__name__)


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a CalDavService call, turning our errors into clean ToolErrors.

    Anything unexpected is logged server-side but never shown to the
    client as a raw stack trace.
    """
    try:
        return fn(*args, **kwargs)
    except TaskMcpError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - safety net for unforeseen failures
        logger.exception("Unexpected error in %s", getattr(fn, "__name__", fn))
        raise ToolError("An unexpected internal error occurred.") from exc


def build_server(settings: Settings, service: CalDavService | None = None) -> FastMCP:
    """Construct the FastMCP server with auth middleware and all task tools registered.

    `service` can be injected for testing; defaults to a real CalDavService
    built from `settings`.
    """
    mcp = FastMCP(name="nextcloud-task-mcp")
    mcp.add_middleware(BearerTokenMiddleware(settings.auth_token))

    caldav_service = service or CalDavService(
        url=settings.caldav_url,
        username=settings.caldav_username,
        password=settings.caldav_password,
    )

    @mcp.tool
    def list_task_lists() -> list[dict[str, str]]:
        """List all available Nextcloud task lists.

        Returns:
            A list of {"name": display name, "url": internal CalDAV URL/ID} dicts.
        """
        return _call(caldav_service.list_task_lists)

    @mcp.tool
    def list_tasks(list_name: str, nur_offene: bool = True) -> list[dict[str, Any]]:
        """List tasks in a Nextcloud task list.

        Args:
            list_name: Display name of the task list.
            nur_offene: If True (default), only return tasks that are not completed.

        Returns:
            A list of task dicts with keys: uid, titel, start_datum, fällig_datum,
            priorität, fortschritt_prozent, status, ort, url, tags, notizen,
            übergeordnete_uid (None unless the task is a subtask).
        """
        return _call(caldav_service.list_tasks, list_name, only_open=nur_offene)

    @mcp.tool
    def create_task(
        liste: str,
        titel: str,
        start_datum: str | None = None,
        fällig_datum: str | None = None,
        priorität: str | None = None,
        fortschritt_prozent: int | None = None,
        ort: str | None = None,
        url: str | None = None,
        tags: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        notizen: str | None = None,
        sichtbarkeit: str | None = None,
        übergeordnete_aufgabe: str | None = None,
    ) -> dict[str, str]:
        """Create a new task in a Nextcloud task list.

        Args:
            liste: Display name of the target task list.
            titel: Task title (VTODO SUMMARY).
            start_datum: Optional ISO 8601 date/datetime -> DTSTART.
            fällig_datum: Optional ISO 8601 date/datetime -> DUE.
            priorität: Optional "hoch" / "mittel" / "niedrig" -> PRIORITY (1/5/9).
            fortschritt_prozent: Optional 0-100 -> PERCENT-COMPLETE.
            ort: Optional location -> LOCATION.
            url: Optional URL -> URL.
            tags: Optional list of category strings -> CATEGORIES.
            erinnerungen: Optional list of reminders, each either a relative RFC 5545
                duration (e.g. "-P1D", "-PT1H", relative to fällig_datum, falling
                back to start_datum) or an absolute ISO 8601 datetime -> VALARM.
            notizen: Optional notes -> DESCRIPTION.
            sichtbarkeit: Optional "öffentlich" / "privat" / "vertraulich" -> CLASS.
            übergeordnete_aufgabe: Optional UID of an existing task to link this
                task to as a subtask -> RELATED-TO (RELTYPE=PARENT).

        Returns:
            {"uid": the new task's UID}.
        """
        new_uid = _call(
            caldav_service.create_task,
            liste,
            titel=titel,
            start_datum=start_datum,
            faellig_datum=fällig_datum,
            prioritaet=priorität,
            fortschritt_prozent=fortschritt_prozent,
            ort=ort,
            url=url,
            tags=tags,
            erinnerungen=erinnerungen,
            notizen=notizen,
            sichtbarkeit=sichtbarkeit,
            uebergeordnete_aufgabe=übergeordnete_aufgabe,
        )
        return {"uid": new_uid}

    @mcp.tool
    def update_task(
        list_name: str,
        task_uid: str,
        titel: str | None = None,
        start_datum: str | None = None,
        fällig_datum: str | None = None,
        priorität: str | None = None,
        fortschritt_prozent: int | None = None,
        ort: str | None = None,
        url: str | None = None,
        tags: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        notizen: str | None = None,
        sichtbarkeit: str | None = None,
        übergeordnete_aufgabe: str | None = None,
    ) -> dict[str, str]:
        """Update an existing task. Only fields that are explicitly given are changed.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to update.
            (all other args): Same meaning and mapping as in create_task; a field
                left as None is left unchanged on the existing task.

        Returns:
            {"uid": task_uid} on success.
        """
        _call(
            caldav_service.update_task,
            list_name,
            task_uid,
            titel=titel,
            start_datum=start_datum,
            faellig_datum=fällig_datum,
            prioritaet=priorität,
            fortschritt_prozent=fortschritt_prozent,
            ort=ort,
            url=url,
            tags=tags,
            erinnerungen=erinnerungen,
            notizen=notizen,
            sichtbarkeit=sichtbarkeit,
            uebergeordnete_aufgabe=übergeordnete_aufgabe,
        )
        return {"uid": task_uid}

    @mcp.tool
    def complete_task(list_name: str, task_uid: str) -> dict[str, str]:
        """Mark a task as completed (sets STATUS, PERCENT-COMPLETE and COMPLETED timestamp).

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to complete.

        Returns:
            {"uid": task_uid} on success.
        """
        _call(caldav_service.complete_task, list_name, task_uid)
        return {"uid": task_uid}

    @mcp.tool
    def delete_task(list_name: str, task_uid: str) -> dict[str, str]:
        """Permanently delete a task.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to delete.

        Returns:
            {"uid": task_uid} on success.
        """
        _call(caldav_service.delete_task, list_name, task_uid)
        return {"uid": task_uid}

    return mcp


def main() -> None:
    """Entry point: read config from the environment and run the HTTP server."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    mcp = build_server(settings)
    mcp.run(transport="http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
