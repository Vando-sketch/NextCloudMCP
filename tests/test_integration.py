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
from nextcloud_task_mcp.errors import EventNotFoundError, NoteNotFoundError
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
            title="nextcloud-task-mcp integration test task",
            notes="Created by the automated integration test suite; safe to delete.",
        ),
    )
    try:
        tasks = live_service.list_tasks(test_list_name, only_open=True)
        assert any(task["uid"] == uid for task in tasks)

        fetched = live_service.get_task(test_list_name, uid)
        assert fetched["uid"] == uid

        live_service.update_task(test_list_name, uid, mapping.TaskFields(notes="updated notes"))
        updated = next(t for t in live_service.list_tasks(test_list_name) if t["uid"] == uid)
        assert updated["notes"] == "updated notes"

        live_service.update_task(test_list_name, uid, mapping.TaskFields(clear=("notes",)))
        cleared = live_service.get_task(test_list_name, uid)
        assert cleared["notes"] is None

        live_service.complete_task(test_list_name, uid)
        all_tasks = live_service.list_tasks(test_list_name, only_open=False)
        completed = next(t for t in all_tasks if t["uid"] == uid)
        assert completed["status"] == "completed"
    finally:
        live_service.delete_task(test_list_name, uid)


def test_recurring_task_completion_behaviour_against_a_real_server(live_service, test_list_name):
    """Pins how a real Nextcloud CalDAV backend stores and round-trips a
    recurring VTODO's RRULE across complete_task.

    This is a deliberate assertion, not a mere observation: complete_task
    only ever PUTs STATUS/PERCENT-COMPLETE/COMPLETED (mapping.mark_completed
    never touches RRULE - see the pure-unit pins in test_caldav_client.py's
    test_complete_task_leaves_rrule_intact and test_mapping.py's
    test_mark_completed_leaves_recurrence_intact), and CalDAV storage is a
    dumb object store: nothing server-side rewrites the ICS a PUT sends it or
    conjures a new VTODO object out of a completed one. So what this test
    below asserts - RRULE unchanged, no new task object - is the behaviour
    this server's client code is meant to produce, confirmed end-to-end
    against a live backend instead of only against `Todo()` objects built in
    memory.

    What this test does NOT verify, and cannot from a raw CalDAV client, is
    how the Nextcloud Tasks *app UI* displays or reacts to the resulting
    object (e.g. whether its web frontend synthesizes a "next" occurrence
    for display) - that is a separate, unverified claim; see docs/tools.md.

    If this assertion starts failing, treat it as a signal that this specific
    CalDAV backend's storage/round-trip behaviour changed, and re-evaluate
    deliberately - don't just edit the assertion to make it pass again.
    """
    uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            title="nextcloud-task-mcp integration test recurring task",
            due_date="2026-07-20",
            recurrence="FREQ=DAILY",
            notes="Created by the automated integration test suite; safe to delete.",
        ),
    )
    try:
        created = live_service.get_task(test_list_name, uid)
        assert created["recurrence"] == "FREQ=DAILY"

        live_service.complete_task(test_list_name, uid)

        all_tasks = live_service.list_tasks(test_list_name, only_open=False)
        completed = next(t for t in all_tasks if t["uid"] == uid)
        assert completed["status"] == "completed"
        assert completed.get("recurrence") == "FREQ=DAILY"

        open_tasks = live_service.list_tasks(test_list_name, only_open=True)
        assert not any(t["uid"] != uid and t["title"] == created["title"] for t in open_tasks)
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
            live_service.create_calendar(name, color="#00679e")
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
    created = live_service.create_calendar(name_a, color="#FF7A66")
    try:
        assert created["name"] == name_a

        calendars = live_service.list_calendars()
        entry = next(c for c in calendars if c["name"] == name_a)
        assert "VEVENT" in entry["components"]
        assert entry["color"].upper().startswith("#FF7A66")

        renamed = live_service.update_calendar(name_a, new_display_name=name_b, color="#00679e")
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
            title="Integration-Test-Event",
            start="2026-09-01T14:00:00",
            end="2026-09-01T15:00:00",
            location="Test location",
            description="Created by the integration test; safe to delete.",
            tags=["MCP-Test"],
            status="confirmed",
            reminders=["-PT30M"],
        ),
    )

    fetched = live_service.get_event(test_calendar, uid)
    assert fetched["title"] == "Integration-Test-Event"
    assert fetched["start"] == "2026-09-01T14:00:00+02:00"
    assert fetched["end"] == "2026-09-01T15:00:00+02:00"
    assert fetched["status"] == "confirmed"
    assert fetched["tags"] == ["MCP-Test"]

    listed = live_service.list_events(
        calendar_names=[test_calendar], start="2026-09-01", end="2026-09-01"
    )
    assert any(e["uid"] == uid for e in listed)

    live_service.update_event(
        test_calendar, uid, event_mapping.EventFields(location="New location", status="tentative")
    )
    updated = live_service.get_event(test_calendar, uid)
    assert updated["location"] == "New location"
    assert updated["status"] == "tentative"

    live_service.update_event(test_calendar, uid, event_mapping.EventFields(clear=("location",)))
    assert live_service.get_event(test_calendar, uid)["location"] is None

    live_service.delete_event(test_calendar, uid)
    remaining = live_service.list_events(
        calendar_names=[test_calendar], start="2026-09-01", end="2026-09-01"
    )
    assert not any(e["uid"] == uid for e in remaining)


def test_all_day_event_round_trip(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(title="All_day-Test", start="2026-09-02", end="2026-09-03"),
    )
    try:
        fetched = live_service.get_event(test_calendar, uid)
        assert fetched["all_day"] is True
        assert fetched["start"] == "2026-09-02"
        assert fetched["end"] == "2026-09-03"  # inclusive last day
    finally:
        live_service.delete_event(test_calendar, uid)


def test_recurring_event_expansion_and_exdate(live_service, test_calendar):
    from nextcloud_task_mcp import event_mapping

    series_uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            title="Weekly-Test",
            start="2026-09-07T10:00:00",
            end="2026-09-07T11:00:00",
            recurrence="FREQ=WEEKLY;BYDAY=MO;COUNT=4",
            exception_dates=["2026-09-14T10:00:00"],
        ),
    )

    # Time-range query must match a later occurrence of the series.
    hits = live_service.list_events(
        calendar_names=[test_calendar], start="2026-09-20", end="2026-09-22"
    )
    assert any(e["title"] == "Weekly-Test" for e in hits)

    # Expansion yields the individual occurrences, minus the EXDATE one.
    expanded = live_service.list_events(
        calendar_names=[test_calendar], start="2026-09-01", end="2026-09-30", expand=True
    )
    occurrences = [e for e in expanded if e["title"] == "Weekly-Test"]
    starts = sorted(e["start"] for e in occurrences)
    try:
        assert len(occurrences) == 3  # 4 occurrences minus 1 exception
        assert "2026-09-14T10:00:00+02:00" not in starts
    finally:
        live_service.delete_event(test_calendar, series_uid)


def test_task_event_linking_and_conversion(live_service, test_list_name, test_calendar):
    from nextcloud_task_mcp import event_mapping, mapping

    task_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            title="Link-Test-Task",
            due_date="2026-09-03T16:00:00",
            notes="Created by the integration test; safe to delete.",
        ),
    )
    event_uid: str | None = None
    second_uid: str | None = None
    try:
        # Task -> event conversion (timeboxing).
        event_uid = live_service.create_event_from_task(
            test_list_name, task_uid, test_calendar, duration_minutes=45
        )
        event = live_service.get_event(test_calendar, event_uid)
        assert event["title"] == "Link-Test-Task"
        assert event["start"] == "2026-09-03T16:00:00+02:00"
        assert event["end"] == "2026-09-03T16:45:00+02:00"
        assert {"uid": task_uid, "relation": "time_block"} in event["linked_tasks"]

        # Explicit linking with the other relation.
        second_uid = live_service.create_event(
            test_calendar,
            event_mapping.EventFields(
                title="Prerequisite-Event",
                start="2026-09-02T10:00:00",
                end="2026-09-02T11:00:00",
            ),
        )
        live_service.link_task_to_event(
            test_list_name, task_uid, test_calendar, second_uid, relation="prerequisite"
        )
        linked = live_service.get_event(test_calendar, second_uid)
        assert {"uid": task_uid, "relation": "prerequisite"} in linked["linked_tasks"]
    finally:
        live_service.delete_task(test_list_name, task_uid)
        for created in (event_uid, second_uid):
            if created:
                live_service.delete_event(test_calendar, created)


def test_get_agenda_combines_events_and_tasks(live_service, test_list_name, test_calendar):
    from nextcloud_task_mcp import event_mapping, mapping

    task_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(title="Agenda-Test-Task", due_date="2026-09-04T09:00:00"),
    )
    event_uid = live_service.create_event(
        test_calendar,
        event_mapping.EventFields(
            title="Agenda-Test-Event",
            start="2026-09-04T14:00:00",
            end="2026-09-04T15:00:00",
        ),
    )
    try:
        agenda = live_service.get_agenda("2026-09-04")
        assert any(e["uid"] == event_uid for e in agenda["events"])
        matching_tasks = [t for t in agenda["tasks"] if t["uid"] == task_uid]
        assert matching_tasks and matching_tasks[0]["list"] == test_list_name
    finally:
        live_service.delete_task(test_list_name, task_uid)
        live_service.delete_event(test_calendar, event_uid)


# ---------------------------------------------------------------------------
# move / list_tags / batch round-trips (write -> read -> same value)
# ---------------------------------------------------------------------------

_MOVE_TARGET_CALENDAR = "MCP-Move-Target-Test"
_MOVE_TARGET_LIST = "MCP-Move-Target-List-Test"


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
            title="Move-Test",
            start="2026-09-10T09:00:00",
            end="2026-09-10T10:00:00",
            location="Source",
            tags=["MCP-Test"],
            recurrence="FREQ=WEEKLY;COUNT=3",
            reminders=["-PT15M"],
        ),
    )
    before = live_service.get_event(test_calendar, uid)

    try:
        result = live_service.move_event(test_calendar, uid, move_target_calendar)
        assert result["uid"] == uid
        assert result["from"] == test_calendar
        assert result["to"] == move_target_calendar
        assert result["method"] in ("MOVE", "copied")

        after = live_service.get_event(move_target_calendar, uid)
        for field in ("uid", "title", "start", "end", "location", "tags", "recurrence"):
            assert after[field] == before[field], field
        assert after["reminders"] == before["reminders"]

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
            title="Move-Test-Task",
            due_date="2026-09-11",
            priority="high",
            tags=["MCP-Test"],
            notes="Created by the integration test; safe to delete.",
        ),
    )
    before = live_service.get_task(test_list_name, uid)

    try:
        result = live_service.move_task(test_list_name, uid, move_target_list)
        assert result["uid"] == uid
        assert result["to"] == move_target_list

        after = live_service.get_task(move_target_list, uid)
        for field in ("uid", "title", "due_date", "priority", "tags", "notes"):
            assert after[field] == before[field], field
    finally:
        for list_name in (move_target_list, test_list_name):
            try:
                live_service.delete_task(list_name, uid)
            except Exception:
                pass


def test_move_task_reports_the_subtask_link_it_orphans(
    live_service, test_list_name, move_target_list
):
    """Against a real server, because the warning depends on what Nextcloud
    actually stores: that a moved subtask keeps its RELATED-TO, and that the
    parent left behind is still listed under the source list."""
    parent_uid = live_service.create_task(
        test_list_name, mapping.TaskFields(title="Orphaned-Test-Elternaufgabe")
    )
    child_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(title="Orphaned-Test-Subtask", parent_task=parent_uid),
    )

    try:
        result = live_service.move_task(test_list_name, child_uid, move_target_list)

        assert result["orphaned_subtask_links"] == [
            {
                "uid": child_uid,
                "title": "Orphaned-Test-Subtask",
                "list": move_target_list,
                "fehlende_parent_uid": parent_uid,
            }
        ]
        # The link itself survives the move untouched - that is exactly why it
        # needs reporting rather than fixing.
        moved = live_service.get_task(move_target_list, child_uid)
        assert moved["parent_uid"] == parent_uid
    finally:
        for list_name, uid in (
            (move_target_list, child_uid),
            (test_list_name, child_uid),
            (test_list_name, parent_uid),
        ):
            try:
                live_service.delete_task(list_name, uid)
            except Exception:
                pass


def test_create_task_with_status_completed_reads_back_as_completed(live_service, test_list_name):
    """One call instead of create_task + complete_task, verified end to end."""
    uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(title="Direkt-completed-Test", status="completed"),
    )
    try:
        task = live_service.get_task(test_list_name, uid)
        assert task["status"] == "completed"
        assert task["progress_percent"] == 100
    finally:
        try:
            live_service.delete_task(test_list_name, uid)
        except Exception:
            pass


def test_move_task_reparents_in_the_target_list(live_service, test_list_name, move_target_list):
    """The whole point of the hierarchy shortcut: one call, and the parent is the new one."""
    parent_uid = live_service.create_task(
        move_target_list,
        mapping.TaskFields(title="New parent task", notes="Integration test; safe to delete."),
    )
    child_uid = live_service.create_task(
        test_list_name,
        mapping.TaskFields(
            title="Move-and-Reparent-Test",
            parent_task="old-parent-uid",
            notes="Integration test; safe to delete.",
        ),
    )

    try:
        result = live_service.move_task(test_list_name, child_uid, move_target_list, parent_uid)
        assert result["to"] == move_target_list
        assert result["hierarchy"] == "set"
        # The re-parent repaired the very link this move would have orphaned,
        # and it ran before the scan, so there is nothing left to warn about.
        assert result["orphaned_subtask_links"] == []

        moved = live_service.get_task(move_target_list, child_uid)
        assert moved["parent_uid"] == parent_uid

        # ... and the same call can detach it again.
        detached = live_service.move_task(
            move_target_list,
            child_uid,
            move_target_list,
            clear=("parent_task",),
        )
        assert detached["hierarchy"] == "cleared"
        assert live_service.get_task(move_target_list, child_uid)["parent_uid"] is None
    finally:
        for list_name in (move_target_list, test_list_name):
            for uid in (child_uid, parent_uid):
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
        event_mapping.EventFields(title="Tag-Test-Event", start="2026-09-12", tags=[tag]),
    )
    task_uid = live_service.create_task(
        test_list_name, mapping.TaskFields(title="Tag-Test-Task", tags=[tag])
    )

    try:
        tags = live_service.list_tags(calendar_names=[test_calendar], list_names=[test_list_name])
        entry = next(e for e in tags if e["tag"] == tag)
        assert entry["count"] == 2

        # Completed tasks keep counting - that is why include_completed is set.
        live_service.complete_task(test_list_name, task_uid)
        tags_after = live_service.list_tags(
            calendar_names=[test_calendar], list_names=[test_list_name]
        )
        assert next(e for e in tags_after if e["tag"] == tag)["count"] == 2
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
                title=f"Batch-Test {index}",
                start=f"2026-09-1{index}T08:00:00",
                end=f"2026-09-1{index}T09:00:00",
            ),
        )
        for index in (3, 4, 5)
    ]

    try:
        result = live_service.update_events(
            test_calendar,
            [*uids, "does-not-exist-mcp-test"],
            event_mapping.EventFields(location="Batch-Location", tags=["MCP-Test"]),
        )
        assert result["succeeded"] == 3
        assert result["failed"] == 1
        assert result["results"][-1]["status"] == "error"

        for uid in uids:
            fetched = live_service.get_event(test_calendar, uid)
            assert fetched["location"] == "Batch-Location"
            assert fetched["tags"] == ["MCP-Test"]
    finally:
        deleted = live_service.delete_events(test_calendar, uids)

    assert deleted["succeeded"] == 3
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
            NoteFields(title=title, category=category, content=initial_content)
        )
        note_id: int = created["id"]

        try:
            # 1. create_note -> get_note: content comes back exactly as written.
            fetched = await service.get_note(note_id)
            assert fetched["id"] == note_id
            assert fetched["title"] == title
            assert fetched["category"] == category
            assert fetched["content"] == initial_content

            # 2. update_note(content=...) replaces the content wholesale. The
            # payload is deliberately awkward - several lines, umlauts, an
            # emoji, a fenced code block, trailing spaces, a trailing newline -
            # since that is what a rules note actually contains.
            demanding_payload = (
                "First line with umlauts: ÄÖÜäöüß.\n"
                "Second line with emoji: 🚀 and 📝.\n"
                "Third line with trailing spaces:   \n"
                "```python\n"
                "def hello_world():\n"
                '    print("Hello world!")\n'
                "```\n"
            )
            updated = await service.update_note(note_id, NoteFields(content=demanding_payload))
            assert updated["content"] == demanding_payload
            assert (await service.get_note(note_id))["content"] == demanding_payload

            # 2b. A note the size of a real rules note must survive a full
            # replacement untruncated - the whole point of this suite is that
            # the deployment's rules live in a note of roughly this size.
            large_payload = "\n".join(
                f"{index:04d} Rule line with umlauts äöü and some filler text for length."
                for index in range(300)
            )
            assert len(large_payload) > 12_000
            await service.update_note(note_id, NoteFields(content=large_payload))
            assert (await service.get_note(note_id))["content"] == large_payload

            # Restore the smaller payload so the append assertions below stay
            # readable.
            await service.update_note(note_id, NoteFields(content=demanding_payload))

            # 3. Renaming must not touch the content (the data-loss case).
            # Keeps the "-test" suffix: a stray leftover has to stay
            # identifiable as test data by its name alone.
            renamed = "mcp-notes-renamed-test"
            await service.update_note(note_id, NoteFields(title=renamed))
            after_rename = await service.get_note(note_id)
            assert after_rename["title"] == renamed
            assert after_rename["content"] == demanding_payload

            # 4. Appending twice keeps everything that was there before.
            first_block = "First attachment block-test"
            second_block = "Second attachment block-test"
            await service.append_note(note_id, first_block)
            await service.append_note(note_id, second_block)
            appended = await service.get_note(note_id)
            assert appended["content"] == (
                f"{demanding_payload}\n\n{first_block}\n\n{second_block}"
            )

            # 5. Listing finds it, and the category filter returns that
            # category only.
            assert any(note["id"] == note_id for note in await service.list_notes())
            in_category = await service.list_notes(category=category)
            assert any(note["id"] == note_id for note in in_category)
            assert all(note["category"] == category for note in in_category)

            # 6. Search matches content and title. Upper-case on purpose: the
            # title is lower-case, so a hit proves the search folds case rather
            # than matching by accident.
            assert any(
                note["id"] == note_id for note in await service.search_notes("first attachment")
            )
            assert any(
                note["id"] == note_id for note in await service.search_notes("MCP-NOTES-RENAMED")
            )
            assert not any(
                note["id"] == note_id
                for note in await service.search_notes("unrelated-random-string-mcp-test-999")
            )

            # 7. Delete the note using delete_note, then verify it is really gone.
            await service.delete_note(note_id)
            with pytest.raises(NoteNotFoundError):
                await service.get_note(note_id)
        finally:
            # Unconditional cleanup: attempt deletion if step 7 was not reached
            # due to an assertion failure earlier in the try block.
            # We swallow NoteNotFoundError here because if step 7 succeeded,
            # the note is already gone, and cleanup must not blow up the test.
            try:
                await service.delete_note(note_id)
            except NoteNotFoundError:
                pass
            await service.aclose()

    run_async(scenario())


def test_get_nonexistent_note_raises_note_not_found() -> None:
    """A missing note must surface as NoteNotFoundError, not a raw HTTP error."""

    async def scenario() -> None:
        service = _live_notes_service()
        try:
            with pytest.raises(NoteNotFoundError):
                await service.get_note(999999999)
        finally:
            await service.aclose()

    run_async(scenario())
