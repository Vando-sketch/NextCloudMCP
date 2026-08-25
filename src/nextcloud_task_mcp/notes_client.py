"""Thin async wrapper around Nextcloud's Notes REST API.

Deliberately separate from caldav_client.py: the Notes app exposes a plain
JSON REST API (see https://github.com/nextcloud/notes/blob/main/docs/api/v1.md),
not CalDAV, so it has nothing to do with the `caldav` library, its VTODO/VEVENT
parsing, or its blocking-call/worker-thread handling. `NotesService` talks to
it directly over `httpx`, natively async - no `anyio.to_thread` offloading is
needed since there's no blocking library call to move off the event loop here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import (
    AuthenticationFailedError,
    ConnectionFailedError,
    InvalidNotizDataError,
    NotizNotFoundError,
    TaskMcpError,
)
from .notes_mapping import NoteFields, parse_note, parse_note_summary, to_request_body
from .notes_sections import replace_section

logger = logging.getLogger(__name__)


def _translate(exc: Exception) -> TaskMcpError:
    """Convert an httpx transport-level exception into a clean TaskMcpError.

    Mirrors caldav_client.py's `_translate` - messages here are forwarded
    verbatim to MCP clients, so they must never embed raw exception text.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ConnectionFailedError("Could not reach the Nextcloud Notes API (request timed out).")
    if isinstance(exc, httpx.TransportError):
        return ConnectionFailedError("Could not reach the Nextcloud Notes API (connection failed).")
    logger.warning("Unexpected error talking to the Notes API", exc_info=exc)
    return TaskMcpError("An unexpected error occurred talking to the Nextcloud Notes API.")


def _raise_for_status(response: httpx.Response) -> None:
    """Translate a non-2xx Notes API response into the matching TaskMcpError."""
    if response.status_code == 401:
        raise AuthenticationFailedError(
            "Nextcloud rejected the Notes API credentials (check username/app password)."
        )
    if response.status_code == 404:
        raise NotizNotFoundError("The requested note was not found.")
    if response.status_code == 400:
        raise InvalidNotizDataError("Nextcloud rejected the note data as invalid.")
    if response.status_code == 507:
        raise TaskMcpError("Nextcloud storage quota exceeded - the note could not be saved.")
    if response.is_error:
        logger.warning("Notes API request failed: %s %s", response.status_code, response.text)
        raise TaskMcpError("The Notes API request failed on the Nextcloud server.")


class NotesService:
    """Holds one reused HTTP client and exposes Notes CRUD operations on it."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` is only ever given in tests (httpx.MockTransport), to
        # exercise this class with no real network access - production
        # callers rely on httpx's own default transport.
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/index.php/apps/notes/api/v1/",
            auth=(username, password),
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client and its connection pool.

        The long-lived server never calls this - its client lives as long as
        the process does. It exists for callers with a bounded lifetime (the
        integration tests, which run each scenario in its own event loop):
        leaving the pool to be garbage-collected after that loop is gone
        raises `ResourceWarning` and closes sockets on a dead loop.
        """
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except Exception as exc:
            raise _translate(exc) from exc
        _raise_for_status(response)
        return response

    async def list_notes(self, category: str | None = None) -> list[dict[str, Any]]:
        """Return all notes as German summary dicts (no `inhalt`/content).

        `category` filters server-side; `exclude=content` keeps the response
        cheap for a plain listing - `get_note` fetches a single note in full.
        """
        params: dict[str, str] = {"exclude": "content"}
        if category is not None:
            params["category"] = category
        response = await self._request("GET", "notes", params=params)
        return [parse_note_summary(note) for note in response.json()]

    async def get_note(self, notiz_id: int) -> dict[str, Any]:
        """Return a single note, including its full content."""
        response = await self._request("GET", f"notes/{notiz_id}")
        return parse_note(response.json())

    async def delete_note(self, notiz_id: int) -> None:
        """Permanently delete a note by id."""
        await self._request("DELETE", f"notes/{notiz_id}")

    async def create_note(self, fields: NoteFields) -> dict[str, Any]:
        """Create a new note and return it, parsed like `get_note`."""
        if fields.titel is None:
            raise InvalidNotizDataError("titel is required to create a note.")
        response = await self._request("POST", "notes", json=to_request_body(fields))
        return parse_note(response.json())

    async def update_note(self, notiz_id: int, fields: NoteFields) -> dict[str, Any]:
        """Update only the given (non-None) fields of an existing note.

        The Notes API's PUT accepts a partial JSON body directly (unlike
        CalDAV, there's no separate read-modify-write needed here), so this
        is a single request.
        """
        body = to_request_body(fields)
        if not body:
            raise InvalidNotizDataError("At least one field must be given to update a note.")
        response = await self._request("PUT", f"notes/{notiz_id}", json=body)
        return parse_note(response.json())

    async def append_note(self, notiz_id: int, text: str) -> dict[str, Any]:
        """Append `text` to a note's existing content (read, then write back).

        Not an atomic server-side append - the Notes API has no such
        operation - so a concurrent edit to the same note between the read
        and the write may be lost. This server doesn't use the API's
        ETag/If-Match support (v1.2+), which would only guard against that
        for a single-writer-at-a-time workflow anyway; out of scope here.
        """
        current = await self.get_note(notiz_id)
        separator = "\n\n" if current["inhalt"] else ""
        new_content = f"{current['inhalt']}{separator}{text}"
        return await self.update_note(notiz_id, NoteFields(inhalt=new_content))

    async def replace_in_note(self, notiz_id: int, alt: str, neu: str) -> dict[str, Any]:
        """Replace exactly one occurrence of `alt` in a note's content with `neu`.

        `alt` must match the current content exactly once: zero matches and
        multiple matches are both rejected, so a caller can never silently
        patch the wrong spot. Like `append_note`, this is a read-then-write,
        not an atomic server-side operation - a concurrent edit between the
        read and the write may be lost.
        """
        if not alt:
            raise InvalidNotizDataError("alt must not be empty.")
        current = await self.get_note(notiz_id)
        count = current["inhalt"].count(alt)
        if count == 0:
            raise InvalidNotizDataError(
                "The text to replace (alt) was not found in the note's content."
            )
        if count > 1:
            raise InvalidNotizDataError(
                f"The text to replace (alt) occurs {count} times in the note's content - "
                "include more surrounding context so it matches exactly once."
            )
        new_content = current["inhalt"].replace(alt, neu, 1)
        return await self.update_note(notiz_id, NoteFields(inhalt=new_content))

    async def replace_note_section(
        self, notiz_id: int, abschnitt: str, inhalt: str
    ) -> dict[str, Any]:
        """Replace one Markdown section (heading line + body) of a note's content.

        `abschnitt` selects exactly one ATX heading and `inhalt` becomes the
        new section text, heading line included - see
        `notes_sections.replace_section` for the matching rules. Like
        `append_note`, this is a read-then-write, not atomic against
        concurrent edits.
        """
        current = await self.get_note(notiz_id)
        new_content = replace_section(current["inhalt"], abschnitt, inhalt)
        return await self.update_note(notiz_id, NoteFields(inhalt=new_content))

    async def search_notes(
        self, suchtext: str, kategorie: str | None = None
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search over title and content.

        The Notes API has no server-side full-text search, so this fetches
        the (optionally category-filtered) notes with content included and
        filters client-side; the result is summary dicts, same shape as
        `list_notes`.
        """
        params: dict[str, str] = {}
        if kategorie is not None:
            params["category"] = kategorie
        response = await self._request("GET", "notes", params=params)
        needle = suchtext.casefold()
        matches = [
            note
            for note in response.json()
            if needle in (note.get("title") or "").casefold()
            or needle in (note.get("content") or "").casefold()
        ]
        return [parse_note_summary(note) for note in matches]
