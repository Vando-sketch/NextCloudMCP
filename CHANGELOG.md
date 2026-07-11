# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow Semantic Versioning releases.

## [Unreleased]

### Fixed

- **Umlauts removed from the public tool schema** (`ä`→`ae`, `ü`→`ue`). The
  Anthropic API validates every MCP tool's `input_schema` property names
  against `^[a-zA-Z0-9_.-]{1,64}$`, so parameter names like `fällig_datum`,
  `priorität` and `übergeordnete_aufgabe` made the API reject the whole tool
  list and the connector unusable. Renamed across the entire public surface -
  tool parameters, `felder_leeren` values, returned task-dict keys
  (`faellig_datum`, `prioritaet`, `uebergeordnete_aufgabe`,
  `uebergeordnete_uid`, `faellig_vor`, `faellig_nach`) - and in error
  messages, docs and tests. **Breaking** for any client that consumed the old
  umlaut spellings.

### Changed

- **OAuth password gate replaced by an interactive consent page** (D2, LOCAL
  PATCH 5 in `personal_auth.py`). A live test against production claude.ai
  confirmed the vendored provider's design - expecting the OAuth client to
  embed `MCP_OAUTH_PASSWORD` in the `state` parameter - can never be
  satisfied: Claude sends its own CSRF token as `state`, so the gate denied
  every legitimate authorization and the connector could not be registered at
  all (it failed closed; no exposure). `/authorize` now parks the request
  under a random single-use pending key (10-minute TTL) and redirects the
  browser to a `/consent` password form; the password is compared in constant
  time (`secrets.compare_digest`, closing D6's non-constant-time substring
  check as well), and the form is rate-limited (5 wrong attempts per pending
  key, 10 failures per client IP per 15 minutes) since it is now a publicly
  reachable password prompt. Form data is never logged; Uvicorn's access log
  stays disabled. During connector setup you now enter the password on that
  page instead of it (never) arriving via `state`.

High-level summary of the improvement-plan work packages (see
`docs/improvement-plan.md`) landed so far:

- **Security (WP1):** reject the placeholder `MCP_OAUTH_PASSWORD`; require a
  password on any non-local deployment; enforce `https://` on the CalDAV URL;
  harden OAuth state-file permissions; cap `icalendar`.
- **Reliability (WP2):** async, non-blocking tools; CalDAV HTTP timeout;
  serialized shared-connection access; distinct conflict errors instead of
  leaking raw exception text.
- **Correctness & API design (WP3):** correct all-day date handling and
  consistent UTC-naive-datetime semantics; a single `TaskFields` dataclass
  replacing five duplicated parameter lists; field-clearing via
  `felder_leeren`; consistent `list_name` naming; new `get_task` tool; cached
  calendar resolution.
- **Auth depth (WP4):** full OAuth code/token/refresh/revocation lifecycle
  tests; bounded refresh-token expiry; `nextcloud-task-mcp-admin` CLI for
  token administration.
- **Tests & CI (WP5):** `Settings.from_env()` coverage; `mypy` and a 90%
  coverage gate in CI; uv dependency caching.
- **Packaging & DX (WP6):** packaging metadata, `py.typed`; pre-commit,
  `CONTRIBUTING.md`, this changelog; `list_tasks` due-date/limit filtering;
  read-only `RRULE` surfacing; CalDAV rate-limit backoff; scheduled
  integration-test workflow.
