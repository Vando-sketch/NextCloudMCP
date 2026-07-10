# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow Semantic Versioning releases.

## [Unreleased]

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
