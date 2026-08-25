"""Translation between the server's task fields and iCalendar VTODO properties."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from icalendar import Alarm, vDuration, vRecur

from .errors import InvalidTaskDataError

PRIORITY_LABELS: dict[str, int] = {"high": 1, "medium": 5, "low": 9}
VISIBILITY_LABELS: dict[str, str] = {
    "public": "PUBLIC",
    "private": "PRIVATE",
    "confidential": "CONFIDENTIAL",
}
# Reverse of VISIBILITY_LABELS for parsing CLASS back to a label; an unknown
# CLASS value (a foreign client's extension) reads as None, like a missing one.
_ICAL_CLASS_TO_LABEL: dict[str, str] = {v: k for k, v in VISIBILITY_LABELS.items()}
# RFC 5545 VTODO STATUS values <-> the status labels `update_task`'s `status`
# parameter and `list_tasks`/`get_task`'s `status` result key use. "completed"
# and "open" existed before this map did (as the two-valued collapse
# `parse_vtodo` used to do); "in-progress"/"cancelled" are new. See
# `task_status_label_to_ical`/`parse_vtodo` for the write/read sides.
TASK_STATUS_LABELS: dict[str, str] = {
    "open": "NEEDS-ACTION",
    "in-progress": "IN-PROCESS",
    "completed": "COMPLETED",
    "cancelled": "CANCELLED",
}
_ICAL_TASK_STATUS_TO_LABEL: dict[str, str] = {v: k for k, v in TASK_STATUS_LABELS.items()}

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

# Maps the LLM-facing `clear_fields` entry name to the
# (TaskFields attribute name, iCalendar property name) it clears. "title" is
# deliberately absent - clearing the title is not a supported operation.
# "reminders" has no single iCalendar property (it clears all VALARM
# subcomponents instead), hence the `None` ical name, handled specially in
# `apply_task_fields`.
_CLEAR_SPECS: dict[str, tuple[str, str | None]] = {
    "start_date": ("start_date", "dtstart"),
    "due_date": ("due_date", "due"),
    "priority": ("priority", "priority"),
    "progress_percent": ("progress_percent", "percent-complete"),
    "location": ("location", "location"),
    "url": ("url", "url"),
    "tags": ("tags", "categories"),
    "reminders": ("reminders", None),
    "notes": ("notes", "description"),
    "visibility": ("visibility", "class"),
    "parent_task": ("parent_task", "related-to"),
    "recurrence": ("recurrence", "rrule"),
    "exception_dates": ("exception_dates", "exdate"),
}


@dataclass(frozen=True)
class TaskFields:
    """The optional task fields shared by create_task/update_task, in one place.

    This is the single definition of the (previously hand-copied five times,
    C3) task parameter list. The MCP tool functions in `server.py`
    keep their own flat parameter lists - that's the
    LLM-facing tool contract - and build a `TaskFields` internally; everything
    below that layer (`CalDavService`, `apply_task_fields`) works with this
    dataclass instead of a long kwarg list.

    A field left as `None` means "leave unchanged" (update_task) or "not set"
    (create_task). `clear` names fields to remove entirely on update_task
    instead (B3) - see `apply_task_fields` for the accepted names and the
    validation rules (unknown names, and setting+clearing the same field in
    one call, both raise `InvalidTaskDataError`).

    `exception_dates`, when set, *replaces* the task's full EXDATE set (not an
    append), mirroring `EventFields.exception_dates` down to the validation.

    `status` (only settable via `update_task`, not `create_task` - a task is
    always created open) is one of `TASK_STATUS_LABELS`: `"completed"` mirrors
    `complete_task` (STATUS/PERCENT-COMPLETE/COMPLETED), `"open"` is the
    reopen path (removes COMPLETED, resets PERCENT-COMPLETE to 0), and
    `"in-progress"`/`"cancelled"` only set STATUS. If `progress_percent` is
    also given in the same call, its explicit value wins over whatever
    `status` would otherwise derive - see `apply_task_fields`'s write order.
    """

    title: str | None = None
    start_date: str | None = None
    due_date: str | None = None
    priority: str | None = None
    progress_percent: int | None = None
    location: str | None = None
    url: str | None = None
    tags: list[str] | None = None
    reminders: list[str] | None = None
    notes: str | None = None
    visibility: str | None = None
    parent_task: str | None = None
    recurrence: str | None = None
    exception_dates: list[str] | None = None
    status: str | None = None
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
    except (ZoneInfoNotFoundError, ValueError, IsADirectoryError):
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
    events and tasks, `start`/`end` and `due_before`/`due_after` bounds, and
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
    """Map a priority label to an RFC 5545 PRIORITY value (1-9)."""
    try:
        return PRIORITY_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown priority '{label}'. Expected one of: {', '.join(PRIORITY_LABELS)}."
        ) from None


def ical_priority_to_label(value: int | None) -> str | None:
    """Map an RFC 5545 PRIORITY value back to a priority label.

    Follows the common client convention: 1-4 high, 5 medium, 6-9 low,
    0/absent undefined.
    """
    if not value:
        return None
    if 1 <= value <= 4:
        return "high"
    if value == 5:
        return "medium"
    if 6 <= value <= 9:
        return "low"
    return None


def task_status_label_to_ical(label: str) -> str:
    """Map a task status label to an RFC 5545 VTODO STATUS value."""
    try:
        return TASK_STATUS_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown status '{label}'. Expected one of: {', '.join(TASK_STATUS_LABELS)}."
        ) from None


def visibility_label_to_ical(label: str) -> str:
    """Map a visibility label to an RFC 5545 CLASS value."""
    try:
        return VISIBILITY_LABELS[label]
    except KeyError:
        raise InvalidTaskDataError(
            f"Unknown visibility '{label}'. Expected one of: {', '.join(VISIBILITY_LABELS)}."
        ) from None


def _split_timezone_name(text: str) -> tuple[str, ZoneInfo | None]:
    """Split "<datetime> <IANA name>" into its two parts.

    The zone is None (and the text returned unchanged) when the value names no
    zone this machine knows - a plain datetime, or a trailing word that is not
    a zone name, both of which the datetime parser then rejects or accepts on
    its own terms.
    """
    if " " not in text:
        return text, None
    candidate_text, _, candidate_zone = text.rpartition(" ")
    try:
        return candidate_text, ZoneInfo(candidate_zone)
    except (ZoneInfoNotFoundError, ValueError, IsADirectoryError):
        return text, None


def names_timezone(value: str) -> bool:
    """Whether this input names an IANA timezone itself (e.g. "... Europe/Berlin").

    The difference between a zone the *caller* chose and the default one this
    server attached to a naive value. Both come back from
    `parse_datetime_input(keep_zone=True)` as an ordinary `ZoneInfo`, but only
    the first is a statement about which zone the value belongs in - which is
    what lets `event_mapping` move an event to another zone on request while
    still writing everything else in the zone the event already has.
    """
    return _split_timezone_name(value.strip())[1] is not None


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

    dt_text, zone = _split_timezone_name(text)

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


def parse_rrule_text(text: str, anchor: date | datetime | None = None) -> vRecur:
    """Validate and parse raw RFC 5545 RRULE text (e.g. "FREQ=WEEKLY;BYDAY=MO").

    Shared by tasks (VTODO RRULE) and events (`event_mapping._parse_rrule`,
    a thin wrapper that re-raises as `InvalidEventDataError`) - one parser,
    one error message, for both. `vRecur.from_ical` silently *skips* parts
    without '=' instead of raising, so completely unparseable input yields an
    empty rule - treated as invalid here as well, since an empty RRULE is
    never what the caller meant.
    """
    stripped = text.strip()
    if stripped.upper().startswith("RRULE:"):
        stripped = stripped[6:].strip()

    VALID_PARTS = {
        "FREQ",
        "UNTIL",
        "COUNT",
        "INTERVAL",
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYDAY",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
        "WKST",
    }

    seen = set()
    for part in stripped.split(";"):
        if not part:
            continue
        if "=" not in part:
            raise InvalidTaskDataError(
                f"Could not parse recurrence '{text}' as an RFC 5545 RRULE "
                "(e.g. 'FREQ=WEEKLY;BYDAY=MO')."
            )
        key = part.split("=")[0].upper()
        if key not in VALID_PARTS:
            raise InvalidTaskDataError(f"Unknown RRULE part: {key}")
        if key in seen:
            raise InvalidTaskDataError(f"Duplicate RRULE part: {key}")
        seen.add(key)

    if "FREQ" not in seen:
        raise InvalidTaskDataError("recurrence requires a FREQ part.")
    if "UNTIL" in seen and "COUNT" in seen:
        raise InvalidTaskDataError("recurrence cannot contain both UNTIL and COUNT.")

    try:
        recur = vRecur.from_ical(stripped)
    except Exception:
        recur = None
    if not recur:
        raise InvalidTaskDataError(
            f"Could not parse recurrence '{text}' as an RFC 5545 RRULE "
            "(e.g. 'FREQ=WEEKLY;BYDAY=MO')."
        )

    if "INTERVAL" in recur:
        if recur["INTERVAL"][0] < 1:
            raise InvalidTaskDataError("INTERVAL must be >= 1.")
    if "COUNT" in recur:
        if recur["COUNT"][0] < 1:
            raise InvalidTaskDataError("COUNT must be >= 1.")
    if "BYMONTHDAY" in recur:
        if 0 in recur["BYMONTHDAY"]:
            raise InvalidTaskDataError("BYMONTHDAY cannot be 0.")
    if "BYMONTH" in recur:
        if any(m < 1 or m > 12 for m in recur["BYMONTH"]):
            raise InvalidTaskDataError("BYMONTH must be between 1 and 12.")
    if "BYHOUR" in recur:
        if any(h < 0 or h > 23 for h in recur["BYHOUR"]):
            raise InvalidTaskDataError("BYHOUR must be between 0 and 23.")

    if anchor is not None and "UNTIL" in recur:
        until_val = recur["UNTIL"][0]
        if isinstance(anchor, datetime):
            anchor_dt = anchor.astimezone(timezone.utc)
            if not isinstance(until_val, datetime):
                until_dt = datetime.combine(until_val, time.min, tzinfo=timezone.utc)
            else:
                until_dt = until_val if until_val.tzinfo else until_val.replace(tzinfo=timezone.utc)
                until_dt = until_dt.astimezone(timezone.utc)
            if until_dt < anchor_dt:
                raise InvalidTaskDataError("UNTIL cannot be before the start date.")
        else:
            until_date = until_val.date() if isinstance(until_val, datetime) else until_val
            if until_date < anchor:
                raise InvalidTaskDataError("UNTIL cannot be before the start date.")

    return recur


# ----------------------------------------------------------------------
# Shared component/recurrence helpers
#
# Everything below is used by *both* VTODOs (this module) and VEVENTs
# (`event_mapping`), which is why it lives here, the lower of the two layers:
# `event_mapping` imports `mapping`, so the reverse import would be a cycle.
# The two helpers that reject bad input raise `InvalidTaskDataError` and are
# re-raised as `InvalidEventDataError` by thin wrappers on the event side -
# the same pattern `parse_datetime_input`/`parse_rrule_text` already follow.
# The field name (`exception_dates`) and the error wording are shared;
# only the noun and the tools named in the hint differ per component kind,
# which is what the `noun`/`reader` arguments carry.
# ----------------------------------------------------------------------

#: How many occurrences of a series `_check_exdates_match_occurrences` expands
#: before giving up on proving that an exception date names one of them, and
#: how far `_expand_recurring_tasks` will scan a rule for in-window
#: occurrences. Ten thousand covers any plausible real series (192 years of
#: weekly occurrences, 27 of daily ones) and takes ~20 ms even for a
#: per-second rule.
_RECURRENCE_SCAN_LIMIT = 10_000


def _component_start(component) -> date | datetime | None:
    """The component's DTSTART value, or None if it has none this module can read.

    A `date` for an all-day component, a `datetime` otherwise - the two kinds
    every other value of the component has to agree with.
    """
    prop = component.get("dtstart")
    value = getattr(prop, "dt", None) if prop is not None else None
    return value if isinstance(value, (date, datetime)) else None


def _component_zone(component) -> tzinfo | None:
    """The zone the component's DTSTART is expressed in, or None.

    None for an all-day (date-valued) or absent DTSTART, and for a floating
    one - none of those anchor anything to a zone.
    """
    value = _component_start(component)
    return value.tzinfo if isinstance(value, datetime) else None


def _wire_zone(zone: tzinfo | None) -> tzinfo:
    """The zone a component's values are written in, given its DTSTART's zone.

    An IANA zone is written as a `TZID` reference (with a matching VTIMEZONE,
    see `caldav_client._sync_vtimezones`); anything else - a bare UTC instant,
    or a fixed offset left by another client - is written as UTC, since
    `icalendar` would otherwise emit a nonstandard `TZID="UTC+02:00"` naming no
    real zone. Shared by everything that has to put several values of one
    component into the same form: DTSTART/DTEND/DUE (`_anchored`) and EXDATE
    (`_exdate_values`).
    """
    return zone if isinstance(zone, ZoneInfo) else timezone.utc


def _anchored(value: date | datetime, zone: tzinfo | None) -> date | datetime:
    """Express a datetime in the component's own zone, keeping the instant.

    Whichever zone a value arrived in - the default one this server attaches to
    naive input, or the plain UTC `parse_datetime_input` turns an explicit
    "+02:00" into - it is written in the zone the component's DTSTART already
    uses. Three things depend on that:

    - `get_event` -> `update_event` must not re-anchor a recurring event to a
      fixed UTC instant, the one form that reintroduces DST drift;
    - the same for `get_task` -> `update_task` on a recurring VTODO: a series
      anchored to a UTC instant slides an hour at every DST transition, which
      is only visible once the listings expand it (finding 5.7);
    - DTSTART and DTEND (and DTSTART and DUE) must not end up anchored to
      *different* zones. Two such ends are the same instant apart on the day
      they are written and an hour apart after the next transition in either
      zone, so the component silently changes length and nothing in the write
      path can see it (finding 2.5).

    Only the spelling changes; the instant stays whatever the input meant,
    including the rule that a naive value means the server's default timezone.
    Moving a component to another zone is done by *naming* that zone, which
    `apply_event_fields`/`apply_task_fields` handle before calling this
    (`names_timezone`).

    `zone` is None when the component has no datetime DTSTART to anchor to (an
    all-day or absent one), and dates carry no zone at all: both pass through.
    Values always come from `parse_datetime_input(keep_zone=True)`, so a
    datetime here is aware.
    """
    if zone is None or not isinstance(value, datetime):
        return value
    return value.astimezone(_wire_zone(zone))


def _as_utc(value: datetime) -> datetime:
    """Make a datetime comparable: a naive value is read in the default zone.

    Same rule as `parse_datetime_input`; our own writes always produce aware
    datetimes, but components written by other clients may carry "floating"
    local times, and those must mean the same thing here as everywhere else in
    the server - reading them as UTC instead would make free/busy and sorting
    disagree with the day windows by the zone's offset.
    """
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=get_default_timezone())


def _exdate_values(component, entries: list[str], *, noun: str = "event") -> list[date | datetime]:
    """Parse `exception_dates` entries into values anchored to the DTSTART.

    Every value of one EXDATE property shares that property's parameters, so
    they must all be expressed the same way: in the zone DTSTART names when it
    has one, in UTC otherwise. Mixing them lets `icalendar` write a single
    `TZID=` next to a value that still carries its own `Z` suffix - forbidden
    by RFC 5545 3.2.19, and invisible when read back - and, more importantly,
    an exception date only cancels an occurrence when it names the same moment
    the recurrence set produced, which is DTSTART's moment in DTSTART's zone.

    Only the spelling is adjusted: each entry keeps the instant it parsed to,
    including the rule that a naive entry means the server's default timezone.

    For the same reason - one property, one set of parameters - every entry
    must be the same *kind* as the component's own start: date-only entries for
    an all-day one, datetimes otherwise. A mixed set (or the wrong kind) is
    rejected rather than written: `icalendar` would put a DATE and a DATE-TIME
    under one property with a single `TZID`, which RFC 5545 3.8.5.1 (one value
    type per property) and 3.2.19 (no TZID on a value without local time) both
    forbid, and which reads back looking fine. A date-only exception on a timed
    series names no occurrence of it in any case.
    """
    start_value = _component_start(component)
    all_day = start_value is not None and not isinstance(start_value, datetime)

    target = _wire_zone(_component_zone(component))
    values: list[date | datetime] = []
    for entry in entries:
        value = parse_datetime_input(entry, keep_zone=True)
        if isinstance(value, datetime):
            value = value.astimezone(target)
        values.append(value)

    kinds = {isinstance(value, datetime) for value in values}
    if start_value is not None:
        kinds.add(not all_day)
    if len(kinds) > 1:
        if start_value is None:
            raise InvalidTaskDataError(
                "exception_dates entries must all be of one kind: either date-only "
                "'YYYY-MM-DD' values or full datetimes, not both."
            )
        expected = "date-only 'YYYY-MM-DD' values" if all_day else "full datetimes"
        state = "all-day" if all_day else "not all-day"
        raise InvalidTaskDataError(
            f"exception_dates entries must match the {noun}'s start: use {expected}, "
            f"because the {noun} is {state}."
        )
    return values


def _occurrence_key(value: date | datetime, *, all_day: bool) -> date | datetime:
    """Identity of one occurrence, for comparing exception dates against a series.

    An all-day series is compared by date (`dateutil` yields its occurrences as
    naive midnights); a timed one by instant, so an exception written in the
    component's zone and an occurrence computed in it match whatever offset
    each side happens to be spelled with.
    """
    if all_day:
        return value.date() if isinstance(value, datetime) else value
    return _as_utc(value).astimezone(timezone.utc) if isinstance(value, datetime) else value


def _rdate_values(component) -> list[date | datetime]:
    """Every RDATE value on the component (extra dates of the recurrence set)."""
    rdate = component.get("rdate")
    if rdate is None:
        return []
    entries = rdate if isinstance(rdate, list) else [rdate]
    values: list[date | datetime] = []
    for entry in entries:
        for item in getattr(entry, "dts", []):
            value = getattr(item, "dt", None)
            if isinstance(value, (date, datetime)):
                values.append(value)
    return values


def _extract_exdates(component) -> list[str]:
    """Read all EXDATE values as ISO strings, whatever wire form they use.

    icalendar exposes a single EXDATE property as one vDDDLists (which may
    itself hold several comma-separated values) and repeated EXDATE
    properties as a list of vDDDLists - both forms occur in the wild, and
    `apply_event_fields`/`apply_task_fields` only ever write the
    single-property form.
    """
    exdate = component.get("exdate")
    if exdate is None:
        return []
    entries = exdate if isinstance(exdate, list) else [exdate]
    result: list[str] = []
    for entry in entries:
        dts = getattr(entry, "dts", None)
        if dts is not None:
            for item in dts:
                formatted = format_datetime_output(item.dt)
                if formatted:
                    result.append(formatted)
        else:
            value: Any = getattr(entry, "dt", None)
            if value is not None and hasattr(value, "isoformat"):
                formatted = format_datetime_output(value)
                if formatted:
                    result.append(formatted)
            else:
                result.append(str(entry))
    return result


def _day_zone(component) -> tzinfo:
    """The zone this component's days are reckoned in.

    Deliberately not `_wire_zone`, which answers a different question - how a
    value is *written*. It sends anything but an IANA zone to UTC, and for a
    floating component that would put a 01:00 occurrence on the previous day,
    disagreeing with `_occurrence_key`, which reads a floating value in the
    server's default zone like everything else here. So: the zone DTSTART
    names when it names one, the default zone otherwise.
    """
    zone = _component_zone(component)
    return zone if zone is not None else get_default_timezone()


def _local_date(value: date | datetime, zone: tzinfo) -> date:
    """The calendar day `value` falls on, seen from the component's own zone.

    A date-only value already is a day; a datetime is re-expressed in `zone`
    first, so "the 9th" means the 9th where the series runs, not wherever the
    value happens to be spelled. A floating value is read in the server's
    default zone on the way (`_as_utc`), the same reading `_occurrence_key`
    gives it - the two must agree, or an occurrence is indexed under one day
    and looked up under another.
    """
    if not isinstance(value, datetime):
        return value
    return _as_utc(value).astimezone(zone).date()


def _exdate_component_values(component) -> list[date | datetime]:
    """Every stored EXDATE value of the component, as dates/datetimes.

    The value-side counterpart of `_extract_exdates` (which formats the same
    values as ISO strings for the read tools), reading all three wire forms
    other clients produce: one property, repeated properties, comma lists.
    """
    exdate = component.get("exdate")
    if exdate is None:
        return []
    entries = exdate if isinstance(exdate, list) else [exdate]
    values: list[date | datetime] = []
    for entry in entries:
        dts = getattr(entry, "dts", None)
        if dts is not None:
            for item in dts:
                value = getattr(item, "dt", None)
                if isinstance(value, (date, datetime)):
                    values.append(value)
        else:
            single = getattr(entry, "dt", None)
            if isinstance(single, (date, datetime)):
                values.append(single)
    return values


class _OccurrenceIndex(NamedTuple):
    """One component's recurrence set, indexed both ways an exception date asks.

    `by_key` answers "is this exact moment an occurrence" (`_occurrence_key`),
    `by_day` answers "which occurrences fall on this day" - the second is what
    lets a whole-day exception apply to a series whose start time it does not
    know.

    `known` is False when the set could not be expanded at all (no RRULE, or a
    rule `dateutil` refuses); `complete` is False when the scan stopped at
    `_RECURRENCE_SCAN_LIMIT` before reaching the requested bound. Both mean the
    absence of a match proves nothing, and callers treat that as inconclusive
    rather than as a mismatch.
    """

    by_key: dict[Any, date | datetime]
    by_day: dict[date, list[date | datetime]]
    known: bool
    complete: bool


def _occurrence_index(component, *, until: date | None) -> _OccurrenceIndex:
    """Expand the component's recurrence set up to and including the day `until`.

    RDATE values belong to that set as much as the rule's own occurrences, so
    they are indexed too - an exception date naming one of them cancels it.
    The expansion stops at the first occurrence past `until` (they ascend) or
    after `_RECURRENCE_SCAN_LIMIT` of them, so a per-second rule cannot turn a
    validity check into a year-deep walk.
    """
    dtstart_value = _component_start(component)
    if dtstart_value is None:
        return _OccurrenceIndex({}, {}, False, False)
    all_day = not isinstance(dtstart_value, datetime)
    zone = _day_zone(component)

    by_key: dict[Any, date | datetime] = {}
    by_day: dict[date, list[date | datetime]] = {}

    def _own_kind(value: date | datetime) -> date | datetime:
        """`dateutil` expands an all-day series into naive midnights, and an
        RDATE may be written either way. The set is kept in the component's
        own kind, so an occurrence taken from it can be written straight back
        as an EXDATE."""
        if all_day and isinstance(value, datetime):
            return value.date()
        return value

    def record(value: date | datetime) -> None:
        value = _own_kind(value)
        key = _occurrence_key(value, all_day=all_day)
        if key in by_key:
            return
        by_key[key] = value
        by_day.setdefault(_local_date(value, zone), []).append(value)

    for extra in _rdate_values(component):
        record(extra)

    if component.get("rrule") is None:
        # RDATEs alone are still a recurrence set this can answer for.
        return _OccurrenceIndex(by_key, by_day, bool(by_key), True)

    complete = True
    try:
        # `_extract_rrule`, not `rrule_prop.to_ical()`: a component carrying a
        # duplicated RRULE property exposes a *list* here, and `.to_ical()` on
        # a list raises AttributeError (finding 5.6, same crash, same fix).
        rule = rrulestr(str(_extract_rrule(component)), dtstart=dtstart_value)
        for position, occurrence in enumerate(rule):
            if position >= _RECURRENCE_SCAN_LIMIT:
                complete = False
                break
            if until is not None and _local_date(_own_kind(occurrence), zone) > until:
                break
            record(occurrence)
    except (ValueError, TypeError, OverflowError):
        return _OccurrenceIndex(by_key, by_day, False, False)  # proves nothing
    return _OccurrenceIndex(by_key, by_day, True, complete)


@dataclass(frozen=True)
class ExdateResolution:
    """What a batch of exception-date specs resolved to on one component.

    `values` are the occurrence values to write, ascending and deduplicated,
    already expressed the way the component writes its own - so the
    one-property/one-kind rule `_exdate_values` enforces holds by
    construction. `skipped` pairs every spec that resolved to nothing with the
    reason it did.
    """

    values: list[date | datetime]
    skipped: list[tuple[str, str]]


def _unresolved_day_reason(index: _OccurrenceIndex, noun: str) -> str:
    """Why a whole-day spec found no occurrence - missing, or unknowable."""
    if not index.known:
        return f"this {noun} has no recurrence to expand, so a whole-day exception names nothing"
    if not index.complete:
        return (
            f"this {noun}'s recurrence is too dense to expand that far - "
            "name the occurrence exactly instead of the day"
        )
    return f"this {noun} has no occurrence on that day"


def resolve_exdate_specs(component, specs: list[str], *, noun: str = "event") -> ExdateResolution:
    """Resolve exception-date specs against the component's own recurrence set.

    Two things separate this from the `_exdate_values` +
    `_check_exdates_match_occurrences` pair that `exception_dates` goes through
    on create/update, and both exist for the same reason - cancelling the same
    days across several series at once:

    - a date-only spec on a *timed* component means "every occurrence on that
      day", not "midnight on that day". Five series that each start at a
      different hour take one list of days here, instead of one exact datetime
      per series;
    - a spec naming no occurrence is *reported*, not raised. A batch spanning
      several series has to tolerate a day some of them simply do not run on.

    A spec that is not a readable date/datetime at all still raises - that is
    a broken call, not a day this component happens not to have.
    """
    start_value = _component_start(component)
    if start_value is None:
        reason = f"this {noun} has no start to anchor an exception date to"
        return ExdateResolution([], [(spec, reason) for spec in specs])
    if not specs:
        return ExdateResolution([], [])

    all_day = not isinstance(start_value, datetime)
    zone = _wire_zone(_component_zone(component))
    day_zone = _day_zone(component)
    parsed = [(spec, parse_datetime_input(spec, keep_zone=True)) for spec in specs]
    index = _occurrence_index(component, until=max(_local_date(v, day_zone) for _, v in parsed))

    chosen: dict[Any, date | datetime] = {}
    skipped: list[tuple[str, str]] = []
    for spec, value in parsed:
        if all_day and isinstance(value, datetime):
            skipped.append((spec, f"this {noun} is all-day - name the day as 'YYYY-MM-DD'"))
            continue
        if not all_day and not isinstance(value, datetime):
            # A whole day of a timed series: every occurrence falling on it.
            matches = index.by_day.get(value, [])
            if not matches:
                skipped.append((spec, _unresolved_day_reason(index, noun)))
                continue
            for match in matches:
                chosen.setdefault(_occurrence_key(match, all_day=all_day), match)
            continue
        anchored = value.astimezone(zone) if isinstance(value, datetime) else value
        key = _occurrence_key(anchored, all_day=all_day)
        exact = index.by_key.get(key)
        if exact is None:
            if index.known and index.complete:
                skipped.append((spec, f"names no occurrence of this {noun}'s recurrence"))
                continue
            # Inconclusive (no rule, or one too dense to expand): keep the
            # caller's own value - the same benefit of the doubt
            # `_check_exdates_match_occurrences` gives.
            exact = anchored
        chosen.setdefault(key, exact)
    return ExdateResolution([chosen[key] for key in sorted(chosen)], skipped)


def match_existing_exdates(
    component, specs: list[str], *, noun: str = "event"
) -> tuple[set[Any], list[tuple[str, str]]]:
    """Pick out the stored EXDATEs that `specs` name, for removal.

    Matched against what the component *stores*, not against its recurrence
    set: an exception date can outlive the occurrence it once cancelled (the
    series moved underneath it), and removing such a leftover has to keep
    working. A date-only spec drops every stored exception on that day,
    mirroring `resolve_exdate_specs`.

    Returns the `_occurrence_key`s to drop, and the specs that matched nothing.
    """
    start_value = _component_start(component)
    all_day = start_value is not None and not isinstance(start_value, datetime)
    zone = _wire_zone(_component_zone(component))
    day_zone = _day_zone(component)

    by_key: dict[Any, date | datetime] = {}
    by_day: dict[date, list[Any]] = {}
    for value in _exdate_component_values(component):
        key = _occurrence_key(value, all_day=all_day)
        if key in by_key:
            continue
        by_key[key] = value
        by_day.setdefault(_local_date(value, day_zone), []).append(key)

    drop: set[Any] = set()
    skipped: list[tuple[str, str]] = []
    for spec in specs:
        value = parse_datetime_input(spec, keep_zone=True)
        if not all_day and not isinstance(value, datetime):
            keys = by_day.get(value, [])
            if not keys:
                skipped.append((spec, f"this {noun} has no exception date on that day"))
                continue
            drop.update(keys)
            continue
        anchored = value.astimezone(zone) if isinstance(value, datetime) else value
        key = _occurrence_key(anchored, all_day=all_day)
        if key not in by_key:
            skipped.append((spec, f"this {noun} has no such exception date"))
            continue
        drop.add(key)
    return drop, skipped


def _check_exdates_match_occurrences(
    component,
    entries: list[str],
    values: list[date | datetime],
    *,
    noun: str = "event",
    reader: str = "list_events/get_event",
    field_name: str = "start",
) -> None:
    """Reject an exception date that names no occurrence of the series.

    An EXDATE only cancels something when it names exactly a moment the
    recurrence set produces. Miss it - by a day, by an hour, or by writing a
    naive value that means the server's default timezone while the series runs
    on another one - and the exception is stored, the occurrence stays, and
    nothing anywhere says so. That silence was half of finding 2.2; the zone
    anchoring above removed the most common cause, this reports what is left.

    Deliberately best-effort, and never a false alarm:

    - without an RRULE there is no occurrence set to check against (an EXDATE
      on a non-recurring component is pointless, but that is not this check's
      business), and neither is there when the rule or DTSTART is one
      `dateutil` refuses;
    - RDATE values count as occurrences too, being part of the same set;
    - the scan stops after `_RECURRENCE_SCAN_LIMIT` occurrences and passes.
      A per-second rule would otherwise be expanded a year deep to prove a
      point, and "we could not check this cheaply" must not read as "this is
      wrong".
    """
    rrule_prop = component.get("rrule")
    dtstart_value = _component_start(component)
    if rrule_prop is None or dtstart_value is None or not values:
        return
    all_day = not isinstance(dtstart_value, datetime)

    wanted: dict[Any, str] = {}
    for spec, value in zip(entries, values, strict=True):
        wanted.setdefault(_occurrence_key(value, all_day=all_day), spec)
    for extra in _rdate_values(component):
        wanted.pop(_occurrence_key(extra, all_day=all_day), None)
    if not wanted:
        return

    day_zone = _day_zone(component)
    index = _occurrence_index(component, until=max(_local_date(v, day_zone) for v in values))
    if not index.known or not index.complete:
        return  # a set we could not expand in full proves nothing
    for key in index.by_key:
        wanted.pop(key, None)
    if not wanted:
        return

    spec = next(iter(wanted.values()))
    raise InvalidTaskDataError(
        f"exception_dates entry '{spec}' does not name an occurrence of this {noun}'s "
        f"recurrence, so it would cancel nothing. Pass the occurrence exactly as "
        f"{reader} reported its '{field_name}' - a value without a timezone is "
        f"read in the server's default timezone, not in the {noun}'s."
    )


def _check_rrule_anchor(todo, fields: TaskFields) -> None:
    """A recurring VTODO needs a DTSTART to recur from.

    Runs after all clears/sets in `apply_task_fields`, so it validates the
    component's *final* state - the same rule
    `event_mapping._check_start_end_consistency` follows for DTSTART/DTEND.
    That matters for `update_task`: a call that only sets `recurrence` must
    be checked against whatever DTSTART the stored task already has, not
    just the fields passed in this call, so it isn't rejected merely because
    the anchor wasn't repeated here - and, symmetrically, an update that both
    sets `recurrence` and clears the task's only anchor in the same call
    must still be rejected.
    """
    if "rrule" not in todo:
        return

    touched_rrule = fields.recurrence is not None or "recurrence" in fields.clear
    touched_start = fields.start_date is not None or "start_date" in fields.clear
    if not (touched_rrule or touched_start):
        return

    if "dtstart" not in todo:
        raise InvalidTaskDataError(
            "recurrence requires the task to have a start_date to recur from."
        )


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
            f"Could not parse Reminder '{spec}': expected an ISO 8601 duration "
            "like '-P1D' / '-PT1H', or an absolute ISO 8601 datetime."
        ) from None

    if has_due:
        related = "END"
    elif has_start:
        related = "START"
    else:
        raise InvalidTaskDataError(
            f"Relative Reminder '{spec}' needs the task to have a due_date or "
            "start_date to be relative to."
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

    None means "this alarm has no `reminders` spelling": returning a string
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
      "reminders" via `clear_fields` still removes every VALARM.

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
            f"Unknown clear_fields entry/entries: {', '.join(unknown)}. "
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
    field (including "title", which cannot be cleared), raises
    `InvalidTaskDataError`. "status" is deliberately absent from `_CLEAR_SPECS`
    - setting `status="open"` is the documented reset path, so there is
    nothing left to clear.

    `start_date`/`due_date` keep the zone they name and are otherwise
    written in the zone the task's DTSTART already uses (`_anchored`), exactly
    as `apply_event_fields` does for DTSTART/DTEND. A recurring VTODO pinned to
    a fixed UTC instant slides an hour at every DST transition, and DTSTART and
    DUE anchored to two different zones silently change the task's window at
    the next one (finding 5.7).

    `exception_dates` *replaces* the task's whole EXDATE set, and is validated
    exactly as `apply_event_fields` validates the event-side field of the same
    name (literally the same helpers): entries of the wrong value kind, and
    entries naming no occurrence of the task's series, are rejected rather than
    stored to cancel nothing. Clearing `recurrence` also drops EXDATE and
    RDATE, which mean nothing without a recurrence set (finding 5.8).
    """
    clear = tuple(fields.clear or ())
    _validate_clear(fields, clear)

    # Clears run first, so a later set of a *different* field (and the
    # reminders rebuild below) observe the final DTSTART/DUE presence.
    for name in clear:
        _, ical_name = _CLEAR_SPECS[name]
        if name == "reminders":
            todo.subcomponents = [c for c in todo.subcomponents if c.name != "VALARM"]
        elif name == "recurrence":
            # EXDATE and RDATE only mean anything relative to a recurrence set.
            # Dropping the RRULE and leaving them behind orphans them on the
            # component: they cancel and add nothing, no tool reports them as a
            # problem, and they silently come back to life the day someone sets
            # `recurrence` again (finding 5.8).
            for orphaned in (ical_name, "exdate", "rdate"):
                if orphaned is not None and orphaned in todo:
                    del todo[orphaned]
        elif ical_name is not None and ical_name in todo:
            del todo[ical_name]

    # The zone the task is already anchored to, read before its DTSTART is
    # replaced, so every value written below goes on the wire in that one zone
    # instead of each keeping whichever zone it happened to parse in.
    zone = _component_zone(todo)

    if fields.title is not None:
        _set(todo, "summary", fields.title)
    if fields.start_date is not None:
        start_value = parse_datetime_input(fields.start_date, keep_zone=True)
        if not names_timezone(fields.start_date):
            # A start that names no zone means "this instant", not "this task
            # now lives in that zone" - so it is written in the task's own.
            # Naming a zone explicitly is how a task is *moved* to one, and
            # then everything below follows the new anchor.
            start_value = _anchored(start_value, zone)
        _set(todo, "dtstart", start_value)
        zone = _component_zone(todo)
    if fields.due_date is not None:
        due_value = parse_datetime_input(fields.due_date, keep_zone=True)
        if not names_timezone(fields.due_date):
            due_value = _anchored(due_value, zone)
        _set(todo, "due", due_value)
    if fields.priority is not None:
        _set(todo, "priority", priority_label_to_ical(fields.priority))
    if fields.status is not None:
        # Runs *before* the progress_percent block below, deliberately:
        # "completed"/"open" both derive a PERCENT-COMPLETE value (100/0) as
        # a side effect, but an explicit progress_percent in the same call
        # must win over that derived value - writing status first and letting
        # progress_percent's own `_set` run after is what makes the later
        # write stick.
        if fields.status == "completed":
            mark_completed(todo)
        else:
            _set(todo, "status", task_status_label_to_ical(fields.status))
            # A COMPLETED timestamp left behind by an earlier completion would
            # outlive the status change and hide the task: caldav's pending
            # filter (`todos(include_completed=False)`, used by only_open and
            # by get_agenda) drops any VTODO that merely *has* a COMPLETED
            # property, whatever its STATUS says. A task moved back to
            # "in-progress" would then read as in progress and still be missing
            # from every open-task listing, so no non-completed status may
            # leave one behind.
            if "completed" in todo:
                del todo["completed"]
            if fields.status == "open":
                # Reopening also undoes the 100% `mark_completed` wrote;
                # "in-progress"/"cancelled" keep whatever progress was recorded.
                _set(todo, "percent-complete", 0)
    if fields.progress_percent is not None:
        if not 0 <= fields.progress_percent <= 100:
            raise InvalidTaskDataError(
                f"progress_percent must be between 0 and 100, got {fields.progress_percent}."
            )
        _set(todo, "percent-complete", fields.progress_percent)
    if fields.location is not None:
        _set(todo, "location", fields.location)
    if fields.url is not None:
        _set(todo, "url", fields.url)
    if fields.tags is not None:
        _set(todo, "categories", list(fields.tags))
    if fields.notes is not None:
        _set(todo, "description", fields.notes)
    if fields.visibility is not None:
        _set(todo, "class", visibility_label_to_ical(fields.visibility))
    if fields.parent_task is not None:
        _set(
            todo,
            "related-to",
            fields.parent_task,
            parameters={"RELTYPE": "PARENT"},
        )
    if fields.recurrence is not None:
        anchor_val = None
        dtstart_prop = todo.get("dtstart")
        if dtstart_prop is not None:
            anchor_val = getattr(dtstart_prop, "dt", None)
        _set(todo, "rrule", parse_rrule_text(fields.recurrence, anchor_val))
    if fields.exception_dates is not None:
        # Replace, not append: drop every existing EXDATE, then write all
        # entries as one EXDATE property with a comma-separated value list.
        # Runs after `recurrence` above, so setting a rule and its exceptions
        # in one call checks the exceptions against the rule this call writes.
        # (`_extract_exdates` reads back all three wire forms other clients may
        # produce: one property, repeated properties, comma lists.)
        if "exdate" in todo:
            del todo["exdate"]
        if fields.exception_dates:
            exdate_entries = list(fields.exception_dates)
            exdate_values = _exdate_values(todo, exdate_entries, noun="task")
            _check_exdates_match_occurrences(
                todo,
                exdate_entries,
                exdate_values,
                noun="task",
                reader="list_tasks/get_task",
                field_name="start_date",
            )
            todo.add("exdate", exdate_values)

    if fields.reminders is not None:
        apply_alarms(todo, list(fields.reminders), str(todo.get("summary", "Reminder")))

    _check_rrule_anchor(todo, fields)


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

    `icalendar` exposes RRULE as a `vRecur` property; `.to_ical()` serializes
    it back to the same textual form RFC 5545 (and Nextcloud Tasks) uses,
    rather than exposing icalendar's internal dict representation. This is
    the read side of `recurrence` - see `parse_rrule_text`/`TaskFields.recurrence`
    for the write side (`create_task`/`update_task`).
    """
    rrule = component.get("rrule")
    if rrule is None:
        return None
    if isinstance(rrule, list):
        rrule = rrule[0]
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
        except (ZoneInfoNotFoundError, ValueError, IsADirectoryError):
            pass
    parts = [part for part in name.split("/") if part]
    for count in (3, 2):
        if len(parts) > count:
            try:
                return ZoneInfo("/".join(parts[-count:]))
            except (ZoneInfoNotFoundError, ValueError, IsADirectoryError):
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
    """Parse an icalendar VTODO component into the server's task dict.

    "status" is one of `TASK_STATUS_LABELS` ("open"/"in-progress"/"completed"/
    "cancelled"). A missing STATUS property reads as "open" per RFC 5545's own
    default (NEEDS-ACTION); a STATUS value this server doesn't know (a foreign
    client's extension, or a typo written directly via another CalDAV client)
    also reads as "open" rather than raising - this is a read path, and a
    liberal one, same stance as `ical_role_to_label`/`RELTYPE_LABELS` on the
    event side: an unrecognized value must never break a listing.
    """
    priority = component.get("priority")
    percent = component.get("percent-complete")
    status = str(component.get("status", "NEEDS-ACTION")).upper()
    class_value = component.get("class")
    return {
        "uid": str(component.get("uid")),
        "title": str(component.get("summary", "")),
        "start_date": _format_date_property(component, "dtstart"),
        "due_date": _format_date_property(component, "due"),
        "priority": ical_priority_to_label(int(priority)) if priority is not None else None,
        "progress_percent": int(percent) if percent is not None else 0,
        "status": _ICAL_TASK_STATUS_TO_LABEL.get(status, "open"),
        "visibility": (
            _ICAL_CLASS_TO_LABEL.get(str(class_value).upper()) if class_value is not None else None
        ),
        "location": _get_text(component, "location"),
        "url": _get_text(component, "url"),
        "tags": _extract_categories(component),
        "reminders": extract_alarms(component),
        "notes": _get_text(component, "description"),
        "parent_uid": _extract_parent_uid(component),
        "recurrence": _extract_rrule(component),
        "exception_dates": _extract_exdates(component),
        # Both only ever set by `_expand_recurring_tasks`: a stored VTODO is a
        # series or a plain task, never one occurrence of one.
        "recurrence_id": None,
        "series_uid": None,
    }


def _to_comparable_datetime(value: str, *, end_of_day: bool) -> datetime:
    """Parse a `list_tasks` due-filter/stored due value into a comparable datetime.

    Reuses `parse_datetime_input`. A bare `date` result (an
    all-day due date, or an all-day filter bound) has no time component to
    compare directly, so it's expanded to a single instant within that day in
    the default timezone: start-of-day (00:00:00) when `end_of_day` is False,
    end-of-day (23:59:59) when True. Callers use `end_of_day=True` only for the
    `due_before` (due-before) bound, so a date-only bound like "2026-07-20"
    still includes tasks due at any time on the 20th; `due_after`
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


def _task_due_instant(due_text: str | None) -> datetime | None:
    """A task's own stored `due_date` as a comparable instant, or None.

    None means "cannot be placed on a timeline at all": either the task has no
    due date, or the value the server stores is not one this server can read
    (a foreign client's `DUE` holding a bare time or a period, say). Both are
    treated identically - excluded from a due-date filter, sorted last -
    because sorting reads *every* task's due date, so raising here would let
    one unreadable task turn an entire healthy listing into an error.
    """
    if due_text is None:
        return None
    try:
        return _to_comparable_datetime(due_text, end_of_day=False)
    except InvalidTaskDataError:
        _logger.debug("Ignoring unreadable due_date %r while filtering/sorting", due_text)
        return None


def _collation_key(value: str) -> tuple[str, str]:
    """A rough locale-independent collation key for a title.

    Raw codepoint order files every umlaut after "Z" ("Ärztin" behind
    "Dentist"), which reads as no order at all in a listing of
    German-language titles - and the content this server serves is routinely
    German even though its own vocabulary is English.
    Decomposing (NFKD) and dropping the combining marks sorts "Ä" with "A";
    case-folding sorts "ärztin" with "Ärztin" and "ß" with "ss", which is
    also DIN 5007 variant 1's rule. This is not full locale-aware collation -
    that needs a collation library this server does not depend on - so the
    original string is kept as a tiebreak to stay deterministic.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return ("".join(c for c in decomposed if not unicodedata.combining(c)).casefold(), value)


def _fold(value: str) -> str:
    """Normalize text for caseless, spelling-independent matching.

    "ü" has two Unicode spellings (precomposed, or "u" plus a combining
    diaeresis) that no client is consistent about, and `.lower()` leaves "ß"
    and "SS" different - both of which matter for German-language content.
    NFC-normalizing and case-*folding* (not lowercasing) makes either
    spelling of an umlaut, and either spelling of a sharp s, match.
    """
    return unicodedata.normalize("NFC", value).casefold()


def _task_sort_key(task: dict[str, Any]) -> tuple[int, datetime, tuple[str, str]]:
    """Sort key for tasks: due_date ascending, tasks without due date last, then by title."""
    title = _collation_key(str(task.get("title") or ""))
    due = _task_due_instant(task.get("due_date"))
    if due is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc), title)
    return (0, due, title)


#: Separates a series UID from the occurrence it identifies in the synthetic
#: UID an expanded instance carries ("<series uid>#2026-09-08T10:00:00+02:00").
#: A real Nextcloud task UID is a UUID, and `split_occurrence_uid` additionally
#: requires the suffix to parse as a date/datetime, so a foreign UID that
#: happens to contain a "#" is not mistaken for an occurrence.
_OCCURRENCE_UID_SEPARATOR = "#"

#: How many occurrences of one recurring task a listing will expand. The
#: queried window is the real bound (`_expand_recurring_tasks` only runs with an
#: upper one); this is the backstop for a window wide enough that a per-minute
#: rule would otherwise fill the whole listing with one task.
_TASK_EXPANSION_LIMIT = 100


def split_occurrence_uid(task_uid: str) -> tuple[str, str | None]:
    """Split an expanded instance's UID into (series UID, occurrence), or (uid, None).

    The write tools address a task by UID, and an expanded instance is not a
    stored task - there is nothing at that UID to edit, complete or delete. So
    instances carry a UID that is deliberately *not* the series' own, and the
    write paths use this to say so in as many words instead of silently acting
    on the whole series (finding 5.1).

    The occurrence part must itself parse as a date/datetime; anything else is
    treated as an ordinary UID that merely contains a "#".
    """
    series_uid, separator, occurrence = task_uid.rpartition(_OCCURRENCE_UID_SEPARATOR)
    if not separator or not series_uid:
        return task_uid, None
    try:
        parse_datetime_input(occurrence)
    except InvalidTaskDataError:
        return task_uid, None
    return series_uid, occurrence


def _rule_from_task(anchor: date | datetime, rrule_text: str) -> tuple[Any, date | datetime] | None:
    """A `dateutil` rule for a parsed task's series, or None if it can't be built.

    The anchor arrives as the ISO string `parse_vtodo` produced, i.e. carrying a
    bare numeric offset rather than the zone name the component actually uses.
    Handing that straight to `dateutil` would generate every occurrence at that
    one *fixed* offset, so an occurrence after the next DST transition would
    come back an hour off the wall clock the series means - reintroducing on the
    read side exactly the drift finding 5.7 removed on the write side. Attaching
    the server's default zone (a real `ZoneInfo`, which recomputes its offset
    per instant) instead keeps a "every Monday 09:00" series at 09:00 local
    year-round.

    This is exact for every task this server writes, since 5.7 anchors those to
    the default zone. A series a foreign client anchored to some *other* zone is
    expanded in the default zone instead, which differs only between the two
    zones' transition dates - and `list_tasks` reports every timestamp in the
    default zone anyway.
    """
    if isinstance(anchor, datetime):
        anchor = anchor.astimezone(get_default_timezone())
    try:
        return rrulestr(rrule_text, dtstart=anchor), anchor
    except (ValueError, TypeError, OverflowError):
        return None  # a rule dateutil refuses expands to nothing we can trust


def _due_instant(value: date | datetime) -> datetime:
    """Where an occurrence's due value sits on the timeline, for window checks."""
    return _as_utc(value) if isinstance(value, datetime) else local_midnight(value)


def _expand_recurring_tasks(
    tasks: list[dict[str, Any]],
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> list[dict[str, Any]]:
    """Replace each recurring task with the occurrences due inside the window.

    CalDAV servers do not expand VTODO series the way they expand VEVENTs, so a
    recurring task used to appear exactly once in every listing - at its
    original due date, never again (finding 5.1). This computes the missing
    occurrences client-side from the RRULE, using the same `dateutil` machinery
    `_check_exdates_match_occurrences` already uses.

    Bounded three ways, because an RRULE need not terminate:

    - it only runs at all when `window_end` is set, i.e. when the caller asked
      for tasks due before some date. Without an upper bound there is no window
      to expand into, and the series is left as the single master row it is
      today - `recurrence` intact - rather than flooding an unfiltered
      `list_tasks` with a hundred copies of every recurring task;
    - occurrences ascend, so the scan stops at the first one past `window_end`;
    - and `_TASK_EXPANSION_LIMIT`/`_RECURRENCE_SCAN_LIMIT` cap what one task can
      emit and scan even so.

    A task is left untouched (one master row) whenever the expansion cannot be
    trusted: no rule, no due date to place occurrences on, an unreadable anchor,
    a start and due of different value kinds, or a rule `dateutil` refuses.
    Degrading to today's behaviour is always safe; inventing occurrences is not.

    Each emitted instance is marked so no caller can mistake it for the stored
    task: `recurrence_id` names the occurrence, `recurrence` is `None`
    (nothing about an instance recurs), `series_uid` points at the stored task,
    and `uid` is a synthetic "<series uid>#<occurrence>" that every write path
    rejects by name (`split_occurrence_uid`). An instance is a read-only view of
    one date in a series; `update_task`/`complete_task` act on series, and take
    `series_uid`.
    """
    if window_end is None:
        return tasks

    expanded: list[dict[str, Any]] = []
    for task in tasks:
        occurrences = _occurrences_of(task, window_start=window_start, window_end=window_end)
        if occurrences is None:
            expanded.append(task)
        else:
            expanded.extend(occurrences)
    return expanded


def _occurrences_of(
    task: dict[str, Any],
    *,
    window_start: datetime | None,
    window_end: datetime,
) -> list[dict[str, Any]] | None:
    """The in-window instances of one recurring task, or None to leave it as is."""
    rrule_text = task.get("recurrence")
    due_text = task.get("due_date")
    if not rrule_text or not due_text:
        # No rule, or nothing due to place the occurrences on: a task with no
        # DUE is excluded from a due-filtered listing either way.
        return None

    start_text = task.get("start_date")
    try:
        due_anchor = parse_datetime_input(due_text, keep_zone=True)
        # RFC 5545 generates the recurrence set from DTSTART; DUE only rides
        # along at a fixed distance from it. A DUE-only series is a foreign
        # client's, and anchoring on DUE is the best reading available.
        anchor = parse_datetime_input(start_text, keep_zone=True) if start_text else due_anchor
    except InvalidTaskDataError:
        return None
    if type(anchor) is not type(due_anchor):
        # An all-day start with a timed due (or vice versa) has no well-defined
        # offset between them - refuse rather than guess.
        return None

    built = _rule_from_task(anchor, str(rrule_text))
    if built is None:
        return None
    rule, anchor = built
    all_day = not isinstance(anchor, datetime)
    # How far DUE sits from DTSTART. Aware datetimes subtract as instants, so
    # the two anchors' spellings do not matter; adding it back to an occurrence
    # is wall-clock arithmetic in the occurrence's own zone, which is what keeps
    # a "09:00 -> 17:00" task 09:00 -> 17:00 on both sides of a transition.
    shift = due_anchor - anchor if start_text else timedelta(0)

    skipped = set()
    for entry in task.get("exception_dates") or []:
        try:
            value = parse_datetime_input(entry, keep_zone=True)
        except InvalidTaskDataError:
            continue  # an EXDATE this server cannot read cancels nothing
        skipped.add(_occurrence_key(value, all_day=all_day))

    instances: list[dict[str, Any]] = []
    for index, occurrence in enumerate(rule):
        if index >= _RECURRENCE_SCAN_LIMIT or len(instances) >= _TASK_EXPANSION_LIMIT:
            break
        occ_start: date | datetime = occurrence.date() if all_day else occurrence
        occ_due = occ_start + shift
        placed = _due_instant(occ_due)
        if placed > window_end:
            break  # occurrences ascend: nothing further can fall inside
        if window_start is not None and placed < window_start:
            continue  # before the window, and not one of this task's results
        if _occurrence_key(occ_start, all_day=all_day) in skipped:
            continue

        occurrence_id = format_datetime_output(occ_start)
        instance = dict(task)
        instance["recurrence"] = None
        instance["recurrence_id"] = occurrence_id
        instance["exception_dates"] = []
        instance["series_uid"] = task.get("uid")
        instance["uid"] = f"{task.get('uid')}{_OCCURRENCE_UID_SEPARATOR}{occurrence_id}"
        if start_text:
            instance["start_date"] = occurrence_id
        instance["due_date"] = format_datetime_output(occ_due)
        instances.append(instance)
    return instances


def filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    due_before: str | None = None,
    due_after: str | None = None,
    priority: str | None = None,
    tag: str | None = None,
    search_text: str | None = None,
    limit: int | None = None,
    without_reminder: bool = False,
    without_visibility: bool = False,
    without_tags: bool = False,
    uid_regex: str | None = None,
) -> list[dict[str, Any]]:
    """Filter already-`parse_vtodo`-parsed task dicts and sort them.

    `due_before`/`due_after` are ISO 8601 date/datetime strings. When either is
    actually given, tasks with no readable `due_date` (due date) are excluded.
    `priority`: "high"/"medium"/"low", validated against `PRIORITY_LABELS`
    (unknown value raises `InvalidTaskDataError`).
    `tag`: exact match against one `tags` entry, `search_text`: substring match over
    `title` and `notes` (skipping None values); both compare case- and
    spelling-insensitively (see `_fold`).

    `without_reminder`/`without_visibility`/`without_tags` keep only tasks whose
    `reminders` list is empty / whose `visibility` is None (no readable
    CLASS) / whose `tags` list is empty. `uid_regex` keeps only tasks whose
    stored `uid` contains a match for the given regular expression
    (`re.search`, case-sensitive - anchor with ^...$ for a full match; an
    unparsable pattern raises `InvalidTaskDataError`). All four are properties
    of the stored task, so they are applied *before* recurrence expansion:
    `uid_regex` matches the series' own uid, never the synthetic
    "<uid>#<occurrence>" uid of an expanded row.

    An empty string means "no filter" for every one of them, due bounds included -
    clients spell an unset argument that way, and these used to disagree about it
    (an error, an empty result, a no-op, and an error again). `limit` keeps
    rejecting 0: an integer parameter spells "unset" as None, so 0 is a caller
    asking for zero results, which is a mistake worth reporting.

    Results are sorted by `due_date` ascending (tasks without a readable due
    date last), then by `title` (see `_collation_key`). `limit`, if given, must be a
    positive integer and caps the number of results returned, applied last.
    """
    if limit is not None and limit <= 0:
        raise InvalidTaskDataError(f"limit must be greater than 0, got {limit}.")

    if priority:
        if priority not in PRIORITY_LABELS:
            raise InvalidTaskDataError(
                f"Unknown priority '{priority}'. Expected one of: {', '.join(PRIORITY_LABELS)}."
            )
        tasks = [task for task in tasks if task.get("priority") == priority]

    if uid_regex:
        try:
            uid_pattern = re.compile(uid_regex)
        except re.error as exc:
            raise InvalidTaskDataError(f"Invalid uid_regex '{uid_regex}': {exc}.") from exc
        tasks = [task for task in tasks if uid_pattern.search(str(task.get("uid") or ""))]

    if without_reminder:
        tasks = [task for task in tasks if not task.get("reminders")]
    if without_visibility:
        tasks = [task for task in tasks if task.get("visibility") is None]
    if without_tags:
        tasks = [task for task in tasks if not task.get("tags")]

    before_bound = _to_comparable_datetime(due_before, end_of_day=True) if due_before else None
    after_bound = _to_comparable_datetime(due_after, end_of_day=False) if due_after else None

    # Before the due filter: expansion turns one recurring task into the rows
    # the filter then trims to the window exactly.
    tasks = _expand_recurring_tasks(tasks, window_start=after_bound, window_end=before_bound)

    if due_before or due_after:
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            due_dt = _task_due_instant(task.get("due_date"))
            if due_dt is None:
                continue
            if before_bound is not None and due_dt > before_bound:
                continue
            if after_bound is not None and due_dt < after_bound:
                continue
            filtered.append(task)
        tasks = filtered

    if tag:
        wanted = _fold(tag)
        tasks = [task for task in tasks if any(_fold(t) == wanted for t in task.get("tags") or [])]

    if search_text:
        needle = _fold(search_text)
        tasks = [
            task
            for task in tasks
            if any(
                needle in _fold(value)
                for value in (task.get("title"), task.get("notes"))
                if value is not None
            )
        ]

    tasks = sorted(tasks, key=_task_sort_key)

    if limit is not None:
        tasks = tasks[:limit]
    return tasks
