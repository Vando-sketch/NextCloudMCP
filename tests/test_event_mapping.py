"""Unit tests for the event field <-> iCalendar VEVENT mapping logic, no CalDAV involved."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from icalendar import Event
from icalendar.prop import vDDDTypes

from nextcloud_task_mcp import event_mapping
from nextcloud_task_mcp.errors import InvalidEventDataError
from nextcloud_task_mcp.event_mapping import EventFields


def _new_event(uid: str = "event-1") -> Event:
    event = Event()
    event.add("uid", uid)
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
    # Naive datetimes are interpreted as UTC (B2).
    assert parsed["start"] == "2026-07-20T14:00:00+00:00"
    assert parsed["ende"] == "2026-07-20T15:30:00+00:00"
    assert parsed["ganztaegig"] is False
    assert parsed["ort"] == "Konferenzraum"
    assert parsed["beschreibung"] == "Sprint-Planung"
    assert set(parsed["tags"]) == {"Arbeit", "Wichtig"}
    assert parsed["status"] == "bestätigt"
    assert parsed["sichtbarkeit"] == "privat"
    assert parsed["url"] == "https://example.com/meeting"
    assert parsed["wiederholung"] is None
    assert parsed["ausnahme_daten"] == []
    assert parsed["verknuepfte_aufgaben"] == []
    assert parsed["wiederholung_von"] is None


def test_z_suffix_datetime_is_utc():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00Z", ende="2026-07-20T15:00:00Z")
    parsed = event_mapping.parse_vevent(event)
    assert parsed["start"] == "2026-07-20T14:00:00+00:00"
    assert parsed["ende"] == "2026-07-20T15:00:00+00:00"


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
        "2026-07-27T14:00:00+00:00",
        "2026-08-03T14:00:00+00:00",
    ]

    _apply(event, clear=("ausnahme_daten",))
    assert event_mapping.parse_vevent(event)["ausnahme_daten"] == []


def test_exdate_replaces_instead_of_appending():
    event = _new_event()
    _apply(event, titel="T", start="2026-07-20T14:00:00", ausnahme_daten=["2026-07-27T14:00:00"])
    _apply(event, ausnahme_daten=["2026-08-03T14:00:00"])
    assert event_mapping.parse_vevent(event)["ausnahme_daten"] == ["2026-08-03T14:00:00+00:00"]


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
    assert parsed["ende"] == "2026-07-20T16:00:00+00:00"


def test_parse_recurrence_id_as_wiederholung_von():
    event = _new_event()
    event.add("dtstart", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))
    event.add("recurrence-id", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc))
    parsed = event_mapping.parse_vevent(event)
    assert parsed["wiederholung_von"] == "2026-07-27T14:00:00+00:00"


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
