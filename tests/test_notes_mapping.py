"""Unit tests for the German field <-> Notes API JSON translation, no HTTP involved."""

from __future__ import annotations

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.notes_mapping import (
    NoteFields,
    parse_note,
    parse_note_summary,
    to_request_body,
)


def test_to_request_body_includes_only_given_fields():
    body = to_request_body(NoteFields(titel="Projekt X"))
    assert body == {"title": "Projekt X"}


def test_to_request_body_maps_all_fields():
    body = to_request_body(
        NoteFields(titel="Projekt X", kategorie="Arbeit", inhalt="Text", favorit=True)
    )
    assert body == {
        "title": "Projekt X",
        "category": "Arbeit",
        "content": "Text",
        "favorite": True,
    }


def test_to_request_body_empty_fields_yields_empty_body():
    assert to_request_body(NoteFields()) == {}


def test_to_request_body_false_favorit_is_included():
    # Regression: `if fields.favorit is not None` must not collapse to a
    # plain truthiness check - explicitly un-favoriting (favorit=False) has
    # to reach the request body, not be treated as "not given".
    body = to_request_body(NoteFields(favorit=False))
    assert body == {"favorite": False}


def test_parse_note_summary_maps_fields():
    note = {
        "id": 42,
        "title": "Projekt X",
        "category": "Arbeit",
        "favorite": True,
        "modified": 1735689600,  # 2025-01-01T00:00:00Z
        "content": "should be ignored by the summary parser",
    }
    result = parse_note_summary(note)
    assert result == {
        "id": 42,
        "titel": "Projekt X",
        "kategorie": "Arbeit",
        "favorit": True,
        # Formatted in the server's default timezone, like every other
        # timestamp this server returns (+01:00 on 1 January in Europe/Berlin).
        "geaendert": "2025-01-01T01:00:00+01:00",
    }
    assert "inhalt" not in result


def test_parse_note_summary_modified_follows_the_default_timezone():
    """A note's `geaendert` is an output timestamp like any other.

    Hardcoding UTC here contradicts the rule the rest of the server follows
    and makes two timestamps in the same answer (a task's due date, a note's
    modification time) read as if they were hours apart.
    """
    note = {"id": 1, "title": "Foo", "modified": 1735689600}
    mapping.set_default_timezone("America/New_York")
    assert parse_note_summary(note)["geaendert"] == "2024-12-31T19:00:00-05:00"


def test_parse_note_summary_empty_category_becomes_none():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False, "modified": 0}
    assert parse_note_summary(note)["kategorie"] is None


def test_parse_note_summary_missing_modified_is_none():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False}
    assert parse_note_summary(note)["geaendert"] is None


def test_parse_note_includes_content_and_readonly():
    note = {
        "id": 1,
        "title": "Foo",
        "category": "",
        "favorite": False,
        "modified": 0,
        "content": "Full text",
        "readonly": True,
    }
    result = parse_note(note)
    assert result["inhalt"] == "Full text"
    assert result["schreibgeschuetzt"] is True


def test_parse_note_missing_content_is_empty_string():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False, "modified": 0}
    assert parse_note(note)["inhalt"] == ""
