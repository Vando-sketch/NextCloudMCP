"""Integration tests against a real Nextcloud CalDAV instance.

Skipped by default - see README.md for how to enable these locally.
"""

from __future__ import annotations

import os
import time

import pytest

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.caldav_client import CalDavService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="Set RUN_INTEGRATION_TESTS=1 (plus real credentials) to run these tests.",
)


@pytest.fixture(scope="session")
def live_service() -> CalDavService:
    return CalDavService(
        url=os.environ["NEXTCLOUD_CALDAV_URL"],
        username=os.environ["NEXTCLOUD_USERNAME"],
        password=os.environ["NEXTCLOUD_APP_PASSWORD"],
    )


@pytest.fixture
def test_list_name() -> str:
    return os.environ["INTEGRATION_TEST_LIST"]


def test_list_task_lists_returns_at_least_the_test_list(live_service, test_list_name):
    lists = live_service.list_task_lists()
    assert any(entry["name"] == test_list_name for entry in lists)


def test_full_task_lifecycle(live_service, test_list_name):
    uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            titel="nextcloud-task-mcp integration test task",
            notizen="Created by the automated integration test suite; safe to delete.",
        ),
    )
    try:
        tasks = live_service.list_tasks(test_list_name, only_open=True)
        assert any(task["uid"] == uid for task in tasks)

        fetched = live_service.get_task(test_list_name, uid)
        assert fetched["uid"] == uid

        live_service.update_task(test_list_name, uid, mapping.TaskFields(notizen="updated notes"))
        updated = next(t for t in live_service.list_tasks(test_list_name) if t["uid"] == uid)
        assert updated["notizen"] == "updated notes"

        live_service.update_task(test_list_name, uid, mapping.TaskFields(clear=("notizen",)))
        cleared = live_service.get_task(test_list_name, uid)
        assert cleared["notizen"] is None

        live_service.complete_task(test_list_name, uid)
        all_tasks = live_service.list_tasks(test_list_name, only_open=False)
        completed = next(t for t in all_tasks if t["uid"] == uid)
        assert completed["status"] == "erledigt"
    finally:
        live_service.delete_task(test_list_name, uid)


# ---------------------------------------------------------------------------
# Calendar / event integration tests (VEVENT)
# ---------------------------------------------------------------------------

# Unique per test run: Nextcloud keeps deleted calendars in its trashbin,
# where they invisibly occupy their collection URI until purged. The service
# dodges occupied ids automatically (see CalDavService._make_collection), but
# unique names keep repeated test runs from piling up on the same slug.
_RUN_SUFFIX = f"{int(time.time())}"
_TEST_CALENDAR = f"MCP-Event-Test-{_RUN_SUFFIX}"


@pytest.fixture(scope="session")
def test_calendar(live_service):
    """One disposable VEVENT calendar shared by the whole test run.

    Session-scoped on purpose: Nextcloud rate-limits calendar creation
    (~10 new calendars per user per hour), so creating a fresh calendar per
    test would make the suite trip that limit after a couple of runs.
    """
    live_service.create_calendar(_TEST_CALENDAR, farbe="#00679e")
    try:
        yield _TEST_CALENDAR
    finally:
        live_service.delete_calendar(_TEST_CALENDAR)


def test_calendar_lifecycle(live_service):
    name_a = f"MCP-Cal-Lifecycle-{_RUN_SUFFIX}"
    name_b = f"MCP-Cal-Renamed-{_RUN_SUFFIX}"
    created = live_service.create_calendar(name_a, farbe="#FF7A66")
    try:
        assert created["name"] == name_a

        calendars = live_service.list_calendars()
        entry = next(c for c in calendars if c["name"] == name_a)
        assert "VEVENT" in entry["komponenten"]
        assert entry["farbe"].upper().startswith("#FF7A66")

        renamed = live_service.update_calendar(name_a, new_display_name=name_b, farbe="#00679e")
        assert renamed["name"] == name_b
        calendars = live_service.list_calendars()
        assert any(c["name"] == name_b for c in calendars)
        assert not any(c["name"] == name_a for c in calendars)
    finally:
        live_service.delete_calendar(name_b)

    assert not any(c["name"] == name_b for c in live_service.list_calendars())


def test_event_lifecycle(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            titel="Integrationstest-Termin",
            start="2026-09-01T14:00:00",
            ende="2026-09-01T15:00:00",
            ort="Testort",
            beschreibung="Vom Integrationstest erstellt; kann weg.",
            tags=["MCP-Test"],
            status="bestätigt",
            erinnerungen=["-PT30M"],
        ),
    )

    fetched = live_service.get_event(test_calendar, uid)
    assert fetched["titel"] == "Integrationstest-Termin"
    assert fetched["start"] == "2026-09-01T14:00:00+00:00"
    assert fetched["ende"] == "2026-09-01T15:00:00+00:00"
    assert fetched["status"] == "bestätigt"
    assert fetched["tags"] == ["MCP-Test"]

    listed = live_service.list_events(
        calendar_names=[test_calendar], von="2026-09-01", bis="2026-09-01"
    )
    assert any(e["uid"] == uid for e in listed)

    live_service.update_event(
        test_calendar, uid, event_mapping.EventFields(ort="Neuer Ort", status="vorläufig")
    )
    updated = live_service.get_event(test_calendar, uid)
    assert updated["ort"] == "Neuer Ort"
    assert updated["status"] == "vorläufig"

    live_service.update_event(test_calendar, uid, event_mapping.EventFields(clear=("ort",)))
    assert live_service.get_event(test_calendar, uid)["ort"] is None

    live_service.delete_event(test_calendar, uid)
    remaining = live_service.list_events(
        calendar_names=[test_calendar], von="2026-09-01", bis="2026-09-01"
    )
    assert not any(e["uid"] == uid for e in remaining)


def test_all_day_event_round_trip(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(titel="Ganztags-Test", start="2026-09-02", ende="2026-09-03"),
    )
    fetched = live_service.get_event(test_calendar, uid)
    assert fetched["ganztaegig"] is True
    assert fetched["start"] == "2026-09-02"
    assert fetched["ende"] == "2026-09-03"  # inclusive last day


def test_recurring_event_expansion_and_exdate(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            titel="Wöchentlicher Test",
            start="2026-09-07T10:00:00",
            ende="2026-09-07T11:00:00",
            wiederholung="FREQ=WEEKLY;BYDAY=MO;COUNT=4",
            ausnahme_daten=["2026-09-14T10:00:00"],
        ),
    )

    # Time-range query must match a later occurrence of the series.
    hits = live_service.list_events(
        calendar_names=[test_calendar], von="2026-09-20", bis="2026-09-22"
    )
    assert any(e["titel"] == "Wöchentlicher Test" for e in hits)

    # Expansion yields the individual occurrences, minus the EXDATE one.
    expanded = live_service.list_events(
        calendar_names=[test_calendar], von="2026-09-01", bis="2026-09-30", expand=True
    )
    occurrences = [e for e in expanded if e["titel"] == "Wöchentlicher Test"]
    starts = sorted(e["start"] for e in occurrences)
    assert len(occurrences) == 3  # 4 occurrences minus 1 exception
    assert "2026-09-14T10:00:00+00:00" not in starts


def test_task_event_linking_and_conversion(live_service, test_list_name, test_calendar):
    from nextcloud_task_mcp import event_mapping, mapping

    task_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            titel="Verknüpfungstest-Aufgabe",
            faellig_datum="2026-09-03T16:00:00",
            notizen="Vom Integrationstest erstellt; kann weg.",
        ),
    )
    try:
        # Task -> event conversion (timeboxing).
        event_uid = live_service.create_event_from_task(
            test_list_name, task_uid, test_calendar, dauer_minuten=45
        )
        event = live_service.get_event(test_calendar, event_uid)
        assert event["titel"] == "Verknüpfungstest-Aufgabe"
        assert event["start"] == "2026-09-03T16:00:00+00:00"
        assert event["ende"] == "2026-09-03T16:45:00+00:00"
        assert {"uid": task_uid, "beziehung": "uebergeordnet"} in event["verknuepfte_aufgaben"]

        # Explicit linking with the other relation.
        second_uid = live_service.create_event(
            test_calendar,
            event_mapping.EventFields(
                titel="Voraussetzungs-Termin",
                start="2026-09-02T10:00:00",
                ende="2026-09-02T11:00:00",
            ),
        )
        live_service.link_task_to_event(
            test_list_name, task_uid, test_calendar, second_uid, beziehung="voraussetzung"
        )
        linked = live_service.get_event(test_calendar, second_uid)
        assert {"uid": task_uid, "beziehung": "untergeordnet"} in linked["verknuepfte_aufgaben"]
    finally:
        live_service.delete_task(test_list_name, task_uid)


def test_get_agenda_combines_events_and_tasks(live_service, test_list_name, test_calendar):
    from nextcloud_task_mcp import event_mapping, mapping

    task_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(titel="Agenda-Test-Aufgabe", faellig_datum="2026-09-04T09:00:00"),
    )
    event_uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            titel="Agenda-Test-Termin",
            start="2026-09-04T14:00:00",
            ende="2026-09-04T15:00:00",
        ),
    )
    try:
        agenda = live_service.get_agenda("2026-09-04")
        assert any(e["uid"] == event_uid for e in agenda["termine"])
        matching_tasks = [t for t in agenda["aufgaben"] if t["uid"] == task_uid]
        assert matching_tasks and matching_tasks[0]["liste"] == test_list_name
    finally:
        live_service.delete_task(test_list_name, task_uid)
