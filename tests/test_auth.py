"""Unit tests for the bearer token middleware, without starting a real server."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from mcp import McpError

from nextcloud_task_mcp.auth import BearerTokenMiddleware


def _run(coro):
    return asyncio.run(coro)


async def _call_next(_context):
    return "ok"


def test_correct_token_passes():
    middleware = BearerTokenMiddleware("secret-token")
    with patch(
        "nextcloud_task_mcp.auth.get_http_headers",
        return_value={"authorization": "Bearer secret-token"},
    ):
        result = _run(middleware.on_request(None, _call_next))
    assert result == "ok"


def test_missing_header_rejected():
    middleware = BearerTokenMiddleware("secret-token")
    with patch("nextcloud_task_mcp.auth.get_http_headers", return_value={}):
        with pytest.raises(McpError):
            _run(middleware.on_request(None, _call_next))


def test_wrong_token_rejected():
    middleware = BearerTokenMiddleware("secret-token")
    with patch(
        "nextcloud_task_mcp.auth.get_http_headers",
        return_value={"authorization": "Bearer wrong-token"},
    ):
        with pytest.raises(McpError):
            _run(middleware.on_request(None, _call_next))


def test_non_bearer_scheme_rejected():
    middleware = BearerTokenMiddleware("secret-token")
    with patch(
        "nextcloud_task_mcp.auth.get_http_headers",
        return_value={"authorization": "Basic secret-token"},
    ):
        with pytest.raises(McpError):
            _run(middleware.on_request(None, _call_next))
