# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and other coding
agents when working with code in this repository.

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and the checks CI runs,
and [docs/architecture.md](docs/architecture.md) for the module layout.

## Timezone & Date Conventions

- **Server Default Timezone**: Configured via `MCP_DEFAULT_TIMEZONE` (default `Europe/Berlin`). `MCP_DEFAULT_TIMEZONE=UTC` gives UTC behavior.
- **Naive Inputs**: Any naive datetime input (no UTC offset) is interpreted in the server's default timezone.
- **Day Windows**: Day bounds (agenda, `due_before`/`due_after`, `start`/`end`) are constructed in the default timezone.
- **Output Timestamps**: Timestamps returned to callers are formatted in the server's default timezone with offset (e.g. `+02:00`). All-day dates remain bare `YYYY-MM-DD` strings.
