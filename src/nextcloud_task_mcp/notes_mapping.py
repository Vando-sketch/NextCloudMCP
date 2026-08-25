"""Translation between the server's German note fields and the Notes API's JSON note objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .mapping import format_datetime_output


@dataclass(frozen=True)
class NoteFields:
    """The optional note fields shared by create_note/update_note.

    A field left as `None` means "leave unchanged" (update_note) or "not
    set" (create_note) - unlike CalDAV's TaskFields/EventFields, the Notes
    API itself accepts a partial JSON body for PUT, so `to_request_body`
    can build that body directly with no separate "clear" concept: a note
    field can't be cleared back to absent, only overwritten (e.g. with an
    empty string).
    """

    title: str | None = None
    category: str | None = None
    content: str | None = None
    favorite: bool | None = None


def to_request_body(fields: NoteFields) -> dict[str, Any]:
    """Build the JSON body for a Notes API POST/PUT from the given fields."""
    body: dict[str, Any] = {}
    if fields.title is not None:
        body["title"] = fields.title
    if fields.category is not None:
        body["category"] = fields.category
    if fields.content is not None:
        body["content"] = fields.content
    if fields.favorite is not None:
        body["favorite"] = fields.favorite
    return body


def _format_modified(modified: Any) -> str | None:
    """Convert the API's `modified` Unix timestamp to an ISO 8601 string.

    Matches the ISO 8601 date/datetime convention used everywhere else in
    this server (mapping.py, event_mapping.py) rather than exposing a raw
    Unix timestamp - including its timezone rule: the instant is the one the
    Notes API reports (a Unix timestamp is unambiguous), rendered in the
    server's default timezone (`MCP_DEFAULT_TIMEZONE`), so a note's
    modification time and a task's due date in the same answer are readable
    against each other.
    """
    if not isinstance(modified, (int, float)):
        return None
    return format_datetime_output(datetime.fromtimestamp(modified, tz=timezone.utc))


def parse_note_summary(note: dict[str, Any]) -> dict[str, Any]:
    """Parse a Notes API note object into the server's German summary dict.

    Used for list_notes/search_notes, which deliberately omit `content`
    (content) to keep a listing cheap - `get_note` returns the full note.
    """
    return {
        "id": note["id"],
        "title": note.get("title"),
        "category": note.get("category") or None,
        "favorite": bool(note.get("favorite", False)),
        "modified": _format_modified(note.get("modified")),
    }


def parse_note(note: dict[str, Any]) -> dict[str, Any]:
    """Parse a Notes API note object into the server's full German note dict."""
    result = parse_note_summary(note)
    result["content"] = note.get("content") or ""
    result["read_only"] = bool(note.get("readonly", False))
    return result
