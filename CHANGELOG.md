# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow Semantic Versioning releases.

## [Unreleased]

### Added

- **Slimmer listing payloads on request.** `list_events` and `list_tasks`
  accept two new optional parameters: `felder`, a whitelist of result keys
  (unknown names error, listing the valid vocabulary), and `kompakt`, which
  drops keys whose value is `null`/`[]`/`""` (e.g. `teilnehmer`,
  `organisator`, `wiederholung` on most entries), drops `liste_url` from
  tasks unless whitelisted, and truncates `beschreibung`/`notizen` to 200
  characters with a visible `… [gekürzt …]` marker (`get_event`/`get_task`
  return the full text). Both combine: whitelist first, then compaction.
  Defaults leave the previous full output unchanged.

- **Cleanup filters on `list_tasks` and `list_events`.** Items created by hand
  on a phone are recognizable by all-uppercase UUIDs and missing
  reminders/visibility/tags; four new filters turn the manual pass over sixty
  events into one call: `ohne_erinnerung`, `ohne_sichtbarkeit` and `ohne_tags`
  keep only items with an empty `erinnerungen` list / no readable `CLASS` /
  no tags, and `uid_regex` keeps only items whose uid contains a match for a
  regular expression (case-sensitive `re.search`; anchor with `^...$` for a
  full match, e.g. `"^[A-F0-9-]+$"`). An unparsable pattern is an error, an
  empty one is no filter. On tasks the filters describe the *stored* task and
  run before recurrence expansion, so `uid_regex` matches the series uid,
  never the synthetic `<uid>#<occurrence>` of an expanded row.
  In support of this, task dicts returned by `list_tasks`/`get_task` now also
  carry a `sichtbarkeit` key (`"öffentlich"`/`"privat"`/`"vertraulich"` or
  `null`), which events already had.

### Changed

- **`list_events` no longer scans the whole account by default.** Called with
  neither `kalender_namen` nor `von`/`bis`, it now applies a default window of
  today ±90 days in the server's default timezone instead of returning every
  event ever stored. Passing any calendar name or either bound restores the
  previous unbounded behaviour.
- **Notes can be patched instead of rewritten.** `update_notiz`'s `inhalt`
  replaces a note's content wholesale, so changing one paragraph of a long
  note meant reading the full content back and re-sending all of it. Two new
  tools carry only what changes. `replace_in_notiz(notiz_id, alt, neu)`
  replaces one text passage: `alt` (which may span lines) must match the
  current content exactly once - zero matches or several are an error and
  nothing is written, so a patch can never land on the wrong spot.
  `update_notiz_abschnitt(notiz_id, abschnitt, inhalt)` replaces one Markdown
  section: `abschnitt` is an ATX heading prefix like `"## 7."` that must
  select exactly one heading of that level (matching stops at a word
  boundary, so `"## 7"` does not select `"## 75."`), and `inhalt` replaces
  the section - heading line included, allowing renames - up to the next
  same-or-higher-level heading. Heading-shaped lines inside fenced code
  blocks or a leading YAML front matter block are ignored; setext headings
  are not recognized. Both are read-then-write like `append_notiz` (the
  Notes API has no server-side patch), with the same concurrent-edit caveat.

- **Exception dates can be changed one at a time.** The new `update_exdates`
  tool adds or removes single `EXDATE`s on up to 200 recurring events at once,
  merging them into what each event already has. `update_event`'s
  `ausnahme_daten` replaces an event's whole exception set, so cancelling one
  more day on a series that already skips sixty occurrences meant reading all
  sixty back and writing sixty-one - per series. This merges server-side, so
  the call carries only what changes.
  An entry given as a plain `"YYYY-MM-DD"` means the whole day: on a timed
  series it cancels every occurrence that day, whatever time that series
  starts, so one list of days covers several series with different start
  times - a sick day, a holiday, a block of school days. An entry that changes
  nothing on a given event (a day that series does not run on, a removal of an
  exception it never had) is reported under that event's `skipped` and the
  rest are still applied, unless `ignore_non_occurrences=false` asks for it to
  fail that event instead. The reply reports counts, not the resulting list -
  not moving that list across the wire is the point.
  This tool's parameters and result keys are English, deliberately unlike the
  German-named tools around it, which are unchanged.

- **Tasks can recur, and can skip single occurrences.** `create_task` and
  `update_task` take `wiederholung` (raw RFC 5545 `RRULE`, the same form the
  event tools use) and `ausnahme_daten` (`EXDATE`), and `list_tasks`/`get_task`
  return both. A recurring task needs a `start_datum` to recur from - RFC 5545
  generates the recurrence set from `DTSTART`, not `DUE` - and the rule is
  validated semantically, not just grammatically: a missing `FREQ`, a duplicate
  or unknown part, `INTERVAL=0`, `BYMONTH=13`, `UNTIL` together with `COUNT` or
  before the anchor are all rejected rather than stored as a series no client
  can resolve.
  `ausnahme_daten` mirrors the event field of the same name exactly: entries
  must match the task's own value kind (date-only for an all-day task), and an
  entry naming no occurrence of the series is rejected instead of stored to
  cancel nothing. Clearing `wiederholung` drops `EXDATE`/`RDATE` with it rather
  than orphaning them on the task.
  Recurring tasks are anchored to the timezone they are written in rather than
  to a fixed UTC instant, so "every Monday 09:00" stays at 09:00 local across a
  daylight-saving transition - the rule events have followed since the
  timezone change below. A task's `DTSTART`/`DUE` can therefore reference a
  `TZID`, and the matching `VTIMEZONE` is now written alongside it.
  Note that `complete_task` still ends a series rather than rolling it forward
  (see under "Changed").
- **Recurring tasks are readable as a series, not just writable.** CalDAV
  servers do not expand `VTODO` series the way they expand `VEVENT`s, so a
  weekly task appeared exactly once in every listing - at its original due date,
  never again, which made "what is due next week" silently wrong. `list_tasks`
  with `faellig_vor`, and `get_agenda`, now expand a recurring task
  client-side into one row per occurrence due inside the queried window
  (`ausnahme_daten` skipped, at most 100 rows per task). Without `faellig_vor`
  there is no window to expand into and the series is returned as the single
  stored row it is, `wiederholung` intact.
  An expanded row is a **read-only view of one date**: `wiederholung_von` names
  its occurrence, `serie_uid` points at the stored task, `wiederholung` is
  `null`, and its own `uid` is a synthetic `"<serie_uid>#<occurrence>"` that
  `update_task`, `complete_task`, `delete_task` and `get_task` all **reject**
  with an error naming the series - rather than silently acting on the whole
  series the caller meant to touch one occurrence of. Two new keys,
  `wiederholung_von` and `serie_uid`, are therefore present (as `null`) on
  every task dict.
- **`list_tasks` queries across task lists, and filters like `list_events`.**
  `listen_namen` replaces `list_name`: `null` (the default) queries every task
  list on the account, a list of names queries those, and `[]` is an empty
  scope that returns nothing without a request. "What is overdue anywhere?"
  used to cost one call per list. New `prioritaet`, `tag` and `suchtext`
  filters mirror the event side; `tag`/`suchtext` compare case-insensitively
  and independently of Unicode spelling (either encoding of "ü", and "STRASSE"
  against "Straße"), and an empty string means "no filter" for every filter
  that takes one - `prioritaet`, `tag`, `suchtext`, `faellig_vor`, and
  `faellig_nach` alike. `limit` still rejects `0`.
  `get_agenda` takes its tasks through the same path.
  **Breaking**, in three ways:
  - Results are **sorted** by `faellig_datum` ascending (tasks without a
    readable due date last), then by `titel` - with umlauts filed under their
    base letter rather than behind "Z" - instead of arriving in server order,
    and `limit` caps the merged, sorted result rather than one list's.
  - Every task dict carries a new **`liste`** key with its task list's display
    name. It is the name every other task tool takes, except in the one case
    Nextcloud permits two task lists to share a display name: `liste` then
    cannot tell them apart, but a new `liste_url` key alongside it carries
    the list's unique collection URL, which you can match against
    `list_task_lists`. However, because no tool accepts a URL to act on,
    such a name is reported as ambiguous by any by-name call. Renaming one
    of them in Nextcloud is the only way to make those tasks addressable again.
  - The MCP tool keeps `list_name` as a deprecated alias (passing both is an
    error), but the underlying `CalDavService.list_tasks` **renamed** its first
    parameter, so an in-process `list_tasks(list_name=...)` call must become
    `list_tasks(list_names=...)`; positional callers are unaffected.
    Everything added to either signature after `limit` is keyword-only, so the
    positional prefix cannot shift under a caller again.
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

- **`complete_task` ends recurring task series.** Completing a recurring task
  does not automatically roll the series forward to the next occurrence
  (unlike what the Nextcloud UI might do). Instead, it hard-ends the series by
  marking the entire recurring task as done. To advance a series, use
  `update_task` on its `faellig_datum` instead.
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
  `ausnahme_daten` are now written in the timezone the event itself is
  anchored to - its `DTSTART`'s - whichever zone the value arrived in, and
  always at the instant that value meant (a naive one still means the server's
  default timezone). Naming a zone on `start`
  (`"2026-07-21T09:00:00 Asia/Tokyo"`) is how an event is *moved* to another
  zone; everything else then follows the new anchor. `ausnahme_daten`
  therefore also shares one representation across all its entries - a mix of
  naive and offset entries used to produce an `EXDATE` with a `TZID` parameter
  next to a value still carrying `Z`, which RFC 5545 3.2.19 forbids and no
  reader reports. For the same reason, `ausnahme_daten` entries must now all be
  the same kind as the event's start - date-only values for an all-day event,
  datetimes otherwise - and a mixed or mismatched set is **rejected** with a
  clear error rather than written as one `EXDATE` carrying two value types
  under a single `TZID`.
- **An exception date that would cancel nothing now says so.** `ausnahme_daten`
  only skips an occurrence when it names exactly a moment the series produces;
  miss it by a day, an hour, or a timezone and the entry used to be stored
  while the occurrence stayed, with nothing reporting it. Entries are now
  checked against the event's `wiederholung` (and its `RDATE`s) and a
  non-matching one is rejected with an error naming it. The check never
  guesses: an event with no recurrence rule, a rule that cannot be expanded,
  or a series that would take more than 10 000 occurrences to search are all
  accepted unchecked.
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

- **Every tool now advertises MCP `annotations`, so read-only calls no longer
  need a human to approve them.** All 44 tools were registered without
  `ToolAnnotations`, so the tool listing said nothing about which of them only
  read and which destroy data. Clients rely on `readOnlyHint`/`destructiveHint`
  to decide what they may run unattended; with nothing to go on they have to
  treat a plain listing exactly like a delete and gate it behind an approval
  prompt. Whenever that prompt goes unanswered the call fails client-side with
  "No approval received" - the server never receives a request at all, which is
  why the matching server log stays clean and why the failure looks parameter-
  independent. Tools are now split into read-only (16), additive
  create (8), overwriting/removing (17) and additive-idempotent (3) sets, all
  with `openWorldHint=True` since every one of them talks to a remote Nextcloud.
- **Collection caches bounded by a 60-second TTL and unified.** The process-wide collection caches now refresh 60 seconds after their last fetch, protecting against out-of-band deletes or renames (e.g. from the Nextcloud web UI) feeding stale collections to tools forever. The cache lists and metadata are now fetched atomically, avoiding skew windows. `get_agenda` freezes that TTL for the duration of its own query so its events and tasks are read from one consistent server state instead of splitting across two if the TTL lapses mid-call - which means the real worst-case staleness bound is 60 seconds plus the duration of the slowest overlapping `get_agenda` call, not a flat 60 seconds.
- **`get_agenda` adds a `quelle_url` key to all entries.** Display names are not unique in Nextcloud; this provides the exact collection URL an event or task came from, so ambiguous entries can be uniquely identified.

- **One unreadable due date no longer breaks a whole task listing.** A `DUE`
  this server cannot parse - a bare time, a period, whatever a foreign client
  wrote - made `list_tasks` fail for every task in that list rather than just
  that one. Such a task is now listed like a task with no due date at all:
  sorted last, and excluded when a `faellig_vor`/`faellig_nach` bound is given,
  because it cannot be judged "before" or "after" anything either.
- **A task list deleted server-side no longer breaks every all-lists query.**
  The collection listing is cached for the life of the process, so a vanished
  list stayed in it and failed on every use; `list_tasks()` and
  `get_agenda(listen_namen=None)` recover from that the way named lists always
  have - drop the caches, re-list once, carry on.
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
