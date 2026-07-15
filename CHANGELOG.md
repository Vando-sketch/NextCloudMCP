# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow Semantic Versioning releases.

## [Unreleased]

### Added

- **Calendar & event support (VEVENT)**: 12 new MCP tools alongside the task
  tools. Calendar management (`list_calendars`, `create_calendar`,
  `update_calendar` for rename/recolor, `delete_calendar`), full event CRUD
  (`list_events` with server-side time-range REPORT, full-text/tag filters and
  optional expansion of recurring events into single occurrences; `get_event`,
  `create_event`, `update_event`, `delete_event`) including recurrence
  (`wiederholung` = raw RRULE), exceptions (`ausnahme_daten` → EXDATE),
  reminders (`erinnerungen` → VALARM relative to DTSTART), status/visibility,
  and all-day events with *inclusive* `ende` semantics. Task↔event linking via
  cross-component `RELATED-TO` written on the event (`link_task_to_event` with
  `"zeitblock"`/`"voraussetzung"`), task→event conversion for timeboxing
  (`create_event_from_task`), and a combined day view (`get_agenda`) returning
  events plus due tasks. New `event_mapping.py` translation layer mirrors
  `mapping.py`; verified live against a Nextcloud instance (calendar/event
  lifecycle, recurrence expansion incl. EXDATE, linking, agenda).

### Changed

- **`list_task_lists` now only returns VTODO-supporting calendars.** Nextcloud
  keeps task lists and event calendars in the same DAV namespace; previously
  event-only calendars (e.g. the default "Personal" calendar) appeared as task
  lists and task operations against them failed server-side. Name resolution
  is component-aware throughout: a task list and an event calendar may share a
  display name without becoming ambiguous, and mixed VEVENT+VTODO calendars
  are reachable from both sides.
- **Occupied collection ids are dodged on create.** Nextcloud's trashbin keeps
  deleted calendars' URIs occupied (invisibly) until purged, which used to
  make `create_task_list`/`create_calendar` fail with "already exists" after a
  delete+recreate of the same name. The generated collection id now retries
  with `-2`, `-3`, … suffixes before giving up; display-name conflicts are
  still rejected.

### Fixed

- **Explicit UTC-offset datetimes (e.g. `2026-07-30T07:50:00+02:00`) no longer
  land on the wrong day after CalDAV sync.** `parse_datetime_input` used to
  keep an aware input's `tzinfo` as-is; `icalendar` serializes a fixed-offset
  `tzinfo` as `DTSTART;TZID="UTC+02:00":...` without ever writing the
  matching `VTIMEZONE` component that TZID requires, so any client that
  doesn't recognize the (nonstandard) TZID falls back to its own local zone -
  shifting the moment, and often the calendar day (reported via
  `create_event_from_task`/`get_event` on iPhone/CalDAV sync). Offset inputs
  are now converted to UTC before being stored, matching the existing
  naive-input-is-UTC convention, so the property is written as plain UTC
  with a `Z` suffix instead.
- **Added optional IANA timezone-name input** (e.g.
  `"2026-07-20T14:00:00 Europe/Berlin"`) to the same date/time parsing used
  by `create_task`, `create_event` and friends. A numeric offset picked once
  and reused (e.g. always `+02:00`) is only correct for half the year in any
  zone that observes daylight saving time; naming the zone instead resolves
  the correct standard/daylight offset per date via `zoneinfo`. Combining a
  numeric offset and a zone name in the same value is rejected as ambiguous.

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
