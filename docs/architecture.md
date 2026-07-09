# Architecture

## Module overview

```
src/nextcloud_task_mcp/
├── server.py         FastMCP app: tool definitions, error-to-ToolError translation, entrypoint
├── personal_auth.py  OAuth 2.1 provider (vendored from crumrine/fastmcp-personal-auth)
├── caldav_client.py  CalDavService: persistent CalDAV connection + task CRUD
├── mapping.py        German task fields <-> iCalendar VTODO properties
├── config.py         Settings.from_env(): all configuration from environment variables
└── errors.py         User-facing exception hierarchy (TaskMcpError + subclasses)
```

## Request flow

1. An MCP request arrives over Streamable HTTP (`/mcp`).
2. FastMCP's own auth middleware (wired up via `auth=PersonalAuthProvider(...)` in
   `build_server`) requires a valid OAuth `Authorization: Bearer <access-token>` header,
   issued through the provider's `/register` → `/authorize` → `/token` flow. On a
   missing/invalid/expired token it returns `401` before tool logic or CalDAV is ever
   reached. See the [README](../README.md#authentication) for the full auth model.
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

**Auth: OAuth 2.1 via a vendored `PersonalAuthProvider`, not a static token.** Claude's
connector UI (web, mobile, Desktop, Cowork) only exposes OAuth fields for custom
connectors - there's no field for a raw bearer token, so a static-token middleware (the
project's original approach) can no longer be registered there at all. `PersonalAuthProvider`
(`personal_auth.py`) fills the gap FastMCP itself leaves between `InMemoryOAuthProvider`
(test-only, no persistence) and `OAuthProxy` (requires an external IdP like Google/GitHub):
Dynamic Client Registration + PKCE, redirect-domain-restricted `/authorize`, and
file-backed token persistence, with no external identity provider required. It isn't
published as a package - its own README instructs vendoring the single source file
directly, which is what's checked in here (see the file's header for provenance/license).
It's registered via `auth=` in `FastMCP(...)`, which wires up token verification on `/mcp`
and the required `/register`, `/authorize`, `/token`, and `.well-known/*` discovery routes
automatically - no custom middleware needed.

The redirect-domain allow-list on `/authorize` is *not* itself a sufficient security
boundary against a scripted (non-browser) client - it only checks that a claimed
`redirect_uri` matches, not that the caller controls that domain, and the authorization
code is returned directly in the HTTP response to whoever calls `/authorize`.
`MCP_OAUTH_PASSWORD` is the real gate and `Settings.__post_init__` (`config.py`) enforces
it's set whenever `public_base_url` isn't localhost. The vendored file also carries two
local patches: removing an upstream dead-code bug that made the password check
unconditionally pass regardless of the password supplied, and dropping the `scope`-based
password channel (only `state` is checked now) since `scope` gets persisted onto every
issued token - see the "LOCAL PATCHES" note at the top of `personal_auth.py`, and
[README > Authentication](../README.md#authentication) for the full writeup (both bugs
and both fixes were confirmed by live reproduction, not just code review). `server.py`
also disables Uvicorn's default HTTP access log, since its default format would otherwise
write the password (delivered via `/authorize`'s query string) to server logs in
plaintext on every authorization.

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
  a mocked `CalDavService` and assert both delegation and error translation - this path
  never goes through HTTP, so it's unaffected by and independent of the auth layer.
- **Auth tests** (`tests/test_auth.py`): drive the real ASGI app FastMCP builds from
  `auth=PersonalAuthProvider(...)` with an in-process `httpx.ASGITransport` client (no
  real server, no real network). They assert `/mcp` rejects missing/invalid tokens, the
  OAuth discovery and DCR endpoints Claude's connector flow depends on are exposed, and
  the redirect-domain gate in `/authorize` actually blocks a disallowed redirect URI.
  Driving the full interactive OAuth+PKCE flow end-to-end isn't practical in an
  automated test (it requires a browser redirect round-trip), so these tests target the
  middleware boundary instead - see the module docstring for the full rationale.
- **Integration tests** (`tests/test_integration.py`): full create→list→update→complete→
  delete lifecycle against a real Nextcloud instance; skipped unless
  `RUN_INTEGRATION_TESTS=1`. See the README for how to run them.
