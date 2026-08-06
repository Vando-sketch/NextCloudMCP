"""Unit tests for the field <-> iCalendar mapping logic, no CalDAV involved."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from icalendar import Todo

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.errors import InvalidTaskDataError
from nextcloud_task_mcp.mapping import TaskFields


def _new_todo(uid: str = "task-1") -> Todo:
    todo = Todo()
    todo.add("uid", uid)
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
    # Note: absolute reminders are formatted in default timezone (Europe/Berlin, +02:00 in August)
    assert parsed["erinnerungen"] == [
        "2026-08-07T11:00:00+02:00",
        "2026-08-07T11:00:00+02:00",
    ]


def test_extract_alarms_preserves_order():
    todo = _new_todo()
    reminders = ["-P1D", "-PT30M", "2026-08-07T11:00:00+02:00"]
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=reminders)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == reminders


def test_extract_alarms_no_valarm_returns_empty():
    todo = _new_todo()
    _apply(todo, titel="Task")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == []


def test_extract_alarms_skips_valarm_without_trigger_or_invalid_trigger():
    from icalendar import Alarm

    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["-PT30M"])

    alarm_without_trigger = Alarm()
    alarm_without_trigger.add("action", "DISPLAY")
    todo.add_component(alarm_without_trigger)

    alarm_with_invalid_trigger = Alarm()
    alarm_with_invalid_trigger.add("action", "DISPLAY")
    alarm_with_invalid_trigger["trigger"] = "unsupported-trigger"
    todo.add_component(alarm_with_invalid_trigger)

    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["-PT30M"]


def test_extract_alarms_absolute_with_offset_formats_in_default_timezone():
    todo = _new_todo()
    _apply(
        todo,
        titel="Task",
        faellig_datum="2026-07-20",
        erinnerungen=["2026-07-19T11:00:00+02:00"],
    )
    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


def test_extract_alarms_resolves_foreign_tzid_trigger():
    """A TRIGGER;TZID=... written by another client is resolved and formatted in default zone."""
    from icalendar import Alarm

    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20")

    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", "Reminder")
    alarm.add("trigger", datetime(2026, 7, 19, 11, 0, 0), parameters={"TZID": "Europe/Berlin"})
    todo.add_component(alarm)

    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


def test_extract_alarms_unknown_tzid_falls_back_to_utc():
    from icalendar import Alarm

    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20")

    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", "Reminder")
    alarm.add("trigger", datetime(2026, 7, 19, 11, 0, 0), parameters={"TZID": "Mars/Olympus"})
    todo.add_component(alarm)

    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["2026-07-19T13:00:00+02:00"]


def test_extract_alarms_skips_date_valued_and_repeated_triggers():
    """Neither wire form is one this server writes, but foreign clients do."""
    from icalendar import Alarm

    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["-PT30M"])

    date_trigger = Alarm()
    date_trigger.add("action", "DISPLAY")
    date_trigger.add("trigger", date(2026, 7, 19))
    todo.add_component(date_trigger)

    repeated_trigger = Alarm()
    repeated_trigger.add("action", "DISPLAY")
    repeated_trigger.add("trigger", timedelta(minutes=-30))
    repeated_trigger.add("trigger", timedelta(minutes=-60))
    todo.add_component(repeated_trigger)

    parsed = mapping.parse_vtodo(todo)
    assert parsed["erinnerungen"] == ["-PT30M"]


def test_extract_alarms_does_not_preserve_related_anchor():
    """Documented, deliberate fidelity loss: `erinnerungen` has no RELATED slot.

    A foreign alarm anchored to DTSTART on a task that also has a DUE reads
    back as a bare duration, and writing it back re-anchors it to DUE (see
    `build_alarm`). Pinned here so the limitation can't drift unnoticed.
    """
    todo = _new_todo()
    _apply(
        todo, titel="Task", start_datum="2026-07-15T09:00:00", faellig_datum="2026-07-20T10:00:00"
    )
    todo.add_component(mapping.build_alarm("-PT30M", "Task", has_due=False, has_start=True))
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["-PT30M"]

    rewritten = _new_todo()
    _apply(
        rewritten,
        titel="Task",
        start_datum="2026-07-15T09:00:00",
        faellig_datum="2026-07-20T10:00:00",
        erinnerungen=["-PT30M"],
    )
    alarm = next(c for c in rewritten.subcomponents if c.name == "VALARM")
    assert alarm.get("trigger").params["RELATED"] == "END"


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


# --- Task status (update_task's status parameter) ---


@pytest.mark.parametrize(
    ("label", "ical_value"),
    [
        ("offen", "NEEDS-ACTION"),
        ("in-arbeit", "IN-PROCESS"),
        ("erledigt", "COMPLETED"),
        ("abgesagt", "CANCELLED"),
    ],
)
def test_task_status_label_to_ical(label, ical_value):
    assert mapping.task_status_label_to_ical(label) == ical_value


def test_task_status_label_to_ical_unknown_raises():
    with pytest.raises(InvalidTaskDataError, match="Unknown status"):
        mapping.task_status_label_to_ical("fertig")


@pytest.mark.parametrize(
    "label",
    ["offen", "in-arbeit", "erledigt", "abgesagt"],
)
def test_apply_task_fields_status_round_trips(label):
    """Every status label round-trips through apply_task_fields/parse_vtodo."""
    todo = _new_todo()
    _apply(todo, titel="Task", status=label)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == label


def test_apply_task_fields_status_erledigt_sets_percent_and_completed():
    todo = _new_todo()
    _apply(todo, titel="Task", status="erledigt")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "erledigt"
    assert parsed["fortschritt_prozent"] == 100
    assert "completed" in todo


def test_apply_task_fields_status_offen_reopens_completed_task():
    """status="offen" is the reopen path: removes COMPLETED, resets percent to 0."""
    todo = _new_todo()
    _apply(todo, titel="Task", status="erledigt")
    assert "completed" in todo

    _apply(todo, status="offen")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "offen"
    assert parsed["fortschritt_prozent"] == 0
    assert "completed" not in todo


@pytest.mark.parametrize("label", ["in-arbeit", "abgesagt"])
def test_apply_task_fields_status_in_arbeit_and_abgesagt_keep_progress(label):
    """in-arbeit/abgesagt keep whatever progress was recorded."""
    todo = _new_todo()
    _apply(todo, titel="Task", fortschritt_prozent=42)
    _apply(todo, status=label)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == label
    assert parsed["fortschritt_prozent"] == 42
    assert "completed" not in todo


@pytest.mark.parametrize("label", ["in-arbeit", "abgesagt", "offen"])
def test_apply_task_fields_leaving_erledigt_drops_completed_timestamp(label):
    """No non-completed status may keep a COMPLETED timestamp around.

    caldav's pending filter (`todos(include_completed=False)`, what nur_offene
    and get_agenda run on) drops any VTODO that merely *has* a COMPLETED
    property, whatever its STATUS says - so a task moved off "erledigt" while
    keeping the timestamp would report its new status and still be invisible
    in every open-task listing.
    """
    todo = _new_todo()
    _apply(todo, titel="Task", status="erledigt")
    assert "completed" in todo

    _apply(todo, status=label)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == label
    assert "completed" not in todo


def test_apply_task_fields_explicit_fortschritt_prozent_wins_over_status():
    """status="erledigt" would derive 100%, but an explicit fortschritt_prozent
    in the same call must win (write-ordering requirement)."""
    todo = _new_todo()
    _apply(todo, titel="Task", status="erledigt", fortschritt_prozent=55)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "erledigt"
    assert parsed["fortschritt_prozent"] == 55

    # And the reverse: status="offen" would derive 0%, explicit wins here too.
    _apply(todo, status="offen", fortschritt_prozent=10)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "offen"
    assert parsed["fortschritt_prozent"] == 10


def test_apply_task_fields_unknown_status_raises_and_does_not_write():
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20")
    with pytest.raises(InvalidTaskDataError, match="Unknown status"):
        _apply(todo, status="fertig")
    # Nothing about the task changed - not even STATUS was written.
    assert "status" not in todo
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "offen"
    assert parsed["faellig_datum"] == "2026-07-20"


@pytest.mark.parametrize(
    ("raw_status", "label"),
    [
        ("NEEDS-ACTION", "offen"),
        ("IN-PROCESS", "in-arbeit"),
        ("COMPLETED", "erledigt"),
        ("CANCELLED", "abgesagt"),
        ("SOME-OTHER-STATUS", "offen"),
    ],
)
def test_parse_vtodo_status_values(raw_status, label):
    todo = _new_todo()
    todo.add("summary", "Task")
    todo.add("status", raw_status)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == label


def test_parse_vtodo_missing_status_reads_as_offen():
    todo = _new_todo()
    todo.add("summary", "Task")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "offen"


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


def test_naive_datetime_input_is_interpreted_in_default_timezone():
    # July date (summer): Europe/Berlin is +02:00 -> UTC 12:00
    summer = mapping.parse_datetime_input("2026-07-20T14:00:00")
    assert isinstance(summer, datetime)
    assert summer.tzinfo == timezone.utc
    assert summer == datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    # January date (winter): Europe/Berlin is +01:00 -> UTC 13:00
    winter = mapping.parse_datetime_input("2026-01-20T14:00:00")
    assert isinstance(winter, datetime)
    assert winter.tzinfo == timezone.utc
    assert winter == datetime(2026, 1, 20, 13, 0, tzinfo=timezone.utc)

    # keep_zone=True attaches default ZoneInfo
    summer_keep = mapping.parse_datetime_input("2026-07-20T14:00:00", keep_zone=True)
    assert isinstance(summer_keep, datetime)
    assert summer_keep.tzinfo == ZoneInfo("Europe/Berlin")
    assert summer_keep == datetime(2026, 7, 20, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def test_set_default_timezone_utc_reproduces_old_expectations():
    mapping.set_default_timezone("UTC")
    result = mapping.parse_datetime_input("2026-07-20T14:00:00")
    assert isinstance(result, datetime)
    assert result.tzinfo == timezone.utc
    assert result.hour == 14

    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20T14:00:00")
    res = mapping.parse_vtodo(todo)
    assert res["faellig_datum"] == "2026-07-20T14:00:00+00:00"


def test_naive_datetime_round_trip():
    """Write a naive datetime, read it back: same wall-clock time with local offset."""
    todo = _new_todo()
    _apply(todo, titel="Roundtrip", faellig_datum="2026-07-20T14:00:00")
    res = mapping.parse_vtodo(todo)
    assert res["faellig_datum"] == "2026-07-20T14:00:00+02:00"


def test_naive_datetime_round_trip_in_winter():
    """The output offset is resolved per date, not frozen at the summer one."""
    todo = _new_todo()
    _apply(todo, titel="Roundtrip", faellig_datum="2026-01-20T14:00:00")
    res = mapping.parse_vtodo(todo)
    assert res["faellig_datum"] == "2026-01-20T14:00:00+01:00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (date(2026, 7, 20), "2026-07-20"),
        (datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc), "2026-07-20T14:00:00+02:00"),
        (datetime(2026, 1, 20, 13, 0, tzinfo=timezone.utc), "2026-01-20T14:00:00+01:00"),
        # A floating (naive) value from a foreign client is already local.
        (datetime(2026, 7, 20, 14, 0), "2026-07-20T14:00:00+02:00"),
        # An explicit foreign zone is converted, not passed through.
        (
            datetime(2026, 7, 20, 14, 0, tzinfo=ZoneInfo("America/New_York")),
            "2026-07-20T20:00:00+02:00",
        ),
    ],
)
def test_format_datetime_output(value, expected):
    assert mapping.format_datetime_output(value) == expected


def test_due_filter_bounds_follow_dst_transition():
    """The spring-forward day is 23 hours long, and the bounds must know it.

    Local midnight on 2026-03-29 is 23:00 UTC the previous day (CET, +01:00);
    local end-of-day is already CEST (+02:00). A frozen offset would place one
    of the two bounds an hour off and silently drop or add tasks.
    """
    start = mapping._to_comparable_datetime("2026-03-29", end_of_day=False)
    end = mapping._to_comparable_datetime("2026-03-29", end_of_day=True)
    assert start.utcoffset() == timedelta(hours=1)
    assert end.utcoffset() == timedelta(hours=2)
    # Compared as instants (subtracting two datetimes sharing one ZoneInfo
    # would compare wall clocks and hide the missing hour).
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert elapsed == timedelta(hours=22, minutes=59, seconds=59)


def test_local_times_inside_a_dst_gap_or_overlap_resolve_by_fold():
    """Wall-clock times that don't exist, or exist twice, still resolve.

    On 2026-03-29 Europe/Berlin jumps 02:00 -> 03:00, so "02:30" never
    happens; on 2026-10-25 it falls back 03:00 -> 02:00, so "02:30" happens
    twice. `zoneinfo` resolves both with the pre-transition offset
    (`fold=0`) rather than raising - pinned here because "the server refuses
    a timestamp twice a year" would be a nasty surprise in production.
    """
    nonexistent = mapping.parse_datetime_input("2026-03-29T02:30:00")
    assert isinstance(nonexistent, datetime)
    assert nonexistent == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)

    ambiguous = mapping.parse_datetime_input("2026-10-25T02:30:00")
    assert isinstance(ambiguous, datetime)
    assert ambiguous == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_absolute_reminder_is_written_to_the_wire_in_utc():
    """RFC 5545 demands a UTC absolute TRIGGER - only the *display* is local."""
    todo = _new_todo()
    _apply(todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["2026-07-19T11:00:00"])
    ical_text = todo.to_ical().decode()
    assert "TRIGGER;VALUE=DATE-TIME:20260719T090000Z" in ical_text
    assert mapping.parse_vtodo(todo)["erinnerungen"] == ["2026-07-19T11:00:00+02:00"]


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
    result = mapping.parse_datetime_input("2026-07-20T14:00:00+02:00")
    assert isinstance(result, datetime)
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


# --- Recurrence (RRULE), now writable ---


def test_parse_vtodo_surfaces_rrule_as_raw_text():
    todo = _new_todo()
    todo.add("rrule", {"FREQ": "WEEKLY", "BYDAY": ["MO"]})
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] == "FREQ=WEEKLY;BYDAY=MO"


def test_parse_vtodo_wiederholung_is_none_when_not_recurring():
    todo = _new_todo()
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] is None


def test_wiederholung_round_trip():
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="FREQ=WEEKLY;BYDAY=MO")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] == "FREQ=WEEKLY;BYDAY=MO"


def test_wiederholung_can_be_changed():
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="FREQ=DAILY")
    _apply(todo, wiederholung="FREQ=WEEKLY;BYDAY=TU")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] == "FREQ=WEEKLY;BYDAY=TU"


def test_clear_removes_wiederholung():
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="FREQ=DAILY")
    mapping.apply_task_fields(todo, TaskFields(clear=("wiederholung",)))
    assert mapping.parse_vtodo(todo)["wiederholung"] is None


def test_invalid_wiederholung_rejected():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError, match="RRULE"):
        _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="kaputt")


def test_wiederholung_empty_result_is_invalid():
    # vRecur.from_ical silently skips parts without '=' instead of raising, so
    # completely unparseable input yields an empty rule - must still be
    # treated as invalid, not silently written as a no-op RRULE.
    with pytest.raises(InvalidTaskDataError, match="RRULE"):
        mapping.parse_rrule_text("not-a-valid-rrule")


def test_wiederholung_without_start_or_due_rejected():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError, match="start_datum|faellig_datum"):
        _apply(todo, titel="T", wiederholung="FREQ=DAILY")


def test_wiederholung_succeeds_when_anchor_already_exists_on_the_task():
    """The anchor check runs against the component's final state (mirrors
    event_mapping._check_start_end_consistency), not just this call's fields -
    so a call that sets only wiederholung succeeds when faellig_datum was
    already set on the task by an earlier call."""
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20")
    _apply(todo, wiederholung="FREQ=WEEKLY")
    assert mapping.parse_vtodo(todo)["wiederholung"] == "FREQ=WEEKLY"


def test_wiederholung_rejected_when_call_clears_the_only_anchor():
    """Setting wiederholung while clearing the task's only anchor in the same
    call must still be rejected - the final-state check catches this too."""
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20")
    with pytest.raises(InvalidTaskDataError, match="start_datum|faellig_datum"):
        mapping.apply_task_fields(
            todo, TaskFields(wiederholung="FREQ=WEEKLY", clear=("faellig_datum",))
        )


def test_clearing_the_only_anchor_is_rejected_for_an_already_recurring_task():
    """The RRULE need not be part of the call to be orphaned by it.

    Clearing a recurring task's only anchor leaves an RRULE nothing can be
    resolved against, whether the recurrence arrived in this call or an
    earlier one - the check looks at the component, never at the fields.
    Narrowing it to "only validate when wiederholung is being set" would slip
    past this and silently produce an unresolvable series.
    """
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="FREQ=WEEKLY")
    with pytest.raises(InvalidTaskDataError, match="start_datum|faellig_datum"):
        mapping.apply_task_fields(todo, TaskFields(clear=("faellig_datum",)))


def test_wiederholung_is_normalized_to_canonical_rrule_form():
    """Non-canonical input reads back canonical, not verbatim.

    `icalendar`'s vRecur uppercases part names and emits them in its own
    order, so a caller that sends lowercase or reordered parts gets an
    equivalent - not identical - string back. Pinned so the round-trip
    guarantee is understood as semantic, not byte-for-byte.
    """
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="byday=mo;freq=weekly")
    assert mapping.parse_vtodo(todo)["wiederholung"] == "FREQ=WEEKLY;BYDAY=MO"


def test_mark_completed_leaves_wiederholung_intact():
    """Pins this server's own observed behaviour: complete_task only sets
    STATUS/COMPLETED/PERCENT-COMPLETE, it does not touch RRULE or roll the
    series forward to a next occurrence."""
    todo = _new_todo()
    _apply(todo, titel="T", faellig_datum="2026-07-20", wiederholung="FREQ=DAILY")
    mapping.mark_completed(todo)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["wiederholung"] == "FREQ=DAILY"
    assert parsed["status"] == "erledigt"


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
    # included: the bound expands to the end of that day (23:59:59 in the
    # server's default timezone), and the task's own all-day due date
    # compares as its start-of-day instant.
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


def test_filter_tasks_prioritaet():
    tasks = [
        dict(_task("1", "2026-07-01"), prioritaet="hoch"),
        dict(_task("2", "2026-07-01"), prioritaet="mittel"),
        dict(_task("3", "2026-07-01"), prioritaet="niedrig"),
    ]
    res = mapping.filter_tasks(tasks, prioritaet="hoch")
    assert [t["uid"] for t in res] == ["1"]


def test_filter_tasks_unknown_prioritaet_raises():
    with pytest.raises(InvalidTaskDataError, match="Unknown prioritaet 'dringend'"):
        mapping.filter_tasks([], prioritaet="dringend")


def test_filter_tasks_tag():
    tasks = [
        dict(_task("1", "2026-07-01"), tags=["Finanzen", "Wichtig"]),
        dict(_task("2", "2026-07-01"), tags=["Arbeit"]),
    ]
    res = mapping.filter_tasks(tasks, tag="finanzen")
    assert [t["uid"] for t in res] == ["1"]


def test_filter_tasks_suchtext():
    tasks = [
        dict(_task("1", "2026-07-01"), titel="Milch kaufen", notizen=None),
        dict(_task("2", "2026-07-01"), titel="Einkauf", notizen="Vollmilch besorgen"),
        dict(_task("3", "2026-07-01"), titel="Post", notizen="Brief senden"),
    ]
    res = mapping.filter_tasks(tasks, suchtext="milch")
    assert [t["uid"] for t in res] == ["2", "1"]


def test_filter_tasks_sorting_due_date_and_no_due_date_and_titel():
    tasks = [
        dict(_task("z-due-later", "2026-08-10"), titel="Z Task"),
        dict(_task("a-due-earlier", "2026-08-01"), titel="A Task"),
        dict(_task("b-no-due", None), titel="B Task"),
        dict(_task("a-no-due", None), titel="A Task"),
    ]
    res = mapping.filter_tasks(tasks)
    assert [t["uid"] for t in res] == ["a-due-earlier", "z-due-later", "a-no-due", "b-no-due"]


def test_filter_tasks_combination():
    tasks = [
        dict(
            _task("1", "2026-07-10"),
            prioritaet="hoch",
            tags=["work"],
            titel="Bericht schreiben",
            notizen="Wichtig",
        ),
        dict(
            _task("2", "2026-07-05"),
            prioritaet="hoch",
            tags=["work"],
            titel="Bericht abgeben",
            notizen="Dringend",
        ),
        dict(
            _task("3", "2026-07-01"),
            prioritaet="niedrig",
            tags=["work"],
            titel="Bericht lesen",
            notizen="Fine",
        ),
        dict(
            _task("4", "2026-07-02"),
            prioritaet="hoch",
            tags=["home"],
            titel="Bericht zuhause",
            notizen=None,
        ),
    ]
    res = mapping.filter_tasks(
        tasks,
        prioritaet="hoch",
        tag="WORK",
        suchtext="bericht",
        due_before="2026-07-15",
        limit=1,
    )
    assert [t["uid"] for t in res] == ["2"]
