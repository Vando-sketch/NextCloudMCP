"""Static bearer token authentication for the MCP server.

FastMCP's built-in StaticTokenVerifier is explicitly documented as
development-only, so authentication is instead enforced via a small
Middleware hook that rejects any request before it reaches tool logic.
"""

from __future__ import annotations

import hmac

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp import McpError
from mcp.types import ErrorData

_UNAUTHORIZED_CODE = -32001


class BearerTokenMiddleware(Middleware):
    """Rejects any MCP request that doesn't carry the expected bearer token.

    Runs on every request via the on_request hook, so an unauthenticated
    client never reaches tool logic or the CalDAV backend.
    """

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def on_request(self, context: MiddlewareContext, call_next):
        headers = get_http_headers() or {}
        auth_header = headers.get("authorization", "")
        scheme, _, token = auth_header.partition(" ")

        valid = (
            scheme.lower() == "bearer"
            and bool(token)
            and hmac.compare_digest(token, self._expected_token)
        )
        if not valid:
            raise McpError(
                ErrorData(code=_UNAUTHORIZED_CODE, message="Missing or invalid bearer token.")
            )
        return await call_next(context)
