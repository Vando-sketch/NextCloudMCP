"""Translation between the server's German task fields and iCalendar VTODO properties."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from icalendar import Alarm, vDuration

from .errors import InvalidTaskDataError

PRIORITY_LABELS: dict[str, int] = {"hoch": 1, "mittel": 5, "niedrig": 9}
VISIBILITY_LABELS: dict[str, str] = {
    "öffentlich": "PUBLIC",
    "privat": "PRIVATE",
    "vertraulich": "CONFIDENTIAL",
}


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
    """Parse an ISO 8601 date or datetime string, accepting a trailing 'Z'."""
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise InvalidTaskDataError(
            f"Could not parse '{value}' as an ISO 8601 date or datetime."
        ) from None


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
    # a naive input is assumed to already be UTC.
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
    ISO 8601 datetime.
    """
    trigger_value, trigger_params = _parse_trigger(spec, has_due=has_due, has_start=has_start)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", description or "Reminder")
    alarm.add("trigger", trigger_value, parameters=trigger_params)
    return alarm


def apply_task_fields(
    todo,
    *,
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
) -> None:
    """Apply the given (non-None) fields onto an icalendar VTODO component in place.

    Fields left as None are left untouched, which is what gives create_task
    and update_task their "only set what's provided" semantics.
    """
    if titel is not None:
        _set(todo, "summary", titel)
    if start_datum is not None:
        _set(todo, "dtstart", parse_datetime_input(start_datum))
    if faellig_datum is not None:
        _set(todo, "due", parse_datetime_input(faellig_datum))
    if prioritaet is not None:
        _set(todo, "priority", priority_label_to_ical(prioritaet))
    if fortschritt_prozent is not None:
        if not 0 <= fortschritt_prozent <= 100:
            raise InvalidTaskDataError(
                f"fortschritt_prozent must be between 0 and 100, got {fortschritt_prozent}."
            )
        _set(todo, "percent-complete", fortschritt_prozent)
    if ort is not None:
        _set(todo, "location", ort)
    if url is not None:
        _set(todo, "url", url)
    if tags is not None:
        _set(todo, "categories", list(tags))
    if notizen is not None:
        _set(todo, "description", notizen)
    if sichtbarkeit is not None:
        _set(todo, "class", visibility_label_to_ical(sichtbarkeit))
    if uebergeordnete_aufgabe is not None:
        _set(todo, "related-to", uebergeordnete_aufgabe, parameters={"RELTYPE": "PARENT"})

    if erinnerungen is not None:
        todo.subcomponents = [c for c in todo.subcomponents if c.name != "VALARM"]
        has_due = "due" in todo
        has_start = "dtstart" in todo
        title_for_alarm = str(todo.get("summary", "Reminder"))
        for spec in erinnerungen:
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
    }
