# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow Semantic Versioning releases.

## [Unreleased]

### Added

- **Reminders are readable**: `list_tasks`, `get_task`, `list_events` and
  `get_event` now return an `erinnerungen` list in the same string form
  `create_task`/`create_event` accept, so a reminder can be verified without
  exporting the calendar and parsing ICS by hand. Only alarms whose trigger
  that string form can express are listed - an alarm anchored to the end of an
  event that has an end, one anchored to the start of a task that has both a
  start and a due date, one with a date-valued/repeated/missing trigger, one
  in a timezone that resolves to no known zone, and a relative one on a
  component with no date at all to be relative to: each would read back as a
  different moment than it fires, or as a value a write would reject. Writing
  `erinnerungen` never touches those alarms - which means a hidden alarm and a
  written reminder can both fire, see `docs/tools.md` - and a reminder that is
  already present is kept as it is rather than
  rebuilt, so its action, dismissed state (`ACKNOWLEDGED`/`X-MOZ-LASTACK`) and
  UID survive an edit. Clearing `"erinnerungen"` via `felder_leeren` still
  removes every alarm. `export_calendar`/`import_ics` remain the verbatim,
  lossless path for alarms. Absolute reminders are resolved in the timezone
  they were stored in (previously always assumed UTC, silently shifting
  foreign-client alarms) and read back formatted in the server's default
  timezone (`MCP_DEFAULT_TIMEZONE`), the same convention `start_datum`/
  `faellig_datum` follow; durations read back in canonical spelling
  (`-P1W` → `-P7D`), and passing the same trigger twice creates one alarm.
- **Notes support**: 6 new MCP tools (`list_notizen`, `get_notiz`,
  `create_notiz`, `update_notiz`, `append_notiz`, `search_notizen`) over the
  Nextcloud Notes app's own JSON REST API - a per-project "living document"
  alongside the task/calendar tools' "what's open" view. Deliberately a
  separate code path from CalDAV: a new async `notes_client.py`
  (`NotesService`, built on `httpx`) and `notes_mapping.py` translation
  layer, with their own `NEXTCLOUD_BASE_URL` config setting (reusing the
  existing CalDAV credentials/OAuth gate). `append_notiz` is a
  read-then-write, not an atomic server-side append - the Notes API has none.
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

- **One configurable server timezone instead of hardcoded UTC**
  (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`). Naive datetime input is
  interpreted in that zone, day windows (`get_agenda`, `von`/`bis`,
  `faellig_vor`/`faellig_nach`) are local days in it, and returned timestamps
  carry its offset (e.g. `+02:00`); all-day values stay bare `YYYY-MM-DD`
  strings. `MCP_DEFAULT_TIMEZONE=UTC` restores the previous behaviour
  *including the wire format*: a zone that is UTC is written as plain
  `...Z`, never as `;TZID=UTC:...` with an accompanying VTIMEZONE. A wall
  clock reading the spring-forward gap skips (`"2026-03-29T02:30:00"` in
  Europe/Berlin) is stored as the real local time of the same instant
  (03:30) instead of being written out as a reading that never happens;
  the autumn overlap keeps the earlier of its two instants.
- **Event timestamps stay anchored to the event's own timezone.** `get_event`
  reports every timestamp with a numeric offset (`"...+02:00"`), which says
  nothing about *which* zone it came from - so writing one straight back used
  to re-anchor a recurring event to a fixed UTC instant, reintroducing the
  hour of DST drift after a single read/write round trip, and updating only
  `start` or only `ende` could leave the two ends anchored differently (the
  event silently changing length at the next transition). `start`, `ende` and
  `ausnahme_daten` values that name no zone of their own are now written in
  the zone the event is already anchored to, same instant. `ausnahme_daten`
  additionally shares one representation across all its entries - a mix of
  naive and offset entries used to produce an `EXDATE` with a `TZID`
  parameter next to a value still carrying `Z`, which RFC 5545 3.2.19 forbids
  and no reader reports.
- **`get_free_busy(benutzer=...)` sends a valid VFREEBUSY again.** The
  scheduling request carried its day bounds as `DTSTART;TZID=Europe/Berlin:...`
  in a request body that has no VTIMEZONE component in it; RFC 5545 3.6.4
  requires UTC bounds there, so they are converted before the POST (the same
  instants either way). Busy periods coming *back* without a `Z` are read as
  UTC too, as that format requires - reading them in the default timezone
  moved every reported busy block by that zone's offset.
- **The last two UTC-only timestamps follow the same rule now**: a note's
  `geaendert` (`list_notizen`, `get_notiz`, …) and the token expiry printed by
  the `nextcloud-task-mcp-admin list` CLI, which reads `MCP_DEFAULT_TIMEZONE`
  from the environment for it (falling back to UTC if it names no known zone).
  `list_trash`'s `geloescht_am` keeps reading a server-side value without an
  offset as UTC - it is Nextcloud's own record of the deletion, not a
  caller's input or a floating calendar time - and is then rendered in the
  default timezone like everything else.
- **`create_event_from_task` produces an ordinary, zone-anchored event.** It
  re-formatted its start before handing it on, which flattened any timezone to
  a numeric offset - so a timebox was the one event this server could never
  anchor to a zone. A `start` naming an IANA zone now keeps it, a start taken
  from the task's own due date (a bare instant - tasks store no zone) is
  anchored in the server's default timezone, and an explicit numeric offset
  still becomes UTC, exactly as in `create_event`. `dauer_minuten` is also a
  real duration now: a block spanning a daylight-saving change no longer
  grows or shrinks by the transition's hour.
- **Day boundaries come from one helper, and are readings that exist.** Zones
  that move their clocks at 00:00 (America/Santiago, Asia/Beirut, …) have no
  midnight on a transition day. The instant such a bound resolved to was
  always the right one - that day's first moment - but `get_free_busy` reports
  its window back, and now does so with a wall clock the day really had
  (`01:00-03:00`, not `00:00-04:00`).
- **`get_agenda` reports its own day, not the calendar server's.** A CalDAV
  time-range query resolves all-day and floating values against the calendar
  collection's timezone (RFC 4791 9.9) - the Nextcloud account's setting,
  which need not be `MCP_DEFAULT_TIMEZONE`. When the two differ, a
  neighbouring day's all-day event turned up in the agenda, or a floating one
  shortly before midnight went missing. The agenda now queries the
  neighbouring days as well and applies its own local-day rule to the result,
  so the events half of the agenda draws the same day boundary the tasks half
  already did. `list_events` keeps passing `von`/`bis` to the server
  untouched: its range is a filter, not a promise about days.
- **The VTIMEZONE attached to a zone-anchored event covers dates past 2038.**
  `icalendar` writes a zone's transitions as an explicit list and ends it at
  2038-01-01 by default; a client applies the last observance it finds to
  everything after that, so occurrences of a long-running series would come
  out an hour off. The list now reaches 2100 (about 2 KB more per written
  event).
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

- **Every tool call was slow (3-10 s, even simple reads): a per-call cascade of
  sequential CalDAV round-trips is now cached down to a handful.** Two
  compounding causes, both measured live against a real Nextcloud (6
  collections):
  - *Discarded-then-refetched properties.* For each collection,
    `_supports_component` went through caldav's `get_supported_components()`,
    and `list_calendars` additionally through a per-calendar color
    `get_properties()` - each its own PROPFIND, even though the calendar
    listing already returned both. So a listing cost `2 + N` (`list_task_lists`)
    or `2 + 2·N` (`list_calendars`) PROPFINDs. The
    `supported-calendar-component-set` and color are now read **once** via a
    single Depth-1 PROPFIND over the calendar-home-set and looked up per
    calendar with no further round-trips (a collection absent from that batch,
    e.g. an external subscription, still falls back to the per-calendar lookup).
  - *Never-cached calendar list.* Every listing and cold name resolution
    called `principal.calendars()`, which caldav answers with two PROPFINDs it
    never reuses; `get_agenda` did it several times. The resolved list is now
    cached for the process lifetime.
  Both caches are invalidated together whenever a collection is
  created/deleted/renamed or a color changes (and when a cached collection
  turns out stale mid-request). Basic credentials are also sent pre-emptively
  (`auth_type="basic"`), dropping the one-time 401-negotiation round-trip.
  Net effect (measured, 6 collections): `list_task_lists`/`list_calendars`
  **14/20 HTTP requests → 0** once warm (fully cached); `list_events` over all
  calendars `20 → 6` (just the real per-calendar data REPORTs); `get_agenda`
  `52 → 24`. The remaining requests are unavoidable CalDAV data queries (one
  REPORT per calendar), no longer per-call discovery overhead.
- **`list_trash` no longer fails with HTTP 501 against real Nextcloud**
  (#13). Nextcloud's `DeletedCalendarObjectsCollection` doesn't support
  listing children via PROPFIND - a Depth-1 PROPFIND on
  `trashbin/objects/` is answered with `501 Not Implemented`. The listing
  now uses a `CALDAV:calendar-query` REPORT instead, which returns the
  trashed objects together with the `nc:deleted-at`/`nc:calendar-uri`
  properties. Verified live against a Nextcloud instance (delete → list →
  restore round-trip).
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
