"""Unit tests for NotesService with the Notes REST API mocked via httpx.MockTransport.

No real network access - `httpx.MockTransport` intercepts every request, mirroring
how test_caldav_client.py mocks the `caldav` library's DAVClient.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nextcloud_task_mcp.errors import (
    AuthenticationFailedError,
    ConnectionFailedError,
    InvalidNotizDataError,
    NotizNotFoundError,
    TaskMcpError,
)
from nextcloud_task_mcp.notes_client import NotesService
from nextcloud_task_mcp.notes_mapping import NoteFields


def _run(coro: Any) -> Any:
    """`asyncio.run` wrapper shared by every async call below (mirrors tests/test_auth.py)."""
    return asyncio.run(coro)


def _service(handler: Callable[[httpx.Request], httpx.Response]) -> NotesService:
    return NotesService(
        base_url="https://cloud.example.com",
        username="u",
        password="p",
        transport=httpx.MockTransport(handler),
    )


def _json_response(status_code: int, body: Any) -> httpx.Response:
    return httpx.Response(status_code, json=body)


# --- base URL / auth wiring ---


def test_base_url_targets_notes_api_v1():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(200, [])

    service = _service(handler)
    _run(service.list_notes())

    assert str(captured[0].url).startswith(
        "https://cloud.example.com/index.php/apps/notes/api/v1/notes"
    )
    assert captured[0].headers["authorization"].startswith("Basic ")


def test_trailing_slash_on_base_url_is_normalized():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(200, [])

    service = NotesService(
        base_url="https://cloud.example.com/",
        username="u",
        password="p",
        transport=httpx.MockTransport(handler),
    )
    _run(service.list_notes())

    assert "//index.php" not in str(captured[0].url)


# --- list_notes ---


def test_list_notes_excludes_content_and_parses_summaries():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            200,
            [{"id": 1, "title": "Foo", "category": "", "favorite": False, "modified": 0}],
        )

    service = _service(handler)
    result = _run(service.list_notes())

    assert result == [
        {
            "id": 1,
            "titel": "Foo",
            "kategorie": None,
            "favorit": False,
            # Output timestamps carry the server's default timezone.
            "geaendert": "1970-01-01T01:00:00+01:00",
        }
    ]
    assert captured[0].url.params["exclude"] == "content"
    assert "category" not in captured[0].url.params


def test_list_notes_passes_category_filter():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(200, [])

    service = _service(handler)
    _run(service.list_notes(category="Arbeit"))

    assert captured[0].url.params["category"] == "Arbeit"


# --- get_note ---


def test_get_note_parses_full_note():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/notes/42")
        return _json_response(
            200,
            {
                "id": 42,
                "title": "Foo",
                "category": "Arbeit",
                "favorite": True,
                "modified": 0,
                "content": "Body",
                "readonly": False,
            },
        )

    service = _service(handler)
    result = _run(service.get_note(42))

    assert result["id"] == 42
    assert result["inhalt"] == "Body"
    assert result["schreibgeschuetzt"] is False


def test_get_note_not_found_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    service = _service(handler)
    with pytest.raises(NotizNotFoundError):
        _run(service.get_note(999))


# --- delete_note ---


def test_delete_note_issues_delete_request():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    service = _service(handler)
    _run(service.delete_note(42))

    assert captured[0].method == "DELETE"
    assert captured[0].url.path.endswith("/notes/42")


def test_delete_note_not_found_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    service = _service(handler)
    with pytest.raises(NotizNotFoundError):
        _run(service.delete_note(999))


def test_delete_note_401_raises_authentication_failed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    service = _service(handler)
    with pytest.raises(AuthenticationFailedError):
        _run(service.delete_note(42))


# --- create_note ---


def test_create_note_requires_titel():
    service = _service(lambda request: _json_response(200, {}))
    with pytest.raises(InvalidNotizDataError, match="titel"):
        _run(service.create_note(NoteFields()))


def test_create_note_posts_request_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            200,
            {
                "id": 1,
                "title": "Foo",
                "category": "",
                "favorite": False,
                "modified": 0,
                "content": "",
            },
        )

    service = _service(handler)
    _run(service.create_note(NoteFields(titel="Foo")))

    assert captured[0].method == "POST"
    assert json.loads(captured[0].content) == {"title": "Foo"}


# --- update_note ---


def test_update_note_requires_at_least_one_field():
    service = _service(lambda request: _json_response(200, {}))
    with pytest.raises(InvalidNotizDataError):
        _run(service.update_note(1, NoteFields()))


def test_update_note_sends_partial_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            200,
            {
                "id": 1,
                "title": "New",
                "category": "",
                "favorite": False,
                "modified": 0,
                "content": "",
            },
        )

    service = _service(handler)
    _run(service.update_note(1, NoteFields(titel="New")))

    assert captured[0].method == "PUT"
    assert captured[0].url.path.endswith("/notes/1")
    assert json.loads(captured[0].content) == {"title": "New"}


# --- append_note ---


def test_append_note_reads_then_writes_with_separator():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return _json_response(
                200,
                {
                    "id": 1,
                    "title": "Foo",
                    "category": "",
                    "favorite": False,
                    "modified": 0,
                    "content": "Alt",
                },
            )
        return _json_response(
            200,
            {
                "id": 1,
                "title": "Foo",
                "category": "",
                "favorite": False,
                "modified": 0,
                "content": "Alt\n\nNeu",
            },
        )

    service = _service(handler)
    result = _run(service.append_note(1, "Neu"))

    put_call = next(c for c in calls if c.method == "PUT")
    assert json.loads(put_call.content) == {"content": "Alt\n\nNeu"}
    assert result["inhalt"] == "Alt\n\nNeu"


def test_append_note_to_empty_note_has_no_leading_separator():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return _json_response(
                200,
                {
                    "id": 1,
                    "title": "Foo",
                    "category": "",
                    "favorite": False,
                    "modified": 0,
                    "content": "",
                },
            )
        return _json_response(
            200,
            {
                "id": 1,
                "title": "Foo",
                "category": "",
                "favorite": False,
                "modified": 0,
                "content": "Neu",
            },
        )

    service = _service(handler)
    _run(service.append_note(1, "Neu"))

    put_call = next(c for c in calls if c.method == "PUT")
    assert json.loads(put_call.content) == {"content": "Neu"}


# --- search_notes ---


def test_search_notes_filters_by_title_and_content_case_insensitively():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            [
                {
                    "id": 1,
                    "title": "Projekt X",
                    "category": "",
                    "favorite": False,
                    "modified": 0,
                    "content": "",
                },
                {
                    "id": 2,
                    "title": "Anderes",
                    "category": "",
                    "favorite": False,
                    "modified": 0,
                    "content": "erwähnt projekt x hier",
                },
                {
                    "id": 3,
                    "title": "Unrelated",
                    "category": "",
                    "favorite": False,
                    "modified": 0,
                    "content": "nothing",
                },
            ],
        )

    service = _service(handler)
    result = _run(service.search_notes("projekt x"))

    assert {note["id"] for note in result} == {1, 2}
    assert "inhalt" not in result[0]


def test_search_notes_passes_category_filter():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(200, [])

    service = _service(handler)
    _run(service.search_notes("x", kategorie="Arbeit"))

    assert captured[0].url.params["category"] == "Arbeit"
    assert "exclude" not in captured[0].url.params


# --- error translation ---


def test_401_becomes_authentication_failed_error():
    service = _service(lambda request: httpx.Response(401))
    with pytest.raises(AuthenticationFailedError):
        _run(service.list_notes())


def test_400_becomes_invalid_notiz_data_error():
    service = _service(lambda request: httpx.Response(400))
    with pytest.raises(InvalidNotizDataError):
        _run(service.list_notes())


def test_507_becomes_generic_task_mcp_error():
    service = _service(lambda request: httpx.Response(507))
    with pytest.raises(TaskMcpError, match="quota"):
        _run(service.list_notes())


def test_unexpected_status_becomes_generic_task_mcp_error():
    service = _service(lambda request: httpx.Response(500))
    with pytest.raises(TaskMcpError, match="Notes API request failed"):
        _run(service.list_notes())


def test_connection_error_becomes_connection_failed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    service = _service(handler)
    with pytest.raises(ConnectionFailedError):
        _run(service.list_notes())


def test_timeout_becomes_connection_failed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    service = _service(handler)
    with pytest.raises(ConnectionFailedError):
        _run(service.list_notes())
