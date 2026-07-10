"""Translation between the server's German task fields and iCalendar VTODO properties."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from icalendar import Alarm, vDuration

from .errors import InvalidTaskDataError

PRIORITY_LABELS: dict[str, int] = {"hoch": 1, "mittel": 5, "niedrig": 9}
VISIBILITY_LABELS: dict[str, str] = {
    "öffentlich": "PUBLIC",
    "privat": "PRIVATE",
    "vertraulich": "CONFIDENTIAL",
}

# Matches exactly "YYYY-MM-DD" (length 10). `date.fromisoformat` on Python
# 3.11+ also accepts other forms (basic format, week dates, ...) that we do
# NOT want to treat as all-day dates here, so the date-only branch of
# `parse_datetime_input` is gated on this pattern rather than a bare
# try/except around `date.fromisoformat` (B1).
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Maps the German, LLM-facing `felder_leeren` entry name to the
# (TaskFields attribute name, iCalendar property name) it clears. "titel" is
# deliberately absent - clearing the title is not a supported operation.
# "erinnerungen" has no single iCalendar property (it clears all VALARM
# subcomponents instead), hence the `None` ical name, handled specially in
# `apply_task_fields`.
_CLEAR_SPECS: dict[str, tuple[str, str | None]] = {
    "start_datum": ("start_datum", "dtstart"),
    "fällig_datum": ("faellig_datum", "due"),
    "priorität": ("prioritaet", "priority"),
    "fortschritt_prozent": ("fortschritt_prozent", "percent-complete"),
    "ort": ("ort", "location"),
    "url": ("url", "url"),
    "tags": ("tags", "categories"),
    "erinnerungen": ("erinnerungen", None),
    "notizen": ("notizen", "description"),
    "sichtbarkeit": ("sichtbarkeit", "class"),
    "übergeordnete_aufgabe": ("uebergeordnete_aufgabe", "related-to"),
}


@dataclass(frozen=True)
class TaskFields:
    """The optional task fields shared by create_task/update_task, in one place.

    This is the single definition of the (previously hand-copied five times,
    C3) 13-field task parameter list. The MCP tool functions in `server.py`
    keep their own flat, German, umlaut-bearing parameter lists - that's the
    LLM-facing tool contract - and build a `TaskFields` internally; everything
    below that layer (`CalDavService`, `apply_task_fields`) works with this
    dataclass instead of a long kwarg list.

    A field left as `None` means "leave unchanged" (update_task) or "not set"
    (create_task). `clear` names fields to remove entirely on update_task
    instead (B3) - see `apply_task_fields` for the accepted names and the
    validation rules (unknown names, and setting+clearing the same field in
    one call, both raise `InvalidTaskDataError`).
    """

    titel: str | None = None
    start_datum: str | None = None
    faellig_datum: str | None = None
    prioritaet: str | None = None
    fortschritt_prozent: int | None = None
    ort: str | None = None
    url: str | None = None
    tags: list[str] | None = None
    erinnerungen: list[str] | None = None
    notizen: str | None = None
    sichtbarkeit: str | None = None
    uebergeordnete_aufgabe: str | None = None
    clear: tuple[str, ...] | list[str] = field(default_factory=tuple)


def priority_label_to_ical(label: str) -> int:
    """Map a German priority label to an RFC 5545 PRIORITY value (1-9)."""
    try:
        return PRIORITY_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown priorität '{label}'. Expected one of: {', '.join(PRIORITY_LABELS)}."
        ) from None


def ical_priority_to_label(value: int | None) -> str | None:
    """Map an RFC 5545 PRIORITY value back to a German label.

    Follows the common client convention: 1-4 high, 5 medium, 6-9 low,
    0/absent undefined.
    """
    if not value:
        return None
    if 1 <= value <= 4:
        return "hoch"
    if value == 5:
        return "mittel"
    if 6 <= value <= 9:
        return "niedrig"
    return None


def visibility_label_to_ical(label: str) -> str:
    """Map a German visibility label to an RFC 5545 CLASS value."""
    try:
        return VISIBILITY_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown sichtbarkeit '{label}'. Expected one of: {', '.join(VISIBILITY_LABELS)}."
        ) from None


def parse_datetime_input(value: str) -> date | datetime:
    """Parse an ISO 8601 date or datetime string, accepting a trailing 'Z'.

    Two rules, applied consistently wherever this is used (DTSTART, DUE, and
    - via `_parse_absolute_trigger` - absolute VALARM triggers):

    - A date-only string of exactly the form "YYYY-MM-DD" (length 10) is
      parsed as a `date`, producing an all-day (`VALUE=DATE`) iCalendar
      property (B1). `date.fromisoformat` is tried first for this case;
      other date-like strings that `date.fromisoformat` would also accept on
      Python 3.11+ (basic format, week dates, ...) are deliberately NOT
      treated as all-day here - only the canonical extended form is.
    - Anything else is parsed as a `datetime`. A *naive* datetime (no UTC
      offset) is interpreted as UTC (B2) - the same rule already used for
      absolute VALARM triggers, so the same-looking input is no longer
      interpreted two different ways depending on which property it ends up
      in.
    """
    text = value.strip()
    if _DATE_ONLY_RE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass  # fall through to the datetime/error path below

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        pass
    else:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    raise InvalidTaskDataError(f"Could not parse '{value}' as an ISO 8601 date or datetime.")


def _set(component, name: str, value: Any, parameters: dict[str, str] | None = None) -> None:
    """Set a property to exactly one value, replacing any existing one.

    Component.add() appends to existing values instead of replacing them,
    which would silently produce duplicate properties on update - so any
    existing value is removed first.
    """
    if name in component:
        del component[name]
    component.add(name, value, parameters=parameters)


def _parse_absolute_trigger(spec: str) -> datetime | None:
    text = spec.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    # RFC 5545 requires absolute VALARM triggers to be expressed in UTC;
    # a naive input is assumed to already be UTC (same rule as
    # `parse_datetime_input`, B2).
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_trigger(
    spec: str, *, has_due: bool, has_start: bool
) -> tuple[datetime | timedelta, dict[str, str]]:
    absolute = _parse_absolute_trigger(spec)
    if absolute is not None:
        return absolute, {"VALUE": "DATE-TIME"}

    try:
        delta = vDuration.from_ical(spec.strip())
    except Exception:
        raise InvalidTaskDataError(
            f"Could not parse Erinnerung '{spec}': expected an ISO 8601 duration "
            "like '-P1D' / '-PT1H', or an absolute ISO 8601 datetime."
        ) from None

    if has_due:
        related = "END"
    elif has_start:
        related = "START"
    else:
        raise InvalidTaskDataError(
            f"Relative Erinnerung '{spec}' needs the task to have a fällig_datum or "
            "start_datum to be relative to."
        )
    return delta, {"RELATED": related}


def build_alarm(spec: str, description: str, *, has_due: bool, has_start: bool) -> Alarm:
    """Build a VALARM component for one reminder spec.

    `spec` is either a relative RFC 5545 duration (e.g. "-P1D", "-PT1H"),
    resolved against DUE if present, otherwise DTSTART, or an absolute
    ISO 8601 datetime. This works the same whether DUE/DTSTART is an all-day
    `date` or a full `datetime` - RFC 5545 permits a relative VALARM trigger
    to be RELATED to a DATE-valued DUE/DTSTART.
    """
    trigger_value, trigger_params = _parse_trigger(spec, has_due=has_due, has_start=has_start)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", description or "Reminder")
    alarm.add("trigger", trigger_value, parameters=trigger_params)
    return alarm


def _validate_clear(fields: TaskFields, clear: tuple[str, ...]) -> None:
    unknown = sorted({name for name in clear if name not in _CLEAR_SPECS})
    if unknown:
        raise InvalidTaskDataError(
            f"Unknown felder_leeren entry/entries: {', '.join(unknown)}. "
            f"Expected one of: {', '.join(_CLEAR_SPECS)}."
        )
    conflicts = sorted(
        {name for name in clear if getattr(fields, _CLEAR_SPECS[name][0]) is not None}
    )
    if conflicts:
        raise InvalidTaskDataError(
            f"Cannot both set and clear the same field in one call: {', '.join(conflicts)}."
        )


def apply_task_fields(todo, fields: TaskFields) -> None:
    """Apply the given `TaskFields` onto an icalendar VTODO component in place.

    Fields left as None are left untouched, which is what gives create_task
    and update_task their "only set what's provided" semantics. Field names
    listed in `fields.clear` are removed from the component entirely (B3);
    clearing and setting the same field in one call, or naming an unknown
    field (including "titel", which cannot be cleared), raises
    `InvalidTaskDataError`.
    """
    clear = tuple(fields.clear or ())
    _validate_clear(fields, clear)

    # Clears run first, so a later set of a *different* field (and the
    # erinnerungen rebuild below) observe the final DTSTART/DUE presence.
    for name in clear:
        _, ical_name = _CLEAR_SPECS[name]
        if name == "erinnerungen":
            todo.subcomponents = [c for c in todo.subcomponents if c.name != "VALARM"]
        elif ical_name is not None and ical_name in todo:
            del todo[ical_name]

    if fields.titel is not None:
        _set(todo, "summary", fields.titel)
    if fields.start_datum is not None:
        _set(todo, "dtstart", parse_datetime_input(fields.start_datum))
    if fields.faellig_datum is not None:
        _set(todo, "due", parse_datetime_input(fields.faellig_datum))
    if fields.prioritaet is not None:
        _set(todo, "priority", priority_label_to_ical(fields.prioritaet))
    if fields.fortschritt_prozent is not None:
        if not 0 <= fields.fortschritt_prozent <= 100:
            raise InvalidTaskDataError(
                f"fortschritt_prozent must be between 0 and 100, got {fields.fortschritt_prozent}."
            )
        _set(todo, "percent-complete", fields.fortschritt_prozent)
    if fields.ort is not None:
        _set(todo, "location", fields.ort)
    if fields.url is not None:
        _set(todo, "url", fields.url)
    if fields.tags is not None:
        _set(todo, "categories", list(fields.tags))
    if fields.notizen is not None:
        _set(todo, "description", fields.notizen)
    if fields.sichtbarkeit is not None:
        _set(todo, "class", visibility_label_to_ical(fields.sichtbarkeit))
    if fields.uebergeordnete_aufgabe is not None:
        _set(
            todo,
            "related-to",
            fields.uebergeordnete_aufgabe,
            parameters={"RELTYPE": "PARENT"},
        )

    if fields.erinnerungen is not None:
        todo.subcomponents = [c for c in todo.subcomponents if c.name != "VALARM"]
        has_due = "due" in todo
        has_start = "dtstart" in todo
        title_for_alarm = str(todo.get("summary", "Reminder"))
        for spec in fields.erinnerungen:
            todo.add_component(
                build_alarm(spec, title_for_alarm, has_due=has_due, has_start=has_start)
            )


def mark_completed(todo) -> None:
    """Mark a VTODO component as completed: STATUS, PERCENT-COMPLETE and COMPLETED timestamp."""
    _set(todo, "status", "COMPLETED")
    _set(todo, "percent-complete", 100)
    _set(todo, "completed", datetime.now(timezone.utc))


def _get_text(component, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None else None


def _format_date_property(component, name: str) -> str | None:
    prop = component.get(name)
    if prop is None:
        return None
    value = getattr(prop, "dt", prop)
    return value.isoformat()


def _extract_categories(component) -> list[str]:
    categories = component.get("categories")
    if categories is None:
        return []
    entries = categories if isinstance(categories, list) else [categories]
    result: list[str] = []
    for entry in entries:
        cats = getattr(entry, "cats", None)
        if cats is not None:
            result.extend(str(c) for c in cats)
        else:
            result.append(str(entry))
    return result


def _extract_parent_uid(component) -> str | None:
    related = component.get("related-to")
    if related is None:
        return None
    entries = related if isinstance(related, list) else [related]
    for entry in entries:
        params = getattr(entry, "params", {}) or {}
        reltype = str(params.get("RELTYPE", "PARENT")).upper()
        if reltype == "PARENT":
            return str(entry)
    return None


def _extract_rrule(component) -> str | None:
    """Return the task's RRULE as raw RFC 5545 text (e.g. "FREQ=WEEKLY;BYDAY=MO"), or None.

    Read-only (C5): this server has no way to create/edit recurrence, only
    surface whether/how a task already recurs. `icalendar` exposes RRULE as a
    `vRecur` property; `.to_ical()` serializes it back to the same textual form
    RFC 5545 (and Nextcloud Tasks) uses, rather than exposing icalendar's
    internal dict representation.
    """
    rrule = component.get("rrule")
    if rrule is None:
        return None
    return rrule.to_ical().decode()


def parse_vtodo(component) -> dict[str, Any]:
    """Parse an icalendar VTODO component into the server's German task dict."""
    priority = component.get("priority")
    percent = component.get("percent-complete")
    status = str(component.get("status", "NEEDS-ACTION")).upper()
    return {
        "uid": str(component.get("uid")),
        "titel": str(component.get("summary", "")),
        "start_datum": _format_date_property(component, "dtstart"),
        "fällig_datum": _format_date_property(component, "due"),
        "priorität": ical_priority_to_label(int(priority)) if priority is not None else None,
        "fortschritt_prozent": int(percent) if percent is not None else 0,
        "status": "erledigt" if status == "COMPLETED" else "offen",
        "ort": _get_text(component, "location"),
        "url": _get_text(component, "url"),
        "tags": _extract_categories(component),
        "notizen": _get_text(component, "description"),
        "übergeordnete_uid": _extract_parent_uid(component),
        "wiederholung": _extract_rrule(component),
    }


def _to_comparable_datetime(value: str, *, end_of_day: bool) -> datetime:
    """Parse a `list_tasks` due-filter value/stored due value into a comparable UTC datetime.

    Reuses `parse_datetime_input`, so a naive datetime is already normalized to
    UTC per the same rule used everywhere else (B2). A bare `date` result (an
    all-day due date, or an all-day filter bound) has no time component to
    compare directly, so it's expanded to a single instant within that day:
    start-of-day (00:00:00 UTC) when `end_of_day` is False, end-of-day
    (23:59:59 UTC) when True. Callers use `end_of_day=True` only for the
    `fällig_vor` (due-before) bound, so a date-only bound like "2026-07-20"
    still includes tasks due at any time on the 20th; `fällig_nach`
    (due-after) bounds and the tasks' own stored due values use
    `end_of_day=False` (start-of-day), so a date-only bound includes tasks due
    from the very start of that day onward, and an all-day task's own due date
    compares as its earliest instant either way.
    """
    parsed = parse_datetime_input(value)
    if isinstance(parsed, datetime):
        return parsed
    time_of_day = time(23, 59, 59) if end_of_day else time.min
    return datetime.combine(parsed, time_of_day, tzinfo=timezone.utc)


def filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    due_before: str | None = None,
    due_after: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter already-`parse_vtodo`-parsed task dicts by due-date range and/or cap the count (C4).

    `due_before`/`due_after` are ISO 8601 date/datetime strings (same format
    `parse_datetime_input` accepts elsewhere). When either is given, tasks with
    no `fällig_datum` (due date) are excluded - a task can't be "due before X"
    or "due after X" if it has no due date at all. See `_to_comparable_datetime`
    for how date-vs-datetime bounds/values are normalized for comparison.

    `limit`, if given, must be a positive integer; it caps the number of
    results returned (applied last, after any due-date filtering).
    """
    if limit is not None and limit <= 0:
        raise InvalidTaskDataError(f"limit must be greater than 0, got {limit}.")

    if due_before is not None or due_after is not None:
        before_bound = (
            _to_comparable_datetime(due_before, end_of_day=True) if due_before is not None else None
        )
        after_bound = (
            _to_comparable_datetime(due_after, end_of_day=False) if due_after is not None else None
        )
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            due_text = task.get("fällig_datum")
            if due_text is None:
                continue
            due_dt = _to_comparable_datetime(due_text, end_of_day=False)
            if before_bound is not None and due_dt > before_bound:
                continue
            if after_bound is not None and due_dt < after_bound:
                continue
            filtered.append(task)
        tasks = filtered

    if limit is not None:
        tasks = tasks[:limit]
    return tasks
