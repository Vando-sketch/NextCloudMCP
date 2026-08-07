"""Unit tests for the event field <-> iCalendar VEVENT mapping logic, no CalDAV involved."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event, FreeBusy
from icalendar.prop import vDDDTypes

from nextcloud_task_mcp import event_mapping, mapping
from nextcloud_task_mcp.errors import InvalidEventDataError
from nextcloud_task_mcp.event_mapping import EventFields


def _new_event(uid: str = "event-1") -> Event:
    event = Event()
    event.add("uid", uid)
    return event


def _event_from_ics(body: str) -> Event:
    """Parse a VEVENT out of real ICS text.

    Foreign-client alarms are only ever seen through the parser, so the tests
    for them build the component the same way instead of hand-assembling
    `icalendar` objects into states parsing can never produce.
    """
    calendar = Calendar.from_ical(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n" + body + "END:VCALENDAR\n"
    )
    event = calendar.walk("VEVENT")[0]
    assert isinstance(event, Event)
    return event


def _dt(prop: object) -> object:
    """Narrow an icalendar property (typed as a wide union) to its date/time payload."""
    assert isinstance(prop, vDDDTypes)
    return prop.dt


def _apply(event, own_organizer=None, **kwargs) -> None:
    """Convenience wrapper: build an EventFields from kwargs and apply it."""
    event_mapping.apply_event_fields(event, EventFields(**kwargs), own_organizer=own_organizer)


def test_apply_and_parse_round_trip():
    event = _new_event()
    _apply(
        event,
        titel="Team-Meeting",
        start="2026-07-20T14:00:00",
        ende="2026-07-20T15:30:00",
        ort="Konferenzraum",
        beschreibung="Sprint-Planung",
        tags=["Arbeit", "Wichtig"],
        status="bestätigt",
        sichtbarkeit="privat",
        url="https://example.com/meeting",
    )
    parsed = event_mapping.parse_vevent(event)

    assert parsed["uid"] == "event-1"
    assert parsed["titel"] == "Team-Meeting"
    # Naive datetimes are interpreted in default timezone (Europe/Berlin, +02:00 in July).
    assert parsed["start"] == "2026-07-20T14:00:00+02:00"
    assert parsed["ende"] == "2026-07-20T15:30:00+02:00"
    assert parsed["ganztaegig"] is False
    assert parsed["ort"] == "Konferenzraum"
    assert parsed["beschreibung"] == "Sprint-Planung"
    assert set(parsed["tags"]) == {"Arbeit", "Wichtig"}
    assert parsed["erinnerungen"] == []
    assert parsed["status"] == "bestätigt"
    assert parsed["sichtbarkeit"] == "privat"
    assert parsed["url"] == "https://example.com/meeting"
    assert parsed["wiederholung"] is None
    assert parsed["ausnahme_daten"] == []
    assert parsed["verknuepfte_aufgaben"] == []


def test_explicit_offset_start_is_stored_and_round_trips_in_utc():
    """Regression test for an explicit UTC-offset start losing its instant."""
    event = _new_event()
    _apply(event, start="2026-07-30T07:50:00+02:00")

    ical_bytes = event.to_ical()
    assert b"TZID" not in ical_bytes
    assert b"DTSTART:20260730T055000Z" in ical_bytes

    parsed = event_mapping.parse_vevent(event)
    assert parsed["start"] == "2026-07-30T07:50:00+02:00"
    assert parsed["wiederholung_von"] is None


def test_named_timezone_start_keeps_iana_zone_instead_of_utc():
    """Regression test for recurring events drifting by an hour across DST."""
    event = _new_event()
    _apply(
        event,
        start="2026-07-20T09:00:00 Europe/Berlin",
        ende="2026-07-20T10:00:00 Europe/Berlin",
        wiederholung="FREQ=DAILY",
    )

    dtstart = _dt(event.get("dtstart"))
    assert dtstart == datetime(2026, 7, 20, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert isinstance(dtstart, datetime)
    assert isinstance(dtstart.tzinfo, ZoneInfo)

    ical_bytes = event.to_ical()
    assert b"DTSTART;TZID=Europe/Berlin:20260720T090000" in ical_bytes

    parsed = event_mapping.parse_vevent(event)
    assert parsed["start"] == "2026-07-20T09:00:00+02:00"  # CEST


def test_get_event_update_event_round_trip_keeps_the_zone_anchor():
    """Reading an event and writing it straight back must not un-anchor it.

    `parse_vevent` renders a TZID-anchored DTSTART with a numeric offset - the
    one spelling that used to collapse back to a fixed UTC instant on the way
    in. A recurring event that makes that trip once keeps its old summer
    offset all winter, which is exactly the drift this zone handling exists to
    prevent.
    """
    event = _new_event()
    _apply(
        event,
        titel="Standup",
        start="2026-07-20T09:00:00 Europe/Berlin",
        ende="2026-07-20T09:15:00 Europe/Berlin",
        wiederholung="FREQ=WEEKLY",
    )
    parsed = event_mapping.parse_vevent(event)

    _apply(event, start=parsed["start"], ende=parsed["ende"])

    dtstart = _dt(event.get("dtstart"))
    dtend = _dt(event.get("dtend"))
    assert isinstance(dtstart, datetime) and isinstance(dtend, datetime)
    assert dtstart.tzinfo == ZoneInfo("Europe/Berlin")
    assert dtend.tzinfo == ZoneInfo("Europe/Berlin")
    ical_text = event.to_ical().decode()
    assert "DTSTART;TZID=Europe/Berlin:20260720T090000" in ical_text
    assert "DTEND;TZID=Europe/Berlin:20260720T091500" in ical_text
    assert event_mapping.parse_vevent(event) == parsed


def test_updating_only_the_end_keeps_the_events_own_zone():
    """Half an update must not leave the two ends anchored differently.

    With DTSTART on a TZID and DTEND collapsed to UTC, the two drift apart at
    the next transition - the event silently gets an hour longer or shorter -
    and nothing in the write path notices, because both are still the same
    instant apart *today*.
    """
    event = _new_event()
    _apply(
        event,
        titel="Standup",
        start="2026-07-20T09:00:00 Europe/Berlin",
        ende="2026-07-20T10:00:00 Europe/Berlin",
        wiederholung="FREQ=WEEKLY",
    )

    _apply(event, ende="2026-07-20T10:30:00+02:00")

    dtend = _dt(event.get("dtend"))
    assert isinstance(dtend, datetime)
    assert dtend.tzinfo == ZoneInfo("Europe/Berlin")
    assert "DTEND;TZID=Europe/Berlin:20260720T103000" in event.to_ical().decode()


def test_naive_input_still_means_the_servers_default_zone_not_the_events():
    """The anchoring is about *spelling*, not about reinterpreting input.

    A naive value is documented to mean the server's default timezone
    everywhere; on an event anchored in a foreign zone it therefore keeps that
    meaning (and its own zone), rather than being silently re-read as the
    event's local wall clock.
    """
    event = _new_event()
    _apply(event, titel="Call", start="2026-07-20T09:00:00 Asia/Tokyo")

    _apply(event, ende="2026-07-20T09:00:00")  # 09:00 Berlin = 16:00 Tokyo

    dtend = _dt(event.get("dtend"))
    assert isinstance(dtend, datetime)
    assert dtend.tzinfo == ZoneInfo("Europe/Berlin")
    assert dtend.astimezone(timezone.utc) == datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)


def test_z_suffix_datetime_input_formatted_in_default_timezone():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00Z", ende="2026-07-20T15:00:00Z")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["start"] == "2026-07-20T16:00:00+02:00"
    assert parsed["ende"] == "2026-07-20T17:00:00+02:00"


# --- all-day handling: `ende` is the inclusive last day, DTEND is exclusive ---


def test_all_day_single_day_round_trip():
    event = _new_event()
    _apply(event, titel="Feiertag", start="2026-08-01", ende="2026-08-01")
    assert _dt(event["dtend"]) == date(2026, 8, 2)  # stored exclusive
    parsed = event_mapping.parse_vevent(event)
    assert parsed["start"] == "2026-08-01"
    assert parsed["ende"] == "2026-08-01"  # returned inclusive
    assert parsed["ganztaegig"] is True


def test_all_day_multi_day_round_trip():
    event = _new_event()
    _apply(event, titel="Urlaub", start="2026-08-01", ende="2026-08-03")
    assert _dt(event["dtend"]) == date(2026, 8, 4)
    parsed = event_mapping.parse_vevent(event)
    assert parsed["ende"] == "2026-08-03"


def test_mixed_date_and_datetime_rejected():
    event = _new_event()
    with pytest.raises(InvalidEventDataError, match="both"):
        _apply(event, titel="T", start="2026-08-01", ende="2026-08-01T10:00:00")


def test_update_only_ende_checked_against_existing_start():
    """Consistency is validated against the final component state, not the call args."""
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00")
    with pytest.raises(InvalidEventDataError, match="both"):
        _apply(event, ende="2026-07-21")  # date-only end onto a datetime start


def test_ende_before_start_rejected():
    event = _new_event()
    with pytest.raises(InvalidEventDataError, match="before"):
        _apply(event, titel="T", start="2026-07-20T14:00:00", ende="2026-07-20T13:00:00")


def test_all_day_ende_before_start_rejected():
    event = _new_event()
    with pytest.raises(InvalidEventDataError, match="before"):
        _apply(event, titel="T", start="2026-08-03", ende="2026-08-01")


# --- recurrence (RRULE) and exceptions (EXDATE) ---


def test_rrule_round_trip():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", wiederholung="FREQ=WEEKLY;BYDAY=MO")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["wiederholung"] == "FREQ=WEEKLY;BYDAY=MO"


def test_invalid_rrule_rejected():
    event = _new_event()
    with pytest.raises(InvalidEventDataError, match="RRULE"):
        _apply(event, titel="T", start="2026-07-20T14:00:00", wiederholung="kaputt")


def test_exdate_set_parse_and_clear():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        wiederholung="FREQ=WEEKLY",
        ausnahme_daten=["2026-07-27T14:00:00", "2026-08-03T14:00:00"],
    )
    parsed = event_mapping.parse_vevent(event)
    assert parsed["ausnahme_daten"] == [
        "2026-07-27T14:00:00+02:00",
        "2026-08-03T14:00:00+02:00",
    ]

    _apply(event, clear=("ausnahme_daten",))
    assert event_mapping.parse_vevent(event)["ausnahme_daten"] == []


def test_exdate_replaces_instead_of_appending():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", ausnahme_daten=["2026-07-27T14:00:00"])
    _apply(event, ausnahme_daten=["2026-08-03T14:00:00"])
    assert event_mapping.parse_vevent(event)["ausnahme_daten"] == ["2026-08-03T14:00:00+02:00"]


def test_exdate_is_anchored_to_the_zone_of_its_own_dtstart():
    """An exception date must be spelled in the zone the instances live in.

    `get_event` returns an occurrence as `"...+02:00"`, so cancelling one means
    passing that string back. Stored as UTC (`...Z`) next to a
    `DTSTART;TZID=Europe/Berlin`, the EXDATE no longer looks like any value of
    the recurrence set - RFC 5545 3.8.5.1 wants it to match DTSTART's own
    form - and nothing reports that the instance stayed.
    """
    event = _new_event()
    _apply(
        event,
        titel="Standup",
        start="2026-07-20T09:00:00 Europe/Berlin",
        wiederholung="FREQ=WEEKLY",
        ausnahme_daten=["2026-07-27T09:00:00+02:00"],
    )

    exdate_line = [
        line for line in event.to_ical().decode().split("\r\n") if line.startswith("EXDATE")
    ][0]
    assert exdate_line == "EXDATE;TZID=Europe/Berlin:20260727T090000"


def test_exdate_entries_in_mixed_zones_share_one_representation():
    """One EXDATE property carries one TZID, so its values cannot disagree.

    A naive entry parses zone-aware and an offset entry collapses to UTC;
    written together, `icalendar` puts one `TZID=` on the property while the
    UTC value keeps its `Z` suffix - a combination RFC 5545 3.2.19 forbids
    (TZID must not be applied to a UTC value), and one that reads back
    plausibly enough to hide the damage.
    """
    event = _new_event()
    _apply(
        event,
        titel="Standup",
        start="2026-07-20T09:00:00 Europe/Berlin",
        wiederholung="FREQ=WEEKLY",
        ausnahme_daten=["2026-07-27T09:00:00", "2026-08-03T07:00:00+00:00"],
    )

    exdate_line = [
        line for line in event.to_ical().decode().split("\r\n") if line.startswith("EXDATE")
    ][0]
    assert exdate_line.count("TZID=") == 1
    assert "Z" not in exdate_line.split(":", 1)[1]
    assert exdate_line == "EXDATE;TZID=Europe/Berlin:20260727T090000,20260803T090000"


def test_exdate_on_a_utc_event_stays_utc_for_every_entry():
    """The mirror image: a UTC-anchored DTSTART takes UTC exception dates.

    Here it is the *naive* entry that would otherwise arrive as
    `ZoneInfo("Europe/Berlin")` and drag a TZID onto a property whose other
    value is written with a `Z`.
    """
    event = _new_event()
    _apply(
        event,
        titel="Standup",
        start="2026-07-20T07:00:00+00:00",
        wiederholung="FREQ=WEEKLY",
        ausnahme_daten=["2026-07-27T09:00:00", "2026-08-03T07:00:00Z"],
    )

    exdate_line = [
        line for line in event.to_ical().decode().split("\r\n") if line.startswith("EXDATE")
    ][0]
    assert exdate_line == "EXDATE:20260727T070000Z,20260803T070000Z"


def test_exdate_parses_repeated_properties_from_other_clients():
    """Other clients may write several EXDATE lines instead of one comma list."""
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))
    event.add("exdate", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))
    event.add("exdate", datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc))
    parsed = event_mapping.parse_vevent(event)
    assert len(parsed["ausnahme_daten"]) == 2


# --- reminders (VALARM) ---


def test_relative_reminder_related_to_start():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=["-PT30M"])
    alarms = [c for c in event.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    trigger = alarms[0]["trigger"]
    assert _dt(trigger) == timedelta(minutes=-30)
    assert trigger.params["RELATED"] == "START"


def test_absolute_reminder():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=["2026-07-20T08:00:00Z"])
    alarms = [c for c in event.subcomponents if c.name == "VALARM"]
    assert _dt(alarms[0]["trigger"]) == datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


def test_reminders_replace_instead_of_appending():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=["-PT30M", "-P1D"])
    _apply(event, erinnerungen=["-PT10M"])
    alarms = [c for c in event.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1


def test_invalid_reminder_raises_event_error():
    event = _new_event()
    with pytest.raises(InvalidEventDataError):
        _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=["quatsch"])


def test_parse_vevent_erinnerungen_relative_round_trip():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=["-PT30M"])
    parsed = event_mapping.parse_vevent(event)
    assert parsed["erinnerungen"] == ["-PT30M"]


def test_parse_vevent_erinnerungen_absolute_round_trip():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        erinnerungen=["2026-08-07T09:00:00+00:00", "2026-08-07T09:00:00Z"],
    )
    parsed = event_mapping.parse_vevent(event)
    # Both spellings name the same instant, so `apply_alarms` collapses them
    # into one alarm; reading it back formats it in the server's default
    # timezone (Europe/Berlin, +02:00 in August), the same convention
    # DTSTART/DTEND follow.
    assert parsed["erinnerungen"] == ["2026-08-07T11:00:00+02:00"]


def test_parse_vevent_erinnerungen_preserves_order():
    event = _new_event()
    reminders = ["-P1D", "-PT30M", "2026-08-07T11:00:00+02:00"]
    _apply(event, titel="T", start="2026-07-20T14:00:00", erinnerungen=reminders)
    parsed = event_mapping.parse_vevent(event)
    assert parsed["erinnerungen"] == reminders


def test_parse_vevent_erinnerungen_no_valarm_returns_empty():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["erinnerungen"] == []


def test_parse_vevent_erinnerungen_skips_valarm_without_trigger_or_invalid_trigger():
    event = _event_from_ics(
        """BEGIN:VEVENT
UID:event-1
SUMMARY:T
DTSTART:20260720T140000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:ours
TRIGGER;RELATED=START:-PT30M
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:no trigger at all
END:VALARM
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:date-valued trigger
TRIGGER;VALUE=DATE:20260719
END:VALARM
END:VEVENT
"""
    )
    parsed = event_mapping.parse_vevent(event)
    assert parsed["erinnerungen"] == ["-PT30M"]


_END_ANCHORED_EVENT_ICS = """BEGIN:VEVENT
UID:event-1
SUMMARY:T
DTSTART:20260720T140000Z
DTEND:20260720T160000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:before the meeting ends
TRIGGER;RELATED=END:-PT30M
END:VALARM
END:VEVENT
"""


def test_parse_vevent_erinnerungen_skips_end_anchored_alarm():
    """An alarm anchored to DTEND has no `erinnerungen` spelling.

    Reporting it as "-PT30M" would claim it fires 30 minutes before the event
    *starts* - here that is a two-hour lie, the length of the event.
    """
    event = _event_from_ics(_END_ANCHORED_EVENT_ICS)
    assert event_mapping.parse_vevent(event)["erinnerungen"] == []


def test_parse_vevent_erinnerungen_lists_end_anchored_alarm_on_event_without_an_end():
    """Without DTEND/DURATION the event has no end distinct from its start.

    `RELATED=END` then names the same moment as `RELATED=START`, so "-PT30M"
    is an honest spelling for it - unlike on the event above, where DTEND puts
    the two anchors two hours apart.
    """
    event = _event_from_ics(
        """BEGIN:VEVENT
UID:event-1
SUMMARY:T
DTSTART:20260720T140000Z
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:end anchored, but there is no end
TRIGGER;RELATED=END:-PT30M
END:VALARM
END:VEVENT
"""
    )
    assert event_mapping.parse_vevent(event)["erinnerungen"] == ["-PT30M"]


def test_apply_event_fields_preserves_end_anchored_alarm():
    event = _event_from_ics(_END_ANCHORED_EVENT_ICS)
    end_anchored = next(c for c in event.subcomponents if c.name == "VALARM").to_ical()

    _apply(event, erinnerungen=["-PT15M"])

    alarms = [c.to_ical() for c in event.subcomponents if c.name == "VALARM"]
    assert end_anchored in alarms
    assert event_mapping.parse_vevent(event)["erinnerungen"] == ["-PT15M"]


# --- status / visibility labels ---


def test_status_labels():
    for label, ical_value in event_mapping.STATUS_LABELS.items():
        event = _new_event()
        _apply(event, titel="T", start="2026-07-20T14:00:00", status=label)
        assert str(event["status"]) == ical_value
        assert event_mapping.parse_vevent(event)["status"] == label


def test_unknown_status_rejected():
    with pytest.raises(InvalidEventDataError, match="status"):
        _apply(_new_event(), titel="T", start="2026-07-20T14:00:00", status="vielleicht")


def test_unknown_visibility_raises_event_error():
    with pytest.raises(InvalidEventDataError, match="sichtbarkeit"):
        _apply(_new_event(), titel="T", start="2026-07-20T14:00:00", sichtbarkeit="geheim")


def test_unknown_ical_status_parses_as_none():
    event = _new_event()
    event.add("status", "X-CUSTOM")
    assert event_mapping.parse_vevent(event)["status"] is None


# --- clear (felder_leeren) ---


def test_clear_unknown_field_rejected():
    with pytest.raises(InvalidEventDataError, match="felder_leeren"):
        _apply(_new_event(), clear=("unbekannt",))


@pytest.mark.parametrize("name", ["titel", "start"])
def test_titel_and_start_not_clearable(name):
    with pytest.raises(InvalidEventDataError, match="felder_leeren"):
        _apply(_new_event(), clear=(name,))


def test_set_and_clear_same_field_rejected():
    with pytest.raises(InvalidEventDataError, match="both set and clear"):
        _apply(_new_event(), ort="Büro", clear=("ort",))


def test_clear_removes_properties():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        ende="2026-07-20T15:00:00",
        ort="Büro",
        erinnerungen=["-PT30M"],
    )
    _apply(event, clear=("ende", "ort", "erinnerungen"))
    parsed = event_mapping.parse_vevent(event)
    assert parsed["ende"] is None
    assert parsed["ort"] is None
    assert not [c for c in event.subcomponents if c.name == "VALARM"]


# --- RELATED-TO links ---


def test_verknuepfte_aufgabe_written_as_parent_relation():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", verknuepfte_aufgabe="task-42")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["verknuepfte_aufgaben"] == [{"uid": "task-42", "beziehung": "zeitblock"}]


def test_add_relation_appends_and_is_idempotent():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", verknuepfte_aufgabe="task-1")
    event_mapping.add_relation(event, "task-2", "CHILD")
    event_mapping.add_relation(event, "task-2", "CHILD")  # no-op duplicate
    parsed = event_mapping.parse_vevent(event)
    assert parsed["verknuepfte_aufgaben"] == [
        {"uid": "task-1", "beziehung": "zeitblock"},
        {"uid": "task-2", "beziehung": "voraussetzung"},
    ]


def test_related_without_reltype_defaults_to_parent():
    event = _new_event()
    event.add("related-to", "task-7")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["verknuepfte_aufgaben"] == [{"uid": "task-7", "beziehung": "zeitblock"}]


def test_related_to_parent_reltype_round_trips_as_zeitblock():
    """Round-trip check for the beziehung vocabulary fix: a RELATED-TO written
    with RELTYPE=PARENT (as link_task_to_event writes for beziehung="zeitblock")
    must parse back with the same "zeitblock" label, not a different word for
    the same relation."""
    event = _new_event()
    event.add("related-to", "task-99", parameters={"RELTYPE": "PARENT"})
    parsed = event_mapping.parse_vevent(event)
    assert parsed["verknuepfte_aufgaben"] == [{"uid": "task-99", "beziehung": "zeitblock"}]


def test_related_to_child_reltype_parses_as_voraussetzung():
    event = _new_event()
    event.add("related-to", "task-100", parameters={"RELTYPE": "CHILD"})
    parsed = event_mapping.parse_vevent(event)
    assert parsed["verknuepfte_aufgaben"] == [{"uid": "task-100", "beziehung": "voraussetzung"}]


# --- attendees / organizer (teilnehmer / organisator) ---


def test_no_organizer_or_attendees_parses_as_empty():
    event = _new_event()
    parsed = event_mapping.parse_vevent(event)
    assert parsed["organisator"] is None
    assert parsed["teilnehmer"] == []


def test_teilnehmer_round_trip_sets_organizer_and_attendees():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        teilnehmer=[
            {"email": "a@example.com", "name": "Alice"},
            {"email": "b@example.com", "rolle": "optional", "rsvp": False},
        ],
        own_organizer="mailto:me@example.com",
    )
    parsed = event_mapping.parse_vevent(event)

    assert parsed["organisator"] == {"email": "me@example.com", "name": None}
    assert parsed["teilnehmer"] == [
        {
            "email": "a@example.com",
            "name": "Alice",
            "status": "ausstehend",
            "rolle": "erforderlich",
            "rsvp": True,
        },
        {
            "email": "b@example.com",
            "name": None,
            "status": "ausstehend",
            "rolle": "optional",
            "rsvp": False,
        },
    ]


def test_teilnehmer_without_own_organizer_leaves_organizer_unset():
    """event_mapping makes no network calls; without an own_organizer supplied
    (the pure-unit-test case), ORGANIZER is simply left unset rather than
    guessed at."""
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        teilnehmer=[{"email": "a@example.com"}],
    )
    assert "organizer" not in event
    assert event_mapping.parse_vevent(event)["organisator"] is None


def test_teilnehmer_does_not_overwrite_existing_organizer():
    event = _new_event()
    event.add("organizer", "mailto:existing@example.com")
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        teilnehmer=[{"email": "a@example.com"}],
        own_organizer="mailto:me@example.com",
    )
    assert event_mapping.parse_vevent(event)["organisator"] == {
        "email": "existing@example.com",
        "name": None,
    }


def test_teilnehmer_replaces_instead_of_appending():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        teilnehmer=[{"email": "a@example.com"}],
        own_organizer="mailto:me@example.com",
    )
    _apply(event, teilnehmer=[{"email": "b@example.com"}])
    parsed = event_mapping.parse_vevent(event)
    assert [t["email"] for t in parsed["teilnehmer"]] == ["b@example.com"]


def test_teilnehmer_missing_email_rejected():
    with pytest.raises(InvalidEventDataError, match="email"):
        _apply(
            _new_event(),
            titel="T",
            start="2026-07-20T14:00:00",
            teilnehmer=[{"name": "Alice"}],
        )


def test_teilnehmer_unknown_rolle_rejected():
    with pytest.raises(InvalidEventDataError, match="rolle"):
        _apply(
            _new_event(),
            titel="T",
            start="2026-07-20T14:00:00",
            teilnehmer=[{"email": "a@example.com", "rolle": "irgendwas"}],
        )


def test_teilnehmer_clear_removes_attendees_and_organizer():
    event = _new_event()
    _apply(
        event,
        titel="T",
        start="2026-07-20T14:00:00",
        teilnehmer=[{"email": "a@example.com"}],
        own_organizer="mailto:me@example.com",
    )
    assert "attendee" in event
    assert "organizer" in event

    _apply(event, clear=("teilnehmer",))
    parsed = event_mapping.parse_vevent(event)
    assert parsed["teilnehmer"] == []
    assert parsed["organisator"] is None
    assert "attendee" not in event
    assert "organizer" not in event


def test_teilnehmer_clear_and_set_conflict_rejected():
    with pytest.raises(InvalidEventDataError, match="both set and clear"):
        _apply(
            _new_event(),
            teilnehmer=[{"email": "a@example.com"}],
            clear=("teilnehmer",),
        )


@pytest.mark.parametrize(
    ("ical_value", "label"),
    [
        ("NEEDS-ACTION", "ausstehend"),
        ("ACCEPTED", "zugesagt"),
        ("DECLINED", "abgesagt"),
        ("TENTATIVE", "vorläufig"),
        ("DELEGATED", "delegiert"),
    ],
)
def test_partstat_label_mapping(ical_value, label):
    assert event_mapping.ical_partstat_to_label(ical_value) == label


def test_partstat_missing_defaults_to_ausstehend():
    assert event_mapping.ical_partstat_to_label(None) == "ausstehend"


def test_partstat_unknown_value_passes_through_lowercased():
    assert event_mapping.ical_partstat_to_label("X-CUSTOM") == "x-custom"


@pytest.mark.parametrize(
    ("ical_value", "label"),
    [
        ("CHAIR", "leitung"),
        ("REQ-PARTICIPANT", "erforderlich"),
        ("OPT-PARTICIPANT", "optional"),
        ("NON-PARTICIPANT", "keine-teilnahme"),
    ],
)
def test_role_label_mapping(ical_value, label):
    assert event_mapping.ical_role_to_label(ical_value) == label


def test_role_missing_defaults_to_erforderlich():
    assert event_mapping.ical_role_to_label(None) == "erforderlich"


def test_role_unknown_value_passes_through_lowercased():
    assert event_mapping.ical_role_to_label("X-WEIRD") == "x-weird"


def test_response_label_to_partstat_valid_values():
    assert event_mapping.response_label_to_partstat("zugesagt") == "ACCEPTED"
    assert event_mapping.response_label_to_partstat("abgesagt") == "DECLINED"
    assert event_mapping.response_label_to_partstat("vorläufig") == "TENTATIVE"


def test_response_label_to_partstat_rejects_ausstehend():
    """ausstehend/delegiert are valid PARTSTAT read-labels but not valid
    respond_to_event replies - you can't RSVP with "no reply yet"."""
    with pytest.raises(InvalidEventDataError, match="antwort"):
        event_mapping.response_label_to_partstat("ausstehend")


# --- respond_to_event's pure counterpart: apply_own_attendee_response ---


def test_apply_own_attendee_response_sets_partstat():
    event = _new_event()
    event.add("attendee", "mailto:me@example.com", parameters={"PARTSTAT": "NEEDS-ACTION"})
    event.add("attendee", "mailto:other@example.com", parameters={"PARTSTAT": "NEEDS-ACTION"})

    event_mapping.apply_own_attendee_response(event, ["mailto:me@example.com"], "ACCEPTED")

    parsed = event_mapping.parse_vevent(event)
    statuses = {t["email"]: t["status"] for t in parsed["teilnehmer"]}
    assert statuses == {"me@example.com": "zugesagt", "other@example.com": "ausstehend"}


def test_apply_own_attendee_response_matches_case_insensitively_and_ignores_mailto():
    event = _new_event()
    event.add("attendee", "mailto:Me@Example.com", parameters={"PARTSTAT": "NEEDS-ACTION"})

    event_mapping.apply_own_attendee_response(event, ["me@example.com"], "DECLINED")

    assert event_mapping.parse_vevent(event)["teilnehmer"][0]["status"] == "abgesagt"


def test_apply_own_attendee_response_writes_comment():
    event = _new_event()
    event.add("attendee", "mailto:me@example.com", parameters={"PARTSTAT": "NEEDS-ACTION"})

    event_mapping.apply_own_attendee_response(
        event, ["mailto:me@example.com"], "TENTATIVE", comment="Vielleicht"
    )

    assert str(event.get("comment")) == "Vielleicht"


def test_apply_own_attendee_response_not_an_attendee_raises():
    event = _new_event()
    event.add("attendee", "mailto:other@example.com", parameters={"PARTSTAT": "NEEDS-ACTION"})

    with pytest.raises(InvalidEventDataError, match="not listed as an attendee"):
        event_mapping.apply_own_attendee_response(event, ["mailto:me@example.com"], "ACCEPTED")


def test_apply_own_attendee_response_no_attendees_at_all_raises():
    event = _new_event()

    with pytest.raises(InvalidEventDataError, match="not listed as an attendee"):
        event_mapping.apply_own_attendee_response(event, ["mailto:me@example.com"], "ACCEPTED")


# --- parse edge cases ---


def test_parse_duration_instead_of_dtend():
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))
    event.add("duration", timedelta(hours=2))
    parsed = event_mapping.parse_vevent(event)
    assert parsed["ende"] == "2026-07-20T18:00:00+02:00"


def test_parse_recurrence_id_as_wiederholung_von():
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))
    event.add("recurrence-id", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))
    parsed = event_mapping.parse_vevent(event)
    assert parsed["wiederholung_von"] == "2026-07-27T16:00:00+02:00"


# --- filter_events ---


def _event_dict(titel="E", start=None, beschreibung=None, ort=None, tags=None):
    return {
        "titel": titel,
        "start": start,
        "beschreibung": beschreibung,
        "ort": ort,
        "tags": tags or [],
    }


def test_filter_events_suchtext_matches_title_description_location():
    events = [
        _event_dict(titel="Zahnarzt", start="2026-07-20T09:00:00"),
        _event_dict(titel="Meeting", beschreibung="Zahnarzt nachbereiten", start="2026-07-21"),
        _event_dict(titel="Sport", ort="ZAHNARZTPRAXIS", start="2026-07-22T18:00:00"),
        _event_dict(titel="Kino", start="2026-07-23T20:00:00"),
    ]
    result = event_mapping.filter_events(events, suchtext="zahnarzt")
    assert [e["titel"] for e in result] == ["Zahnarzt", "Meeting", "Sport"]


def test_filter_events_tag_exact_case_insensitive():
    events = [
        _event_dict(titel="A", tags=["Arbeit"], start="2026-07-20"),
        _event_dict(titel="B", tags=["Arbeitsamt"], start="2026-07-21"),
    ]
    result = event_mapping.filter_events(events, tag="arbeit")
    assert [e["titel"] for e in result] == ["A"]


def test_filter_events_sorts_dates_and_datetimes_chronologically():
    events = [
        _event_dict(titel="spät", start="2026-07-20T18:00:00"),
        _event_dict(titel="ganztags", start="2026-07-20"),
        _event_dict(titel="ohne start"),
        _event_dict(titel="früher Tag", start="2026-07-19T23:00:00"),
    ]
    result = event_mapping.filter_events(events)
    assert [e["titel"] for e in result] == ["früher Tag", "ganztags", "spät", "ohne start"]


def test_filter_events_limit_returns_earliest():
    events = [
        _event_dict(titel="B", start="2026-07-21"),
        _event_dict(titel="A", start="2026-07-20"),
    ]
    result = event_mapping.filter_events(events, limit=1)
    assert [e["titel"] for e in result] == ["A"]


def test_filter_events_limit_must_be_positive():
    with pytest.raises(InvalidEventDataError, match="limit"):
        event_mapping.filter_events([], limit=0)


# --- local day window (get_agenda) ---


def _in_day(events: list[dict[str, Any]], day: date = date(2026, 7, 20)) -> list[str]:
    start, end = event_mapping.local_day_window(day)
    return [event["uid"] for event in event_mapping.events_in_window(events, start, end)]


def test_events_in_window_uses_local_day_edges():
    """Both edges are half-open: midnight belongs to the day that starts there."""
    events: list[dict[str, Any]] = [
        {"uid": "ends-at-midnight", "start": "2026-07-19T23:00:00+02:00", "ende": "2026-07-20"},
        {"uid": "starts-at-midnight", "start": "2026-07-20T00:00:00+02:00", "ende": None},
        {"uid": "last-minute", "start": "2026-07-20T23:59:00+02:00", "ende": None},
        {"uid": "next-midnight", "start": "2026-07-21T00:00:00+02:00", "ende": None},
    ]
    # "ends-at-midnight" has a datetime start and an all-day end - a pairing
    # this server never writes, so its end is ignored and it counts as a
    # zero-length event at 23:00 on the 19th.
    assert _in_day(events) == ["starts-at-midnight", "last-minute"]


def test_events_in_window_covers_a_multi_day_all_day_event():
    events = [
        {"uid": "holiday", "start": "2026-07-18", "ende": "2026-07-21"},
        {"uid": "other-week", "start": "2026-07-27", "ende": "2026-07-28"},
    ]
    assert _in_day(events) == ["holiday"]


def test_events_in_window_ignores_a_mismatched_all_day_pair():
    """An all-day start with a timed end - only a foreign client writes that.

    There is no sane length to read out of the pair, so the event counts as
    the one day its start names rather than as something reaching into the
    next.
    """
    events = [{"uid": "odd", "start": "2026-07-20", "ende": "2026-07-20T15:00:00+02:00"}]
    assert _in_day(events) == ["odd"]
    assert _in_day(events, date(2026, 7, 21)) == []


def test_events_in_window_keeps_what_it_cannot_judge():
    """Neither a series master nor an event without a start may be dropped."""
    events: list[dict[str, Any]] = [
        {"uid": "series", "start": "2020-01-06T09:00:00+01:00", "wiederholung": "FREQ=WEEKLY"},
        {"uid": "no-start", "start": None},
        {"uid": "unparseable", "start": "irgendwann"},
        {"uid": "elsewhere", "start": "2026-09-01T09:00:00+02:00"},
    ]
    assert _in_day(events) == ["series", "no-start", "unparseable"]


# --- free-busy: event_busy_interval ---


def test_event_busy_interval_timed_event():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", ende="2026-07-20T15:00:00")
    interval = event_mapping.event_busy_interval(event)
    assert interval == (
        datetime(2026, 7, 20, 14, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        datetime(2026, 7, 20, 15, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )


def test_event_busy_interval_all_day_event_spans_full_local_days():
    """An all-day event blocks its own local days, not the UTC ones.

    With a UTC anchor, a Berlin all-day event would start being busy at 02:00
    local and bleed two hours into the next day - and disagree with the day
    windows `list_events`/`get_agenda` use for the very same date.
    """
    event = _new_event()
    _apply(event, titel="T", start="2026-08-01", ende="2026-08-02")
    interval = event_mapping.event_busy_interval(event)
    berlin = ZoneInfo("Europe/Berlin")
    assert interval == (
        datetime(2026, 8, 1, 0, 0, tzinfo=berlin),
        datetime(2026, 8, 3, 0, 0, tzinfo=berlin),
    )


def test_event_busy_interval_all_day_event_follows_configured_timezone():
    event = _new_event()
    _apply(event, titel="T", start="2026-08-01", ende="2026-08-01")
    mapping.set_default_timezone("UTC")
    interval = event_mapping.event_busy_interval(event)
    assert interval == (
        datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc),
    )


def test_event_busy_interval_cancelled_is_not_busy():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", status="abgesagt")
    assert event_mapping.event_busy_interval(event) is None


def test_event_busy_interval_transparent_is_not_busy():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00")
    event.add("transp", "TRANSPARENT")
    assert event_mapping.event_busy_interval(event) is None


def test_event_busy_interval_opaque_is_busy():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00")
    event.add("transp", "OPAQUE")
    assert event_mapping.event_busy_interval(event) is not None


def test_event_busy_interval_no_dtstart_is_none():
    event = _new_event()
    assert event_mapping.event_busy_interval(event) is None


def test_event_busy_interval_uses_duration_when_no_dtend():
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))
    event.add("duration", timedelta(hours=2))
    interval = event_mapping.event_busy_interval(event)
    assert interval == (
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
    )


def test_event_busy_interval_without_end_is_zero_length():
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))
    interval = event_mapping.event_busy_interval(event)
    assert interval == (
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
    )


# --- free-busy: merge_busy_intervals ---


def test_merge_busy_intervals_merges_overlapping():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    b = datetime(2026, 7, 20, 10, 30, tzinfo=timezone.utc)
    c = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    d = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    result = event_mapping.merge_busy_intervals([(a, b), (c, d)])
    assert result == [(a, d)]


def test_merge_busy_intervals_merges_touching():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    b = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    c = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    d = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    result = event_mapping.merge_busy_intervals([(a, b), (c, d)])
    assert result == [(a, d)]


def test_merge_busy_intervals_keeps_separate_intervals_apart():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    b = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    c = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    d = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    result = event_mapping.merge_busy_intervals([(a, b), (c, d)])
    assert result == [(a, b), (c, d)]


def test_merge_busy_intervals_sorts_unordered_input():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    b = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    c = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    d = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    result = event_mapping.merge_busy_intervals([(c, d), (a, b)])
    assert result == [(a, b), (c, d)]


def test_merge_busy_intervals_drops_zero_length():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    result = event_mapping.merge_busy_intervals([(a, a)])
    assert result == []


def test_merge_busy_intervals_empty_input():
    assert event_mapping.merge_busy_intervals([]) == []


def test_merge_busy_intervals_naive_datetimes_use_default_timezone():
    """A foreign client's floating time means the same thing here as elsewhere."""
    a = datetime(2026, 7, 20, 9, 0)
    b = datetime(2026, 7, 20, 10, 0)
    result = event_mapping.merge_busy_intervals([(a, b)])
    berlin = ZoneInfo("Europe/Berlin")
    assert result == [
        (
            datetime(2026, 7, 20, 9, 0, tzinfo=berlin),
            datetime(2026, 7, 20, 10, 0, tzinfo=berlin),
        )
    ]


# --- free-busy: extract_freebusy_periods ---


def _add_freebusy(vfb: FreeBusy, start: datetime, end: datetime, fbtype: str) -> None:
    vfb.add("freebusy", [(start, end)], parameters={"FBTYPE": fbtype})


def test_extract_freebusy_periods_reads_busy_period():
    vfb = FreeBusy()
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    _add_freebusy(vfb, start, end, "BUSY")

    assert event_mapping.extract_freebusy_periods(vfb) == [(start, end)]


def test_extract_freebusy_periods_excludes_free():
    vfb = FreeBusy()
    busy_start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    _add_freebusy(vfb, busy_start, datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc), "BUSY")
    _add_freebusy(
        vfb,
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        "FREE",
    )

    periods = event_mapping.extract_freebusy_periods(vfb)
    assert len(periods) == 1
    assert periods[0][0] == busy_start


def test_extract_freebusy_periods_includes_busy_tentative_and_unavailable():
    vfb = FreeBusy()
    _add_freebusy(
        vfb,
        datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
        "BUSY-TENTATIVE",
    )
    _add_freebusy(
        vfb,
        datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        "BUSY-UNAVAILABLE",
    )

    assert len(event_mapping.extract_freebusy_periods(vfb)) == 2


def test_extract_freebusy_periods_reads_a_value_without_z_as_utc():
    """RFC 5545 3.8.2.6: FREEBUSY periods are UTC, whether or not the Z is there.

    Unlike a VEVENT, a VFREEBUSY has no floating times to interpret - so a
    value that arrives without its `Z` is a UTC value spelled sloppily, not a
    local one, and reading it in the server's default timezone moves every
    busy block by that zone's offset.
    """
    component = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VFREEBUSY\r\nUID:fb-1\r\n"
        "FREEBUSY;FBTYPE=BUSY:20260720T090000/20260720T100000\r\n"
        "END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    ).walk("VFREEBUSY")[0]

    assert event_mapping.extract_freebusy_periods(component) == [
        (
            datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
        )
    ]


def test_extract_freebusy_periods_no_freebusy_property_is_empty():
    vfb = FreeBusy()
    assert event_mapping.extract_freebusy_periods(vfb) == []
