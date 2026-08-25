"""Unit tests for the note field <-> Notes API JSON translation, no HTTP involved."""

from __future__ import annotations

from nextcloud_task_mcp import mapping
from nextcloud_task_mcp.notes_mapping import (
    NoteFields,
    parse_note,
    parse_note_summary,
    to_request_body,
)


def test_to_request_body_includes_only_given_fields():
    body = to_request_body(NoteFields(title="Project X"))
    assert body == {"title": "Project X"}


def test_to_request_body_maps_all_fields():
    body = to_request_body(
        NoteFields(title="Project X", category="Work", content="Text", favorite=True)
    )
    assert body == {
        "title": "Project X",
        "category": "Work",
        "content": "Text",
        "favorite": True,
    }


def test_to_request_body_empty_fields_yields_empty_body():
    assert to_request_body(NoteFields()) == {}


def test_to_request_body_false_favorite_is_included():
    # Regression: `if fields.favorite is not None` must not collapse to a
    # plain truthiness check - explicitly un-favoriting (favorite=False) has
    # to reach the request body, not be treated as "not given".
    body = to_request_body(NoteFields(favorite=False))
    assert body == {"favorite": False}


def test_parse_note_summary_maps_fields():
    note = {
        "id": 42,
        "title": "Project X",
        "category": "Work",
        "favorite": True,
        "modified": 1735689600,  # 2025-01-01T00:00:00Z
        "content": "should be ignored by the summary parser",
    }
    result = parse_note_summary(note)
    assert result == {
        "id": 42,
        "title": "Project X",
        "category": "Work",
        "favorite": True,
        # Formatted in the server's default timezone, like every other
        # timestamp this server returns (+01:00 on 1 January in Europe/Berlin).
        "modified": "2025-01-01T01:00:00+01:00",
    }
    assert "content" not in result


def test_parse_note_summary_modified_follows_the_default_timezone():
    """A note's `changed` is an output timestamp like any other.

    Hardcoding UTC here contradicts the rule the rest of the server follows
    and makes two timestamps in the same answer (a task's due date, a note's
    modification time) read as if they were hours apart.
    """
    note = {"id": 1, "title": "Foo", "modified": 1735689600}
    mapping.set_default_timezone("America/New_York")
    assert parse_note_summary(note)["modified"] == "2024-12-31T19:00:00-05:00"


def test_parse_note_summary_empty_category_becomes_none():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False, "modified": 0}
    assert parse_note_summary(note)["category"] is None


def test_parse_note_summary_missing_modified_is_none():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False}
    assert parse_note_summary(note)["modified"] is None


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
    assert result["content"] == "Full text"
    assert result["read_only"] is True


def test_parse_note_missing_content_is_empty_string():
    note = {"id": 1, "title": "Foo", "category": "", "favorite": False, "modified": 0}
    assert parse_note(note)["content"] == ""
