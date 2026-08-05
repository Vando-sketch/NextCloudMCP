# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this is

A FastMCP server exposing a personal Nextcloud instance to MCP clients:
tasks and calendars over CalDAV, notes over the Notes REST API. It runs in
production — regressions cost more than slow progress. `main` must stay
deployable at all times.

## Project structure

| Path | Role |
|------|------|
| `src/nextcloud_task_mcp/server.py` | Entry point (`main()`), `build_server()`, every `@mcp.tool` definition. Tool docstrings are the LLM-facing contract. |
| `src/nextcloud_task_mcp/caldav_client.py` | `CalDavService`: the one CalDAV connection, collection resolution + caching, all task/event/sharing/trashbin/ICS operations. Raw DAV XML for the Nextcloud-only extensions. |
| `src/nextcloud_task_mcp/mapping.py` | German task fields ⇄ iCalendar VTODO (`TaskFields`, `apply_task_fields`, `parse_vtodo`, `filter_tasks`, date parsing, VALARM building). |
| `src/nextcloud_task_mcp/event_mapping.py` | German event fields ⇄ iCalendar VEVENT (`EventFields`, `apply_event_fields`, `parse_vevent`, `filter_events`, free/busy helpers). Reuses `mapping`'s shared primitives. |
| `src/nextcloud_task_mcp/notes_client.py`, `notes_mapping.py` | Nextcloud Notes JSON REST API (async httpx) and its field mapping. |
| `src/nextcloud_task_mcp/personal_auth.py` | OAuth 2.1 provider for a single personal account. Excluded from ruff/mypy on purpose — see `CONTRIBUTING.md`. |
| `src/nextcloud_task_mcp/config.py`, `errors.py`, `admin.py` | Settings from env, the `TaskMcpError` hierarchy, admin helpers. |
| `tests/` | Pure unit tests with the caldav library mocked (`MagicMock`); `test_integration.py` only runs with `RUN_INTEGRATION_TESTS=1` plus real credentials. |
| `docs/tools.md` | The tool reference. Every tool change updates it. |

Layering: `server.py` → `CalDavService`/`NotesService` → `mapping`/`event_mapping`.
Tool functions never touch icalendar directly; mapping modules never make
network calls.

## Conventions

- **Tool parameters and returned dict keys are German**: `kalender_name`,
  `listen_namen`, `erinnerungen`, `faellig_datum`, `wiederholung`,
  `felder_leeren`, `tags`, `notizen`. New fields follow the same pattern.
  Internal Python below the tool layer uses English names
  (`calendar_names`, `due_before`) — the German↔English hop happens in
  `server.py` and the `*Fields` dataclasses.
- **Date/time semantics — do not change**: exactly `"YYYY-MM-DD"` means
  all-day (`VALUE=DATE`); a naive datetime is UTC; `"<iso> Europe/Berlin"`
  resolves the correct standard/daylight offset for that date; combining a
  numeric offset with a zone name is an error. Events keep the IANA zone
  (`keep_zone=True`, so RRULEs stay wall-clock correct); tasks normalize to
  UTC. All-day `ende` is the *inclusive* last day on the way in and out,
  RFC 5545's exclusive DTEND only exists on the wire.
- **Errors speak**: raise a `TaskMcpError` subclass from `errors.py` with a
  sentence a user can act on. Never forward raw HTTP/caldav text — see
  `caldav_client._translate`.
- **`None` means "leave unchanged"**, `felder_leeren` means "remove the
  property". Setting and clearing the same field in one call is an error.
- Docstrings carry the reasoning ("why", not "what"). Match the density of
  the surrounding code.

## Commands

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest -q --cov=src/nextcloud_task_mcp --cov-report=term-missing --cov-fail-under=90
```

Coverage gate is 90% (currently ~94%). Integration tests:
`RUN_INTEGRATION_TESTS=1` plus `NEXTCLOUD_CALDAV_URL`, `NEXTCLOUD_USERNAME`,
`NEXTCLOUD_APP_PASSWORD`, `INTEGRATION_TEST_LIST`.

## Definition of done for a change

Tests green, lint + mypy clean, `docs/tools.md` and `README.md` updated for
any tool whose signature or return shape moved, and a round-trip test
(write → read → same value) for every changed tool.

## Git workflow

Follow this for every implementation task, without waiting to be asked:

1. **Classify the task** as `feat` (new capability), `fix` (bug fix), or
   another conventional type (`refactor`, `chore`, `docs`, `test`) based on
   what the diff actually does.
2. **Create a branch** named `<type>/<short-kebab-description>` (e.g.
   `feat/wardrobe-photo-batching`, `fix/dashboard-auth-bypass`) off the current
   base branch before making changes. Always create a branch — never commit
   directly to `main` or to whatever branch you started on.
3. **Implement, test, and commit** on that branch.
4. **Open a pull request** (`gh pr create`) once the work is done and verified
   (tests + lint pass), targeting the branch you branched from.

### Rules that hold for every branch

- One issue = one branch = one pull request.
- Commits are small and imperative, one topic per commit.
- No commit without green tests.
- PR description states: what, why, how it was tested, and breaking changes
  explicitly.
- Never push directly to `main`; never force-push a shared branch.

### Breaking down larger tasks with subagents

For a task large enough to naturally split into independent subtasks:

1. Create your own `<type>/<description>` branch first, as above — this is the
   integration branch.
2. Split the work into subtasks and dispatch each to a subagent, picking the
   model per subtask's complexity (e.g. a small, mechanical change → a
   cheaper/faster model; a subtask requiring deep design or judgment → a
   stronger model).
3. Each subagent creates its own branch off the integration branch (same
   `<type>/<description>` naming convention), and independently follows the
   full workflow above on that branch: implement, test, commit. Subagents do
   not open their own pull requests and do not merge — only the main agent
   does.
4. Once a subagent's branch is done, the main agent merges it into its own
   integration branch, resolving any conflicts.
5. After all subagent branches are merged, the main agent runs the full test
   suite and lint on the integration branch, then opens the single pull request
   for the whole task.
