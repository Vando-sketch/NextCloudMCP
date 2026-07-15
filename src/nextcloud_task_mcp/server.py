"""FastMCP server exposing Nextcloud Tasks (CalDAV) as MCP tools."""

from __future__ import annotations

import functools
import logging
from typing import Any
from urllib.parse import urlparse

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import event_mapping, mapping
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
    async def create_task_list(display_name: str) -> dict[str, str]:
        """Create a new Nextcloud task list (a CalDAV calendar collection supporting VTODO).

        Args:
            display_name: Display name for the new task list. A URL-safe
                collection id is generated from it automatically; if that id
                collides with an existing collection, or another list
                already has this exact display name, the call fails instead
                of silently reusing/overwriting the existing list.

        Returns:
            {"name": display name, "url": internal CalDAV URL/ID} for the new
            list, in the same shape as one entry of list_task_lists.
        """
        return await _call(caldav_service.create_task_list, display_name)

    @mcp.tool
    async def delete_task_list(list_name: str) -> dict[str, str]:
        """Permanently delete a Nextcloud task list and every task inside it.

        WARNING: this is irreversible from this server's point of view -
        deleting the list deletes all of its tasks along with it. Confirm
        with the user before calling this.

        Args:
            list_name: Display name of the task list to delete.

        Returns:
            {"list_name": list_name} on success.
        """
        await _call(caldav_service.delete_task_list, list_name)
        return {"list_name": list_name}

    @mcp.tool
    async def rename_task_list(list_name: str, new_display_name: str) -> dict[str, str]:
        """Rename a Nextcloud task list. Only its display name changes, not its URL/id.

        Args:
            list_name: Current display name of the task list to rename.
            new_display_name: New display name for the list. The call fails
                if another list already has this exact name, instead of
                silently producing two identically-named lists.

        Returns:
            {"name": new display name, "url": internal CalDAV URL/ID} for the
            renamed list, in the same shape as one entry of list_task_lists.
        """
        return await _call(caldav_service.rename_task_list, list_name, new_display_name)

    @mcp.tool
    async def list_tasks(
        list_name: str,
        nur_offene: bool = True,
        faellig_vor: str | None = None,
        faellig_nach: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks in a Nextcloud task list.

        Args:
            list_name: Display name of the task list.
            nur_offene: If True (default), only return tasks that are not completed.
            faellig_vor: Optional ISO 8601 date/datetime; only return tasks due at or
                before this point. A date-only bound (e.g. "2026-07-20") includes
                tasks due at any time on that day.
            faellig_nach: Optional ISO 8601 date/datetime; only return tasks due at or
                after this point. A date-only bound includes tasks due from the start
                of that day onward.
            limit: Optional maximum number of results to return (must be > 0).

        If `faellig_vor` and/or `faellig_nach` is given, tasks with no faellig_datum
        (due date) at all are excluded - they can't be judged "before"/"after"
        anything. `limit` is applied after any due-date filtering.

        Returns:
            A list of task dicts with keys: uid, titel, start_datum, faellig_datum,
            prioritaet, fortschritt_prozent, status, ort, url, tags, notizen,
            uebergeordnete_uid (None unless the task is a subtask), wiederholung
            (raw RRULE text, e.g. "FREQ=WEEKLY;BYDAY=MO", or None if the task
            doesn't recur; read-only - this server can't create/edit recurrence).
        """
        return await _call(
            caldav_service.list_tasks,
            list_name,
            only_open=nur_offene,
            due_before=faellig_vor,
            due_after=faellig_nach,
            limit=limit,
        )

    @mcp.tool
    async def get_task(list_name: str, task_uid: str) -> dict[str, Any]:
        """Fetch a single task by UID, without listing the whole task list.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to fetch.

        Returns:
            A task dict with the same shape as one entry from list_tasks: uid,
            titel, start_datum, faellig_datum, prioritaet, fortschritt_prozent,
            status, ort, url, tags, notizen, uebergeordnete_uid, wiederholung.
        """
        return await _call(caldav_service.get_task, list_name, task_uid)

    @mcp.tool
    async def create_task(
        list_name: str,
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
    ) -> dict[str, str]:
        """Create a new task in a Nextcloud task list.

        Args:
            list_name: Display name of the target task list.
            titel: Task title (VTODO SUMMARY).
            start_datum: Optional ISO 8601 date/datetime -> DTSTART.
            faellig_datum: Optional ISO 8601 date/datetime -> DUE.
            prioritaet: Optional "hoch" / "mittel" / "niedrig" -> PRIORITY (1/5/9).
            fortschritt_prozent: Optional 0-100 -> PERCENT-COMPLETE.
            ort: Optional location -> LOCATION.
            url: Optional URL -> URL.
            tags: Optional list of category strings -> CATEGORIES.
            erinnerungen: Optional list of reminders, each either a relative RFC 5545
                duration (e.g. "-P1D", "-PT1H", relative to faellig_datum, falling
                back to start_datum) or an absolute ISO 8601 datetime -> VALARM.
            notizen: Optional notes -> DESCRIPTION.
            sichtbarkeit: Optional "öffentlich" / "privat" / "vertraulich" -> CLASS.
            uebergeordnete_aufgabe: Optional UID of an existing task to link this
                task to as a subtask -> RELATED-TO (RELTYPE=PARENT).

        Date/time semantics for start_datum and faellig_datum: a value that is
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
        new_uid = await _call(caldav_service.create_task, list_name, fields)
        return {"uid": new_uid}

    @mcp.tool
    async def update_task(
        list_name: str,
        task_uid: str,
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
                Accepted values: "start_datum", "faellig_datum", "prioritaet",
                "fortschritt_prozent", "ort", "url", "tags", "erinnerungen",
                "notizen", "sichtbarkeit", "uebergeordnete_aufgabe". "titel"
                cannot be cleared. Naming an unknown field, or naming a field
                here that is *also* given a new value in the same call, is an
                error.

        Returns:
            {"uid": task_uid} on success.
        """
        fields = mapping.TaskFields(
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

    @mcp.tool
    async def list_calendars() -> list[dict[str, Any]]:
        """List all Nextcloud event calendars (VEVENT); task-only lists are excluded.

        Returns:
            A list of {"name": display name, "url": internal CalDAV URL/ID,
            "farbe": "#RRGGBB" color or None, "komponenten": supported
            component names (e.g. ["VEVENT"])} dicts.
        """
        return await _call(caldav_service.list_calendars)

    @mcp.tool
    async def create_calendar(display_name: str, farbe: str | None = None) -> dict[str, Any]:
        """Create a new Nextcloud event calendar (a CalDAV collection supporting VEVENT).

        Args:
            display_name: Display name for the new calendar. A URL-safe
                collection id is generated from it automatically; a collision
                with an existing calendar (by display name or generated id)
                fails instead of silently reusing the existing one.
            farbe: Optional calendar color as "#RRGGBB" (or "#RRGGBBAA").

        Returns:
            {"name", "url", "farbe"} for the new calendar.
        """
        return await _call(caldav_service.create_calendar, display_name, farbe)

    @mcp.tool
    async def delete_calendar(calendar_name: str) -> dict[str, str]:
        """Permanently delete an event calendar and every event inside it.

        WARNING: this is irreversible from this server's point of view -
        deleting the calendar deletes all of its events along with it.
        Confirm with the user before calling this.

        Args:
            calendar_name: Display name of the calendar to delete.

        Returns:
            {"calendar_name": calendar_name} on success.
        """
        await _call(caldav_service.delete_calendar, calendar_name)
        return {"calendar_name": calendar_name}

    @mcp.tool
    async def update_calendar(
        calendar_name: str,
        new_display_name: str | None = None,
        farbe: str | None = None,
    ) -> dict[str, Any]:
        """Rename an event calendar and/or change its color. The URL/id stays stable.

        Args:
            calendar_name: Current display name of the calendar.
            new_display_name: Optional new display name; fails if another
                event calendar already has this exact name.
            farbe: Optional new color as "#RRGGBB" (or "#RRGGBBAA").

        At least one of new_display_name / farbe must be given.

        Returns:
            {"name", "url", "farbe"} for the updated calendar.
        """
        return await _call(caldav_service.update_calendar, calendar_name, new_display_name, farbe)

    @mcp.tool
    async def list_events(
        kalender_namen: list[str] | None = None,
        von: str | None = None,
        bis: str | None = None,
        suchtext: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        wiederholungen_aufloesen: bool = False,
    ) -> list[dict[str, Any]]:
        """List calendar events, across one, several, or all event calendars.

        Args:
            kalender_namen: Optional list of calendar display names to query;
                None queries every event calendar on the account.
            von: Optional ISO 8601 date/datetime lower bound. Recurring events
                with an occurrence inside the window are included. A date-only
                value means the start of that day.
            bis: Optional ISO 8601 date/datetime upper bound. A date-only
                value includes that entire day.
            suchtext: Optional case-insensitive substring filter over title,
                description and location.
            tag: Optional category/tag filter (exact, case-insensitive match).
            limit: Optional maximum number of results (must be > 0).
            wiederholungen_aufloesen: If True, expand recurring events into
                their individual occurrences within [von, bis] (both bounds
                required); each occurrence carries wiederholung_von.

        Naive datetimes (no UTC offset) are interpreted as UTC, like everywhere
        else in this server.

        Returns:
            Event dicts sorted by start, each with keys: uid, titel, start,
            ende (all-day: inclusive last day), ganztaegig, ort, beschreibung,
            tags, status ("bestätigt"/"vorläufig"/"abgesagt" or None),
            sichtbarkeit, wiederholung (raw RRULE text or None), ausnahme_daten,
            url, verknuepfte_aufgaben (RELATED-TO links; each entry's
            "beziehung" uses the same values as link_task_to_event's
            beziehung parameter - "zeitblock"/"voraussetzung" - plus
            "gleichrangig" or a raw lowercased RELTYPE for links written by
            other CalDAV clients), wiederholung_von, kalender (the calendar's
            display name), organisator ({"email", "name"} or None), teilnehmer
            (list of {"email", "name", "status", "rolle", "rsvp"}; "status" is
            "ausstehend"/"zugesagt"/"abgesagt"/"vorläufig"/"delegiert").
        """
        return await _call(
            caldav_service.list_events,
            calendar_names=kalender_namen,
            von=von,
            bis=bis,
            suchtext=suchtext,
            tag=tag,
            limit=limit,
            expand=wiederholungen_aufloesen,
        )

    @mcp.tool
    async def get_event(kalender_name: str, event_uid: str) -> dict[str, Any]:
        """Fetch a single event by UID.

        Args:
            kalender_name: Display name of the calendar containing the event.
            event_uid: UID of the event to fetch.

        Returns:
            An event dict with the same shape as one entry from list_events.
        """
        return await _call(caldav_service.get_event, kalender_name, event_uid)

    @mcp.tool
    async def create_event(
        kalender_name: str,
        titel: str,
        start: str,
        ende: str | None = None,
        ort: str | None = None,
        beschreibung: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        sichtbarkeit: str | None = None,
        wiederholung: str | None = None,
        ausnahme_daten: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        url: str | None = None,
        verknuepfte_aufgabe: str | None = None,
        teilnehmer: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """Create a new calendar event.

        Args:
            kalender_name: Display name of the target event calendar.
            titel: Event title (VEVENT SUMMARY).
            start: ISO 8601 start -> DTSTART. Exactly "YYYY-MM-DD" creates an
                all-day event; naive datetimes are interpreted as UTC.
            ende: Optional ISO 8601 end -> DTEND. For all-day events this is
                the last day INCLUSIVE (e.g. start="2026-07-20",
                ende="2026-07-21" spans two days). start and ende must both be
                dates or both be datetimes.
            ort: Optional location -> LOCATION.
            beschreibung: Optional description -> DESCRIPTION.
            tags: Optional list of category strings -> CATEGORIES.
            status: Optional "bestätigt" / "vorläufig" / "abgesagt" -> STATUS.
            sichtbarkeit: Optional "öffentlich" / "privat" / "vertraulich" -> CLASS.
            wiederholung: Optional recurrence rule as raw RFC 5545 RRULE text,
                e.g. "FREQ=WEEKLY;BYDAY=MO" -> RRULE.
            ausnahme_daten: Optional ISO 8601 dates/datetimes of skipped
                occurrences of a recurring event -> EXDATE.
            erinnerungen: Optional reminders, each either a relative RFC 5545
                duration before the start (e.g. "-PT30M", "-P1D") or an
                absolute ISO 8601 datetime -> VALARM.
            url: Optional URL -> URL.
            verknuepfte_aufgabe: Optional UID of an existing task this event
                reserves time for -> RELATED-TO;RELTYPE=PARENT on the event
                (the "zeitblock" semantics of link_task_to_event; reading the
                event back via list_events/get_event surfaces this as a
                verknuepfte_aufgaben entry with beziehung "zeitblock").
            teilnehmer: Optional list of attendees -> ATTENDEE. Each entry:
                {"email": required, "name": optional, "rolle": optional
                "leitung"/"erforderlich"/"optional"/"keine-teilnahme" (default
                "erforderlich"), "rsvp": optional bool (default True)}. The
                first time attendees are added to an event with none yet,
                ORGANIZER is set to your own account's address automatically.
                IMPORTANT: Nextcloud's CalDAV server does server-side
                scheduling - saving an event with ORGANIZER+ATTENDEE sends
                iMIP invitation mails automatically; this tool does not send
                any mail itself.

        Returns:
            {"uid": the new event's UID}.
        """
        fields = event_mapping.EventFields(
            titel=titel,
            start=start,
            ende=ende,
            ort=ort,
            beschreibung=beschreibung,
            tags=tags,
            status=status,
            sichtbarkeit=sichtbarkeit,
            wiederholung=wiederholung,
            ausnahme_daten=ausnahme_daten,
            erinnerungen=erinnerungen,
            url=url,
            verknuepfte_aufgabe=verknuepfte_aufgabe,
            teilnehmer=teilnehmer,
        )
        new_uid = await _call(caldav_service.create_event, kalender_name, fields)
        return {"uid": new_uid}

    @mcp.tool
    async def update_event(
        kalender_name: str,
        event_uid: str,
        titel: str | None = None,
        start: str | None = None,
        ende: str | None = None,
        ort: str | None = None,
        beschreibung: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        sichtbarkeit: str | None = None,
        wiederholung: str | None = None,
        ausnahme_daten: list[str] | None = None,
        erinnerungen: list[str] | None = None,
        url: str | None = None,
        verknuepfte_aufgabe: str | None = None,
        teilnehmer: list[dict[str, Any]] | None = None,
        felder_leeren: list[str] | None = None,
    ) -> dict[str, str]:
        """Update an existing event. Only fields that are explicitly given are changed.

        Args:
            kalender_name: Display name of the calendar containing the event.
            event_uid: UID of the event to update.
            (all other args): Same meaning and mapping as in create_event; a
                field left as None is left unchanged. To move a single
                occurrence of a recurring event, add its original date to
                ausnahme_daten and create a separate replacement event.
            teilnehmer: Optional, same shape as in create_event. Setting this
                REPLACES the event's entire attendee list (it is not an
                append). As in create_event, ORGANIZER is set to your own
                account's address the first time attendees are added to an
                event that has none yet; Nextcloud sends iMIP invitation
                mails server-side once the event is saved, not this tool. To
                respond to an event you were invited to (set your own RSVP
                status), use respond_to_event instead of this tool.
            felder_leeren: Optional list of field names to clear (remove the
                property entirely). Accepted values: "ende", "ort",
                "beschreibung", "tags", "status", "sichtbarkeit",
                "wiederholung", "ausnahme_daten", "erinnerungen", "url",
                "verknuepfte_aufgabe", "teilnehmer" (clearing "teilnehmer"
                removes every attendee and, if none remain, ORGANIZER too).
                "titel" and "start" cannot be cleared. Naming an unknown
                field, or naming a field that is also given a new value in
                the same call, is an error.

        Returns:
            {"uid": event_uid} on success.
        """
        fields = event_mapping.EventFields(
            titel=titel,
            start=start,
            ende=ende,
            ort=ort,
            beschreibung=beschreibung,
            tags=tags,
            status=status,
            sichtbarkeit=sichtbarkeit,
            wiederholung=wiederholung,
            ausnahme_daten=ausnahme_daten,
            erinnerungen=erinnerungen,
            url=url,
            verknuepfte_aufgabe=verknuepfte_aufgabe,
            teilnehmer=teilnehmer,
            clear=tuple(felder_leeren) if felder_leeren else (),
        )
        await _call(caldav_service.update_event, kalender_name, event_uid, fields)
        return {"uid": event_uid}

    @mcp.tool
    async def delete_event(kalender_name: str, event_uid: str) -> dict[str, str]:
        """Permanently delete an event.

        Args:
            kalender_name: Display name of the calendar containing the event.
            event_uid: UID of the event to delete.

        Returns:
            {"uid": event_uid} on success.
        """
        await _call(caldav_service.delete_event, kalender_name, event_uid)
        return {"uid": event_uid}

    @mcp.tool
    async def respond_to_event(
        kalender_name: str,
        event_uid: str,
        antwort: str,
        kommentar: str | None = None,
    ) -> dict[str, str]:
        """Reply to a calendar invitation - set your own RSVP status on an event.

        Finds your own ATTENDEE entry on the event by matching it against
        your account's CalDAV calendar-user-addresses, and sets its PARTSTAT.
        Fails with a clear error if you are not listed as an attendee of this
        event at all. Saves the event afterwards; Nextcloud's CalDAV server
        propagates the reply to the organizer as an iMIP/iTIP REPLY mail
        automatically - this tool does not send any mail itself.

        Args:
            kalender_name: Display name of the calendar containing the event
                (typically the calendar the invitation landed in).
            event_uid: UID of the event to respond to.
            antwort: One of "zugesagt" (accept), "abgesagt" (decline),
                "vorläufig" (tentative) -> ATTENDEE PARTSTAT.
            kommentar: Optional comment to attach to the reply -> COMMENT.

        Returns:
            {"uid": event_uid, "antwort": antwort} on success.
        """
        await _call(caldav_service.respond_to_event, kalender_name, event_uid, antwort, kommentar)
        return {"uid": event_uid, "antwort": antwort}

    @mcp.tool
    async def link_task_to_event(
        list_name: str,
        task_uid: str,
        kalender_name: str,
        event_uid: str,
        beziehung: str = "zeitblock",
    ) -> dict[str, str]:
        """Link an existing task to an existing calendar event (RELATED-TO).

        The link is stored on the event (the Nextcloud Tasks UI would
        misrender a task-side link as a broken subtask), and shows up in the
        event's verknuepfte_aufgaben with a "beziehung" equal to the
        `beziehung` value passed here - the request and response vocabulary
        is identical ("zeitblock"/"voraussetzung"), so a link written as
        "zeitblock" reads back as "zeitblock", never "uebergeordnet" or
        similar internal RELTYPE naming.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to link.
            kalender_name: Display name of the calendar containing the event.
            event_uid: UID of the event to link.
            beziehung: "zeitblock" (default) - the event reserves time to work
                on the task; or "voraussetzung" - the event must happen before
                the task can be completed.

        Returns:
            {"task_uid", "event_uid", "beziehung"} on success.
        """
        await _call(
            caldav_service.link_task_to_event,
            list_name,
            task_uid,
            kalender_name,
            event_uid,
            beziehung,
        )
        return {"task_uid": task_uid, "event_uid": event_uid, "beziehung": beziehung}

    @mcp.tool
    async def list_events_for_task(
        list_name: str,
        task_uid: str,
        kalender_namen: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find events linked to a task - the task-side counterpart of link_task_to_event.

        link_task_to_event stores the RELATED-TO link on the event only (see
        its docstring for why), so there is normally no way to discover a
        link starting from the task; this tool does the reverse lookup by
        scanning the queried calendars' events for a verknuepfte_aufgaben
        entry pointing at task_uid.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to find linked events for.
            kalender_namen: Optional list of calendar display names to
                search; None searches every event calendar on the account.

        Returns:
            Event dicts (same shape as list_events entries, each with an
            added "kalender_name" key), sorted by start.
        """
        return await _call(
            caldav_service.list_events_for_task,
            list_name,
            task_uid,
            calendar_names=kalender_namen,
        )

    @mcp.tool
    async def create_event_from_task(
        list_name: str,
        task_uid: str,
        kalender_name: str,
        start: str | None = None,
        dauer_minuten: int = 60,
    ) -> dict[str, str]:
        """Create a calendar event from an existing task (timeboxing) and link them.

        Title, notes, location and tags are copied from the task. The event is
        linked back to the task via RELATED-TO (the "zeitblock" semantics of
        link_task_to_event); the task itself is not modified. The new event's
        verknuepfte_aufgaben will show this task with beziehung "zeitblock",
        same as if link_task_to_event had been called explicitly.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to convert.
            kalender_name: Display name of the calendar for the new event.
            start: Optional ISO 8601 start for the event; defaults to the
                task's faellig_datum (due date). Fails if neither is given. A
                date-only start produces a one-day all-day event.
            dauer_minuten: Event duration in minutes (default 60); ignored for
                all-day events.

        Returns:
            {"uid": the new event's UID, "task_uid": task_uid}.
        """
        new_uid = await _call(
            caldav_service.create_event_from_task,
            list_name,
            task_uid,
            kalender_name,
            start,
            dauer_minuten,
        )
        return {"uid": new_uid, "task_uid": task_uid}

    @mcp.tool
    async def get_agenda(
        datum: str,
        kalender_namen: list[str] | None = None,
        listen_namen: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one day's calendar events and due tasks together (agenda view).

        Args:
            datum: The day as a date-only "YYYY-MM-DD" string. Day boundaries
                are UTC, consistent with the naive-input-is-UTC rule used
                everywhere else in this server.
            kalender_namen: Optional list of event calendars to include;
                None means all.
            listen_namen: Optional list of task lists to include; None means
                all.

        Returns:
            {"datum": the day, "termine": event dicts (recurring events
            expanded to that day's occurrences, sorted by start), "aufgaben":
            open tasks due that day, each with an added "liste" key}.
        """
        return await _call(
            caldav_service.get_agenda,
            datum,
            calendar_names=kalender_namen,
            list_names=listen_namen,
        )

    return mcp


def main() -> None:
    """Entry point: read config from the environment and run the HTTP server."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    mcp = build_server(settings)
    # MCP_OAUTH_PASSWORD now only ever travels in the POST body of the
    # /consent form (personal_auth.py, LOCAL PATCH 5), which Uvicorn never
    # logs - but its default access log still records full request paths
    # including query strings, which for /consent carry the single-use pending
    # keys gating authorization. Keep the access log disabled.
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    main()
