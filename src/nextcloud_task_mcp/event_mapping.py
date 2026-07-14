"""Translation between the server's German event fields and iCalendar VEVENT properties."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from icalendar import vRecur

from .errors import InvalidEventDataError, InvalidTaskDataError
from .mapping import (
    VISIBILITY_LABELS,
    _extract_categories,
    _set,
    build_alarm,
    parse_datetime_input,
    visibility_label_to_ical,
)

STATUS_LABELS: dict[str, str] = {
    "bestätigt": "CONFIRMED",
    "vorläufig": "TENTATIVE",
    "abgesagt": "CANCELLED",
}
_ICAL_STATUS_TO_LABEL: dict[str, str] = {v: k for k, v in STATUS_LABELS.items()}

# RFC 5545 RELTYPE -> German relation name used in the `verknuepfte_aufgaben`
# entries returned by `parse_vevent`. A RELATED-TO property without an
# explicit RELTYPE parameter means PARENT per RFC 5545 (handled where the
# parameter is read, in `_extract_related` and `add_relation`).
RELTYPE_LABELS: dict[str, str] = {
    "PARENT": "uebergeordnet",
    "CHILD": "untergeordnet",
    "SIBLING": "gleichrangig",
}

# Reverse of mapping.VISIBILITY_LABELS for parsing CLASS back to German.
_ICAL_CLASS_TO_LABEL: dict[str, str] = {v: k for k, v in VISIBILITY_LABELS.items()}

# Maps the German, LLM-facing `felder_leeren` entry name to the
# (EventFields attribute name, iCalendar property name) it clears. "titel"
# and "start" are deliberately absent - clearing them is not a supported
# operation (a VEVENT without DTSTART is not addressable in any useful way).
# "erinnerungen" has no single iCalendar property (it clears all VALARM
# subcomponents instead), hence the `None` ical name, handled specially in
# `apply_event_fields`. "ausnahme_daten" and "verknuepfte_aufgabe" clear
# *all* EXDATE / RELATED-TO properties (deleting the key removes every
# occurrence, icalendar stores repeated properties as a list under one key).
_CLEAR_SPECS: dict[str, tuple[str, str | None]] = {
    "ende": ("ende", "dtend"),
    "ort": ("ort", "location"),
    "beschreibung": ("beschreibung", "description"),
    "tags": ("tags", "categories"),
    "status": ("status", "status"),
    "sichtbarkeit": ("sichtbarkeit", "class"),
    "wiederholung": ("wiederholung", "rrule"),
    "ausnahme_daten": ("ausnahme_daten", "exdate"),
    "erinnerungen": ("erinnerungen", None),
    "url": ("url", "url"),
    "verknuepfte_aufgabe": ("verknuepfte_aufgabe", "related-to"),
}


@dataclass(frozen=True)
class EventFields:
    """The optional event fields shared by create_event/update_event, in one place.

    Mirrors `mapping.TaskFields`: the MCP tool functions keep their flat,
    German, umlaut-bearing parameter lists - that's the LLM-facing tool
    contract - and build an `EventFields` internally; everything below that
    layer works with this dataclass instead of a long kwarg list.

    A field left as `None` means "leave unchanged" (update_event) or "not
    set" (create_event). `clear` names fields to remove entirely on
    update_event instead - see `apply_event_fields` for the accepted names
    and the validation rules (unknown names, and setting+clearing the same
    field in one call, both raise `InvalidEventDataError`).

    Date semantics: `start`/`ende` follow `mapping.parse_datetime_input` - a
    string of exactly the form "YYYY-MM-DD" makes the event all-day
    (VALUE=DATE), a naive datetime is interpreted as UTC. For all-day events
    `ende` is the *inclusive* last day; RFC 5545 DTEND is exclusive, so one
    day is added when writing and subtracted again when parsing.
    """

    titel: str | None = None  # SUMMARY
    start: str | None = None  # DTSTART, ISO 8601
    ende: str | None = None  # DTEND (all-day: inclusive last day, see above)
    ort: str | None = None  # LOCATION
    beschreibung: str | None = None  # DESCRIPTION
    tags: list[str] | None = None  # CATEGORIES
    status: str | None = None  # STATUS via STATUS_LABELS
    sichtbarkeit: str | None = None  # CLASS via mapping.VISIBILITY_LABELS
    wiederholung: str | None = None  # RRULE as raw RFC 5545 text, e.g. "FREQ=WEEKLY;BYDAY=MO"
    ausnahme_daten: list[str] | None = None  # EXDATE, list of ISO date/datetime strings
    erinnerungen: list[str] | None = None  # VALARM specs (relative to DTSTART, or absolute)
    url: str | None = None  # URL
    verknuepfte_aufgabe: str | None = None  # VTODO UID -> RELATED-TO;RELTYPE=PARENT (timeboxing)
    clear: tuple[str, ...] | list[str] = field(default_factory=tuple)


def status_label_to_ical(label: str) -> str:
    """Map a German event status label to an RFC 5545 STATUS value."""
    try:
        return STATUS_LABELS[label]
    except KeyError:
        raise InvalidEventDataError(
            f"Unknown status '{label}'. Expected one of: {', '.join(STATUS_LABELS)}."
        ) from None


def ical_status_to_label(value: str | None) -> str | None:
    """Map an RFC 5545 STATUS value back to a German label.

    Unknown or missing values parse as None rather than raising - other
    clients may write statuses (or X- extensions) this server doesn't model.
    """
    if value is None:
        return None
    return _ICAL_STATUS_TO_LABEL.get(value.strip().upper())


def _parse_datetime(value: str) -> date | datetime:
    """`mapping.parse_datetime_input`, re-raised as the event-side error class.

    The shared helpers in `mapping` raise `InvalidTaskDataError`; callers of
    the event tools should only ever see `InvalidEventDataError`, so the
    task-side error is translated here with the same message.
    """
    try:
        return parse_datetime_input(value)
    except InvalidTaskDataError as exc:
        raise InvalidEventDataError(str(exc)) from None


def _as_utc(value: datetime) -> datetime:
    """Make a datetime comparable: a naive value is treated as UTC.

    Same rule as `mapping.parse_datetime_input` (B2); our own writes always
    produce aware datetimes, but components written by other clients may not.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_rrule(text: str) -> vRecur:
    """Validate and parse raw RFC 5545 RRULE text (e.g. "FREQ=WEEKLY;BYDAY=MO").

    `vRecur.from_ical` silently *skips* parts without '=' instead of raising,
    so completely unparseable input yields an empty rule - treated as invalid
    here as well, since an empty RRULE is never what the caller meant.
    """
    stripped = text.strip()
    try:
        recur = vRecur.from_ical(stripped)
    except Exception:
        recur = None
    if not recur:
        raise InvalidEventDataError(
            f"Could not parse wiederholung '{text}' as an RFC 5545 RRULE "
            "(e.g. 'FREQ=WEEKLY;BYDAY=MO')."
        )
    return recur


def _validate_clear(fields: EventFields, clear: tuple[str, ...]) -> None:
    unknown = sorted({name for name in clear if name not in _CLEAR_SPECS})
    if unknown:
        raise InvalidEventDataError(
            f"Unknown felder_leeren entry/entries: {', '.join(unknown)}. "
            f"Expected one of: {', '.join(_CLEAR_SPECS)}."
        )
    conflicts = sorted(
        {name for name in clear if getattr(fields, _CLEAR_SPECS[name][0]) is not None}
    )
    if conflicts:
        raise InvalidEventDataError(
            f"Cannot both set and clear the same field in one call: {', '.join(conflicts)}."
        )


def _check_start_end_consistency(event) -> None:
    """Validate DTSTART/DTEND against the *final* component state.

    Runs after all sets so update_event calls that change only one of the two
    are still checked against the value that stays. Both must be the same
    type (both all-day dates or both datetimes), and the end must not lie
    before the start. Note that an all-day DTEND is already stored shifted +1
    day (RFC 5545 exclusive end), so the minimum valid value is the day after
    DTSTART - i.e. exclusive end > start.
    """
    dtstart = event.get("dtstart")
    dtend = event.get("dtend")
    if dtstart is None or dtend is None:
        return
    start_value = dtstart.dt
    end_value = dtend.dt
    start_is_all_day = not isinstance(start_value, datetime)
    end_is_all_day = not isinstance(end_value, datetime)
    if start_is_all_day != end_is_all_day:
        raise InvalidEventDataError(
            "start and ende must both be all-day dates or both be datetimes; "
            "got one of each. Use 'YYYY-MM-DD' for both, or full datetimes for both."
        )
    if start_is_all_day:
        if end_value <= start_value:
            raise InvalidEventDataError(
                f"ende ({(end_value - timedelta(days=1)).isoformat()}) must not be "
                f"before start ({start_value.isoformat()})."
            )
    elif _as_utc(end_value) < _as_utc(start_value):
        raise InvalidEventDataError(
            f"ende ({end_value.isoformat()}) must not be before start ({start_value.isoformat()})."
        )


def apply_event_fields(event, fields: EventFields) -> None:
    """Apply the given `EventFields` onto an icalendar VEVENT component in place.

    Fields left as None are left untouched, which is what gives create_event
    and update_event their "only set what's provided" semantics. Field names
    listed in `fields.clear` are removed from the component entirely;
    clearing and setting the same field in one call, or naming an unknown
    field (including "titel" and "start", which cannot be cleared), raises
    `InvalidEventDataError`.
    """
    clear = tuple(fields.clear or ())
    _validate_clear(fields, clear)

    # Clears run first, so later sets (and the erinnerungen rebuild below)
    # observe the final DTSTART presence.
    for name in clear:
        _, ical_name = _CLEAR_SPECS[name]
        if name == "erinnerungen":
            event.subcomponents = [c for c in event.subcomponents if c.name != "VALARM"]
        elif ical_name is not None and ical_name in event:
            del event[ical_name]

    if fields.titel is not None:
        _set(event, "summary", fields.titel)
    if fields.start is not None:
        _set(event, "dtstart", _parse_datetime(fields.start))
    if fields.ende is not None:
        end_value = _parse_datetime(fields.ende)
        if not isinstance(end_value, datetime):
            # `ende` is the inclusive last day; RFC 5545 DTEND is exclusive,
            # so the stored all-day end is one day later.
            end_value = end_value + timedelta(days=1)
        _set(event, "dtend", end_value)
    if fields.ort is not None:
        _set(event, "location", fields.ort)
    if fields.beschreibung is not None:
        _set(event, "description", fields.beschreibung)
    if fields.tags is not None:
        _set(event, "categories", list(fields.tags))
    if fields.status is not None:
        _set(event, "status", status_label_to_ical(fields.status))
    if fields.sichtbarkeit is not None:
        try:
            ical_class = visibility_label_to_ical(fields.sichtbarkeit)
        except InvalidTaskDataError as exc:
            raise InvalidEventDataError(str(exc)) from None
        _set(event, "class", ical_class)
    if fields.wiederholung is not None:
        _set(event, "rrule", _parse_rrule(fields.wiederholung))
    if fields.ausnahme_daten is not None:
        # Replace, not append: drop every existing EXDATE, then write all
        # entries as one EXDATE property with a comma-separated value list.
        # (`parse_vevent` reads back all three wire forms other clients may
        # produce: one property, repeated properties, comma lists.)
        if "exdate" in event:
            del event["exdate"]
        if fields.ausnahme_daten:
            event.add("exdate", [_parse_datetime(entry) for entry in fields.ausnahme_daten])
    if fields.url is not None:
        _set(event, "url", fields.url)
    if fields.verknuepfte_aufgabe is not None:
        # Timeboxing: the event is the "child" of the task it schedules, so
        # the task UID is written as the event's PARENT relation (replacing
        # any existing RELATED-TO; `add_relation` is the appending variant).
        _set(
            event,
            "related-to",
            fields.verknuepfte_aufgabe,
            parameters={"RELTYPE": "PARENT"},
        )

    if fields.erinnerungen is not None:
        event.subcomponents = [c for c in event.subcomponents if c.name != "VALARM"]
        has_start = "dtstart" in event
        title_for_alarm = str(event.get("summary", "Reminder"))
        for spec in fields.erinnerungen:
            try:
                # Relative reminders on a VEVENT resolve against DTSTART
                # (RELATED=START); there is no DUE. build_alarm raises the
                # "needs a start" error itself when DTSTART is absent.
                alarm = build_alarm(spec, title_for_alarm, has_due=False, has_start=has_start)
            except InvalidTaskDataError as exc:
                raise InvalidEventDataError(str(exc)) from None
            event.add_component(alarm)

    _check_start_end_consistency(event)


def add_relation(component, uid: str, reltype: str) -> None:
    """Append one extra RELATED-TO;RELTYPE=<reltype> property, idempotently.

    Unlike the replacing `verknuepfte_aufgabe` set in `apply_event_fields`,
    this *adds* to whatever relations already exist - the service layer uses
    it for link_task_to_event on VEVENTs. Idempotent: if a RELATED-TO with
    the same UID and the same RELTYPE (missing RELTYPE counts as PARENT per
    RFC 5545) is already present, the call is a no-op.
    """
    wanted = reltype.strip().upper()
    related = component.get("related-to")
    entries = [] if related is None else (related if isinstance(related, list) else [related])
    for entry in entries:
        params = getattr(entry, "params", {}) or {}
        existing = str(params.get("RELTYPE", "PARENT")).upper()
        if str(entry) == uid and existing == wanted:
            return
    component.add("related-to", uid, parameters={"RELTYPE": wanted})


def _text(component, name: str) -> str | None:
    value = component.get(name)
    return str(value) if value is not None else None


def _format_end(component, start_value: date | datetime | None) -> str | None:
    """Return the event's end as an ISO string, or None.

    An all-day DTEND is exclusive per RFC 5545; the German `ende` field is
    the inclusive last day, so one day is subtracted on the way out. When
    DTEND is absent but a DURATION is present, the end is computed as
    start + duration (RFC 5545 allows either form); with neither, None.
    """
    dtend = component.get("dtend")
    if dtend is not None:
        value = dtend.dt
        if not isinstance(value, datetime):
            value = value - timedelta(days=1)
        return value.isoformat()
    duration = component.get("duration")
    if duration is not None and start_value is not None:
        end_value = start_value + duration.dt
        if not isinstance(end_value, datetime):
            # date + duration is again the exclusive end day.
            end_value = end_value - timedelta(days=1)
        return end_value.isoformat()
    return None


def _extract_exdates(component) -> list[str]:
    """Read all EXDATE values as ISO strings, whatever wire form they use.

    icalendar exposes a single EXDATE property as one vDDDLists (which may
    itself hold several comma-separated values) and repeated EXDATE
    properties as a list of vDDDLists - both forms occur in the wild, and
    `apply_event_fields` only ever writes the single-property form.
    """
    exdate = component.get("exdate")
    if exdate is None:
        return []
    entries = exdate if isinstance(exdate, list) else [exdate]
    result: list[str] = []
    for entry in entries:
        dts = getattr(entry, "dts", None)
        if dts is not None:
            result.extend(item.dt.isoformat() for item in dts)
        else:
            value: Any = getattr(entry, "dt", None)
            if value is not None and hasattr(value, "isoformat"):
                result.append(value.isoformat())
            else:
                result.append(str(entry))
    return result


def _extract_related(component) -> list[dict[str, str]]:
    """Read all RELATED-TO properties as {"uid", "beziehung"} dicts.

    A missing RELTYPE parameter means PARENT per RFC 5545; RELTYPEs outside
    `RELTYPE_LABELS` are surfaced lowercased rather than dropped, so links
    written by other clients stay visible.
    """
    related = component.get("related-to")
    if related is None:
        return []
    entries = related if isinstance(related, list) else [related]
    result: list[dict[str, str]] = []
    for entry in entries:
        params = getattr(entry, "params", {}) or {}
        reltype = str(params.get("RELTYPE", "PARENT")).upper()
        result.append(
            {"uid": str(entry), "beziehung": RELTYPE_LABELS.get(reltype, reltype.lower())}
        )
    return result


def _format_recurrence_id(component) -> str | None:
    prop = component.get("recurrence-id")
    if prop is None:
        return None
    value = getattr(prop, "dt", prop)
    return value.isoformat()


def parse_vevent(component) -> dict[str, Any]:
    """Parse an icalendar VEVENT component into the server's German event dict.

    `ganztaegig` is True when DTSTART is a bare date (all-day event);
    `wiederholung_von` carries the RECURRENCE-ID of a materialized single
    occurrence of a recurring series, None for ordinary events.
    """
    dtstart = component.get("dtstart")
    start_value = dtstart.dt if dtstart is not None else None
    status = component.get("status")
    class_value = component.get("class")
    rrule = component.get("rrule")
    return {
        "uid": str(component.get("uid")),
        "titel": str(component.get("summary", "")),
        "start": start_value.isoformat() if start_value is not None else None,
        "ende": _format_end(component, start_value),
        "ganztaegig": start_value is not None and not isinstance(start_value, datetime),
        "ort": _text(component, "location"),
        "beschreibung": _text(component, "description"),
        "tags": _extract_categories(component),
        "status": ical_status_to_label(str(status) if status is not None else None),
        "sichtbarkeit": (
            _ICAL_CLASS_TO_LABEL.get(str(class_value).upper()) if class_value is not None else None
        ),
        "wiederholung": rrule.to_ical().decode() if rrule is not None else None,
        "ausnahme_daten": _extract_exdates(component),
        "url": _text(component, "url"),
        "verknuepfte_aufgaben": _extract_related(component),
        "wiederholung_von": _format_recurrence_id(component),
    }


def _start_sort_key(event: dict[str, Any]) -> tuple[int, datetime]:
    """Chronological sort key over parsed event dicts.

    Events without a start sort last. All-day starts (bare dates) are
    normalized to start-of-day UTC so they compare cleanly against datetime
    starts; naive datetimes are treated as UTC (same rule as everywhere
    else), aware ones compare by instant.
    """
    start = event.get("start")
    if start is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    parsed = _parse_datetime(start)
    if isinstance(parsed, datetime):
        return (0, _as_utc(parsed))
    return (0, datetime.combine(parsed, time.min, tzinfo=timezone.utc))


def filter_events(
    events: list[dict[str, Any]],
    *,
    suchtext: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter already-`parse_vevent`-parsed event dicts, chronologically sorted.

    `suchtext` is a case-insensitive substring match over titel, beschreibung
    and ort (None fields are skipped). `tag` must match one tags entry
    exactly (case-insensitively). Results are sorted by start (events
    without a start last); `limit`, if given, must be a positive integer and
    caps the number of results, applied last - so it returns the *earliest*
    N matches.
    """
    if limit is not None and limit <= 0:
        raise InvalidEventDataError(f"limit must be greater than 0, got {limit}.")

    if suchtext is not None:
        needle = suchtext.lower()
        events = [
            event
            for event in events
            if any(
                needle in value.lower()
                for value in (event.get("titel"), event.get("beschreibung"), event.get("ort"))
                if value is not None
            )
        ]
    if tag is not None:
        wanted = tag.lower()
        events = [
            event for event in events if any(t.lower() == wanted for t in event.get("tags") or [])
        ]

    events = sorted(events, key=_start_sort_key)
    if limit is not None:
        events = events[:limit]
    return events
