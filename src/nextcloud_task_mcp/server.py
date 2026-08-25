"""FastMCP server exposing Nextcloud Tasks (CalDAV) as MCP tools."""

# No `from __future__ import annotations` here: with PEP 563 string annotations,
# fastmcp (<3) rebuilds each tool function to resolve them and drops
# `__kwdefaults__` in the process, so every keyword-only parameter loses its
# default and is marked required in the MCP schema clients see.

import functools
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import anyio.to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import event_mapping, mapping, notes_mapping
from .caldav_client import CalDavService
from .config import Settings, is_local_hostname
from .errors import TaskMcpError
from .notes_client import NotesService
from .personal_auth import PersonalAuthProvider

logger = logging.getLogger(__name__)

_EXDATE_COMPACT_THRESHOLD = 10

#: `kompakt=True` truncates free text (beschreibung/notizen) to this many chars.
_COMPACT_TEXT_LIMIT = 200

#: Window applied when list_events is called with neither calendars nor bounds.
_DEFAULT_EVENT_WINDOW_DAYS = 90

#: Every key a list_events entry can carry - the vocabulary `felder` validates
#: against. Must track `event_mapping.parse_vevent` plus the "kalender" key the
#: client layer adds.
_EVENT_RESULT_KEYS = frozenset(
    {
        "uid",
        "titel",
        "start",
        "ende",
        "ganztaegig",
        "ort",
        "beschreibung",
        "tags",
        "erinnerungen",
        "status",
        "sichtbarkeit",
        "wiederholung",
        "ausnahme_daten",
        "url",
        "verknuepfte_aufgaben",
        "wiederholung_von",
        "kalender",
        "organisator",
        "teilnehmer",
    }
)

#: Every key a list_tasks entry can carry - tracks `mapping.parse_vtodo` plus
#: the "liste"/"liste_url" keys the client layer adds.
_TASK_RESULT_KEYS = frozenset(
    {
        "uid",
        "titel",
        "start_datum",
        "faellig_datum",
        "prioritaet",
        "fortschritt_prozent",
        "status",
        "ort",
        "url",
        "tags",
        "erinnerungen",
        "notizen",
        "uebergeordnete_uid",
        "wiederholung",
        "ausnahme_daten",
        "wiederholung_von",
        "serie_uid",
        "liste",
        "liste_url",
    }
)


def _slim_rows(
    rows: list[dict[str, Any]],
    *,
    felder: list[str] | None,
    kompakt: bool,
    valid_keys: frozenset[str],
    text_key: str,
    detail_tool: str,
    kompakt_drop: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Apply the `felder` whitelist and/or `kompakt` mode to listing results.

    `felder` keeps only the named keys (validated against `valid_keys`, so a
    typo errors instead of silently returning nothing). `kompakt` then drops
    keys whose value is None/[]/"" plus everything in `kompakt_drop`, and
    truncates `text_key` to `_COMPACT_TEXT_LIMIT` characters. A key the caller
    whitelisted explicitly is exempt from `kompakt_drop` - asking for it wins.

    An empty `felder` list means "no whitelist", not "no keys": the only other
    reading returns a row of nothing per result, which no caller can want, and
    MCP clients routinely send `[]` for an array parameter they mean to leave
    unset. (`listen_namen=[]` reads the other way - an empty *scope* returns
    no rows, which is a coherent answer - so the two differ deliberately.)

    Falsy-but-real values survive: the emptiness test is `== []`/`== ""`
    against those two literals only, so `fortschritt_prozent=0` and
    `ganztaegig=False` are kept, and `ausnahme_daten` already summarized into
    a dict by `list_events` is kept too.
    """
    if isinstance(felder, str):
        felder = [felder]
    if felder:
        unknown = sorted(set(felder) - valid_keys)
        if unknown:
            raise ToolError(
                f"Unbekannte felder-Einträge: {', '.join(unknown)}. "
                f"Gültige Feldnamen: {', '.join(sorted(valid_keys))}"
            )
        wanted = set(felder)
        rows = [{key: value for key, value in row.items() if key in wanted} for row in rows]
        kompakt_drop = kompakt_drop - wanted
    if not kompakt:
        return rows
    slimmed: list[dict[str, Any]] = []
    for row in rows:
        out: dict[str, Any] = {}
        for key, value in row.items():
            if key in kompakt_drop:
                continue
            if value is None or value == [] or value == "":
                continue
            if key == text_key and isinstance(value, str) and len(value) > _COMPACT_TEXT_LIMIT:
                value = (
                    value[:_COMPACT_TEXT_LIMIT]
                    + f"… [gekürzt von {len(value)} Zeichen - Volltext über {detail_tool}]"
                )
            out[key] = value
        slimmed.append(out)
    return slimmed


# MCP tool annotations (spec: ToolAnnotations). These are behaviour *hints* for
# clients, not enforcement, but they are the only signal a client has for
# deciding which calls it may run on its own and which need a human to approve
# them. A server that ships no annotations gets every one of its tools treated
# as potentially destructive, so even a plain listing sits behind an approval
# prompt - and when that prompt goes unanswered the call fails client-side
# ("No approval received") without the server ever seeing a request.
#
# `openWorldHint` is True throughout: every tool here talks to a remote
# Nextcloud instance over CalDAV or the Notes REST API.

#: Reads only. Safe for a client to run without asking.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

#: Creates something new. Additive, so re-running adds another copy rather than
#: clobbering anything - hence not destructive, but not idempotent either.
_CREATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

#: Overwrites or removes existing state. Re-running with the same arguments
#: lands on the same end state, so idempotent, but the original is gone.
_MODIFY = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

#: Adds to existing state without discarding any of it (a share, a link, a
#: restore), and converges on the same end state when repeated.
_ADD = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


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


async def _call_notes(coro: Any) -> Any:
    """Await a NotesService coroutine call, translating errors like `_call`.

    Unlike `_call`, there's no blocking library call to move off the event
    loop - `NotesService` talks to the Notes REST API over `httpx` natively
    async - so this just awaits directly instead of using
    `anyio.to_thread.run_sync`.
    """
    try:
        return await coro
    except TaskMcpError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - safety net for unforeseen failures
        logger.exception("Unexpected error in Notes API call")
        raise ToolError("An unexpected internal error occurred.") from exc


def build_server(
    settings: Settings,
    service: CalDavService | None = None,
    notes_service: NotesService | None = None,
) -> FastMCP:
    """Construct the FastMCP server with OAuth 2.1 auth and all task tools registered.

    `service`/`notes_service` can be injected for testing; default to a real
    CalDavService/NotesService built from `settings`.
    """
    mapping.set_default_timezone(settings.default_timezone)
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
    notes_svc = notes_service or NotesService(
        base_url=settings.notes_base_url,
        username=settings.caldav_username,
        password=settings.caldav_password,
        # Shared with CalDAV's timeout - both are just "how long to wait for
        # a Nextcloud HTTP request", not worth a second env var for.
        timeout=settings.caldav_timeout_seconds,
    )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_task_lists() -> list[dict[str, str]]:
        """List all available Nextcloud task lists.

        Returns:
            A list of {"name": display name, "url": internal CalDAV URL/ID} dicts.
        """
        return await _call(caldav_service.list_task_lists)

    @mcp.tool(annotations=_CREATE)
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

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_READ_ONLY)
    async def list_tasks(
        listen_namen: list[str] | None = None,
        nur_offene: bool = True,
        faellig_vor: str | None = None,
        faellig_nach: str | None = None,
        limit: int | None = None,
        *,
        prioritaet: str | None = None,
        tag: str | None = None,
        suchtext: str | None = None,
        felder: list[str] | None = None,
        kompakt: bool = False,
        list_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks across one, several, or all Nextcloud task lists.

        Args:
            listen_namen: Optional list of task list display names to query;
                None queries **every** task list on the account. That is one
                request per list and returns every open task in the account
                unless something narrows it - pass `listen_namen`, a due-date
                bound, or `limit` unless you really want the lot.
            nur_offene: If True (default), only return tasks that are not
                completed - "not completed" here means STATUS is neither
                COMPLETED nor CANCELLED (and no COMPLETED timestamp is set):
                a task with status="abgesagt" is excluded just like one with
                status="erledigt", it is not treated as "still open". This is
                the caldav library's own three-way "pending" query
                (`Calendar.todos(include_completed=...)`), not something this
                server layers on top.
            faellig_vor: Optional ISO 8601 date/datetime; only return tasks due at or
                before this point. A date-only bound (e.g. "2026-07-20") includes
                tasks due at any time on that day.
            faellig_nach: Optional ISO 8601 date/datetime; only return tasks due at or
                after this point. A date-only bound includes tasks due from the start
                of that day onward.
            limit: Optional maximum number of results to return (must be > 0).
            prioritaet: Optional priority filter ("hoch", "mittel", "niedrig").
            tag: Optional category/tag filter (exact match).
            suchtext: Optional substring filter over title (titel) and notes
                (notizen). Both it and `tag` ignore case and Unicode spelling
                ("STRASSE" matches "Straße").
            felder: Optional whitelist of result keys (see Returns for the
                vocabulary); every other key is omitted from each task dict.
                Unknown names error, an empty list means "no whitelist" (unlike
                an empty `listen_namen`, which is an empty scope). Use this to
                keep payloads small when you only need a few fields.
            kompakt: If True, omit keys whose value is None, [] or "" plus the
                rarely useful liste_url (unless whitelisted via `felder`), and
                truncate notizen to 200 characters (marked with "… [gekürzt
                ...]"; get_task returns the full text). Values are otherwise
                unchanged - an absent key just means empty/None. Combines with
                `felder` (whitelist first, then compaction).
            list_name: Deprecated alias for `listen_namen` (takes a single list display name).
                Pass `listen_namen` instead. Passing both `list_name` and `listen_namen`
                is an error.

        An empty string is "no filter" for every filter that takes one -
        prioritaet, tag, suchtext, faellig_vor and faellig_nach alike. (`limit`
        still rejects 0: omit it rather than ask for zero results.) An empty
        `listen_namen` list is an empty scope and returns nothing.

        If `faellig_vor` and/or `faellig_nach` is given, tasks with no readable
        faellig_datum (due date) are excluded - they can't be judged "before"/"after"
        anything. Results are sorted by faellig_datum ascending (those tasks last),
        then by titel. `limit` is applied last, after merging across lists.

        Recurring tasks (wiederholung) and `faellig_vor`: a recurring task is
        stored once but is due many times, so when `faellig_vor` is given it is
        expanded into one row per occurrence due inside the window - that is the
        only way "what is due next week" can include a weekly task started in
        March. Without `faellig_vor` there is no window to expand into and the
        series is returned as the single row it is stored as, wiederholung
        intact. At most 100 occurrences per task are produced.

        What an expanded row is, and is not: it is a read-only view of one date
        in a series. `wiederholung_von` names its occurrence, `wiederholung` is
        None, `serie_uid` is the stored task's uid, and its own `uid` is a
        synthetic "<serie_uid>#<occurrence>" that update_task, complete_task,
        delete_task and get_task all reject with an explanation. To act on the
        series, pass `serie_uid` - but note that update_task changes every
        occurrence and complete_task ends the whole series (it does not roll it
        forward). To make the series skip one date, add that date to its
        ausnahme_daten via update_task.

        Returns:
            A list of task dicts with keys: uid, titel, start_datum, faellig_datum,
            prioritaet, fortschritt_prozent, status ("offen"/"in-arbeit"/
            "erledigt"/"abgesagt" - see update_task's status parameter to set
            it), ort, url, tags, erinnerungen
            (list of reminder strings, each either a relative RFC 5545 duration
            like "-PT30M" or an absolute ISO 8601 datetime like
            "2026-08-07T09:00:00+00:00", exactly what create_task/update_task
            accepts; alarms whose trigger this form cannot express are omitted,
            and update_task leaves those untouched), notizen,
            uebergeordnete_uid (None unless the task is a
            subtask), wiederholung (raw RRULE text, e.g. "FREQ=WEEKLY;BYDAY=MO",
            or None if the task doesn't recur or is an expanded occurrence -
            see create_task/update_task to set it), ausnahme_daten (the
            occurrences the series skips, [] if none), wiederholung_von and
            serie_uid (both None unless the row is an expanded occurrence, see
            above), liste (the display name of the task list
            containing the task), and liste_url (the collection URL of the task
            list, which tells same-named lists apart, though no tool accepts a
            URL to act on them - an ambiguous name still must be renamed).
        """
        if list_name is not None and listen_namen is not None:
            raise ToolError("list_name is the deprecated alias of listen_namen; pass only one")

        target_list_names: list[str] | None
        if list_name is not None:
            target_list_names = [list_name]
        elif isinstance(listen_namen, str):
            target_list_names = [listen_namen]
        else:
            target_list_names = listen_namen

        tasks: list[dict[str, Any]] = await _call(
            caldav_service.list_tasks,
            list_names=target_list_names,
            only_open=nur_offene,
            due_before=faellig_vor,
            due_after=faellig_nach,
            prioritaet=prioritaet,
            tag=tag,
            suchtext=suchtext,
            limit=limit,
        )
        return _slim_rows(
            tasks,
            felder=felder,
            kompakt=kompakt,
            valid_keys=_TASK_RESULT_KEYS,
            text_key="notizen",
            detail_tool="get_task",
            kompakt_drop=frozenset({"liste_url"}),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_task(list_name: str, task_uid: str) -> dict[str, Any]:
        """Fetch a single task by UID, without listing the whole task list.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to fetch.

        Returns:
            A task dict holding what one entry from list_tasks holds, minus its
            "liste" key (the list is `list_name`, which you passed): uid, titel,
            start_datum, faellig_datum, prioritaet, fortschritt_prozent, status,
            ort, url, tags, erinnerungen, notizen, uebergeordnete_uid,
            wiederholung.
        """
        return await _call(caldav_service.get_task, list_name, task_uid)

    @mcp.tool(annotations=_CREATE)
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
        wiederholung: str | None = None,
        ausnahme_daten: list[str] | None = None,
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
                The leading "-" is what makes a relative reminder fire *before*
                that date; a positive duration ("PT30M") is valid and means 30
                minutes *after* it.
            notizen: Optional notes -> DESCRIPTION.
            sichtbarkeit: Optional "öffentlich" / "privat" / "vertraulich" -> CLASS.
            uebergeordnete_aufgabe: Optional UID of an existing task to link this
                task to as a subtask -> RELATED-TO (RELTYPE=PARENT).
            wiederholung: Optional recurrence rule as raw RFC 5545 RRULE text,
                e.g. "FREQ=WEEKLY;BYDAY=MO" -> RRULE. Requires the task to
                have a start_datum or faellig_datum (in this same call) to
                recur from - a task with neither is rejected.
            ausnahme_daten: Optional ISO 8601 dates/datetimes of skipped
                occurrences of a recurring task -> EXDATE. Each entry must be
                the same value kind as the task's start_datum (date-only for an
                all-day task, a full datetime otherwise) and must name an
                occurrence the wiederholung actually produces; an entry that
                would cancel nothing is rejected rather than stored.

        Date/time semantics for start_datum and faellig_datum: a value that is
        exactly "YYYY-MM-DD" (e.g. "2026-07-20") creates an all-day entry
        (iCalendar VALUE=DATE). Any other ISO 8601 value is stored as a
        datetime; a *naive* datetime (no UTC offset, e.g.
        "2026-07-20T14:00:00") is interpreted in the server's default timezone
        (`MCP_DEFAULT_TIMEZONE`, default Europe/Berlin). A datetime may instead
        be followed by a space and an IANA timezone name, e.g.
        "2026-07-20T14:00:00 Europe/Berlin" - the correct offset (standard or
        daylight time) is then resolved for that specific date, so callers
        don't need to work out themselves whether e.g. CET or CEST applies.
        Combining a numeric offset with a timezone name is rejected.

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
            wiederholung=wiederholung,
            ausnahme_daten=ausnahme_daten,
        )
        new_uid = await _call(caldav_service.create_task, list_name, fields)
        return {"uid": new_uid}

    @mcp.tool(annotations=_MODIFY)
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
        wiederholung: str | None = None,
        ausnahme_daten: list[str] | None = None,
        status: str | None = None,
        felder_leeren: list[str] | None = None,
    ) -> dict[str, str]:
        """Update an existing task. Only fields that are explicitly given are changed.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to update.
            (all other args): Same meaning and mapping as in create_task; a field
                left as None is left unchanged on the existing task. Date/time
                semantics also match create_task: a "YYYY-MM-DD" value creates an
                all-day entry, and naive datetimes are interpreted in the
                server's default timezone (`MCP_DEFAULT_TIMEZONE`, default Europe/Berlin).
                wiederholung's anchor requirement (start_datum or faellig_datum)
                is checked against the task's final state, so setting only
                wiederholung succeeds as long as the task already has a
                start_datum or faellig_datum from before this call.
            status: Optional "offen" / "in-arbeit" / "erledigt" / "abgesagt" ->
                STATUS. "erledigt" behaves like complete_task (also sets
                PERCENT-COMPLETE=100 and the COMPLETED timestamp); "offen" is
                the reopen path for a task completed by mistake (removes
                COMPLETED and resets PERCENT-COMPLETE to 0); "in-arbeit" and
                "abgesagt" only set STATUS. If this call also passes
                fortschritt_prozent, that explicit value wins over whatever
                percentage status would otherwise derive. An unknown value is
                a speaking error naming the accepted labels; nothing is
                written to the task in that case. Not accepted in
                felder_leeren - set status="offen" to reopen a task instead.
            felder_leeren: Optional list of field names to clear (remove the
                property from the task entirely) instead of changing them.
                Accepted values: "start_datum", "faellig_datum", "prioritaet",
                "fortschritt_prozent", "ort", "url", "tags", "erinnerungen",
                "notizen", "sichtbarkeit", "uebergeordnete_aufgabe",
                "wiederholung", "ausnahme_daten". Clearing "wiederholung" also
                drops the task's ausnahme_daten (EXDATE) and any RDATE, which
                cancel and add nothing once the series is gone. "titel" cannot
                be cleared. Naming an unknown
                field, or naming a field here that is *also* given a new
                value in the same call, is an error.

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
            wiederholung=wiederholung,
            ausnahme_daten=ausnahme_daten,
            status=status,
            clear=tuple(felder_leeren) if felder_leeren else (),
        )
        await _call(caldav_service.update_task, list_name, task_uid, fields)
        return {"uid": task_uid}

    @mcp.tool(annotations=_MODIFY)
    async def complete_task(list_name: str, task_uid: str) -> dict[str, str]:
        """Mark a task as completed (sets STATUS, PERCENT-COMPLETE and COMPLETED timestamp).

        Warning: for a recurring task, completing it (unlike in the Nextcloud
        UI) does not automatically roll the series forward to the next
        occurrence; instead, it hard-ends the series by marking the entire
        recurring task as done. To advance a series instead, use
        `update_task` on its `faellig_datum`.

        A task completed by mistake can be reopened afterwards with
        update_task's status="offen" (removes COMPLETED, resets
        PERCENT-COMPLETE to 0) - there is no separate "uncomplete" tool.

        Args:
            list_name: Display name of the task list containing the task.
            task_uid: UID of the task to complete.

        Returns:
            {"uid": task_uid} on success.
        """
        await _call(caldav_service.complete_task, list_name, task_uid)
        return {"uid": task_uid}

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_MODIFY)
    async def move_task(list_name: str, task_uid: str, ziel_liste: str) -> dict[str, str]:
        """Verschiebt eine Aufgabe in eine andere Aufgabenliste.

        Args:
            list_name: Anzeige-Name der Quell-Aufgabenliste.
            task_uid: UID der zu verschiebenden Aufgabe.
            ziel_liste: Anzeige-Name der Ziel-Aufgabenliste.

        Returns:
            {"uid": ..., "von": Quell-Liste, "nach": Ziel-Liste,
            "methode": "MOVE" | "kopiert"}
        """

        res: dict[str, str] = await _call(caldav_service.move_task, list_name, task_uid, ziel_liste)
        return res

    @mcp.tool(annotations=_READ_ONLY)
    async def list_calendars() -> list[dict[str, Any]]:
        """List all Nextcloud event calendars (VEVENT); task-only lists are excluded.

        Returns:
            A list of {"name": display name, "url": internal CalDAV URL/ID,
            "farbe": "#RRGGBB" color or None, "komponenten": supported
            component names (e.g. ["VEVENT"])} dicts.
        """
        return await _call(caldav_service.list_calendars)

    @mcp.tool(annotations=_CREATE)
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

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_READ_ONLY)
    async def list_events(
        kalender_namen: list[str] | None = None,
        von: str | None = None,
        bis: str | None = None,
        suchtext: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        wiederholungen_aufloesen: bool = False,
        felder: list[str] | None = None,
        kompakt: bool = False,
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
            felder: Optional whitelist of result keys (see Returns for the
                vocabulary); every other key is omitted from each event dict.
                Unknown names error, an empty list means "no whitelist". Use
                this to keep payloads small when you only need a few fields.
            kompakt: If True, omit keys whose value is None, [] or "" (e.g.
                teilnehmer, organisator, wiederholung on most events) and
                truncate beschreibung to 200 characters (marked with
                "… [gekürzt ...]"; get_event returns the full text). Values
                are otherwise unchanged - an absent key just means empty/None.
                Combines with `felder` (whitelist first, then compaction).

        Called with neither `kalender_namen` nor a time bound, this would scan
        every event in the account; instead a default window of today ±90 days
        (in the server's default timezone) is applied. Pass `von` and/or `bis`
        explicitly - or name a calendar - to query outside that window. Naming
        a calendar is a scoping decision, so it turns the default window off
        rather than narrowing it: `kalender_namen` plus
        `wiederholungen_aufloesen=True` and no bounds still fails with
        "requires both von and bis", the same as before this default existed.

        Naive datetimes (no UTC offset) are interpreted in the server's default
        timezone (`MCP_DEFAULT_TIMEZONE`, default Europe/Berlin), like everywhere else
        in this server.

        Returns:
            Event dicts sorted by start, each with keys: uid, titel, start,
            ende (all-day: inclusive last day), ganztaegig, ort, beschreibung,
            tags, erinnerungen (list of reminder strings, each either a relative
            RFC 5545 duration like "-PT30M" or an absolute ISO 8601 datetime
            like "2026-08-07T09:00:00+00:00", exactly what create_event/update_event
            accepts; alarms whose trigger this form cannot express - an
            end-anchored one, say - are omitted, and update_event leaves those
            untouched), status ("bestätigt"/"vorläufig"/"abgesagt" or None),
            sichtbarkeit, wiederholung (raw RRULE text or None), ausnahme_daten
            (list of EXDATE strings, or if >10 entries, a summary dict
            {"anzahl", "erste", "hinweis"}; call get_event for full list),
            url, verknuepfte_aufgaben (RELATED-TO links; each entry's
            "beziehung" uses the same values as link_task_to_event's
            beziehung parameter - "zeitblock"/"voraussetzung" - plus
            "gleichrangig" or a raw lowercased RELTYPE for links written by
            other CalDAV clients), wiederholung_von, kalender (the calendar's
            display name), organisator ({"email", "name"} or None), teilnehmer
            (list of {"email", "name", "status", "rolle", "rsvp"}; "status" is
            "ausstehend"/"zugesagt"/"abgesagt"/"vorläufig"/"delegiert").
        """
        if kalender_namen is None and not von and not bis:
            today = datetime.now(mapping.get_default_timezone()).date()
            von = (today - timedelta(days=_DEFAULT_EVENT_WINDOW_DAYS)).isoformat()
            bis = (today + timedelta(days=_DEFAULT_EVENT_WINDOW_DAYS)).isoformat()
        events: list[dict[str, Any]] = await _call(
            caldav_service.list_events,
            calendar_names=kalender_namen,
            von=von,
            bis=bis,
            suchtext=suchtext,
            tag=tag,
            limit=limit,
            expand=wiederholungen_aufloesen,
        )
        processed_events: list[dict[str, Any]] = []
        for event in events:
            exdates = event.get("ausnahme_daten")
            if isinstance(exdates, list) and len(exdates) > _EXDATE_COMPACT_THRESHOLD:
                event = dict(event)
                event["ausnahme_daten"] = {
                    "anzahl": len(exdates),
                    "erste": exdates[:5],
                    "hinweis": "gekürzt - vollständige Liste über get_event abrufen",
                }
            processed_events.append(event)
        return _slim_rows(
            processed_events,
            felder=felder,
            kompakt=kompakt,
            valid_keys=_EVENT_RESULT_KEYS,
            text_key="beschreibung",
            detail_tool="get_event",
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_event(kalender_name: str, event_uid: str) -> dict[str, Any]:
        """Fetch a single event by UID.

        Args:
            kalender_name: Display name of the calendar containing the event.
            event_uid: UID of the event to fetch.

        Returns:
            An event dict with the same shape as one entry from list_events.
        """
        return await _call(caldav_service.get_event, kalender_name, event_uid)

    @mcp.tool(annotations=_CREATE)
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
    ) -> dict[str, Any]:
        """Create a new calendar event.

        Args:
            kalender_name: Display name of the target event calendar.
            titel: Event title (VEVENT SUMMARY).
            start: ISO 8601 start -> DTSTART. Exactly "YYYY-MM-DD" creates an
                all-day event; naive datetimes are interpreted in the server's default
                timezone (`MCP_DEFAULT_TIMEZONE`, default Europe/Berlin). A
                datetime may instead be followed by a space and an IANA
                timezone name (e.g. "2026-07-20T14:00:00 Europe/Berlin") to
                have the correct standard/daylight offset resolved for that
                date automatically; combining a numeric offset with a
                timezone name is rejected.
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
                absolute ISO 8601 datetime -> VALARM. The leading "-" is what
                makes a relative reminder fire *before* the start; a positive
                duration ("PT30M") is valid and means 30 minutes *after* it.
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
            The created event dict, same shape as get_event's return
            value (see get_event's docstring for the full key list).
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
        event: dict[str, Any] = await _call(caldav_service.get_event, kalender_name, new_uid)
        return event

    @mcp.tool(annotations=_CREATE)
    async def create_birthday(
        name: str,
        datum: str,
        jahr: int | None = None,
        kalender: str = event_mapping.BIRTHDAY_CALENDAR,
    ) -> dict[str, Any]:
        """Create a birthday entry, with the whole birthday convention filled in.

        One call instead of create_event plus an update_event per person: the
        title, recurrence, tag, visibility and reminders are not parameters
        here, they are the convention every entry in the birthday calendar
        follows. What gets written:

        - Title "🎂 <name> (<Geburtsjahr>)" - without the parentheses when no
          birth year is known.
        - An all-day, one-day event starting on the *birth* date, so each
          occurrence's year minus the start year is the age being celebrated.
          Without a birth year it starts on the next upcoming occurrence.
        - wiederholung "FREQ=YEARLY", tags ["Geburtstag"], sichtbarkeit
          "privat", erinnerungen ["-PT0M", "-P1D"] (on the day itself and the
          day before).

        Args:
            name: The person's name, without the cake and without the year
                (both are added). Passing a title read back from an existing
                entry ("🎂 Papa (1975)") works too - the cake is not doubled
                and the year in parentheses is read as the birth year.
            datum: The birthday as "MM-DD" (e.g. "07-04"), or as a full
                "YYYY-MM-DD" whose year is the year of BIRTH. Never fill in
                the current (or next) year to turn "on the 4th of July" into
                a full date - that is the year of the next celebration, not a
                birth year, and it would make the person 0 years old. Pass
                "MM-DD" whenever the birth year is unknown. A 02-29 birthday
                stays 02-29, and a yearly rule then only fires in leap years -
                pass 02-28 or 03-01 instead if the entry should show up every
                year.
            jahr: Optional year of birth (e.g. 1975), only ever a year the
                person was actually born in. May instead come from `datum` or
                from a trailing "(1975)" in `name`; naming it twice is fine as
                long as the values agree, and conflicting values are an error.
                An unknown birth year is fine - leave it out and the title
                carries no year. A birth date that is still ahead is rejected.
            kalender: Display name of the target calendar, "Geburtstage" by
                default.

        Returns:
            The created event dict, same shape as get_event's return value
            (see get_event's docstring for the full key list).
        """
        try:
            fields = event_mapping.birthday_fields(name, datum, jahr)
        except TaskMcpError as exc:
            raise ToolError(str(exc)) from exc
        new_uid = await _call(caldav_service.create_event, kalender, fields)
        event: dict[str, Any] = await _call(caldav_service.get_event, kalender, new_uid)
        return event

    @mcp.tool(annotations=_MODIFY)
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
    ) -> dict[str, Any]:
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
            The updated event dict, same shape as get_event's return
            value (see get_event's docstring for the full key list).
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
        event: dict[str, Any] = await _call(caldav_service.get_event, kalender_name, event_uid)
        return event

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_MODIFY)
    async def update_events(
        kalender_name: str,
        event_uids: list[str],
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
    ) -> dict[str, Any]:
        """Update multiple events in a calendar with the same field patch.

        The patch is validated up front before any writes take place. If the patch
        is invalid or empty, the call fails immediately and no events are changed.

        Args:
            kalender_name: Display name of the calendar containing the events.
            event_uids: List of event UIDs to update. Max 200 UIDs per call.
                Empty list is rejected. Duplicate UIDs are deduplicated while
                preserving order.
            (all other args): Same meaning and mapping as in update_event; fields left
                as None are left unchanged. To clear fields, pass their names in
                felder_leeren.

        Returns:
            Dict containing kalender_name, erfolgreich count, fehlgeschlagen count,
            and ergebnisse list with per-UID statuses.
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
        res: dict[str, Any] = await _call(
            caldav_service.update_events, kalender_name, event_uids, fields
        )
        return res

    @mcp.tool(annotations=_MODIFY)
    async def update_exdates(
        calendar_name: str,
        event_uids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        ignore_non_occurrences: bool = True,
    ) -> dict[str, Any]:
        """Add or remove single exception dates on recurring events, without rewriting the rest.

        Use this - not update_event's ausnahme_daten - whenever the goal is
        "also skip these dates" or "no longer skip these dates". ausnahme_daten
        REPLACES an event's whole exception list, so it has to be handed every
        date the series already skips, which means reading each event first and
        writing dozens of dates back. This tool merges server-side: one call
        carries only the dates that change, across as many series as needed.

        Cancelling a day on several series at once (sick leave, holiday, a
        block of school days) is the case it is built for: pass every affected
        UID and the days themselves.

        Args:
            calendar_name: Display name of the calendar holding the events.
            event_uids: UIDs of the events to change. Max 200 per call; an
                empty list is rejected, duplicates are ignored.
            add: Exception dates to add. A plain "YYYY-MM-DD" means the whole
                day: on a timed series it cancels every occurrence that day,
                whatever time the series starts - so the same list of days
                works across series with different start times. An exact
                ISO 8601 datetime cancels that one occurrence. Dates the event
                already skips are left as they are.
            remove: Exception dates to remove, so the occurrence happens again.
                Same forms as `add`; a plain "YYYY-MM-DD" removes every
                exception date the event has on that day.
            ignore_non_occurrences: When true (the default), an entry that
                changes nothing on a given event - a day that series does not
                run on, a removal it never had - is reported under "skipped"
                for that event and the rest are still applied. Set false to
                have such an entry fail that event instead, leaving it
                untouched; useful when a date is expected to exist everywhere
                and a typo should not pass unnoticed.

        Returns:
            {"calendar_name", "succeeded", "failed", "results"}, where each
            result is {"uid", "status": "ok"|"error"} plus, for "ok",
            {"added", "removed", "total", "skipped": [{"value", "reason"}]} -
            "total" being how many exception dates the event has afterwards.
            The full list itself is deliberately not returned; read it with
            get_event if it is really needed.
        """
        res: dict[str, Any] = await _call(
            caldav_service.change_exdates,
            calendar_name,
            event_uids,
            add,
            remove,
            ignore_non_occurrences,
        )
        # `_batch_over_events` reports in the German shape the other batch
        # tools return; this tool's surface is English throughout, so the
        # three envelope keys and the per-event failure entry are renamed.
        results: list[dict[str, Any]] = []
        for entry in res["ergebnisse"]:
            if entry["status"] == "fehler":
                results.append({"uid": entry["uid"], "status": "error", "error": entry["fehler"]})
            else:
                results.append(entry)
        return {
            "calendar_name": res["kalender_name"],
            "succeeded": res["erfolgreich"],
            "failed": res["fehlgeschlagen"],
            "results": results,
        }

    @mcp.tool(annotations=_MODIFY)
    async def delete_events(kalender_name: str, event_uids: list[str]) -> dict[str, Any]:
        """Permanently delete multiple events from a calendar.

        WARNING: this is irreversible from this server's point of view, and a
        batch multiplies the damage a wrong UID list does. Confirm the list
        with the user before calling this.

        A UID that does not exist is reported as a failed entry; the other
        events are still deleted.

        Args:
            kalender_name: Display name of the calendar containing the events.
            event_uids: List of event UIDs to delete. Max 200 UIDs per call.
                Empty list is rejected. Duplicate UIDs are deduplicated while
                preserving order.

        Returns:
            Dict containing kalender_name, erfolgreich count, fehlgeschlagen count,
            and ergebnisse list with per-UID statuses.
        """
        res: dict[str, Any] = await _call(caldav_service.delete_events, kalender_name, event_uids)
        return res

    @mcp.tool(annotations=_MODIFY)
    async def move_event(kalender_name: str, event_uid: str, ziel_kalender: str) -> dict[str, str]:
        """Verschiebt einen Kalendereintrag in einen anderen Kalender.

        Args:
            kalender_name: Anzeige-Name des Quell-Kalenders.
            event_uid: UID des zu verschiebenden Kalendereintrags.
            ziel_kalender: Anzeige-Name des Ziel-Kalenders.

        Returns:
            {"uid": ..., "von": Quell-Kalender, "nach": Ziel-Kalender,
            "methode": "MOVE" | "kopiert"}
        """

        res: dict[str, str] = await _call(
            caldav_service.move_event, kalender_name, event_uid, ziel_kalender
        )
        return res

    @mcp.tool(annotations=_MODIFY)
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

    @mcp.tool(annotations=_ADD)
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

    @mcp.tool(annotations=_READ_ONLY)
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

    @mcp.tool(annotations=_CREATE)
    async def create_event_from_task(
        list_name: str,
        task_uid: str,
        kalender_name: str,
        start: str | None = None,
        dauer_minuten: int | None = None,
        ende: str | None = None,
        beschreibung: str | None = None,
        erinnerungen: list[str] | None = None,
        sichtbarkeit: str | None = None,
    ) -> dict[str, str]:
        """Create a calendar event from an existing task (timeboxing) and link them.

        Title, location and tags are copied from the task. The event is
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
                date-only start produces a one-day all-day event, and then
                ende (if given) must also be a date - see create_event's
                start/ende consistency rule.
            dauer_minuten: Event duration in minutes; ignored for all-day
                events. Mutually exclusive with ende - giving both is an
                error naming both. With neither given, the event runs 60
                minutes.
            ende: Optional explicit ISO 8601 end for the event, as an
                alternative to dauer_minuten (giving both is an error).
            beschreibung: Optional event description. Left as None (the
                default), the task's notizen are copied as before; an
                explicit "" sets an empty description instead of inheriting
                notizen.
            erinnerungen: Optional reminders for the new event, same format
                as create_event's erinnerungen -> VALARM.
            sichtbarkeit: Optional "öffentlich" / "privat" / "vertraulich" for
                the new event -> CLASS.

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
            ende,
            beschreibung,
            erinnerungen,
            sichtbarkeit,
        )
        return {"uid": new_uid, "task_uid": task_uid}

    @mcp.tool(annotations=_READ_ONLY)
    async def get_agenda(
        datum: str,
        kalender_namen: list[str] | None = None,
        listen_namen: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one day's calendar events and due tasks together (agenda view).

        Args:
            datum: The day as a date-only "YYYY-MM-DD" string. Day boundaries
                are constructed in the server's default timezone (`MCP_DEFAULT_TIMEZONE`,
                default Europe/Berlin), consistent with the naive-input rule used
                everywhere else in this server.
            kalender_namen: Optional list of event calendars to include;
                None means all.
            listen_namen: Optional list of task lists to include; None means
                all.

        Returns:
            {"datum": the day, "termine": event dicts (recurring events
            expanded to that day's occurrences, sorted by start), "aufgaben":
            open tasks due that day, each with an added "liste" key}. Recurring
            *tasks* are expanded to that day's occurrences too - see list_tasks
            for what an expanded row can and cannot be used for (in short: read
            it, act on its "serie_uid", never on its own "uid"). Every
            entry in both lists also carries "quelle_url" - the CalDAV URL of
            the exact calendar/task list it came from (Nextcloud doesn't
            enforce unique display names, so "kalender"/"liste" alone can't
            always tell two collections apart). Calendar/task-list listings
            are cached for up to a minute, so a rename or deletion made in
            the Nextcloud web UI can take that long to show up here.
        """
        return await _call(
            caldav_service.get_agenda,
            datum,
            calendar_names=kalender_namen,
            list_names=listen_namen,
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_tags(
        kalender_namen: list[str] | None = None,
        listen_namen: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregierte Tags (CATEGORIES) und Häufigkeit über Sammlungen abrufen.

        Liest alle VEVENT-Termine und VTODO-Aufgaben (inklusive erledigter Aufgaben)
        aus den angegebenen Kalendern und Listen. Die Zusammenfassung erfolgt
        case-insensitiv; als Tag-Name wird die häufigste Schreibweise gemeldet, bei
        Gleichstand die alphabetisch erste - damit zwei gleiche Aufrufe nicht
        unterschiedlich antworten, nur weil der Server anders sortiert hat.

        HINWEIS: Dies ist eine aufwendige Operation, da die Sammlungen vollständig
        ohne Zeitfenster ausgelesen werden; solange sie läuft, warten andere
        Aufrufe auf dieselbe CalDAV-Verbindung.

        Args:
            kalender_namen: Liste von Kalendernamen. None = alle Kalender, [] = keine Kalender.
            listen_namen: Liste von Aufgabenlisten-Namen. None = alle Listen, [] = keine Listen.

        Returns:
            Eine Liste von {"tag": Tag-Name, "anzahl": Anzahl} Dicts, sortiert nach
            anzahl absteigend, bei Gleichstand alphabetisch nach tag.
        """
        return await _call(
            caldav_service.list_tags,
            calendar_names=kalender_namen,
            list_names=listen_namen,
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_free_busy(
        von: str,
        bis: str,
        benutzer: str | None = None,
    ) -> dict[str, Any]:
        """Get busy time intervals in a date/time range, for yourself or another user.

        Args:
            von: ISO 8601 start of the range. Naive datetimes are interpreted
                in the server's default timezone (`MCP_DEFAULT_TIMEZONE`, default Europe/Berlin);
                a date-only value means the start of that day.
            bis: ISO 8601 end of the range. A date-only value includes that
                entire day.
            benutzer: Optional Nextcloud user id or email of another account
                to query. When omitted (default), returns your own
                availability, computed by aggregating your own event
                calendars. When given, this sends a CalDAV free-busy
                scheduling request to the server for that user - the server
                resolves `benutzer`, not this tool; a user with no visible
                scheduling info (unknown to the server, or with scheduling
                disabled) produces an error rather than an empty result, so
                it isn't mistaken for "fully free".

        Returns:
            {"von": range start, "bis": range end, "benutzer": benutzer,
            "belegt": merged, sorted busy intervals as a list of
            {"von": iso, "bis": iso} dicts}. Cancelled and "transparent"
            (does-not-block-time) events are excluded from your own
            availability; overlapping/back-to-back busy blocks are merged
            into one interval.
        """
        return await _call(caldav_service.get_free_busy, von, bis, benutzer)

    @mcp.tool(annotations=_ADD)
    async def share_calendar(
        kalender_name: str,
        empfaenger: str,
        gruppe: bool = False,
        schreibzugriff: bool = False,
    ) -> dict[str, Any]:
        """Share a task list or event calendar with a Nextcloud user or group.

        Uses Nextcloud's own CalDAV sharing extension - this only works
        against a real Nextcloud server, not a generic CalDAV server, since
        it isn't part of any CalDAV RFC. Calling this again for the same
        empfaenger updates their access level instead of creating a
        duplicate share.

        Args:
            kalender_name: Display name of the task list or event calendar
                to share (resolved across both kinds).
            empfaenger: Nextcloud user id (or group id when gruppe=True) to
                share with.
            gruppe: If True, empfaenger names a group instead of a user.
            schreibzugriff: If True, grant read-write access; otherwise the
                share is read-only.

        Returns:
            {"kalender_name", "empfaenger", "schreibzugriff"} on success.
        """
        return await _call(
            caldav_service.share_calendar, kalender_name, empfaenger, gruppe, schreibzugriff
        )

    @mcp.tool(annotations=_MODIFY)
    async def unshare_calendar(
        kalender_name: str,
        empfaenger: str,
        gruppe: bool = False,
    ) -> dict[str, str]:
        """Remove a user's or group's share of a task list or event calendar.

        A no-op (not an error) if empfaenger doesn't currently have a share
        of this calendar.

        Args:
            kalender_name: Display name of the task list or event calendar.
            empfaenger: Nextcloud user id (or group id when gruppe=True) to
                unshare from.
            gruppe: If True, empfaenger names a group instead of a user.

        Returns:
            {"kalender_name", "empfaenger"} on success.
        """
        await _call(caldav_service.unshare_calendar, kalender_name, empfaenger, gruppe)
        return {"kalender_name": kalender_name, "empfaenger": empfaenger}

    @mcp.tool(annotations=_READ_ONLY)
    async def list_calendar_shares(kalender_name: str) -> list[dict[str, Any]]:
        """List everyone a task list or event calendar is currently shared with.

        Args:
            kalender_name: Display name of the task list or event calendar.

        Returns:
            A list of {"empfaenger": user/group id, "typ": "benutzer" or
            "gruppe", "schreibzugriff": bool, "status": invite status, e.g.
            "akzeptiert"/"ausstehend"/"abgelehnt" (an unrecognized raw status
            from the server comes back lowercased instead of being dropped)}
            dicts.
        """
        return await _call(caldav_service.list_calendar_shares, kalender_name)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_trash() -> list[dict[str, Any]]:
        """List deleted tasks/events in Nextcloud's calendar trash bin.

        Uses Nextcloud's calendar-trashbin DAV plugin; on a server without
        it, this fails with a clean "trash bin not available" error instead
        of a raw HTTP error.

        Returns:
            A list of {"id": trash item id (pass to restore_from_trash),
            "titel": title if derivable from the item's data or None,
            "typ": "aufgabe"/"termin"/None, "kalender": the original
            calendar's URI if reported by the server or None, "geloescht_am":
            ISO 8601 deletion timestamp or None} dicts.
        """
        return await _call(caldav_service.list_trash)

    @mcp.tool(annotations=_ADD)
    async def restore_from_trash(id: str) -> dict[str, str]:
        """Restore a deleted task/event from the trash bin to its original calendar.

        Args:
            id: Trash item id, as returned by list_trash's "id" field.

        Returns:
            {"id": id} on success.
        """
        await _call(caldav_service.restore_from_trash, id)
        return {"id": id}

    @mcp.tool(annotations=_READ_ONLY)
    async def export_calendar(kalender_name: str) -> dict[str, str]:
        """Export a task list or event calendar as a single ICS (VCALENDAR) text.

        Args:
            kalender_name: Display name of the task list or event calendar
                to export (resolved across both kinds).

        Returns:
            {"kalender_name", "ics": the full VCALENDAR text, containing
            every task/event in the calendar}.
        """
        return await _call(caldav_service.export_calendar, kalender_name)

    @mcp.tool(annotations=_CREATE)
    async def import_ics(kalender_name: str, ics: str) -> dict[str, Any]:
        """Import ICS (VCALENDAR) text into an existing task list or event calendar.

        Top-level VEVENT/VTODO components are grouped by UID, so a recurring
        event/task and its override instances are saved together as one
        calendar object. A component whose kind (event/task) the target
        calendar doesn't support is skipped rather than failing the whole
        import.

        Args:
            kalender_name: Display name of the target task list or event
                calendar (resolved across both kinds).
            ics: Full ICS text; must be a VCALENDAR containing at least one
                VEVENT or VTODO.

        Returns:
            {"kalender_name", "importiert": number of calendar objects
            created, "uebersprungen": number skipped because the target
            calendar doesn't support that component kind}.
        """
        return await _call(caldav_service.import_ics, kalender_name, ics)

    # ------------------------------------------------------------------
    # Notes (Nextcloud Notes app's JSON REST API - see notes_client.py)
    # ------------------------------------------------------------------

    @mcp.tool(annotations=_READ_ONLY)
    async def list_notizen(kategorie: str | None = None) -> list[dict[str, Any]]:
        """List all Nextcloud notes (title/category/favorite only, not content).

        Args:
            kategorie: Optional category name to filter by.

        Returns:
            A list of {"id": note id, "titel": title, "kategorie": category
            name or None, "favorit": bool, "geaendert": ISO 8601 last-modified
            timestamp or None} dicts. Note content is not included here - use
            get_notiz to read a specific note's content.
        """
        return await _call_notes(notes_svc.list_notes(kategorie))

    @mcp.tool(annotations=_READ_ONLY)
    async def get_notiz(notiz_id: int) -> dict[str, Any]:
        """Fetch a single note by id, including its full content.

        Args:
            notiz_id: The note's id, as returned by list_notizen/search_notizen.

        Returns:
            {"id", "titel", "kategorie" (or None), "inhalt": full content,
            "favorit": bool, "geaendert": ISO 8601 last-modified timestamp or
            None, "schreibgeschuetzt": True if the note is read-only}.
        """
        return await _call_notes(notes_svc.get_note(notiz_id))

    @mcp.tool(annotations=_CREATE)
    async def create_notiz(
        titel: str,
        kategorie: str | None = None,
        inhalt: str | None = None,
        favorit: bool | None = None,
    ) -> dict[str, Any]:
        """Create a new Nextcloud note.

        Args:
            titel: Note title.
            kategorie: Optional category name.
            inhalt: Optional initial content.
            favorit: Optional favorite flag (defaults to false server-side).

        Returns:
            The created note, same shape as get_notiz's return value.
        """
        fields = notes_mapping.NoteFields(
            titel=titel, kategorie=kategorie, inhalt=inhalt, favorit=favorit
        )
        return await _call_notes(notes_svc.create_note(fields))

    @mcp.tool(annotations=_MODIFY)
    async def update_notiz(
        notiz_id: int,
        titel: str | None = None,
        kategorie: str | None = None,
        inhalt: str | None = None,
        favorit: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing note. Only fields that are explicitly given are changed.

        Args:
            notiz_id: The note's id.
            titel: New title, or None to leave unchanged.
            kategorie: New category, or None to leave unchanged.
            inhalt: New full content - this REPLACES the existing content
                (use append_notiz to add to it, replace_in_notiz to change
                one passage, or update_notiz_abschnitt to change one Markdown
                section instead), or None to leave unchanged.
            favorit: New favorite flag, or None to leave unchanged.

        At least one field must be given.

        Returns:
            The updated note, same shape as get_notiz's return value.
        """
        fields = notes_mapping.NoteFields(
            titel=titel, kategorie=kategorie, inhalt=inhalt, favorit=favorit
        )
        return await _call_notes(notes_svc.update_note(notiz_id, fields))

    @mcp.tool(annotations=_MODIFY)
    async def replace_in_notiz(notiz_id: int, alt: str, neu: str) -> dict[str, Any]:
        """Replace exactly one occurrence of a text passage in a note's content.

        The targeted alternative to update_notiz's whole-content `inhalt`:
        instead of re-sending the full content to change one passage, send
        only the passage. `alt` must match the current content exactly
        (character for character, including whitespace and newlines; may
        span multiple lines) and exactly once - zero matches or more than
        one match is an error and nothing is changed. On an "occurs N
        times" error, retry with more surrounding context in `alt` (and the
        same context repeated in `neu`) so it matches exactly once.

        Read-then-write like append_notiz, so not atomic - a concurrent
        edit between the read and the write may be lost.

        Args:
            notiz_id: The note's id.
            alt: Existing text to replace, exactly as it appears in the content.
            neu: Replacement text (may be empty to delete the passage).

        Returns:
            The updated note, same shape as get_notiz's return value.
        """
        return await _call_notes(notes_svc.replace_in_note(notiz_id, alt, neu))

    @mcp.tool(annotations=_MODIFY)
    async def update_notiz_abschnitt(notiz_id: int, abschnitt: str, inhalt: str) -> dict[str, Any]:
        """Replace one Markdown section of a note - heading line plus body.

        `abschnitt` is an ATX heading prefix like "## 7." that must select
        exactly one heading line of the same level (same number of '#')
        starting with it; the match stops at a word boundary, so "## 7"
        does not select "## 75. History" and "## 7.1" does not select
        "## 7.1.1 Details". The section runs from that
        heading up to the next heading of the same or a higher level (or
        the end of the note). Heading-shaped lines inside fenced code
        blocks or a leading YAML front matter block are ignored; setext
        (underlined) headings are not recognized.

        `inhalt` replaces the whole section INCLUDING its heading line, so
        start it with the (possibly renamed) heading. An empty `inhalt`
        removes the section entirely.

        Read-then-write like append_notiz, so not atomic - a concurrent
        edit between the read and the write may be lost.

        Args:
            notiz_id: The note's id.
            abschnitt: Heading prefix selecting the section, e.g. "## 7."
                or "### Offene Punkte".
            inhalt: The section's new text, starting with its heading line.

        Returns:
            The updated note, same shape as get_notiz's return value.
        """
        return await _call_notes(notes_svc.replace_note_section(notiz_id, abschnitt, inhalt))

    @mcp.tool(annotations=_CREATE)
    async def append_notiz(notiz_id: int, text: str) -> dict[str, Any]:
        """Append text to an existing note's content, keeping what's already there.

        Reads the note's current content and writes it back with `text`
        appended (separated by a blank line if the note already has content).
        Not an atomic server-side append - the Notes API has none - so a
        concurrent edit to the same note between the read and the write may
        be lost.

        Args:
            notiz_id: The note's id.
            text: Text to append.

        Returns:
            The updated note, same shape as get_notiz's return value.
        """
        return await _call_notes(notes_svc.append_note(notiz_id, text))

    @mcp.tool(annotations=_READ_ONLY)
    async def search_notizen(suchtext: str, kategorie: str | None = None) -> list[dict[str, Any]]:
        """Search notes by a case-insensitive substring match over title and content.

        The Notes API has no server-side full-text search, so this fetches
        the (optionally category-filtered) notes and filters client-side.

        Args:
            suchtext: Substring to search for in the title or content.
            kategorie: Optional category name to narrow the search to first.

        Returns:
            Matching notes, same shape as list_notizen's return value (no content).
        """
        return await _call_notes(notes_svc.search_notes(suchtext, kategorie))

    @mcp.tool(annotations=_MODIFY)
    async def delete_notiz(notiz_id: int) -> dict[str, int]:
        """Permanently delete a Nextcloud note.

        WARNING: this is irreversible from this server's point of view -
        this server cannot restore a deleted note. Confirm with the user
        before calling this.

        Args:
            notiz_id: The note's id, as returned by list_notizen/search_notizen.

        Returns:
            {"id": notiz_id} on success.
        """
        await _call_notes(notes_svc.delete_note(notiz_id))
        return {"id": notiz_id}

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
