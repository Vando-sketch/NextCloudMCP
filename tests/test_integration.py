"""Integration tests against a real Nextcloud CalDAV instance.

Skipped by default - see README.md for how to enable these locally.
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import run_async

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.caldav_client import CalDavService
from nextcloud_task_mcp.errors import EventNotFoundError, NotizNotFoundError
from nextcloud_task_mcp.notes_client import NotesService
from nextcloud_task_mcp.notes_mapping import NoteFields

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


@pytest.fixture(scope="session")
def test_list_name(live_service) -> str:
    """The task list these tests write into, created on demand if it is gone.

    Session-scoped and deliberately *not* deleted afterwards: Nextcloud
    rate-limits collection creation (~10 per user per hour) and keeps deleted
    collections in its trashbin, so re-creating this list on every run would
    trip the limit and pile up trashbin entries.
    """
    return _reused_collection(live_service, os.environ["INTEGRATION_TEST_LIST"], kind="VTODO")


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


def test_recurring_task_completion_behaviour_against_a_real_server(live_service, test_list_name):
    """Records what a real Nextcloud server does with a recurring VTODO's
    RRULE once complete_task is called on it.

    This server's own code (caldav_client.complete_task / mapping.mark_completed)
    only ever sets STATUS/PERCENT-COMPLETE/COMPLETED and never touches RRULE or
    creates a new occurrence - see the pure-unit pin in test_caldav_client.py's
    test_complete_task_leaves_rrule_intact and test_mapping.py's
    test_mark_completed_leaves_wiederholung_intact for that. What is NOT
    verified anywhere else in this suite is how a real Nextcloud Tasks
    server/app treats the resulting object afterwards (e.g. whether the web UI
    materializes/display a "next" occurrence) - this test only records this
    server's own round trip against a live CalDAV backend, not the Nextcloud
    Tasks app's UI behaviour, which remains unverified here.
    """
    uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            titel="nextcloud-task-mcp integration test recurring task",
            faellig_datum="2026-07-20",
            wiederholung="FREQ=DAILY",
            notizen="Created by the automated integration test suite; safe to delete.",
        ),
    )
    try:
        created = live_service.get_task(test_list_name, uid)
        assert created["wiederholung"] == "FREQ=DAILY"

        live_service.complete_task(test_list_name, uid)

        all_tasks = live_service.list_tasks(test_list_name, only_open=False)
        completed = next(t for t in all_tasks if t["uid"] == uid)
        # Recorded, not asserted as "correct" one way or the other: this is
        # exactly the live-server data point the task description asks to
        # capture so it can be confirmed later without writing new code.
        assert completed["status"] == "erledigt"
        assert completed["wiederholung"] == "FREQ=DAILY"

        open_tasks = live_service.list_tasks(test_list_name, only_open=True)
        # No second/"next" occurrence is created as a separate open task by
        # this server - completing a recurring task ends it as far as this
        # server is concerned (see docs/tools.md).
        assert not any(t["uid"] != uid and t["titel"] == created["titel"] for t in open_tasks)
    finally:
        live_service.delete_task(test_list_name, uid)


# ---------------------------------------------------------------------------
# Calendar / event integration tests (VEVENT)
# ---------------------------------------------------------------------------

# Only for object names that must not collide between concurrent runs -
# collections themselves are reused, see `_reused_collection`.
_RUN_SUFFIX = f"{int(time.time())}"
_TEST_CALENDAR = "MCP-Event-Test"


def _reused_collection(live_service, name: str, *, kind: str) -> str:
    """Create `name` only if it is missing, and never delete it.

    Nextcloud rate-limits collection creation to roughly ten per user per
    hour and keeps deleted collections in its trashbin, occupying their URI
    until purged. A suite that created and deleted its calendars every run
    therefore locked itself out after two runs (HTTP 429) - so these
    collections are set up once and left in place for the next run.
    """
    if kind == "VEVENT":
        if not any(entry["name"] == name for entry in live_service.list_calendars()):
            live_service.create_calendar(name, farbe="#00679e")
    elif not any(entry["name"] == name for entry in live_service.list_task_lists()):
        live_service.create_task_list(name)
    return name


@pytest.fixture(scope="session")
def test_calendar(live_service):
    """The VEVENT calendar the whole run writes into."""
    return _reused_collection(live_service, _TEST_CALENDAR, kind="VEVENT")


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
    try:
        fetched = live_service.get_event(test_calendar, uid)
        assert fetched["ganztaegig"] is True
        assert fetched["start"] == "2026-09-02"
        assert fetched["ende"] == "2026-09-03"  # inclusive last day
    finally:
        live_service.delete_event(test_calendar, uid)


def test_recurring_event_expansion_and_exdate(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    series_uid = live_service.create_event(
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
    try:
        assert len(occurrences) == 3  # 4 occurrences minus 1 exception
        assert "2026-09-14T10:00:00+00:00" not in starts
    finally:
        live_service.delete_event(test_calendar, series_uid)


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
    event_uid: str | None = None
    second_uid: str | None = None
    try:
        # Task -> event conversion (timeboxing).
        event_uid = live_service.create_event_from_task(
            test_list_name, task_uid, test_calendar, dauer_minuten=45
        )
        event = live_service.get_event(test_calendar, event_uid)
        assert event["titel"] == "Verknüpfungstest-Aufgabe"
        assert event["start"] == "2026-09-03T16:00:00+00:00"
        assert event["ende"] == "2026-09-03T16:45:00+00:00"
        assert {"uid": task_uid, "beziehung": "zeitblock"} in event["verknuepfte_aufgaben"]

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
        assert {"uid": task_uid, "beziehung": "voraussetzung"} in linked["verknuepfte_aufgaben"]
    finally:
        live_service.delete_task(test_list_name, task_uid)
        for created in (event_uid, second_uid):
            if created:
                live_service.delete_event(test_calendar, created)


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
        live_service.delete_event(test_calendar, event_uid)


# ---------------------------------------------------------------------------
# move / list_tags / batch round-trips (write -> read -> same value)
# ---------------------------------------------------------------------------

_MOVE_TARGET_CALENDAR = "MCP-Move-Ziel-Test"
_MOVE_TARGET_LIST = "MCP-Move-Ziel-Liste-Test"


@pytest.fixture(scope="session")
def move_target_calendar(live_service):
    """A second VEVENT calendar, so a move has somewhere to go."""
    return _reused_collection(live_service, _MOVE_TARGET_CALENDAR, kind="VEVENT")


@pytest.fixture(scope="session")
def move_target_list(live_service):
    """A second VTODO list, to move a task into."""
    return _reused_collection(live_service, _MOVE_TARGET_LIST, kind="VTODO")


def test_move_event_keeps_uid_and_every_property(live_service, test_calendar, move_target_calendar):
    """The whole point of MOVE over create+delete: nothing changes but the collection."""
    from nextcloud_task_mcp import event_mapping

    uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            titel="Verschiebe-Test",
            start="2026-09-10T09:00:00",
            ende="2026-09-10T10:00:00",
            ort="Quelle",
            tags=["MCP-Test"],
            wiederholung="FREQ=WEEKLY;COUNT=3",
            erinnerungen=["-PT15M"],
        ),
    )
    before = live_service.get_event(test_calendar, uid)

    try:
        result = live_service.move_event(test_calendar, uid, move_target_calendar)
        assert result["uid"] == uid
        assert result["von"] == test_calendar
        assert result["nach"] == move_target_calendar
        assert result["methode"] in ("MOVE", "kopiert")

        after = live_service.get_event(move_target_calendar, uid)
        for field in ("uid", "titel", "start", "ende", "ort", "tags", "wiederholung"):
            assert after[field] == before[field], field
        assert after["erinnerungen"] == before["erinnerungen"]

        with pytest.raises(EventNotFoundError):
            live_service.get_event(test_calendar, uid)
    finally:
        for calendar in (move_target_calendar, test_calendar):
            try:
                live_service.delete_event(calendar, uid)
            except Exception:
                pass


def test_move_task_keeps_uid_and_fields(live_service, test_list_name, move_target_list):
    uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            titel="Verschiebe-Test-Aufgabe",
            faellig_datum="2026-09-11",
            prioritaet="hoch",
            tags=["MCP-Test"],
            notizen="Vom Integrationstest erstellt; kann weg.",
        ),
    )
    before = live_service.get_task(test_list_name, uid)

    try:
        result = live_service.move_task(test_list_name, uid, move_target_list)
        assert result["uid"] == uid
        assert result["nach"] == move_target_list

        after = live_service.get_task(move_target_list, uid)
        for field in ("uid", "titel", "faellig_datum", "prioritaet", "tags", "notizen"):
            assert after[field] == before[field], field
    finally:
        for list_name in (move_target_list, test_list_name):
            try:
                live_service.delete_task(list_name, uid)
            except Exception:
                pass


def test_list_tags_counts_a_tag_written_to_both_kinds(live_service, test_list_name, test_calendar):
    """One tag on an event and on a task has to come back as one entry counting two."""
    from nextcloud_task_mcp import event_mapping

    tag = f"MCP-Tag-Test-{_RUN_SUFFIX}"
    event_uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(titel="Tag-Test-Termin", start="2026-09-12", tags=[tag]),
    )
    task_uid = live_service.create_task(
        test_list_name, mapping.TaskFields(titel="Tag-Test-Aufgabe", tags=[tag])
    )

    try:
        tags = live_service.list_tags(calendar_names=[test_calendar], list_names=[test_list_name])
        entry = next(e for e in tags if e["tag"] == tag)
        assert entry["anzahl"] == 2

        # Completed tasks keep counting - that is why include_completed is set.
        live_service.complete_task(test_list_name, task_uid)
        tags_after = live_service.list_tags(
            calendar_names=[test_calendar], list_names=[test_list_name]
        )
        assert next(e for e in tags_after if e["tag"] == tag)["anzahl"] == 2
    finally:
        try:
            live_service.delete_event(test_calendar, event_uid)
        except Exception:
            pass
        try:
            live_service.delete_task(test_list_name, task_uid)
        except Exception:
            pass


def test_batch_update_and_delete_events_round_trip(live_service, test_calendar):
    """Patch three events at once, read each back, then delete them all at once."""
    from nextcloud_task_mcp import event_mapping

    uids = [
        live_service.create_event(
            test_calendar,
            event_mapping.EventFields(
                titel=f"Batch-Test {index}",
                start=f"2026-09-1{index}T08:00:00",
                ende=f"2026-09-1{index}T09:00:00",
            ),
        )
        for index in (3, 4, 5)
    ]

    try:
        result = live_service.update_events(
            test_calendar,
            [*uids, "gibt-es-nicht-mcp-test"],
            event_mapping.EventFields(ort="Batch-Ort", tags=["MCP-Test"]),
        )
        assert result["erfolgreich"] == 3
        assert result["fehlgeschlagen"] == 1
        assert result["ergebnisse"][-1]["status"] == "fehler"

        for uid in uids:
            fetched = live_service.get_event(test_calendar, uid)
            assert fetched["ort"] == "Batch-Ort"
            assert fetched["tags"] == ["MCP-Test"]
    finally:
        deleted = live_service.delete_events(test_calendar, uids)

    assert deleted["erfolgreich"] == 3
    for uid in uids:
        with pytest.raises(EventNotFoundError):
            live_service.get_event(test_calendar, uid)


# ---------------------------------------------------------------------------
# Notes API integration tests
# ---------------------------------------------------------------------------


def _live_notes_service() -> NotesService:
    """Build a NotesService bound to the *currently running* event loop.

    Deliberately not a session-scoped fixture: `NotesService` holds an
    `httpx.AsyncClient`, whose connection pool belongs to the loop it was
    created in. These tests drive coroutines with `asyncio.run`, which opens a
    fresh loop per call, so a service built once and reused across calls dies
    with "Event loop is closed" on the second one. Each test therefore builds
    its service *inside* the single `asyncio.run` that executes its whole
    scenario.
    """
    return NotesService(
        base_url=os.environ["NEXTCLOUD_BASE_URL"],
        username=os.environ["NEXTCLOUD_USERNAME"],
        password=os.environ["NEXTCLOUD_APP_PASSWORD"],
    )


def test_notes_full_lifecycle() -> None:
    """Verify the complete notes workflow against a live Nextcloud instance.

    The whole scenario runs inside ONE `asyncio.run` (see
    `_live_notes_service`): create, read back exactly, replace the content
    wholesale, rename without touching the content, append twice, list with and
    without a category filter, search by content and by title, and delete.
    """

    async def scenario() -> None:
        service = _live_notes_service()
        title = "mcp-notes-test"
        category = "mcp-test"
        initial_content = "Initial content for live notes integration test."

        created = await service.create_note(
            NoteFields(titel=title, kategorie=category, inhalt=initial_content)
        )
        notiz_id: int = created["id"]

        try:
            # 1. create_note -> get_note: content comes back exactly as written.
            fetched = await service.get_note(notiz_id)
            assert fetched["id"] == notiz_id
            assert fetched["titel"] == title
            assert fetched["kategorie"] == category
            assert fetched["inhalt"] == initial_content

            # 2. update_note(inhalt=...) replaces the content wholesale. The
            # payload is deliberately awkward - several lines, umlauts, an
            # emoji, a fenced code block, trailing spaces, a trailing newline -
            # since that is what a rules note actually contains.
            demanding_payload = (
                "Erste Zeile mit Umlauten: ÄÖÜäöüß.\n"
                "Zweite Zeile mit Emoji: 🚀 und 📝.\n"
                "Dritte Zeile mit Leerzeichen am Ende:   \n"
                "```python\n"
                "def hello_world():\n"
                '    print("Hallo Welt!")\n'
                "```\n"
            )
            updated = await service.update_note(notiz_id, NoteFields(inhalt=demanding_payload))
            assert updated["inhalt"] == demanding_payload
            assert (await service.get_note(notiz_id))["inhalt"] == demanding_payload

            # 2b. A note the size of a real rules note must survive a full
            # replacement untruncated - the whole point of this suite is that
            # the deployment's rules live in a note of roughly this size.
            large_payload = "\n".join(
                f"{index:04d} Regelzeile mit Umlauten äöü und etwas Fülltext zur Länge."
                for index in range(300)
            )
            assert len(large_payload) > 12_000
            await service.update_note(notiz_id, NoteFields(inhalt=large_payload))
            assert (await service.get_note(notiz_id))["inhalt"] == large_payload

            # Restore the smaller payload so the append assertions below stay
            # readable.
            await service.update_note(notiz_id, NoteFields(inhalt=demanding_payload))

            # 3. Renaming must not touch the content (the data-loss case).
            # Keeps the "-test" suffix: a stray leftover has to stay
            # identifiable as test data by its name alone.
            renamed = "mcp-notes-renamed-test"
            await service.update_note(notiz_id, NoteFields(titel=renamed))
            after_rename = await service.get_note(notiz_id)
            assert after_rename["titel"] == renamed
            assert after_rename["inhalt"] == demanding_payload

            # 4. Appending twice keeps everything that was there before.
            first_block = "Erster Anhang block-test"
            second_block = "Zweiter Anhang block-test"
            await service.append_note(notiz_id, first_block)
            await service.append_note(notiz_id, second_block)
            appended = await service.get_note(notiz_id)
            assert appended["inhalt"] == (f"{demanding_payload}\n\n{first_block}\n\n{second_block}")

            # 5. Listing finds it, and the category filter returns that
            # category only.
            assert any(note["id"] == notiz_id for note in await service.list_notes())
            in_category = await service.list_notes(category=category)
            assert any(note["id"] == notiz_id for note in in_category)
            assert all(note["kategorie"] == category for note in in_category)

            # 6. Search matches content and title. Upper-case on purpose: the
            # title is lower-case, so a hit proves the search folds case rather
            # than matching by accident.
            assert any(
                note["id"] == notiz_id for note in await service.search_notes("erster anhang")
            )
            assert any(
                note["id"] == notiz_id for note in await service.search_notes("MCP-NOTES-RENAMED")
            )
            assert not any(
                note["id"] == notiz_id
                for note in await service.search_notes("unrelated-random-string-mcp-test-999")
            )

            # 7. Delete the note using delete_note, then verify it is really gone.
            await service.delete_note(notiz_id)
            with pytest.raises(NotizNotFoundError):
                await service.get_note(notiz_id)
        finally:
            # Unconditional cleanup: attempt deletion if step 7 was not reached
            # due to an assertion failure earlier in the try block.
            # We swallow NotizNotFoundError here because if step 7 succeeded,
            # the note is already gone, and cleanup must not blow up the test.
            try:
                await service.delete_note(notiz_id)
            except NotizNotFoundError:
                pass
            await service.aclose()

    run_async(scenario())


def test_get_nonexistent_note_raises_notiz_not_found() -> None:
    """A missing note must surface as NotizNotFoundError, not a raw HTTP error."""

    async def scenario() -> None:
        service = _live_notes_service()
        try:
            with pytest.raises(NotizNotFoundError):
                await service.get_note(999999999)
        finally:
            await service.aclose()

    run_async(scenario())
