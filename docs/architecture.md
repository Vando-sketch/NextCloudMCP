# Architecture

## Module overview

```
src/nextcloud_task_mcp/
├── server.py         FastMCP app: tool definitions, error-to-ToolError translation, entrypoint
├── auth.py           Bearer token middleware (runs before any tool logic)
├── caldav_client.py  CalDavService: persistent CalDAV connection + task CRUD
├── mapping.py        German task fields <-> iCalendar VTODO properties
├── config.py         Settings.from_env(): all configuration from environment variables
└── errors.py         User-facing exception hierarchy (TaskMcpError + subclasses)
```

## Request flow

1. An MCP request arrives over Streamable HTTP (`/mcp`).
2. `BearerTokenMiddleware.on_request` checks the `Authorization: Bearer <token>` header
   against `MCP_AUTH_TOKEN` using a constant-time comparison. On mismatch it raises a
   JSON-RPC error (code `-32001`) — tool logic and CalDAV are never reached.
3. The tool function in `server.py` forwards to `CalDavService` through `_call()`, which
   translates any `TaskMcpError` into a clean `ToolError` message. Unexpected exceptions
   are logged server-side with a full traceback but surface to the client only as
   "An unexpected internal error occurred."
4. `CalDavService` performs the CalDAV operation, reusing one `DAVClient` and one cached
   `Principal` for all requests (the principal lookup is the expensive discovery step;
   it happens once, guarded by a lock).
5. `mapping.py` converts between the tool-level German field names and raw iCalendar
   properties in both directions.

## Design decisions

**Connection reuse.** `DAVClient` and the discovered `Principal` are created once and
kept for the process lifetime. Calendars and todos are still looked up per request —
they are cheap single requests, and caching them would risk acting on stale state.

**Auth: custom middleware instead of FastMCP's built-ins.** FastMCP ships a
`StaticTokenVerifier`, but its documentation marks it development-only. The threat model
here (single user, private tailnet, TLS terminated by Tailscale) doesn't justify a full
OAuth/JWT setup, so a small `on_request` middleware with `hmac.compare_digest` is the
right size: it rejects unauthenticated requests before any tool code runs, and nothing
more.

**caldav 3.x with the classic API.** The project pins `caldav>=3.0,<4` but uses the
long-standing v2-style API (`DAVClient(...)`, `principal.calendars()`,
`calendar.save_todo(ical=...)`, `.icalendar_component`). These are kept as compatibility
aliases until at least caldav v5, and are the most battle-tested code paths. Note that
caldav 3.x uses `niquests` (a `requests` fork) as its HTTP backend; error translation in
`caldav_client.py` imports its exceptions with a `requests` fallback.

**Property replacement, not appending.** `icalendar`'s `Component.add()` *appends* when a
property already exists, which would silently produce duplicate `SUMMARY`/`DUE`/... lines
on every update. `mapping._set()` therefore deletes the old value first. This is what
makes `update_task`'s "only touch fields that were passed" contract safe.

**Reminder semantics.** Relative `VALARM` triggers are anchored with
`TRIGGER;RELATED=END` (i.e. relative to `DUE`) when the task has a due date, falling back
to `RELATED=START`, matching Nextcloud Tasks' own "before due date" behavior. Absolute
triggers are converted to UTC as RFC 5545 requires. Setting `erinnerungen` on update
replaces all existing alarms — merging alarm lists has no unambiguous semantics.

**UID generation client-side.** `create_task` generates the UID itself (uuid4) instead of
reading it back from the server, saving a round-trip and making the return value reliable
across servers.

**Error hierarchy.** `caldav`/HTTP exceptions never cross module boundaries:
`caldav_client._translate()` maps them onto the small `errors.py` hierarchy
(`AuthenticationFailedError`, `ConnectionFailedError`, `TaskListNotFoundError`,
`TaskNotFoundError`, ...), and `server._call()` maps those onto `ToolError`. The MCP
client only ever sees one-line messages.

**German tool schema on purpose.** The tool parameters (`fällig_datum`, `priorität`,
`übergeordnete_aufgabe`, ...) are the literal MCP schema field names. Since the server is
operated in German via Claude, keeping the schema in German gives the model the most
direct mapping from user language to tool arguments. Code, comments and docs stay
English.

## Testing strategy

- **Unit tests** (`tests/test_*.py` except integration): `caldav.DAVClient` is patched
  out entirely; mapping tests work on real `icalendar` components so the actual
  serialization is exercised. Tool tests call the registered FastMCP tool functions with
  a mocked `CalDavService` and assert both delegation and error translation.
- **Integration tests** (`tests/test_integration.py`): full create→list→update→complete→
  delete lifecycle against a real Nextcloud instance; skipped unless
  `RUN_INTEGRATION_TESTS=1`. See the README for how to run them.
