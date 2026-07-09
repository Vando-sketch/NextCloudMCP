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
# edit .env with your Nextcloud CalDAV URL, an app password, and PUBLIC_BASE_URL
```

Generate a Nextcloud app password under **Settings → Security → Devices & sessions**
(never use your account password). The CalDAV URL is your DAV endpoint root, typically:

```
https://<your-nextcloud-domain>/remote.php/dav/
```

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
  `MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS`, but don't rely on it alone.
- **`MCP_OAUTH_PASSWORD` is the actual security gate**, and is required (the server
  refuses to start without it) whenever `PUBLIC_BASE_URL` isn't `localhost`/`127.0.0.1`.
  Without it, anyone who can reach the server can self-issue a valid access token - see
  [deployment guide](docs/deployment.md) for how it's checked.
- **Access tokens are opaque random strings** (not JWTs with inspectable claims) and are
  persisted to `MCP_OAUTH_STATE_DIR` (default `.oauth-state/oauth_tokens.json`, gitignored)
  so they survive server restarts.
- The `/mcp` endpoint itself rejects any request without a valid `Authorization: Bearer
  <access-token>` header before any tool or CalDAV logic runs.
- The server disables Uvicorn's default HTTP access log (`uvicorn_config={"access_log":
  False}` in `server.py`), since the default format logs the full request path
  *including the query string* - and `MCP_OAUTH_PASSWORD` is delivered to `/authorize`
  via a query parameter (see below). Without this, the password would be written in
  plaintext to server logs (e.g. `journalctl`) on every authorization.

**Local security patches.** The vendored `PersonalAuthProvider` carries two fixes for
upstream issues found while building this integration, both confirmed by live
reproduction against a running instance, not just by reading the code - see the "LOCAL
PATCHES" note at the top of [`personal_auth.py`](src/nextcloud_task_mcp/personal_auth.py):
1. The password check had a dead-code fallback that could never evaluate false, so *any*
   password (or none) was accepted as long as the redirect domain matched the allow-list.
2. The password could be supplied via the OAuth `scope` parameter, which - unlike
   `state` - gets persisted verbatim onto every issued access/refresh token and echoed
   back in the `/token` response, multiplying at-rest plaintext copies of the password.
   Only `state` is accepted now.

**Open question - password delivery through Claude.ai's own UI.** `PersonalAuthProvider`
checks the password against the OAuth `state` parameter of the `/authorize`
request. That's generated by the OAuth *client* (Claude's own backend/browser flow
during "Add custom connector"), not typed in by you, so it's unverified whether Claude's
official connector setup can supply your password at all - this repo has no way to drive
a real claude.ai session to test it. If registering the connector fails after setting
`MCP_OAUTH_PASSWORD`, check the server logs for what `/authorize` actually received.

### Registering the connector in Claude

Once the server is running and reachable at `PUBLIC_BASE_URL` (see the
[deployment guide](docs/deployment.md) for exposing it via Tailscale Funnel):

1. In Claude.ai (or Cowork/Desktop): **Settings → Connectors → Add custom connector**.
2. **URL:** `<PUBLIC_BASE_URL>/mcp`, e.g. `https://your-host.your-tailnet.ts.net/mcp`.
3. Leave any Client ID / Client Secret fields blank - Dynamic Client Registration handles
   this automatically; there's nothing to copy from the server.
4. Save. Claude opens the OAuth authorization flow in a browser; approve it once and the
   connector is authenticated (synced automatically to Claude mobile).

Claude Desktop (no native remote-connector UI yet) instead uses the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge in `claude_desktop_config.json`
- see the [deployment guide](docs/deployment.md#5-connect-claude) for the exact config.

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
