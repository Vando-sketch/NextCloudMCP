# nextcloud-task-mcp

An MCP server that manages tasks (VTODOs) in a self-hosted Nextcloud instance over CalDAV.
Connect it to Claude as a custom connector to create, list, update and complete Nextcloud
tasks using natural language.

Built with [FastMCP](https://gofastmcp.com) on the Streamable HTTP transport, and the
[`caldav`](https://github.com/python-caldav/caldav) library for talking to Nextcloud.

**Documentation:**

- [Deployment guide](docs/deployment.md) — Ubuntu LXC + Tailscale + systemd + Claude connector setup
- [Tool reference](docs/tools.md) — all tools with parameters, examples and error messages
- [Architecture](docs/architecture.md) — module layout, request flow, design decisions

## How it works

- One CalDAV connection is opened at startup and reused for every request (no
  reconnect-per-call).
- Every incoming MCP request must carry a static bearer token
  (`Authorization: Bearer <token>`), checked by a middleware before any tool or CalDAV
  logic runs.
- The server binds to a local HTTP port only (e.g. `127.0.0.1:8000`). It does not handle
  TLS itself - in the intended deployment, `tailscale serve` terminates TLS in front of it
  and only devices on your tailnet can reach it.
- CalDAV/network failures (auth errors, timeouts, missing task lists/UIDs, ...) are caught
  and turned into short, clean error messages - no raw stack traces are ever returned to
  the MCP client.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
# edit .env with your Nextcloud CalDAV URL, an app password, and a bearer token
```

Generate a Nextcloud app password under **Settings → Security → Devices & sessions**
(never use your account password). The CalDAV URL is your DAV endpoint root, typically:

```
https://<your-nextcloud-domain>/remote.php/dav/
```

Generate a random bearer token, e.g.:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run the server:

```bash
set -a; source .env; set +a
uv run nextcloud-task-mcp
```

It listens on `MCP_HOST:MCP_PORT` (default `127.0.0.1:8000`) at the `/mcp` path, using the
Streamable HTTP transport. Point your Claude custom connector at
`http://<host>:<port>/mcp` with the configured bearer token.

## Tools

All tool parameter names match the field names below exactly (including German field
names with umlauts, e.g. `priorität`, `fällig_datum`) - this is the literal MCP tool
schema Claude calls.

### `list_task_lists()`

Returns all available Nextcloud task lists as `{"name": ..., "url": ...}` dicts
(display name and internal CalDAV URL/ID).

### `list_tasks(list_name, nur_offene=True)`

Returns tasks in a list. `nur_offene=True` (default) excludes completed tasks. Each task
is a dict with: `uid`, `titel`, `start_datum`, `fällig_datum`, `priorität`,
`fortschritt_prozent`, `status` (`"offen"` / `"erledigt"`), `ort`, `url`, `tags`,
`notizen`, `übergeordnete_uid` (parent task UID, or `null` if not a subtask).

### `create_task(liste, titel, ...)`

Creates a task. Required: `liste`, `titel`. Optional fields and their CalDAV mapping:

| Parameter | CalDAV property | Notes |
|---|---|---|
| `start_datum` | `DTSTART` | ISO 8601 date or datetime |
| `fällig_datum` | `DUE` | ISO 8601 date or datetime |
| `priorität` | `PRIORITY` | `"hoch"`→1, `"mittel"`→5, `"niedrig"`→9 |
| `fortschritt_prozent` | `PERCENT-COMPLETE` | 0-100 |
| `ort` | `LOCATION` | |
| `url` | `URL` | |
| `tags` | `CATEGORIES` | list of strings |
| `erinnerungen` | `VALARM` | see below |
| `notizen` | `DESCRIPTION` | |
| `sichtbarkeit` | `CLASS` | `"öffentlich"`→PUBLIC, `"privat"`→PRIVATE, `"vertraulich"`→CONFIDENTIAL |
| `übergeordnete_aufgabe` | `RELATED-TO;RELTYPE=PARENT` | UID of an existing task; makes this task its subtask |

**Reminders (`erinnerungen`):** each entry is either a relative RFC 5545 duration (e.g.
`"-P1D"`, `"-PT1H"`) or an absolute ISO 8601 datetime. Relative reminders trigger before
`fällig_datum` if set, otherwise before `start_datum`; a relative reminder without either
date raises an error. Absolute reminders without a UTC offset are interpreted as UTC (per
RFC 5545, VALARM triggers must be in UTC).

### `update_task(list_name, task_uid, ...)`

Same fields as `create_task`, all optional except `task_uid`. Only fields you pass are
changed; everything else on the task is left untouched. Passing `erinnerungen` replaces
*all* existing reminders on the task.

### `complete_task(list_name, task_uid)`

Sets `STATUS:COMPLETED`, `PERCENT-COMPLETE:100`, and a `COMPLETED` timestamp.

### `delete_task(list_name, task_uid)`

Permanently deletes the task.

## Testing

Unit tests mock the `caldav` library entirely - no network access, no real Nextcloud
instance required:

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

## License

[MIT](LICENSE)
