# nextcloud-task-mcp

An MCP server that manages tasks (VTODOs) and calendar events (VEVENTs) in a
self-hosted Nextcloud instance over CalDAV. Connect it to Claude as a custom
connector to create, list, update and complete Nextcloud tasks, manage
calendars and events (including recurring ones), link tasks to events
(timeboxing), and get combined day agendas using natural language.

Built with [FastMCP](https://gofastmcp.com) on the Streamable HTTP transport, and the
[`caldav`](https://github.com/python-caldav/caldav) library for talking to Nextcloud.

**Documentation:**

- [Deployment guide](docs/deployment.md) — Ubuntu LXC + Tailscale + systemd + Claude connector setup
- [Tool reference](docs/tools.md) — all tools with parameters, examples and error messages
- [Architecture](docs/architecture.md) — module layout, request flow, design decisions
- [Contributing](CONTRIBUTING.md) — dev setup, checks to run, pre-commit, vendored-file rules
- [Changelog](CHANGELOG.md) — notable changes by work package

## How it works

- One CalDAV connection is opened at startup and reused for every request (no
  reconnect-per-call).
- The server authenticates MCP clients with OAuth 2.1 (Dynamic Client Registration +
  PKCE), via [`PersonalAuthProvider`](https://github.com/crumrine/fastmcp-personal-auth).
  No tool or CalDAV logic runs until a request carries a valid access token. See
  [Authentication](#authentication) below.
- The server binds to a local HTTP port only (e.g. `127.0.0.1:8000`). It does not handle
  TLS itself - in the intended deployment, `tailscale funnel` terminates TLS in front of
  it and exposes it to the public internet (required so Claude's backend can reach it and
  complete the OAuth flow).
- CalDAV/network failures (auth errors, timeouts, missing task lists/UIDs, ...) are caught
  and turned into short, clean error messages - no raw stack traces are ever returned to
  the MCP client.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
# edit .env with your Nextcloud base URL, an app password, and PUBLIC_BASE_URL
```

Generate a Nextcloud app password under **Settings → Security → Devices & sessions**
(never use your account password). `NEXTCLOUD_BASE_URL` is required — your Nextcloud
instance's base URL with no path, typically:

```
https://<your-nextcloud-domain>
```

Must be `https://` — the server refuses to start with a `http://` URL unless it
points at a local address (`localhost`/`127.0.0.1`/`::1`) or `NEXTCLOUD_ALLOW_INSECURE_HTTP=1`
is set, since `http://` sends the app password above in cleartext Basic Auth.

`NEXTCLOUD_CALDAV_URL` is optional and defaults to `<base>/remote.php/dav/`. It is only
needed when your DAV endpoint is not `<base>/remote.php/dav/` (e.g. if CalDAV sits behind a
different host or proxy path). Both URLs must point at the same Nextcloud instance.

`PUBLIC_BASE_URL` is the exact URL clients will use to reach this server - see
[Authentication](#authentication) below for why this has to match precisely.

Run the server:

```bash
set -a; source .env; set +a
uv run nextcloud-task-mcp
```

It listens on `MCP_HOST:MCP_PORT` (default `127.0.0.1:8000`) at the `/mcp` path, using the
Streamable HTTP transport.

## Authentication

The server authenticates MCP clients with **OAuth 2.1** (Dynamic Client Registration +
PKCE), via [`PersonalAuthProvider`](https://github.com/crumrine/fastmcp-personal-auth) -
vendored into [`src/nextcloud_task_mcp/personal_auth.py`](src/nextcloud_task_mcp/personal_auth.py)
since it ships as a single file to copy in, not an installable package. There is no
static bearer token to configure.

This exists because Claude's connector UI (web, mobile, Desktop, Cowork) only exposes
OAuth fields for custom connectors - it has no field for a raw static token. OAuth is
also what makes the server usable from Claude mobile at all, since mobile has no config
file to hand-edit.

How it's secured, since anyone on the internet can reach the OAuth discovery and
registration endpoints once the server is public:

- **Dynamic Client Registration is intentionally open** (`/register` accepts any client) -
  this is required for Claude.ai's connector flow and is not itself a security boundary.
- **The redirect-domain allow-list is *not*, by itself, a security boundary.** A script
  never has to actually control a listed domain (e.g. `claude.ai`) to pass this check -
  it only has to *claim* a matching `redirect_uri` when calling `/authorize`, and the
  authorization code comes back directly in that same HTTP response. Configurable via
  `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS`; when unset and `PUBLIC_BASE_URL` isn't local, the
  server also drops `localhost` from the vendored default allow-list (a `localhost`
  entry can never be reached by a real OAuth redirect on a public deployment anyway) -
  but don't rely on this list alone either way.
- **`MCP_OAUTH_PASSWORD` is the actual security gate**, and is required (the server
  refuses to start without it) whenever `PUBLIC_BASE_URL` isn't `localhost`/`127.0.0.1`,
  or `MCP_HOST` is bound to a non-local address (e.g. `0.0.0.0` - a stale localhost
  `PUBLIC_BASE_URL` with a `0.0.0.0` bind is a common Docker misconfiguration).
  Without it, anyone who can reach the server can self-issue a valid access token. It is
  enforced by an interactive **consent page**: `/authorize` parks the request under a
  cryptographically random, single-use pending key (10-minute TTL) and redirects the
  browser to `/consent`, which asks for the password before any authorization code is
  minted. The comparison is constant-time (`secrets.compare_digest`), and the form is
  rate-limited (max 5 wrong attempts per pending key, max 10 failures per client IP per
  15 minutes) since it is a publicly reachable password prompt. The placeholder value
  shipped (commented out) in `.env.example` is rejected outright if left in place.
- **Access tokens are opaque random strings** (not JWTs with inspectable claims) and are
  persisted to `MCP_OAUTH_STATE_DIR` (default `.oauth-state/oauth_tokens.json`, gitignored)
  so they survive server restarts.
- The `/mcp` endpoint itself rejects any request without a valid `Authorization: Bearer
  <access-token>` header before any tool or CalDAV logic runs.
- The server disables Uvicorn's default HTTP access log (`uvicorn_config={"access_log":
  False}` in `server.py`). The password itself only ever travels in the POST body of the
  `/consent` form, which Uvicorn never logs - but the default access-log format records
  full request paths *including query strings*, which for `/consent` carry the
  single-use pending keys that gate authorization, so the access log stays off. The
  consent handlers themselves never log or echo submitted form data anywhere either.

**Local security patches.** The vendored `PersonalAuthProvider` carries five fixes for
upstream issues found while building this integration, all confirmed by live
reproduction against a running instance, not just by reading the code - see the "LOCAL
PATCHES" note at the top of [`personal_auth.py`](src/nextcloud_task_mcp/personal_auth.py)
for the full log. The most consequential: upstream's password check had a dead-code
fallback that accepted *any* password (or none) as long as the redirect domain matched
the allow-list, and its whole delivery mechanism - expecting the OAuth client to embed
the password in the `state`/`scope` parameters - turned out to be unworkable against
real Claude clients (see below), so it was replaced by the interactive consent page.

**Why a consent page (confirmed 2026-07-10).** Upstream's design expected Claude to
somehow send your password in the OAuth `state` parameter of the `/authorize` request.
A live test against production claude.ai (real "Add custom connector" flow, `/authorize`
request captured in the browser's DevTools network tab) confirmed that can never happen:
`state` carries Claude's own randomly generated CSRF token, and the connector UI has no
field that could influence it. The gate therefore denied every legitimate authorization
- fail-closed, so no exposure, but the connector could not be set up at all. The consent
page replaces it: you now type the password into a form served by this server during the
OAuth flow, which is what upstream's `state` trick was trying to approximate.

### Registering the connector in Claude

Once the server is running and reachable at `PUBLIC_BASE_URL` (see the
[deployment guide](docs/deployment.md) for exposing it via Tailscale Funnel):

1. In Claude.ai (or Cowork/Desktop): **Settings → Connectors → Add custom connector**.
2. **URL:** `<PUBLIC_BASE_URL>/mcp`, e.g. `https://your-host.your-tailnet.ts.net/mcp`.
3. Leave any Client ID / Client Secret fields blank - Dynamic Client Registration handles
   this automatically; there's nothing to copy from the server.
4. Save. Claude opens the OAuth authorization flow in a browser, which lands on this
   server's consent page - enter your `MCP_OAUTH_PASSWORD` there and the connector is
   authenticated (synced automatically to Claude mobile).

Claude Desktop (no native remote-connector UI yet) instead uses the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge in `claude_desktop_config.json`
- see the [deployment guide](docs/deployment.md#5-connect-claude) for the exact config.

## Tools

All tool parameter names match the field names below exactly (e.g. `priority`,
`due_date`) - this is the literal MCP tool schema Claude calls. Names are
plain ASCII, since the Anthropic API only allows `[a-zA-Z0-9_.-]` in schema
property names.

### `list_task_lists()`

Returns all available Nextcloud task lists (calendars supporting VTODO) as
`{"name": ..., "url": ...}` dicts (display name and internal CalDAV URL/ID).
Event-only calendars (e.g. Nextcloud's default "Personal" calendar) are
excluded — `list_calendars` is their counterpart.

### `list_tasks(list_names=None, only_open=True, due_before=None, due_after=None, limit=None, priority=None, tag=None, search_text=None, without_reminder=False, without_visibility=False, without_tags=False, uid_regex=None, fields=None, compact=False, list_name=None)`

Returns tasks across one, several, or all task lists (`list_names=None` queries *every* list on the account, unbounded unless you narrow it; `list_name` is a deprecated alias). `only_open=True` (default) excludes completed *and* cancelled tasks - this is the underlying `caldav` library's own "pending" query (any `STATUS` of `COMPLETED`/`CANCELLED`, or a `COMPLETED` timestamp, counts as not-open), not a choice layered on top here. Each task
is a dict with: `uid`, `title`, `start_date`, `due_date`, `priority`,
`progress_percent`, `status` (`"open"` / `"in-progress"` / `"completed"` / `"cancelled"` -
**breaking change:** two more values than before, settable via `update_task`'s `status`
parameter), `location`, `url`, `tags`,
`reminders`, `notes`, `parent_uid` (parent task UID, or `null` if not a subtask),
`recurrence` (raw RRULE text, or `null` if the task doesn't recur — settable via
`create_task`/`update_task`), `exception_dates` (the occurrences the series skips, `EXDATE`; `[]` if none),
`recurrence_id` and `series_uid` (both `null` unless the row is an expanded occurrence, see below),
`list` (the task list's display name), and `list_url` (its unique URL). Nextcloud allows two lists to share a name: `list` cannot tell them apart, but `list_url` can. You still cannot address such a list by name (it is ambiguous), so it must be renamed in Nextcloud.

**Recurring tasks:** with `due_before` given, a recurring task is expanded into one row per occurrence due inside the window (capped at 100 per task) — otherwise "what is due next week" could never include a weekly task started in March. Without `due_before` the series is returned as the single stored row it is, `recurrence` intact. An expanded row is a *read-only view of one date*: `recurrence_id` names its occurrence, `series_uid` points at the stored task, and its own `uid` is rejected by `update_task`/`complete_task`/`delete_task`/`get_task` rather than silently acting on the whole series. See [`docs/tools.md`](docs/tools.md).

Results are sorted by `due_date` ascending (tasks without a readable due date last), then by `title`. Filters: `priority` (`"high"`/`"medium"`/`"low"`), `tag` (exact match), `search_text` (substring over title and notes), `due_before`/`due_after` (due range bounds); `tag` and `search_text` ignore case and Unicode spelling, and `""` means "no filter" for all five. Cleanup filters (shared with `list_events`): `without_reminder`/`without_visibility`/`without_tags` keep only items with no reminders / no `visibility` / no tags, and `uid_regex` keeps only items whose uid matches a regular expression (case-sensitive `re.search`) — together they shortlist hand-created phone entries (all-uppercase UUIDs, nothing else set) in one call, e.g. `uid_regex="^[A-F0-9-]+$"`. `limit` (must be `> 0` — `null`, not `0`, is "no limit") caps the number of results, applied last after merging across lists. Payload slimming: `fields=[...]` whitelists result keys (unknown names error), `compact=true` drops `null`/`[]`/`""` values plus `list_url` and truncates `notes` to 200 chars (marked; `get_task` has the full text). See [`docs/tools.md`](docs/tools.md) for details.

### `get_task(list_name, task_uid)`

Fetches a single task by UID, without listing the whole task list. Returns what one
entry from `list_tasks` holds, minus its `list` key.

### `create_task(list_name, title, ...)`

Creates a task. Required: `list_name`, `title`. Optional fields and their CalDAV mapping:

| Parameter | CalDAV property | Notes |
|---|---|---|
| `start_date` | `DTSTART` | ISO 8601 date or datetime |
| `due_date` | `DUE` | ISO 8601 date or datetime |
| `priority` | `PRIORITY` | `"high"`→1, `"medium"`→5, `"low"`→9 |
| `progress_percent` | `PERCENT-COMPLETE` | 0-100 |
| `location` | `LOCATION` | |
| `url` | `URL` | |
| `tags` | `CATEGORIES` | list of strings |
| `reminders` | `VALARM` | see below |
| `notes` | `DESCRIPTION` | |
| `visibility` | `CLASS` | `"public"`→PUBLIC, `"private"`→PRIVATE, `"confidential"`→CONFIDENTIAL |
| `parent_task` | `RELATED-TO;RELTYPE=PARENT` | UID of an existing task; makes this task its subtask |
| `recurrence` | `RRULE` | raw RFC 5545 text, e.g. `"FREQ=WEEKLY;BYDAY=MO"`; requires the task to have a `start_date` or `due_date` (existing or set in the same call) to recur from |
| `exception_dates` | `EXDATE` | ISO 8601 occurrences the series skips; each must match `start_date`'s value kind and name a real occurrence |

**Reminders (`reminders`):** each entry is either a relative RFC 5545 duration (e.g.
`"-P1D"`, `"-PT1H"`) or an absolute ISO 8601 datetime. Relative reminders trigger before
`due_date` if set, otherwise before `start_date`; a relative reminder without either
date raises an error. Absolute reminders without a UTC offset are interpreted in the server's
default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`) and stored as UTC per RFC
5545; reading them back formats the same instant in the default timezone, so the string may
differ from what was written. Reading a reminder and writing it back is safe — the alarm is
recognized as already present and left alone — but the strings are normalized (`"-P1W"` reads
back as `"-P7D"`, `"...Z"` as the default timezone's offset). Alarms whose trigger this format
cannot express are not listed, and are never touched by a write; see `docs/tools.md`.

> **BREAKING CHANGE**: Server timezone handling uses a single configurable default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`). Setting `MCP_DEFAULT_TIMEZONE=UTC` restores the previous UTC-hardcoded behavior.

**Date/time semantics** (applies to `start_date`, `due_date`, `start`, `end`, and absolute
`reminders` entries): a value of exactly `"YYYY-MM-DD"` creates an all-day entry
(`VALUE=DATE`); any other ISO 8601 value is a datetime, and a *naive* datetime (no UTC
offset) is interpreted in the server's default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`).
Returned timestamps carry the default timezone's offset (e.g. `+02:00`).
An event keeps the timezone it is anchored to, so a value read from `get_event` can be written
straight back through `update_event` without the event losing that anchor — which is what keeps
a recurring event on its wall-clock time across daylight-saving changes.

### `update_task(list_name, task_uid, ...)`

Same fields as `create_task`, all optional except `task_uid`, plus `status`. Only fields
you pass are changed; everything else on the task is left untouched. Passing `reminders`
replaces the reminders `list_tasks` shows; `clear_fields` clears *every* alarm instead.

**`status`** (`"open"` / `"in-progress"` / `"completed"` / `"cancelled"`) sets `STATUS`.
`"completed"` behaves exactly like `complete_task` (also sets `PERCENT-COMPLETE=100` and
the `COMPLETED` timestamp); `"open"` is the **reopen** path for a task completed by
mistake (removes `COMPLETED`, resets `PERCENT-COMPLETE` to `0`); `"in-progress"`/`"cancelled"`
only set `STATUS`. If the same call also passes `progress_percent`, that explicit value
wins over whatever percentage `status` would derive. An unknown value is a speaking error
naming the four accepted labels, and writes nothing. `status` is not accepted in
`clear_fields` - use `status="open"` to reopen instead.

> **BREAKING CHANGE**: task `status` now has **four** values instead of two
> (`"open"`/`"in-progress"`/`"completed"`/`"cancelled"`) and is directly settable via this
> parameter, not just an implicit read-only result of `complete_task`.

To remove a property entirely (e.g. delete a due date), list its field name in the
optional `clear_fields` parameter instead of just omitting it — omitting a field
leaves it unchanged. Accepted names: `start_date`, `due_date`, `priority`,
`progress_percent`, `location`, `url`, `tags`, `reminders`, `notes`, `visibility`,
`parent_task`, `recurrence`, `exception_dates` (`title` and `status` cannot be
cleared). Clearing `recurrence` also drops the task's `exception_dates` and any `RDATE`,
which mean nothing without a recurrence rule. A field can't be both set and cleared in the
same call; `recurrence`'s anchor requirement is checked
against the task's final state, so clearing the task's only `start_date`/`due_date`
while a recurrence is set or remains is rejected too. See [`docs/tools.md`](docs/tools.md)
for details and examples.

### `complete_task(list_name, task_uid)`

Sets `STATUS:COMPLETED`, `PERCENT-COMPLETE:100`, and a `COMPLETED` timestamp. This does
**not** roll a recurring task's series forward — the task's `recurrence` (`RRULE`) is
left untouched, so completing a recurring task ends it as far as this server is
concerned; advance `due_date` instead to keep a series going. This is this server's
own verified behaviour (see `docs/tools.md`'s `complete_task` section) — how the
Nextcloud Tasks app itself displays a completed recurring task is not verified here.
A task completed by mistake can be reopened with `update_task(status="open")`.

### `delete_task(list_name, task_uid)`

Permanently deletes the task.

### `move_task(list_name, task_uid, target_list, parent_task=None, clear_fields=None)`

Moves a task to another task list. Uses CalDAV `MOVE` to preserve server URL identity, UID, ETags, and all properties; falls back to verified copy-then-delete if the server rejects `MOVE` (HTTP 403/405/409/501/502). The fallback never deletes the source before writing and verifying the target copy, and the verification compares every instance of a recurring series, not just the UID. If the target list rejects tasks, an error is raised before touching the source. Returns `{"uid": ..., "from": ..., "to": ..., "method": "MOVE" | "copied"}`.

A list change almost always changes the hierarchy too, since the old parent stays behind in the source list. `parent_task` sets a new parent, or `clear_fields=["parent_task"]` detaches the task, in the same call — the write lands on the copy in the target list after the move succeeded, and the result then also carries `"hierarchy": "set" | "cleared"`. Only that one field is accepted here; everything else still goes through `update_task`.

### Calendar & event tools (VEVENT)

The same CalDAV account also holds event calendars; these tools mirror the task
tools' conventions (same parameter naming, same ISO 8601 date semantics,
`clear_fields` for clearing fields). See [`docs/tools.md`](docs/tools.md) for
the full reference.

| Tool | Purpose |
|---|---|
| `list_calendars()` | All event calendars with `color` (`#RRGGBB`) and supported `components` |
| `create_calendar(display_name, color=None)` | New VEVENT calendar via `MKCALENDAR`, optional color |
| `update_calendar(calendar_name, new_display_name=None, color=None)` | Rename and/or recolor (`PROPPATCH`); URL/id stays stable |
| `delete_calendar(calendar_name)` | Permanently delete a calendar and all its events |
| `list_events(calendar_names=None, start=None, end=None, search_text=None, tag=None, limit=None, expand_recurrences=False, without_reminder=False, without_visibility=False, without_tags=False, uid_regex=None, fields=None, compact=False)` | Time-range query across one/several/all calendars, full-text, tag and cleanup filters (`without_*`, `uid_regex` — see `list_tasks`); optionally expands recurring events into single occurrences. `fields` whitelists result keys, `compact` drops empty fields and truncates `description`. Without calendars *and* bounds, a default window of today ±90 days applies |
| `get_event(calendar_name, event_uid)` | Single event by UID |
| `create_event(calendar_name, title, start, ...)` | Full event creation: all-day or timed, `location`, `description`, `tags`, `status` (`"confirmed"`/`"tentative"`/`"cancelled"`), `visibility`, recurrence (`recurrence` = raw RRULE), exceptions (`exception_dates` → EXDATE), reminders (`reminders` → VALARM), `url`, task link (`linked_task`) |
| `create_birthday(name, date, year=None, calendar=None)` | One call for the fixed birthday convention: title `"🎂 <name> (<birth year>)"`, all-day on the birth date (so the age is readable from each occurrence), `FREQ=YEARLY`, tag `Birthday`, `visibility` `private`, reminders on the day and the day before. `date` is `"MM-DD"` or `"YYYY-MM-DD"`; the birth year is optional. Without `calendar` it writes to `Birthdays`, or to a pre-existing legacy `Geburtstage` calendar if that is the only one of the two |
| `update_event(calendar_name, event_uid, ...)` | Partial update, same fields; `clear_fields` clears properties |
| `update_events(calendar_name, event_uids, ...)` | Batch update up to 200 events with the same field patch; patch validated up front |
| `update_exdates(calendar_name, event_uids, add=None, remove=None, ...)` | Add/remove single exception dates on up to 200 recurring events without rewriting the whole list |
| `delete_event(calendar_name, event_uid)` | Permanently delete an event |
| `delete_events(calendar_name, event_uids)` | Batch delete up to 200 events from a calendar |
| `move_event(calendar_name, event_uid, target_calendar, linked_task=None, clear_fields=None)` | Move an event to another calendar via CalDAV MOVE, fallback to verified copy-then-delete; optionally re-links (or unlinks) its task in the same call, mirroring `move_task` |
| `link_task_to_event(list_name, task_uid, calendar_name, event_uid, relation="time_block")` | Cross-component `RELATED-TO` link, written on the event: `"time_block"` (event reserves time for the task) or `"prerequisite"` (event must happen before the task) |
| `create_event_from_task(list_name, task_uid, calendar_name, start=None, duration_minutes=None, end=None, description=None, reminders=None, visibility=None)` | Timeboxing: builds an event from a task (title/location/tags, due date as start; `description` inherits `notes` unless overridden) and links both. `end`/`duration_minutes` are mutually exclusive; neither given = 60 minutes |
| `get_agenda(date, calendar_names=None, list_names=None)` | One day's events (recurring ones expanded) and due open tasks together |
| `list_tags(calendar_names=None, list_names=None)` | Aggregated tags (`CATEGORIES`) and usage counts across calendars and task lists (expensive: reads collections completely) |


For all-day events `end` is the **inclusive** last day (RFC 5545's exclusive
`DTEND` is translated on the way in and out). Mixed calendars (VEVENT+VTODO in
one collection) are supported and show up in both `list_task_lists` and
`list_calendars`.

### Notes tools

The Nextcloud Notes app, over its own JSON REST API - a separate code path
from the CalDAV tools above, with its own `NEXTCLOUD_BASE_URL` config (see
[Setup](#setup)). Useful as a per-project "living document" (current state,
decisions + rationale, open questions, next step) alongside the task/calendar
tools' "what's open" view. See [`docs/tools.md`](docs/tools.md) for the full
reference.

| Tool | Purpose |
|---|---|
| `list_notes(category=None)` | All notes, title/category/favorite only (no content) |
| `get_note(note_id)` | Single note by id, including full content |
| `create_note(title, category=None, content=None, favorite=None)` | New note |
| `update_note(note_id, ...)` | Partial update; `content` replaces content wholesale |
| `replace_in_note(note_id, old_text, new_text)` | Patch one passage: `old_text` must match the content exactly once (0 or >1 matches is an error), then it is replaced by `new_text` |
| `update_note_section(note_id, section, content)` | Replace one Markdown section (ATX heading + body, up to the next same-or-higher-level heading) selected by a heading prefix like `"## 7."`; `content` includes the heading line |
| `append_to_note(note_id, text)` | Read-then-write append to existing content |
| `search_notes(search_text, category=None)` | Case-insensitive substring search over title/content (client-side - the API has no full-text search) |
| `delete_note(note_id)` | Permanently delete a note |

## Testing

Unit tests mock the `caldav` library and the Notes REST API (via
`httpx.MockTransport`) entirely - no network access, no real Nextcloud instance
required:

```bash
uv sync          # installs the dev group (pytest, ruff) by default
uv run pytest -q
```

Integration tests exercise the full flow against your real Nextcloud instance (create,
list, update, complete, delete a task in a disposable test list). They're skipped by
default. To run them:

```bash
export RUN_INTEGRATION_TESTS=1
export NEXTCLOUD_CALDAV_URL=... NEXTCLOUD_USERNAME=... NEXTCLOUD_APP_PASSWORD=...
export INTEGRATION_TEST_LIST="Test"   # an existing task list; tasks are created/deleted in it
uv run pytest -q
```

`.github/workflows/integration.yml` runs these on a weekly schedule (and on manual
dispatch) against a disposable `nextcloud` Docker container, so this path is exercised
against a real server periodically even though it's excluded from per-PR CI.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full local dev setup (lint/type-check/
coverage commands, pre-commit hooks, and the vendored-file rules for `personal_auth.py`).

## License

[MIT](LICENSE)
