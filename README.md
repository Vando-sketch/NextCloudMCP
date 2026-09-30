<p align="center">
  <img src="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/logo.svg" alt="nextcloud-organizer-mcp logo" width="160">
</p>

<h1 align="center">nextcloud-organizer-mcp</h1>

<p align="center">
  <b>Let Claude run your Nextcloud tasks, calendar and notes.</b><br>
  A self-hosted MCP server for CalDAV tasks, events and the Notes app.
</p>

<p align="center">
  <a href="https://pypi.org/project/nextcloud-organizer-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/nextcloud-organizer-mcp"></a>
  <a href="https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Vando-sketch/Nextcloud-Organizer-MCP/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/pypi/pyversions/nextcloud-organizer-mcp">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/demo/demo.gif" alt="A scripted client creating a task, a recurring event and a time block, then reading the day's agenda" width="820">
  <br>
  <sub>Real tool calls against a real Nextcloud &middot;
  <a href="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/demo/demo.mp4">watch as video</a></sub>
</p>

<!-- mcp-name: io.github.Vando-sketch/nextcloud-organizer-mcp -->

Ask in plain language, and your own Nextcloud changes. No copy-paste, no
third-party calendar service: the data stays on your server and the tools talk
to it over standard CalDAV and the Notes REST API.

> Community project, not affiliated with or endorsed by Nextcloud GmbH.
> Formerly `nextcloud-task-mcp`.

## What you can say

| You say | What happens |
|---|---|
| "Add a high-priority task: finish the Q4 report by Thursday 5pm, remind me 2 hours before." | `create_task` with due date, priority and a `VALARM` reminder |
| "Add a weekly team sync, Mondays 10:00." | `create_event` with an `RRULE` |
| "Block Thursday 9-11 for the report." | `create_event_from_task` &mdash; a linked time block, task and event stay connected |
| "What does my Thursday look like?" | `get_agenda` &mdash; events (recurring ones expanded) and due tasks in one answer |
| "Start a note for the Q4 report, then update just the status section." | `create_note`, `update_note_section` |

What Claude wrote shows up in the normal Nextcloud apps, immediately:

<p align="center">
  <img src="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/demo/calendar.png" alt="Nextcloud Calendar showing the 9-11 time block and the task deadline" width="820">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/demo/tasks.png" alt="Nextcloud Tasks showing the high-priority task with its tag and due date" width="820">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/Vando-sketch/Nextcloud-Organizer-MCP/main/assets/demo/notes.png" alt="Nextcloud Notes showing the note after a single-section update" width="820">
</p>

<!-- Slot for real Claude chat recordings (web + mobile): see assets/demo/README.md for the shot list. -->

## Features

- **Tasks** (VTODO): priorities, tags, reminders, subtasks, recurrence with exception dates, four-state status, batch update/delete/move.
- **Calendars and events** (VEVENT): recurring events, attendees and RSVP, free/busy, sharing, ICS import/export, birthdays, batch operations, trash bin.
- **Timeboxing**: turn a task into a calendar block and keep both linked.
- **Notes**: create, search, append, replace a passage or a single Markdown section.
- **Combined day agenda**: events and due tasks together.
- **Safe by default**: OAuth 2.1 with a password consent page, `https://` enforced for Nextcloud, clean error messages instead of stack traces.
- **51 tools** in total, documented in the [tool reference](docs/tools.md).

## Compatibility

| Client | Status |
|---|---|
| Claude (claude.ai web) as a custom connector | Supported, connector and consent flow tested against claude.ai |
| Claude mobile and Cowork | Use the same connector as web; not tested separately |
| Claude Desktop via [`mcp-remote`](https://github.com/geelen/mcp-remote) | Documented in the [deployment guide](docs/deployment.md#5-connect-claude) |
| Other MCP clients and model providers | Not tested yet, planned |

The server speaks standard Streamable HTTP MCP with OAuth 2.1 (Dynamic Client
Registration + PKCE), so other clients may work. The OAuth redirect allow-list
defaults to `claude.ai` and `claude.com`; other clients need
`MCP_OAUTH_ALLOWED_REDIRECT_DOMAINS` set. Reports from other clients are welcome.

## Quick start

Requires Python 3.10+ and a Nextcloud with the Calendar/Tasks and Notes apps.

```bash
uv tool install nextcloud-organizer-mcp   # or: pipx install nextcloud-organizer-mcp
```

Create an app password under **Settings → Security → Devices & sessions** (never
your account password), then set:

```bash
export NEXTCLOUD_BASE_URL=https://cloud.example.com
export NEXTCLOUD_USERNAME=your-username
export NEXTCLOUD_APP_PASSWORD=your-app-password
export PUBLIC_BASE_URL=https://your-host.ts.net   # the URL clients use
export MCP_OAUTH_PASSWORD=a-long-random-password  # required unless local
```

```bash
nextcloud-organizer-mcp     # listens on 127.0.0.1:8000, path /mcp
```

Prefer containers? A multi-arch image is on GHCR and needs no Python or uv; see
[Running in Docker](docs/docker.md) for a compose file and a `docker run` one-liner.

From a checkout instead: `uv sync && cp .env.example .env`, edit `.env`, then
`set -a; . ./.env; set +a; uv run nextcloud-organizer-mcp` (the server does not read
`.env` itself, so the variables must be exported first). Every setting is documented in
[`.env.example`](.env.example). `NEXTCLOUD_BASE_URL` must be `https://` unless it
points at a local address.

Client on the same machine? No public URL or password needed. The simplest setup is the
[native stdio transport](docs/deployment.md#native-stdio-transport)
(`MCP_TRANSPORT=stdio`, the client starts the server itself); a loopback HTTP server also
works, see [Local-only use](docs/deployment.md#local-only-use-no-public-url).

Otherwise expose the server (the [deployment guide](docs/deployment.md) covers Caddy
and an existing Nextcloud reverse proxy, Tailscale Funnel for TLS, and a
[Cloudflare Tunnel](docs/deployment.md#4c-alternative-cloudflare-tunnel) alternative;
the Caddy and Cloudflare recipes are only partly tested) and add it in Claude under
**Settings → Connectors → Add custom connector** with the URL `<PUBLIC_BASE_URL>/mcp`.
Leave Client ID and Secret blank
and enter your `MCP_OAUTH_PASSWORD` on the consent page that opens. Details:
[Authentication](docs/authentication.md#registering-the-connector-in-claude).

## How it works

```
Claude ── HTTPS + OAuth 2.1 ──► your server (this project) ── CalDAV / Notes API ──► Nextcloud
```

- One CalDAV connection is opened at startup and reused for every request.
- No tool or CalDAV logic runs until a request carries a valid access token.
- The server binds to a local HTTP port and does not handle TLS itself.
- Timestamps use `MCP_DEFAULT_TIMEZONE` (default `Europe/Berlin`); naive inputs are interpreted in it.

## Documentation

- [Deployment guide](docs/deployment.md) &mdash; systemd, Caddy/existing Nextcloud proxy, Tailscale Funnel or Cloudflare Tunnel, local-only use, and Claude connector setup
- [Running in Docker](docs/docker.md) &mdash; container image, compose file, persistence, TLS
- [Authentication](docs/authentication.md) &mdash; OAuth model, consent page, local security patches
- [Tool reference](docs/tools.md) &mdash; all tools with parameters, examples and error messages
- [Architecture](docs/architecture.md) &mdash; module layout, request flow, design decisions
- [Contributing](CONTRIBUTING.md) &mdash; dev setup, checks, branches (`main` released, `dev` active)
- [Changelog](CHANGELOG.md) &middot; [Security policy](SECURITY.md)

## License

[MIT](LICENSE)
