"""Translation between the server's German task fields and iCalendar VTODO properties."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# Identity of a VALARM trigger, used to match the reminder specs a caller
# passes against the alarms a component already carries: ("dur", timedelta)
# for a relative trigger, ("dt", UTC datetime) for an absolute one. Built by
# `_trigger_key` so that equivalent spellings ("-P1W"/"-P7D", "...Z"/"+00:00")
# compare equal.
_TriggerKey = tuple[str, datetime] | tuple[str, timedelta]

# Maps the German, LLM-facing `felder_leeren` entry name to the
# (TaskFields attribute name, iCalendar property name) it clears. "titel" is
# deliberately absent - clearing the title is not a supported operation.
# "erinnerungen" has no single iCalendar property (it clears all VALARM
# subcomponents instead), hence the `None` ical name, handled specially in
# `apply_task_fields`.
_CLEAR_SPECS: dict[str, tuple[str, str | None]] = {
    "start_datum": ("start_datum", "dtstart"),
    "faellig_datum": ("faellig_datum", "due"),
    "prioritaet": ("prioritaet", "priority"),
    "fortschritt_prozent": ("fortschritt_prozent", "percent-complete"),
    "ort": ("ort", "location"),
    "url": ("url", "url"),
    "tags": ("tags", "categories"),
    "erinnerungen": ("erinnerungen", None),
    "notizen": ("notizen", "description"),
    "sichtbarkeit": ("sichtbarkeit", "class"),
    "uebergeordnete_aufgabe": ("uebergeordnete_aufgabe", "related-to"),
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


#: The zone this server falls back to before `set_default_timezone` runs, kept
#: in sync with `config.Settings.default_timezone` (the value the server
#: actually applies at startup).
_SHIPPED_DEFAULT_TIMEZONE = "Europe/Berlin"

# IANA names that denote UTC itself. A zone from this set is stored as
# `datetime.timezone.utc` rather than as a `ZoneInfo`, so `icalendar` writes
# the plain `...Z` form instead of `;TZID=UTC:...` plus a VTIMEZONE holding one
# zero-offset observance - which is what `MCP_DEFAULT_TIMEZONE=UTC` is
# documented to do ("restores the previous UTC behavior"). Zones that merely
# *happen* to sit at +00:00 (Europe/London in winter, Africa/Abidjan) are real,
# distinct zones and deliberately not listed.
_UTC_ZONE_KEYS = frozenset(
    {
        "UTC",
        "Etc/UTC",
        "Etc/GMT",
        "Etc/GMT+0",
        "Etc/GMT-0",
        "Etc/GMT0",
        "Etc/Greenwich",
        "Etc/Universal",
        "Etc/Zulu",
        "GMT",
        "GMT+0",
        "GMT-0",
        "GMT0",
        "Greenwich",
        "Universal",
        "Zulu",
    }
)

_logger = logging.getLogger(__name__)


def _initial_default_timezone() -> tzinfo:
    """Resolve the shipped default zone, falling back to UTC if tzdata is absent.

    A Python installation without the IANA database (a slim container image,
    Windows without the `tzdata` package) cannot resolve any zone name at all.
    Raising from a module-level statement would take the whole server down with
    an unhandled `ZoneInfoNotFoundError` traceback before the config layer -
    which reports missing/invalid zones properly - even gets to run.
    """
    try:
        return ZoneInfo(_SHIPPED_DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        _logger.warning(
            "Timezone database unavailable: %r could not be resolved, falling back to "
            "UTC. Install the 'tzdata' package to use MCP_DEFAULT_TIMEZONE.",
            _SHIPPED_DEFAULT_TIMEZONE,
        )
        return timezone.utc


_DEFAULT_TIMEZONE: tzinfo = _initial_default_timezone()


def set_default_timezone(zone: str | tzinfo) -> None:
    """Set the server-wide default timezone."""
    global _DEFAULT_TIMEZONE
    if isinstance(zone, str):
        _DEFAULT_TIMEZONE = ZoneInfo(zone)
    else:
        _DEFAULT_TIMEZONE = zone


def get_default_timezone() -> tzinfo:
    """Return the server-wide default timezone (defaults to Europe/Berlin)."""
    return _DEFAULT_TIMEZONE


def _resolve_in_zone(value: datetime) -> datetime:
    """Settle a zone-anchored wall-clock datetime into the form it is written in.

    Two adjustments, both of which only matter once the zone is *kept* (the
    `keep_zone=True` path, i.e. events) instead of collapsed to a UTC instant:

    - A zone that *is* UTC collapses to `datetime.timezone.utc`, so the value
      is serialized as `...Z` instead of as a TZID reference (`_UTC_ZONE_KEYS`).
    - A wall clock reading that the zone's spring-forward gap skips (02:30 on a
      day that jumps 02:00 -> 03:00) is respelled as the real local time of the
      same instant (03:30). `zoneinfo` resolves such a reading with the
      pre-transition offset (`fold=0`), which is a well-defined *instant* - but
      writing it out as `DTSTART;TZID=Europe/Berlin:...T023000` hands every
      other client a reading that never happens, to resolve its own way.
      Ambiguous readings (the autumn overlap) are left exactly as they are:
      they do happen, twice, and `fold=0` picks the earlier one.
    """
    zone = value.tzinfo
    if not isinstance(zone, ZoneInfo):
        return value
    if zone.key in _UTC_ZONE_KEYS:
        return value.replace(tzinfo=timezone.utc)
    settled = value.astimezone(timezone.utc).astimezone(zone)
    if settled.replace(tzinfo=None) != value.replace(tzinfo=None):
        return settled
    return value


def format_datetime_output(value: date | datetime | None) -> str | None:
    """Format a date or datetime object for output in the server's default timezone.

    - `None` returns `None`.
    - A bare `date` (and not `datetime`) returns "YYYY-MM-DD".
    - A `datetime` is converted to `get_default_timezone()` (a naive datetime is
      treated as already in `get_default_timezone()`) and formatted as ISO 8601
      with offset (e.g. "2026-08-07T14:00:00+02:00").
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=get_default_timezone())
        else:
            value = value.astimezone(get_default_timezone())
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _local_wall_time(day: date, wall: time) -> datetime:
    """A wall clock reading of a local day, as an instant in the default timezone."""
    return _resolve_in_zone(datetime.combine(day, wall, tzinfo=get_default_timezone()))


def local_midnight(day: date) -> datetime:
    """The first instant of a local day, in the server's default timezone.

    The one definition of where a day starts, shared by every part of the
    server that has to decide which day something falls in: `get_agenda`'s
    events and tasks, `von`/`bis` and `faellig_vor`/`faellig_nach` bounds, and
    the instant an all-day value is compared at.

    Not every day has a midnight: America/Santiago, Asia/Beirut and others move
    their clocks *at* 00:00, so on a transition day the reading 00:00 never
    happens. `zoneinfo` still maps it to the correct instant - the transition
    itself is that day's first moment - but a bound like this is also printed
    back to callers (`get_free_busy` reports the window it used), so it is
    respelled as the day's first real reading. Same instant either way.
    """
    return _local_wall_time(day, time.min)


def priority_label_to_ical(label: str) -> int:
    """Map a German priority label to an RFC 5545 PRIORITY value (1-9)."""
    try:
        return PRIORITY_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown prioritaet '{label}'. Expected one of: {', '.join(PRIORITY_LABELS)}."
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


def parse_datetime_input(value: str, *, keep_zone: bool = False) -> date | datetime:
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
      offset) is interpreted in the server's default timezone (`MCP_DEFAULT_TIMEZONE`,
      default Europe/Berlin). An *explicit* UTC offset (e.g. "+02:00") is converted to
      UTC rather than kept as-is: `icalendar` serializes a fixed-offset `tzinfo` as
      `DTSTART;TZID="UTC+02:00":...` without ever emitting the matching
      VTIMEZONE component the TZID reference requires, so CalDAV clients that
      don't recognize the (nonstandard) TZID fall back to interpreting the
      timestamp in their own local zone - shifting the moment, and often the
      calendar day. Converting to UTC first means the property is written
      with a plain "Z" suffix instead, which every client understands.
    - A datetime may instead be followed by a space and an IANA timezone
      name, e.g. "2026-01-15T08:00:00 Europe/Berlin" - the datetime part
      must then be naive (no numeric offset; combining both is rejected as
      ambiguous). By default the offset is resolved for that specific date
      via `zoneinfo` and the result converted to UTC, so callers no longer
      need to work out themselves whether standard or daylight time (e.g.
      CET vs. CEST) applies on a given day - a fixed numeric offset picked
      once and reused year-round is wrong for half the year in any zone
      that observes DST.
    - `keep_zone=True` changes how naive input and IANA timezone input are returned:
      instead of converting to UTC, naive input gets the server's default
      `ZoneInfo` attached and explicit IANA timezone input keeps its `zoneinfo.ZoneInfo`
      tzinfo. Collapsing to a fixed UTC instant is correct for a one-off value, but
      wrong for anything that repeats on wall-clock time (an RRULE) - a
      recurring "09:00 Europe/Berlin" stored as a fixed UTC instant keeps
      that UTC instant across a DST transition, so it displays as 08:00 or
      10:00 local for half the year. Keeping the zone lets the RRULE be
      evaluated in local time, the way RFC 5545 intends. Explicit numeric offsets
      are unaffected by this flag - they carry no IANA zone to preserve, so they still
      normalize to UTC as before.
    - A local wall-clock time that a DST change makes nonexistent (the spring
      gap) or ambiguous (the autumn overlap) is resolved by `zoneinfo`'s
      default `fold=0`, i.e. with the pre-transition offset, rather than
      rejected: refusing a timestamp for one hour twice a year would be a
      worse failure mode than picking the earlier of two plausible instants.
      With `keep_zone=True` a nonexistent reading is additionally respelled as
      the real local time of that same instant (02:30 -> 03:30), so what goes
      on the wire is a wall clock reading that actually happens - see
      `_resolve_in_zone`.
    """
    text = value.strip()
    if _DATE_ONLY_RE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass  # fall through to the datetime/error path below

    dt_text = text
    zone: ZoneInfo | None = None
    if " " in text:
        candidate_text, _, candidate_zone = text.rpartition(" ")
        try:
            zone = ZoneInfo(candidate_zone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
        else:
            dt_text = candidate_text

    normalized = dt_text[:-1] + "+00:00" if dt_text.endswith("Z") else dt_text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        pass
    else:
        if zone is not None:
            if dt.tzinfo is not None:
                raise InvalidTaskDataError(
                    f"Could not parse '{value}': an explicit UTC offset and a "
                    "timezone name cannot both be given - use one or the other."
                )
            dt = dt.replace(tzinfo=zone)
            return _resolve_in_zone(dt) if keep_zone else dt.astimezone(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=get_default_timezone())
            return _resolve_in_zone(dt) if keep_zone else dt.astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)

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
    # a naive input is interpreted in the default timezone then converted to UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_default_timezone())
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
            f"Relative Erinnerung '{spec}' needs the task to have a faellig_datum or "
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

    The leading "-" is what makes a relative reminder fire *before* its
    anchor. A positive duration ("PT30M") is legal RFC 5545 and accepted as
    written - it schedules the reminder half an hour *after* the due/start
    date, which is occasionally what a caller wants and never what a missing
    sign means to guess about.
    """
    trigger_value, trigger_params = _parse_trigger(spec, has_due=has_due, has_start=has_start)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", description or "Reminder")
    alarm.add("trigger", trigger_value, parameters=trigger_params)
    return alarm


def _trigger_key(value: datetime | timedelta) -> _TriggerKey:
    """Identity of a trigger, independent of how it was spelled.

    "-P1W" and "-P7D" are the same relative trigger, and "...Z" and
    "...+02:00" name the same instant, so both compare equal here.
    """
    if isinstance(value, datetime):
        return ("dt", value.astimezone(timezone.utc))
    return ("dur", value)


def _expected_related(component) -> str | None:
    """The RELATED anchor `build_alarm` would give a relative trigger on this component.

    None when the component has neither DUE nor DTSTART - a relative reminder
    cannot be written onto it at all.
    """
    if "due" in component:
        return "END"
    if "dtstart" in component:
        return "START"
    return None


def _has_anchor(component, related: str) -> bool:
    """Whether the component carries the property a RELATED value names.

    START is DTSTART; END is DUE on a task and DTEND (or DTSTART+DURATION) on
    an event. An anchor the component does not have cannot place an alarm at a
    different moment than the anchor it does have - which is what makes a
    differently-named anchor harmless in `_read_alarm`.
    """
    if related == "START":
        return "dtstart" in component
    if related == "END":
        return "due" in component or "dtend" in component or "duration" in component
    return False


def _read_alarm(alarm, component) -> tuple[str, _TriggerKey] | None:
    """Render one VALARM's TRIGGER as a reminder spec plus its identity, or None.

    None means "this alarm has no `erinnerungen` spelling": returning a string
    for it would either be a lie about when it fires or a value the write path
    would reject. That covers a missing TRIGGER, a repeated TRIGGER property
    (`icalendar` hands those back as a list), a DATE-valued trigger, an
    absolute trigger whose TZID cannot be resolved, a relative trigger on a
    component with neither DUE nor DTSTART (nothing to anchor to, so writing
    the string back would be rejected), and a relative trigger anchored to an
    anchor the component really has while `build_alarm` would re-derive the
    other one - `RELATED=END` on an event with a DTEND, `RELATED=START` on a
    task that has both dates. Such alarms are left strictly alone by
    `apply_alarms`.

    A named anchor the component does *not* have is not a mismatch: RELATED is
    omissible and defaults to START, so `TRIGGER:-PT30M` on a task with only a
    due date is the ordinary wire form for "30 minutes before it is due", not
    an alarm hanging off a DTSTART that isn't there.
    """
    prop = alarm.get("trigger")
    if prop is None:
        return None
    value = getattr(prop, "dt", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            zone = _trigger_zone(prop)
            if zone is None:
                return None
            value = value.replace(tzinfo=zone)
        formatted = format_datetime_output(value)
        if formatted is None:
            return None
        return formatted, _trigger_key(value)
    if isinstance(value, timedelta):
        expected_related = _expected_related(component)
        if expected_related is None:
            return None
        params = getattr(prop, "params", {}) or {}
        # RFC 5545: a TRIGGER without RELATED is anchored to the start.
        related = str(params.get("RELATED", "START")).upper()
        if related != expected_related and _has_anchor(component, related):
            return None
        return vDuration(value).to_ical().decode(), _trigger_key(value)
    return None


def apply_alarms(component, specs: list[str], description: str) -> None:
    """Replace the component's reminders with `specs`, without collateral damage.

    Only VALARMs that `extract_alarms` can express are up for replacement:
    - an alarm already triggering at one of the requested moments is left
      exactly as it is, keeping its ACTION, DESCRIPTION, UID and dismiss state
      (ACKNOWLEDGED / X-MOZ-LASTACK) - rebuilding it would make an already
      dismissed reminder fire again;
    - an alarm whose trigger is not in the list is removed (that is what
      "replaces all reminders" means);
    - an alarm this format cannot express (see `_read_alarm`) is never touched,
      because it was never visible to the caller in the first place. Clearing
      "erinnerungen" via `felder_leeren` still removes every VALARM.

    Duplicate specs (including different spellings of the same trigger) produce
    one alarm, not several.
    """
    has_due = "due" in component
    has_start = "dtstart" in component

    wanted: dict[_TriggerKey, str] = {}
    for spec in specs:
        value, _params = _parse_trigger(spec, has_due=has_due, has_start=has_start)
        wanted.setdefault(_trigger_key(value), spec)

    kept: set[_TriggerKey] = set()
    subcomponents = []
    for sub in component.subcomponents:
        if getattr(sub, "name", None) != "VALARM":
            subcomponents.append(sub)
            continue
        read = _read_alarm(sub, component)
        if read is None:
            subcomponents.append(sub)
            continue
        key = read[1]
        if key in wanted and key not in kept:
            kept.add(key)
            subcomponents.append(sub)
    for key, spec in wanted.items():
        if key not in kept:
            subcomponents.append(
                build_alarm(spec, description, has_due=has_due, has_start=has_start)
            )
    component.subcomponents = subcomponents


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
        apply_alarms(todo, list(fields.erinnerungen), str(todo.get("summary", "Reminder")))


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
    return format_datetime_output(value)


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


def extract_alarms(component) -> list[str]:
    """Extract VALARM subcomponents from an iCalendar component as reminder strings.

    Returns each alarm's TRIGGER in the string format accepted by create_task /
    create_event:
    - Relative trigger (timedelta): RFC 5545 duration string, e.g. "-PT30M", "-P1D",
      serialized via `vDuration`. Equivalent spellings are normalized to the
      canonical one ("-P1W" reads back as "-P7D", "-PT90M" as "-PT1H30M") -
      the same trigger, a different string.
    - Absolute trigger (datetime): ISO 8601 string with offset, e.g.
      "2026-08-07T09:00:00+02:00". RFC 5545 requires absolute triggers to be
      UTC, and this server only ever writes them that way - but other clients
      do emit `TRIGGER;VALUE=DATE-TIME;TZID=Europe/Berlin:...`, which
      `icalendar` hands back as a *naive* datetime with the zone left in the
      property's parameters. Reading that as UTC would silently shift the
      reminder by the zone's offset, so the TZID parameter is resolved via
      `_trigger_zone` (which understands plain IANA names, Windows/Outlook
      names, and the prefixed forms Evolution and older clients emit - see
      `_resolve_tzid`); a naive value without any TZID is taken at its word as
      UTC (B2), and a TZID this server cannot resolve is *not* silently read
      as UTC - the alarm is skipped instead (see `_read_alarm`), because
      guessing a zone could shift the reminder by hours. Output is formatted
      in the server's default timezone (`MCP_DEFAULT_TIMEZONE`), the same
      convention DTSTART/DUE follow.

    Alarms appear in the returned list in the order they are defined in the
    component, and only alarms whose trigger this format can express are
    listed at all - see `_read_alarm` for the ones that are skipped and
    `apply_alarms` for how they survive a write untouched.

    What the string form still cannot carry, for the alarms it does list: a
    foreign ACTION (EMAIL/AUDIO), an ATTACH, and VALARM DURATION/REPEAT (alarm
    self-repetition). Those are preserved as long as the alarm stays in the
    list a write passes back; a reminder whose time is *changed* is replaced by
    a plain DISPLAY alarm. `export_calendar`/`import_ics` round-trip every
    alarm verbatim and are the lossless path.
    """
    alarms: list[str] = []
    for sub in getattr(component, "subcomponents", []):
        if getattr(sub, "name", None) != "VALARM":
            continue
        read = _read_alarm(sub, component)
        if read is not None:
            alarms.append(read[0])
    return alarms


# Windows/Outlook zone names for the zones this server is most likely to meet.
# They are not IANA names, so `zoneinfo` cannot resolve them; the mapping is
# the CLDR windowsZones default ("001") territory for each. Deliberately a
# short list of the common ones rather than all ~140 - anything not in it is
# handled by `_trigger_zone` returning None (the alarm is then left alone
# instead of being reported at a guessed time).
_WINDOWS_TZIDS: dict[str, str] = {
    "utc": "Etc/UTC",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "romance standard time": "Europe/Paris",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Kyiv",
    "gtb standard time": "Europe/Bucharest",
    "russian standard time": "Europe/Moscow",
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "india standard time": "Asia/Kolkata",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "aus eastern standard time": "Australia/Sydney",
}


def _resolve_tzid(tzid: str) -> ZoneInfo | None:
    """Resolve a TZID parameter to a zone, or None if it names no zone we know.

    Three forms are accepted, in order: a plain IANA name ("Europe/Berlin"), a
    Windows/Outlook zone name ("W. Europe Standard Time", see `_WINDOWS_TZIDS`),
    and the prefixed forms Evolution and older clients emit
    ("/freeassociation.sourceforge.net/Europe/Berlin"), whose trailing path
    segments are an IANA name.
    """
    name = tzid.strip()
    for candidate in (name, _WINDOWS_TZIDS.get(name.lower(), "")):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    parts = [part for part in name.split("/") if part]
    for count in (3, 2):
        if len(parts) > count:
            try:
                return ZoneInfo("/".join(parts[-count:]))
            except (ZoneInfoNotFoundError, ValueError):
                pass
    return None


def _trigger_zone(prop: Any) -> timezone | ZoneInfo | None:
    """The zone a naive absolute TRIGGER value is expressed in.

    UTC when the property carries no TZID at all (RFC 5545 requires absolute
    triggers to be UTC, so that is the value's own claim). Otherwise the zone
    the TZID names, or None when it names none this server can resolve -
    reporting such a reminder at a guessed hour would be worse than not
    listing it, since the alarm itself is preserved either way.
    """
    params = getattr(prop, "params", {}) or {}
    tzid = params.get("TZID")
    if not tzid:
        return timezone.utc
    return _resolve_tzid(str(tzid))


def parse_vtodo(component) -> dict[str, Any]:
    """Parse an icalendar VTODO component into the server's German task dict."""
    priority = component.get("priority")
    percent = component.get("percent-complete")
    status = str(component.get("status", "NEEDS-ACTION")).upper()
    return {
        "uid": str(component.get("uid")),
        "titel": str(component.get("summary", "")),
        "start_datum": _format_date_property(component, "dtstart"),
        "faellig_datum": _format_date_property(component, "due"),
        "prioritaet": ical_priority_to_label(int(priority)) if priority is not None else None,
        "fortschritt_prozent": int(percent) if percent is not None else 0,
        "status": "erledigt" if status == "COMPLETED" else "offen",
        "ort": _get_text(component, "location"),
        "url": _get_text(component, "url"),
        "tags": _extract_categories(component),
        "erinnerungen": extract_alarms(component),
        "notizen": _get_text(component, "description"),
        "uebergeordnete_uid": _extract_parent_uid(component),
        "wiederholung": _extract_rrule(component),
    }


def _to_comparable_datetime(value: str, *, end_of_day: bool) -> datetime:
    """Parse a `list_tasks` due-filter/stored due value into a comparable datetime.

    Reuses `parse_datetime_input`. A bare `date` result (an
    all-day due date, or an all-day filter bound) has no time component to
    compare directly, so it's expanded to a single instant within that day in
    the default timezone: start-of-day (00:00:00) when `end_of_day` is False,
    end-of-day (23:59:59) when True. Callers use `end_of_day=True` only for the
    `faellig_vor` (due-before) bound, so a date-only bound like "2026-07-20"
    still includes tasks due at any time on the 20th; `faellig_nach`
    (due-after) bounds and the tasks' own stored due values use
    `end_of_day=False` (start-of-day), so a date-only bound includes tasks due
    from the very start of that day onward, and an all-day task's own due date
    compares as its earliest instant either way.
    """
    parsed = parse_datetime_input(value)
    if isinstance(parsed, datetime):
        return parsed
    if end_of_day:
        return _local_wall_time(parsed, time(23, 59, 59))
    return local_midnight(parsed)


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
    no `faellig_datum` (due date) are excluded - a task can't be "due before X"
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
            due_text = task.get("faellig_datum")
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
