"""Unit tests for the field <-> iCalendar mapping logic, no CalDAV involved."""

from __future__ import annotations

import pytest
from icalendar import Todo

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.errors import InvalidTaskDataError


def _new_todo(uid: str = "task-1") -> Todo:
    todo = Todo()
    todo.add("uid", uid)
    return todo


def test_apply_and_parse_round_trip():
    todo = _new_todo()
    mapping.apply_task_fields(
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
    assert parsed["start_datum"] == "2026-07-01T00:00:00"
    assert parsed["fällig_datum"] == "2026-07-20T00:00:00"
    assert parsed["priorität"] == "hoch"
    assert parsed["fortschritt_prozent"] == 20
    assert parsed["status"] == "offen"
    assert parsed["ort"] == "Zuhause"
    assert parsed["url"] == "https://example.com/steuer"
    assert set(parsed["tags"]) == {"Finanzen", "Wichtig"}
    assert parsed["notizen"] == "Belege sammeln"
    assert parsed["übergeordnete_uid"] is None


def test_apply_task_fields_replaces_instead_of_appending():
    """Component.add() appends by default; applying twice must not duplicate values."""
    todo = _new_todo()
    mapping.apply_task_fields(todo, titel="Erster Titel", faellig_datum="2026-07-20")
    mapping.apply_task_fields(todo, titel="Zweiter Titel")

    parsed = mapping.parse_vtodo(todo)
    assert parsed["titel"] == "Zweiter Titel"
    assert parsed["fällig_datum"] == "2026-07-20T00:00:00"  # untouched field survives


def test_apply_task_fields_leaves_unset_fields_untouched():
    todo = _new_todo()
    mapping.apply_task_fields(todo, titel="Titel", ort="Büro")
    mapping.apply_task_fields(todo, notizen="Neue Notiz")

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
        mapping.apply_task_fields(todo, fortschritt_prozent=150)


def test_relative_reminder_relative_to_due():
    todo = _new_todo()
    mapping.apply_task_fields(
        todo, titel="Task", faellig_datum="2026-07-20T10:00:00", erinnerungen=["-P1D"]
    )
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("RELATED") == "END"


def test_relative_reminder_falls_back_to_start_when_no_due():
    todo = _new_todo()
    mapping.apply_task_fields(
        todo, titel="Task", start_datum="2026-07-15T09:00:00", erinnerungen=["-PT1H"]
    )
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("RELATED") == "START"


def test_relative_reminder_without_start_or_due_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        mapping.apply_task_fields(todo, titel="Task", erinnerungen=["-P1D"])


def test_absolute_reminder_sets_value_date_time():
    todo = _new_todo()
    mapping.apply_task_fields(
        todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["2026-07-19T09:00:00"]
    )
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    trigger = alarms[0]["trigger"]
    assert trigger.params.get("VALUE") == "DATE-TIME"


def test_invalid_reminder_spec_raises():
    todo = _new_todo()
    with pytest.raises(InvalidTaskDataError):
        mapping.apply_task_fields(
            todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["not-a-duration"]
        )


def test_updating_reminders_replaces_old_alarms():
    todo = _new_todo()
    mapping.apply_task_fields(
        todo, titel="Task", faellig_datum="2026-07-20", erinnerungen=["-P1D", "-PT1H"]
    )
    assert len([c for c in todo.subcomponents if c.name == "VALARM"]) == 2

    mapping.apply_task_fields(todo, erinnerungen=["-P2D"])
    alarms = [c for c in todo.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1


def test_parent_uid_set_and_extracted():
    todo = _new_todo()
    mapping.apply_task_fields(todo, titel="Subtask", uebergeordnete_aufgabe="parent-uid-42")
    parsed = mapping.parse_vtodo(todo)
    assert parsed["übergeordnete_uid"] == "parent-uid-42"


def test_mark_completed_sets_status_and_percent():
    todo = _new_todo()
    mapping.apply_task_fields(todo, titel="Task")
    mapping.mark_completed(todo)
    parsed = mapping.parse_vtodo(todo)
    assert parsed["status"] == "erledigt"
    assert parsed["fortschritt_prozent"] == 100
    assert "completed" in todo
