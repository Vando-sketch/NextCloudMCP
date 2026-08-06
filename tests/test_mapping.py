"""Unit tests for the field <-> iCalendar mapping logic, no CalDAV involved."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from icalendar import Calendar, Todo

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.errors import InvalidTaskDataError
from nextcloud_task_mcp.mapping import TaskFields


def _new_todo(uid: str = "task-1") -> Todo:
    todo = Todo()
    todo.add("uid", uid)
    return todo


def _todo_from_ics(body: str) -> Todo:
    """Parse a VTODO out of real ICS text.

    Foreign-client alarms are only ever seen through the parser, so the tests
    for them build the component the same way instead of hand-assembling
    `icalendar` objects into states parsing can never produce.
    """
    calendar = Calendar.from_ical(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n" + body + "END:VCALENDAR\n"
    )
    todo = calendar.walk("VTODO")[0]
    assert isinstance(todo, Todo)
    return todo


def _apply(todo, **kwargs) -> None:
    """Convenience wrapper: build a TaskFields from kwargs and apply it."""
    mapping.apply_task_fields(todo, TaskFields(**kwargs))


def test_apply_and_parse_round_trip():
    todo = _new_todo()
    _apply(
        todo,
        titel="Steuererklärung",
        start_datum="2026-07-01",
        faellig_datum="2026-07-20",
        prioritaet="hoch",
        fortschritt_prozent=20,
        ort="Zuhause",
        url="https://example.com/steuer",
        tags=["Finanzen", "Wichtig"],
        notizen="Belege sammeln",
        sichtbarkeit="privat",
    )
    parsed = mapping.parse_vtodo(todo)

    assert parsed["uid"] == "task-1"
    assert parsed["titel"] == "Steuererklärung"
    # Date-only input (B1): all-day, not a midnight datetime.
    assert parsed["start_datum"] == "2026-07-01"
    assert parsed["faellig_datum"] == "2026-07-20"
    assert parsed["prioritaet"] == "hoch"
    assert parsed["fortschritt_prozent"] == 20
    assert parsed["status"] == "offen"
    assert parsed["ort"] == "Zuhause"
    assert parsed["url"] == "https://example.com/steuer"
    assert set(parsed["tags"]) == {"Finanzen", "Wichtig"}
    assert parsed["notizen"] == "Belege sammeln"
    assert parsed["erinnerungen"] == []
    assert parsed["uebergeordnete_uid"] is None


def test_apply_task_fields_replaces_instead_of_appending():
    """Component.add() appends by default; applying twice must not duplicate values."""
    todo = _new_todo()
    _apply(todo, titel="Erster Titel", faellig_datum="2026-07-20")
    _apply(todo, titel="Zweiter Titel")

    parsed = mapping.parse_vtodo(todo)
    assert parsed["titel"] == "Zweiter Titel"
    assert parsed["faellig_datum"] == "2026-07-20"  # untouched field survives


def test_apply_task_fields_leaves_unset_fields_untouched():
    todo = _new_todo()
    _apply(todo, titel="Titel", ort="Büro")
    _apply(todo, notizen="Neue Notiz")

    parsed = mapping.parse_vtodo(todo)
    assert parsed["titel"] == "Titel"
    assert parsed["ort"] == "Büro"
    assert parsed["notizen"] == "Neue Notiz"


@pytest.mark.parametrize(
    ("label", "value"),
    [("hoch", 1), ("mittel", 5), ("niedrig", 9)],
)
def test_priority_label_to_ical(label, value):
    assert mapping.priority_label_to_ical(label) == value


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (1, "hoch"),
        (4, "hoch"),
        (5, "mittel"),
        (6, "niedrig"),
        (9, "niedrig"),
        (0, None),
        (None, None),
    ],
)
def test_ical_priority_to_label(value, label):
    assert mapping.ical_priority_to_label(value) == label


def test_invalid_priority_label_raises():
    with pytest.raises(InvalidTaskDataError):
        mapping.priority_label_to_ical("super-dringend")


def test_invalid_visibility_label_raises():
    with pytest.raises(InvalidTaskDataError):
        mapping.visibility_label_to_ical("geheim")


def test_percent_complete_out_of_range_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        _apply(todo, fortschritt_prozent=150)


def test_relative_reminder_relative_to_due():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20T10:00:00", erinnerungen=["-P1D"])
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("RELATED") == "END"


def test_relative_reminder_falls_back_to_start_when_no_due():
    todo = _new_todo()
    _apply(todo, titel="Task", start_datum="2026-07-15T09:00:00", erinnerungen=["-PT1H"])
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("RELATED") == "START"


def test_relative_reminder_without_start_or_due_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        _apply(todo, titel="Task", erinnerungen=["-P1D"])


def test_absolute_reminder_sets_value_date_time():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["2026-07-19T09:00:00"])
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("VALUE") == "DATE-TIME"


def test_relative_reminder_on_all_day_due_is_legal():
    """A relative VALARM trigger may RELATE to a DATE-valued DUE (B1 + reminders)."""
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["-P1D"])

    parsed = mapping.parse_vtodo(todo)
    assert parsed["faellig_datum"] == "2026-07-20"  # all-day, not midnight datetime
    assert isinstance(todo.get("due").dt, date)

    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("RELATED") == "END"


def test_invalid_reminder_spec_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["not-a-duration"])


def test_updating_reminders_replaces_old_alarms():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["-P1D", "-PT1H"])
    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 2

    _apply(todo, erinnerungen=["-P2D"])
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1


def test_extract_alarms_round_trip_relative_with_due():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20T10:00:00", erinnerungen=["-PT30M"])
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["-PT30M"]


def test_extract_alarms_round_trip_relative_with_start_only():
    todo = _new_todo()
    _apply(todo, titel="Task", start_datum="2026-07-20T10:00:00", erinnerungen=["-PT30M"])
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["-PT30M"]


def test_extract_alarms_round_trip_absolute():
    todo = _new_todo()
    _apply(
        todo,
        titel="Task",
        faellig_datum="2026-07-20",
        erinnerungen=["2026-08-07T09:00:00+00:00", "2026-08-07T09:00:00Z"],
    )
    parsed = mapping.parse_vtodo(todo)
    # Note: "...Z" input reads back as "+00:00", and both spellings name the
    # same instant, so they collapse into one alarm.
    assert parsed["erinnerungen"] == ["2026-08-07T09:00:00+00:00"]


def test_extract_alarms_preserves_order():
    todo = _new_todo()
    reminders = ["-P1D", "-PT30M", "2026-08-07T09:00:00+00:00"]
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=reminders)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == reminders


def test_extract_alarms_no_valarm_returns_empty():
    todo = _new_todo()
    _apply(todo, titel="Task")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == []


def test_extract_alarms_absolute_with_offset_normalizes_to_utc():
    todo = _new_todo()
    _apply(
        todo,
        titel="Task",
        faellig_datum="2026-07-20",
        erinnerungen=["2026-07-19T11:00:00+02:00"],
    )
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["2026-07-19T09:00:00+00:00"]


def _todo_with_tzid_trigger(tzid: str) -> Todo:
    """A task carrying one absolute alarm at 11:00 in the zone `tzid` names.

    `icalendar` resolves a TZID on DUE/DTSTART but leaves a VALARM's TRIGGER
    naive, with the zone name in the property's parameters - so this is what
    an absolute foreign reminder really looks like after parsing.
    """
    zone_param = f";TZID={tzid}" if tzid else ""
    return _todo_from_ics(
        f"""BEGIN:VTODO
UID:task-1
SUMMARY:Task
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:foreign
TRIGGER;VALUE=DATE-TIME{zone_param}:20260719T110000
END:VALARM
END:VTODO
"""
    )


def test_extract_alarms_resolves_foreign_tzid_trigger():
    """A TRIGGER;TZID=... written by another client is not UTC.

    Reading it as UTC would shift the reminder by the zone's offset (two hours
    for Berlin in July). The value keeps its own zone on the way out, like
    DTSTART/DUE do.
    """
    todo = _todo_with_tzid_trigger("Europe/Berlin")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


def test_extract_alarms_resolves_windows_tzid_trigger():
    """Outlook writes Windows zone names, which `zoneinfo` does not know."""
    todo = _todo_with_tzid_trigger("W. Europe Standard Time")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


def test_extract_alarms_resolves_prefixed_tzid_trigger():
    """Evolution and older clients prefix the IANA name with a namespace path."""
    todo = _todo_with_tzid_trigger("/freeassociation.sourceforge.net/Europe/Berlin")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


def test_extract_alarms_resolves_prefixed_three_segment_tzid_trigger():
    """Some IANA names have three segments, so two trailing segments is not enough."""
    todo = _todo_with_tzid_trigger("/softwarestudio.org/Olson_20011030_5/America/Indiana/Knox")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00-05:00"]


def test_extract_alarms_naive_trigger_without_tzid_is_utc():
    """RFC 5545 requires absolute triggers in UTC, so an unqualified one is taken at its word."""
    todo = _todo_with_tzid_trigger("")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00+00:00"]


def test_extract_alarms_skips_unresolvable_tzid_trigger_but_keeps_the_alarm():
    """An unknown zone name is not silently read as UTC.

    Guessing a zone would move the reminder by hours, so the alarm is left out
    of `erinnerungen` - and, being invisible, is left untouched by a write.
    """
    todo = _todo_with_tzid_trigger("Mars/Olympus")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == []

    _apply(todo, erinnerungen=["-PT30M"])

    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 2


def test_extract_alarms_tzid_naming_a_tzdata_directory_does_not_crash():
    """A TZID like "Europe" names a tzdata *directory*, not a zone."""
    todo = _todo_with_tzid_trigger("Europe")
    assert mapping.parse_vtodo(todo)["erinnerungen"] == []


_FOREIGN_ALARMS_ICS = """BEGIN:VTODO
UID:task-1
SUMMARY:Task
DTSTART:20260715T090000Z
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:ours
TRIGGER;RELATED=END:-PT30M
END:VALARM
BEGIN:VALARM
ACTION:AUDIO
DESCRIPTION:anchored to the start
TRIGGER;RELATED=START:-PT15M
UID:foreign-alarm-1
ACKNOWLEDGED:20260714T100000Z
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:date-valued trigger
TRIGGER;VALUE=DATE:20260719
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:repeated trigger
TRIGGER;RELATED=END:-PT30M
TRIGGER:-PT60M
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:no trigger at all
END:VALARM
END:VTODO
"""


def test_extract_alarms_skips_alarms_the_string_form_cannot_express():
    """Only alarms that would survive being written back unchanged are listed.

    A START-anchored alarm on a task that has a DUE would be re-anchored to
    DUE on write-back (`build_alarm` derives RELATED from the task's own
    dates), so reporting it as a bare "-PT15M" would be a lie about when it
    fires. Same for the trigger forms this server cannot write at all.
    """
    todo = _todo_from_ics(_FOREIGN_ALARMS_ICS)
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["-PT30M"]


_UNANCHORED_ALARM_ICS = """BEGIN:VTODO
UID:task-1
SUMMARY:Task
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:no RELATED parameter
TRIGGER:-PT30M
END:VALARM
END:VTODO
"""


def test_extract_alarms_lists_a_trigger_that_names_no_anchor():
    """RELATED is omissible, so the bare form is what foreign clients mostly write.

    Its RFC default is START, but this task has no DTSTART for that to mean
    anything - the due date is the only anchor there is, which is exactly what
    "-PT30M" says here. Hiding it would be a regression: the reminder would
    disappear from the API while still firing.
    """
    todo = _todo_from_ics(_UNANCHORED_ALARM_ICS)
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["-PT30M"]


def test_apply_task_fields_does_not_duplicate_a_trigger_that_names_no_anchor():
    """Writing the same reminder back must not add a second alarm beside it."""
    todo = _todo_from_ics(_UNANCHORED_ALARM_ICS)
    before = next(c for c in todo.subcomponents if c.name == "VALARM").to_ical()

    _apply(todo, erinnerungen=["-PT30M"])

    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    assert alarms[0].to_ical() == before


def test_extract_alarms_still_hides_an_anchor_the_task_really_has():
    """The both-dates case stays hidden: there DTSTART exists and means something else."""
    todo = _todo_from_ics(
        """BEGIN:VTODO
UID:task-1
SUMMARY:Task
DTSTART:20260715T090000Z
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:no RELATED parameter
TRIGGER:-PT30M
END:VALARM
END:VTODO
"""
    )
    assert mapping.parse_vtodo(todo)["erinnerungen"] == []


def test_apply_task_fields_preserves_alarms_it_cannot_express():
    """Writing `erinnerungen` must not delete what the reader could not show."""
    todo = _todo_from_ics(_FOREIGN_ALARMS_ICS)
    # The first VALARM in the fixture is the one `erinnerungen` can express;
    # every other one is a form this server has no string for.
    unexpressible = [c.to_ical() for c in todo.subcomponents if c.name == "VALARM"][1:]

    _apply(todo, erinnerungen=["-PT30M", "-P1D"])

    kept = [c.to_ical() for c in todo.subcomponents if c.name == "VALARM"]
    for alarm in unexpressible:
        assert alarm in kept
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["-PT30M", "-P1D"]


def test_apply_task_fields_leaves_an_already_present_reminder_untouched():
    """A reminder that is already there keeps its UID/ACKNOWLEDGED/ACTION.

    Rebuilding it as a fresh DISPLAY alarm would drop the dismiss state, so a
    reminder the user already clicked away could fire again.
    """
    todo = _todo_from_ics(
        """BEGIN:VTODO
UID:task-1
SUMMARY:Task
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:EMAIL
DESCRIPTION:dismissed already
TRIGGER;RELATED=END:-PT30M
UID:foreign-alarm-1
ACKNOWLEDGED:20260718T100000Z
X-MOZ-LASTACK:20260718T100000Z
END:VALARM
END:VTODO
"""
    )
    before = next(c for c in todo.subcomponents if c.name == "VALARM").to_ical()

    _apply(todo, titel="Renamed", erinnerungen=["-PT30M"])

    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    assert alarms[0].to_ical() == before


def test_apply_task_fields_matches_an_equivalent_duration_spelling():
    """A week and seven days are the same trigger, so the alarm is not rebuilt."""
    todo = _todo_from_ics(
        """BEGIN:VTODO
UID:task-1
SUMMARY:Task
DUE:20260720T100000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:week before
TRIGGER;RELATED=END:-P1W
UID:foreign-alarm-1
ACKNOWLEDGED:20260713T100000Z
END:VALARM
END:VTODO
"""
    )
    before = next(c for c in todo.subcomponents if c.name == "VALARM").to_ical()

    _apply(todo, erinnerungen=["-P7D"])

    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    assert alarms[0].to_ical() == before


def test_reminder_round_trip_leaves_the_component_unchanged():
    """Read `erinnerungen`, write the same list back: nothing moves."""
    todo = _todo_from_ics(_FOREIGN_ALARMS_ICS)
    before = todo.to_ical()

    _apply(todo, erinnerungen=mapping.parse_vtodo(todo)["erinnerungen"])

    assert todo.to_ical() == before


def test_relative_alarm_without_any_anchor_is_not_listed():
    """A bare duration on a task with neither DUE nor DTSTART cannot be written back.

    Listing it would produce a value that `update_task` rejects, so it is
    treated like any other alarm this server cannot express: hidden, but kept.
    """
    todo = _todo_from_ics(
        """BEGIN:VTODO
UID:task-1
SUMMARY:Task
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:floating
TRIGGER:-PT30M
END:VALARM
END:VTODO
"""
    )
    assert mapping.parse_vtodo(todo)["erinnerungen"] == []

    _apply(todo, titel="Renamed")

    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 1


def test_duplicate_reminder_specs_produce_one_alarm():
    todo = _new_todo()
    _apply(
        todo,
        titel="Task",
        faellig_datum="2026-07-20T10:00:00",
        erinnerungen=["-PT30M", "-PT30M", "-PT0H30M"],
    )
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["-PT30M"]


def test_updating_other_fields_leaves_alarms_alone():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20T10:00:00", erinnerungen=["-PT30M"])
    before = todo.to_ical()

    _apply(todo, ort="Zuhause")

    assert [c for c in todo.subcomponents if c.name == "VALARM"]
    assert todo.to_ical().replace(b"LOCATION:Zuhause\r\n", b"") == before


def test_parent_uid_set_and_extracted():
    todo = _new_todo()
    _apply(todo, titel="Subtask", uebergeordnete_aufgabe="parent-uid-42")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["uebergeordnete_uid"] == "parent-uid-42"


def test_mark_completed_sets_status_and_percent():
    todo = _new_todo()
    _apply(todo, titel="Task")
    mapping.mark_completed(todo)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "erledigt"
    assert parsed["fortschritt_prozent"] == 100
    assert "completed" in todo


# --- All-day dates (B1) ---


def test_date_only_input_produces_value_date_property():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20")
    due_prop = todo.get("due")
    assert isinstance(due_prop.dt, date)
    assert due_prop.params.get("VALUE") == "DATE"
    assert b"VALUE=DATE" in todo.to_ical()


def test_date_only_input_round_trips_to_date_string_not_midnight_datetime():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["faellig_datum"] == "2026-07-20"


def test_full_datetime_input_still_produces_datetime():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20T14:00:00+02:00")
    due_prop = todo.get("due")
    assert isinstance(due_prop.dt, datetime)
    assert due_prop.params.get("VALUE") != "DATE"


@pytest.mark.parametrize(
    "text",
    ["2026072", "26-07-20", "2026-7-20", "2026/07/20"],
)
def test_non_canonical_date_strings_are_not_treated_as_all_day(text):
    # These are not exactly "YYYY-MM-DD" so they must not silently become
    # all-day dates; they should either parse as something else or raise.
    try:
        result = mapping.parse_datetime_input(text)
    except InvalidTaskDataError:
        return
    assert type(result) is not date


# --- Naive datetimes are UTC (B2) ---


def test_naive_datetime_input_is_interpreted_as_utc():
    result = mapping.parse_datetime_input("2026-07-20T14:00:00")
    assert isinstance(result, datetime)  # narrows away the `date` half of the return type
    assert result.tzinfo == timezone.utc
    assert result.hour == 14


def test_named_timezone_input_resolves_dst_offset_for_the_date():
    """A named IANA zone resolves the correct offset per date (CEST vs. CET).

    A fixed numeric offset picked once (e.g. always "+02:00" for
    Europe/Berlin) is only correct for half the year - Germany observes CET
    (+01:00) outside daylight saving time. Passing the zone name instead of
    a manually chosen offset avoids that whole class of off-by-one-hour bug.
    """
    summer = mapping.parse_datetime_input("2026-07-20T14:00:00 Europe/Berlin")
    assert summer == datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)  # CEST = UTC+2

    winter = mapping.parse_datetime_input("2026-01-20T14:00:00 Europe/Berlin")
    assert winter == datetime(2026, 1, 20, 13, 0, tzinfo=timezone.utc)  # CET = UTC+1


def test_named_timezone_with_explicit_offset_is_rejected():
    with pytest.raises(InvalidTaskDataError):
        mapping.parse_datetime_input("2026-07-20T14:00:00+02:00 Europe/Berlin")


def test_unknown_timezone_name_falls_back_to_plain_datetime_error():
    with pytest.raises(InvalidTaskDataError):
        mapping.parse_datetime_input("2026-07-20T14:00:00 Mars/Olympus_Mons")


def test_offset_datetime_input_is_normalized_to_utc():
    """An explicit offset must survive as the same instant, but stored in UTC.

    `icalendar` serializes a fixed-offset tzinfo (e.g. "+02:00") as
    DTSTART;TZID="UTC+02:00":... without ever writing the matching
    VTIMEZONE component that TZID requires - CalDAV clients that don't
    recognize it fall back to their local zone, shifting the moment (and
    often the calendar day). Normalizing to UTC here means it's written
    with a plain "Z" suffix instead, which every client understands.
    """
    result = mapping.parse_datetime_input("2026-07-20T14:00:00+02:00")
    assert isinstance(result, datetime)  # narrows away the `date` half of the return type
    assert result.tzinfo == timezone.utc
    assert result == datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def test_naive_datetime_matches_absolute_trigger_semantics():
    """The same naive input must mean the same thing whether it's a DUE or a trigger."""
    due_result = mapping.parse_datetime_input("2026-07-19T09:00:00")
    trigger_result = mapping._parse_absolute_trigger("2026-07-19T09:00:00")
    assert due_result == trigger_result


def test_date_only_input_is_not_coerced_to_utc_datetime():
    result = mapping.parse_datetime_input("2026-07-20")
    assert type(result) is date


def test_invalid_input_raises():
    with pytest.raises(InvalidTaskDataError):
        mapping.parse_datetime_input("not-a-date")


# --- Field clearing (B3) ---


def test_clear_removes_due_date():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20")
    assert "due" in todo
    mapping.apply_task_fields(todo, TaskFields(clear=("faellig_datum",)))
    assert "due" not in todo


def test_clear_removes_start_datum():
    todo = _new_todo()
    _apply(todo, start_datum="2026-07-01")
    mapping.apply_task_fields(todo, TaskFields(clear=("start_datum",)))
    assert "dtstart" not in todo


def test_clear_removes_priority():
    todo = _new_todo()
    _apply(todo, prioritaet="hoch")
    mapping.apply_task_fields(todo, TaskFields(clear=("prioritaet",)))
    assert "priority" not in todo


def test_clear_removes_percent_complete():
    todo = _new_todo()
    _apply(todo, fortschritt_prozent=42)
    mapping.apply_task_fields(todo, TaskFields(clear=("fortschritt_prozent",)))
    assert "percent-complete" not in todo


def test_clear_removes_location():
    todo = _new_todo()
    _apply(todo, ort="Büro")
    mapping.apply_task_fields(todo, TaskFields(clear=("ort",)))
    assert "location" not in todo


def test_clear_removes_url():
    todo = _new_todo()
    _apply(todo, url="https://example.com")
    mapping.apply_task_fields(todo, TaskFields(clear=("url",)))
    assert "url" not in todo


def test_clear_removes_categories():
    todo = _new_todo()
    _apply(todo, tags=["a", "b"])
    mapping.apply_task_fields(todo, TaskFields(clear=("tags",)))
    assert "categories" not in todo


def test_clear_removes_description():
    todo = _new_todo()
    _apply(todo, notizen="Notiz")
    mapping.apply_task_fields(todo, TaskFields(clear=("notizen",)))
    assert "description" not in todo


def test_clear_removes_class():
    todo = _new_todo()
    _apply(todo, sichtbarkeit="privat")
    mapping.apply_task_fields(todo, TaskFields(clear=("sichtbarkeit",)))
    assert "class" not in todo


def test_clear_removes_related_to():
    todo = _new_todo()
    _apply(todo, uebergeordnete_aufgabe="parent-uid")
    mapping.apply_task_fields(todo, TaskFields(clear=("uebergeordnete_aufgabe",)))
    assert "related-to" not in todo


def test_clear_removes_all_alarms():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20", erinnerungen=["-P1D", "-PT1H"])
    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 2

    mapping.apply_task_fields(todo, TaskFields(clear=("erinnerungen",)))
    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 0


def test_clear_multiple_fields_at_once():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20", ort="Büro", prioritaet="hoch")
    mapping.apply_task_fields(todo, TaskFields(clear=("faellig_datum", "ort", "prioritaet")))
    assert "due" not in todo
    assert "location" not in todo
    assert "priority" not in todo


def test_clear_unknown_field_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError, match="Unknown"):
        mapping.apply_task_fields(todo, TaskFields(clear=("nonexistent_field",)))


def test_clear_titel_raises():
    """Clearing the title is not supported - "titel" isn't a valid clear name."""
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        mapping.apply_task_fields(todo, TaskFields(clear=("titel",)))


def test_clear_and_set_same_field_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        mapping.apply_task_fields(
            todo, TaskFields(faellig_datum="2026-07-20", clear=("faellig_datum",))
        )


def test_clear_does_not_affect_untouched_fields():
    todo = _new_todo()
    _apply(todo, faellig_datum="2026-07-20", ort="Büro")
    mapping.apply_task_fields(todo, TaskFields(clear=("faellig_datum",)))
    parsed = mapping.parse_vtodo(todo)
    assert parsed["ort"] == "Büro"


def test_clear_on_field_that_was_never_set_is_a_no_op():
    todo = _new_todo()
    _apply(todo, titel="Task")
    mapping.apply_task_fields(todo, TaskFields(clear=("ort",)))
    parsed = mapping.parse_vtodo(todo)
    assert parsed["titel"] == "Task"
    assert parsed["ort"] is None


# --- Remaining branch coverage (WP5, E1/E4 remainder) ---


@pytest.mark.parametrize("value", [10, 20, -1])
def test_ical_priority_to_label_out_of_range_is_none(value):
    # Real RFC 5545 PRIORITY is 0-9, but ical_priority_to_label doesn't assume
    # that - anything outside 1-9 (other than the falsy 0/None handled above)
    # is "undefined", not an error.
    assert mapping.ical_priority_to_label(value) is None


def test_date_shaped_but_invalid_date_falls_through_and_raises():
    # Matches the "YYYY-MM-DD" shape but isn't a real date (month 13) - both
    # date.fromisoformat and datetime.fromisoformat reject it, so parsing
    # must fall all the way through to the final InvalidTaskDataError.
    with pytest.raises(InvalidTaskDataError):
        mapping.parse_datetime_input("2026-13-40")


def test_absolute_trigger_with_explicit_offset_is_converted_to_utc():
    result = mapping._parse_absolute_trigger("2026-07-20T14:00:00+05:00")
    assert result == datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    assert result.tzinfo == timezone.utc


def test_extract_categories_handles_plain_string_entry():
    # A CATEGORIES value that isn't a vCategory-like object with a `.cats`
    # attribute (e.g. built/edited by another client) must still be read
    # back as a single plain string, not dropped or crash the parser.
    todo = _new_todo()
    todo["categories"] = "just-a-string"  # bypass icalendar's vCategory wrapping
    parsed = mapping.parse_vtodo(todo)
    assert parsed["tags"] == ["just-a-string"]


def test_extract_parent_uid_ignores_non_parent_reltype():
    todo = _new_todo()
    todo.add("related-to", "sibling-uid", parameters={"RELTYPE": "CHILD"})
    parsed = mapping.parse_vtodo(todo)
    assert parsed["uebergeordnete_uid"] is None


# --- Recurrence surfaced read-only (C5) ---


def test_parse_vtodo_surfaces_rrule_as_raw_text():
    todo = _new_todo()
    todo.add("rrule", {"FREQ": "WEEKLY", "BYDAY": ["MO"]})
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] == "FREQ=WEEKLY;BYDAY=MO"


def test_parse_vtodo_wiederholung_is_none_when_not_recurring():
    todo = _new_todo()
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] is None


# --- list_tasks filtering (C4) ---


def _task(uid: str, faellig_datum: str | None) -> dict:
    return {
        "uid": uid,
        "titel": uid,
        "start_datum": None,
        "faellig_datum": faellig_datum,
        "prioritaet": None,
        "fortschritt_prozent": 0,
        "status": "offen",
        "ort": None,
        "url": None,
        "tags": [],
        "notizen": None,
        "uebergeordnete_uid": None,
        "wiederholung": None,
    }


def test_filter_tasks_no_filters_returns_all_tasks_unchanged():
    tasks = [_task("a", "2026-07-01"), _task("b", None)]
    assert mapping.filter_tasks(tasks) == tasks


def test_filter_tasks_due_after_excludes_earlier_and_no_due_date():
    tasks = [
        _task("early", "2026-07-01"),
        _task("late", "2026-07-20"),
        _task("no-due", None),
    ]
    result = mapping.filter_tasks(tasks, due_after="2026-07-10")
    assert [t["uid"] for t in result] == ["late"]


def test_filter_tasks_due_before_excludes_later_and_no_due_date():
    tasks = [
        _task("early", "2026-07-01"),
        _task("late", "2026-07-20"),
        _task("no-due", None),
    ]
    result = mapping.filter_tasks(tasks, due_before="2026-07-10")
    assert [t["uid"] for t in result] == ["early"]


def test_filter_tasks_due_before_and_after_combined_is_a_range():
    tasks = [
        _task("too-early", "2026-07-01"),
        _task("in-range", "2026-07-10"),
        _task("too-late", "2026-07-20"),
    ]
    result = mapping.filter_tasks(tasks, due_after="2026-07-05", due_before="2026-07-15")
    assert [t["uid"] for t in result] == ["in-range"]


def test_filter_tasks_date_only_due_before_bound_includes_all_day_task_on_boundary():
    # An all-day task due exactly on the faellig_vor date must still be
    # included: the bound expands to the end of that day (23:59:59 UTC), and
    # the task's own all-day due date compares as its start-of-day instant.
    tasks = [_task("boundary", "2026-07-20")]
    result = mapping.filter_tasks(tasks, due_before="2026-07-20")
    assert [t["uid"] for t in result] == ["boundary"]


def test_filter_tasks_date_only_due_after_bound_includes_all_day_task_on_boundary():
    tasks = [_task("boundary", "2026-07-20")]
    result = mapping.filter_tasks(tasks, due_after="2026-07-20")
    assert [t["uid"] for t in result] == ["boundary"]


def test_filter_tasks_datetime_due_before_bound_excludes_all_day_task_next_day():
    # An all-day task due the day *after* a datetime faellig_vor bound must be
    # excluded, even though the bound's date matches - the bound is a precise
    # instant here, not expanded to end-of-day (only date-only bounds are).
    tasks = [_task("next-day", "2026-07-21")]
    result = mapping.filter_tasks(tasks, due_before="2026-07-20T12:00:00")
    assert result == []


def test_filter_tasks_mixed_date_and_datetime_due_values():
    tasks = [
        _task("all-day", "2026-07-10"),
        _task("timed", "2026-07-10T08:00:00+00:00"),
    ]
    result = mapping.filter_tasks(tasks, due_after="2026-07-01", due_before="2026-07-31")
    assert {t["uid"] for t in result} == {"all-day", "timed"}


def test_filter_tasks_limit_caps_result_count():
    tasks = [_task("a", None), _task("b", None), _task("c", None)]
    result = mapping.filter_tasks(tasks, limit=2)
    assert [t["uid"] for t in result] == ["a", "b"]


def test_filter_tasks_limit_applied_after_due_date_filter():
    tasks = [
        _task("a", "2026-07-01"),
        _task("b", "2026-07-05"),
        _task("c", "2026-07-10"),
        _task("excluded", None),
    ]
    result = mapping.filter_tasks(tasks, due_after="2026-07-01", limit=2)
    assert [t["uid"] for t in result] == ["a", "b"]


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_filter_tasks_non_positive_limit_raises(limit):
    with pytest.raises(InvalidTaskDataError):
        mapping.filter_tasks([], limit=limit)


def test_filter_tasks_invalid_due_bound_raises():
    with pytest.raises(InvalidTaskDataError):
        mapping.filter_tasks([], due_before="not-a-date")
