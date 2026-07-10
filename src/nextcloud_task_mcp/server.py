"""FastMCP server exposing Nextcloud Tasks (CalDAV) as MCP tools."""

from __future__ import annotations

import functools
import logging
from typing import Any
from urllib.parse import urlparse

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import mapping
from .caldav_client import CalDavService
from .config import Settings, is_local_hostname
from .errors import TaskMcpError
from .personal_auth import PersonalAuthProvider

logger = logging.getLogger(__name__)


async def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a (blocking) CalDavService call in a worker thread and translate errors.

    `caldav.DAVClient` does blocking HTTP, so calling `fn` inline here would
    stall the asyncio event loop for every other client (A1). We offload the
    actual call to a worker thread via `anyio.to_thread.run_sync` - which only
    accepts a no-arg callable, hence the `functools.partial` wrapping - and
    keep the error-translation semantics identical to the previous sync
    version: our own errors become clean ToolErrors, anything unexpected is
    logged server-side but never shown to the client as a raw stack trace.
    """
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except TaskMcpError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - safety net for unforeseen failures
        logger.exception("Unexpected error in %s", getattr(fn, "__name__", fn))
        raise ToolError("An unexpected internal error occurred.") from exc


def build_server(settings: Settings, service: CalDavService | None = None) -> FastMCP:
    """Construct the FastMCP server with OAuth 2.1 auth and all task tools registered.

    `service` can be injected for testing; defaults to a real CalDavService
    built from `settings`.
    """
    allowed_redirect_domains = settings.oauth_allowed_redirect_domains
    if allowed_redirect_domains is None and not is_local_hostname(
        urlparse(settings.public_base_url).hostname
    ):
        # PersonalAuthProvider's own built-in default allow-list includes
        # "localhost" (see its docstring), which is reasonable for its own
        # local-dev use case but meaningless - and needlessly widens a
        # security-relevant list - once PUBLIC_BASE_URL is public: a
        # redirect_uri claiming host "localhost" can never actually reach the
        # browser completing a real claude.ai OAuth flow against a public
        # deployment. Only override when the operator hasn't explicitly set
        # MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS themselves. (D9)
        allowed_redirect_domains = ["claude.ai", "claude.com"]

    auth = PersonalAuthProvider(
        base_url=settings.public_base_url,
        password=settings.oauth_password,
        allowed_redirect_domains=allowed_redirect_domains,
        access_token_expiry_seconds=settings.oauth_access_token_expiry_seconds,
        refresh_token_expiry_seconds=settings.oauth_refresh_token_expiry_seconds,
        state_dir=settings.oauth_state_dir,
    )
    mcp = FastMCP(name="nextcloud-task-mcp", auth=auth)

    caldav_service = service or CalDavService(
        url=settings.caldav_url,
        username=settings.caldav_username,
        password=settings.caldav_password,
        timeout=settings.caldav_timeout_seconds,
    )

    @mcp.tool
    async def list_task_lists() -> list[dict[str, str]]:
        """List all available Nextcloud task lists.

        Returns:
            A list of {"name": display name, "url": internal CalDAV URL/ID} dicts.
        """
        return await _call(caldav_service.list_task_lists)

    @mcp.tool
    async def list_tasks(list_name: str, nur_offene: bool = True) -> list[dict[str, Any]]:
        """List tasks in a Nextcloud task list.

        Args:
            list_name: Display name of the task list.
            nur_offene: If True (default), only return tasks that are not completed.

        Returns:
            A list of task dicts with keys: uid, titel, start_datum, fällig_datum,
            priorität, fortschritt_prozent, status, ort, url, tags, notizen,
            übergeordnete_uid (None unless the task is a subtask).
        """
        return await _call(caldav_service.list_tasks, list_name, only_open=nur_offene)

    @mcp.tool
    async def get_task(list_name: str, task_uid: str) -> dict[str, Any]:
        """Fetch a single task by UID, without listing the whole task list.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to fetch.

        Returns:
            A task dict with the same shape as one entry from list_tasks: uid,
            titel, start_datum, fällig_datum, priorität, fortschritt_prozent,
            status, ort, url, tags, notizen, übergeordnete_uid.
        """
        return await _call(caldav_service.get_task, list_name, task_uid)

    @mcp.tool
    async def create_task(
        list_name: str,
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
            list_name: Display name of the target task list.
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

        Date/time semantics for start_datum and fällig_datum: a value that is
        exactly "YYYY-MM-DD" (e.g. "2026-07-20") creates an all-day entry
        (iCalendar VALUE=DATE). Any other ISO 8601 value is stored as a
        datetime; a *naive* datetime (no UTC offset, e.g.
        "2026-07-20T14:00:00") is interpreted as UTC.

        Returns:
            {"uid": the new task's UID}.
        """
        fields = mapping.TaskFields(
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
        new_uid = await _call(caldav_service.create_task, list_name, fields)
        return {"uid": new_uid}

    @mcp.tool
    async def update_task(
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
        felder_leeren: list[str] | None = None,
    ) -> dict[str, str]:
        """Update an existing task. Only fields that are explicitly given are changed.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to update.
            (all other args): Same meaning and mapping as in create_task; a field
                left as None is left unchanged on the existing task. Date/time
                semantics also match create_task: a "YYYY-MM-DD" value creates an
                all-day entry, and naive datetimes are interpreted as UTC.
            felder_leeren: Optional list of field names to clear (remove the
                property from the task entirely) instead of changing them.
                Accepted values: "start_datum", "fällig_datum", "priorität",
                "fortschritt_prozent", "ort", "url", "tags", "erinnerungen",
                "notizen", "sichtbarkeit", "übergeordnete_aufgabe". "titel"
                cannot be cleared. Naming an unknown field, or naming a field
                here that is *also* given a new value in the same call, is an
                error.

        Returns:
            {"uid": task_uid} on success.
        """
        fields = mapping.TaskFields(
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
            clear=tuple(felder_leeren) if felder_leeren else (),
        )
        await _call(caldav_service.update_task, list_name, task_uid, fields)
        return {"uid": task_uid}

    @mcp.tool
    async def complete_task(list_name: str, task_uid: str) -> dict[str, str]:
        """Mark a task as completed (sets STATUS, PERCENT-COMPLETE and COMPLETED timestamp).

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to complete.

        Returns:
            {"uid": task_uid} on success.
        """
        await _call(caldav_service.complete_task, list_name, task_uid)
        return {"uid": task_uid}

    @mcp.tool
    async def delete_task(list_name: str, task_uid: str) -> dict[str, str]:
        """Permanently delete a task.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to delete.

        Returns:
            {"uid": task_uid} on success.
        """
        await _call(caldav_service.delete_task, list_name, task_uid)
        return {"uid": task_uid}

    return mcp


def main() -> None:
    """Entry point: read config from the environment and run the HTTP server."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    mcp = build_server(settings)
    # PersonalAuthProvider's /authorize gate reads MCP_OAUTH_PASSWORD out of the
    # `state`/`scope` query string (see personal_auth.py). Uvicorn's default access
    # log records the full request path including the query string, which would
    # otherwise write that password in plaintext into server logs on every
    # authorization - the exact secret this deployment relies on once public.
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    main()
