"""Unit tests for the field <-> iCalendar mapping logic, no CalDAV involved."""

from __future__ import annotations

from datetime import date, datetime, timezone

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


def test_offset_datetime_input_keeps_its_offset():
    result = mapping.parse_datetime_input("2026-07-20T14:00:00+02:00")
    assert isinstance(result, datetime)  # narrows away the `date` half of the return type
    offset = result.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 2 * 3600


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
