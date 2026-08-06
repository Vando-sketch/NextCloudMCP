# Tool reference

Detailed reference for all MCP tools (task, task-list, calendar and event
tools), including argument/result examples. Parameter names are the literal
MCP schema names (German field names in ASCII transliteration).

Values for enum-like fields:

| Field | Allowed values |
|---|---|
| `prioritaet` | `"hoch"`, `"mittel"`, `"niedrig"` |
| `sichtbarkeit` | `"öffentlich"`, `"privat"`, `"vertraulich"` |
| `status` (tasks; `update_task`'s `status` parameter and its result key) | `"offen"`, `"in-arbeit"`, `"erledigt"`, `"abgesagt"` |
| `status` (events) | `"bestätigt"`, `"vorläufig"`, `"abgesagt"` |
| `beziehung` (`link_task_to_event`) | `"zeitblock"`, `"voraussetzung"` |
| `farbe` | `"#RRGGBB"` or `"#RRGGBBAA"` |
| `teilnehmer[].status` (read-only, in event results) | `"ausstehend"`, `"zugesagt"`, `"abgesagt"`, `"vorläufig"`, `"delegiert"` |
| `teilnehmer[].rolle` | `"leitung"`, `"erforderlich"` (default), `"optional"`, `"keine-teilnahme"` |
| `antwort` (`respond_to_event`) | `"zugesagt"`, `"abgesagt"`, `"vorläufig"` |
| `typ` (`share_calendar`/`list_calendar_shares`) | `"benutzer"`, `"gruppe"` |
| `status` (`list_calendar_shares`) | `"akzeptiert"`, `"ausstehend"`, `"abgelehnt"`, `"ungueltig"`, `"geloescht"`, or a raw lowercased status the server reported |
| `typ` (`list_trash`) | `"aufgabe"`, `"termin"`, or `null` |

> **BREAKING CHANGE**: Task `status` now has **four** possible values instead of two -
> `"offen"`, `"in-arbeit"`, `"erledigt"`, `"abgesagt"` - and is settable via `update_task`'s
> new `status` parameter (see below). Any code that only checked for `"offen"`/`"erledigt"`
> should treat `"in-arbeit"`/`"abgesagt"` as "not erledigt" rather than assuming those are
> the only two values.

Dates are ISO 8601 strings. Rules applying everywhere a date/datetime is
accepted (`start_datum`, `faellig_datum`, `start`, `ende`, `von`, `bis`,
`ausnahme_daten` and absolute `erinnerungen` entries):

> **BREAKING CHANGE**: Timezone handling uses one configured server default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`). Setting `MCP_DEFAULT_TIMEZONE=UTC` restores the previous UTC behavior.

- A value that is exactly `"YYYY-MM-DD"` (e.g. `"2026-07-20"`) creates an
  **all-day** entry (iCalendar `VALUE=DATE`) — it comes back from `list_tasks`
  / `get_task` as `"2026-07-20"`, not a midnight datetime.
- Any other ISO 8601 datetime (e.g. `"2026-07-20T14:00:00"`,
  `"2026-07-20T14:00:00+02:00"`) is stored as a datetime. A **naive**
  datetime (no UTC offset) is interpreted in the server's default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`).
  Output timestamps are returned formatted in the default timezone with offset (e.g. `"+02:00"`).
- A datetime may instead be followed by a space and an **IANA timezone
  name**, e.g. `"2026-07-20T14:00:00 Europe/Berlin"` — the correct
  standard/daylight offset (e.g. CET vs. CEST) is then resolved for that
  specific date, so callers don't have to work out themselves which one
  applies. Combining a numeric offset and a timezone name in the same value
  is rejected.

---

## `list_task_lists()`

No parameters. Returns every VTODO-supporting calendar (task list) on the account:

```json
[
  {"name": "Privat", "url": "https://cloud.example.com/remote.php/dav/calendars/demo/privat/"},
  {"name": "Arbeit", "url": "https://cloud.example.com/remote.php/dav/calendars/demo/arbeit/"}
]
```

Note: Nextcloud keeps task lists and event calendars in the same CalDAV
namespace. Event-only calendars (e.g. the default "Personal" calendar) are
excluded here — they can't hold tasks; `list_calendars` is their counterpart.
A mixed calendar supporting both VEVENT and VTODO appears in both listings.

---

## `list_tasks(listen_namen=None, nur_offene=True, faellig_vor=None, faellig_nach=None, prioritaet=None, tag=None, suchtext=None, limit=None, list_name=None)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `listen_namen` | list of strings | no | Task list display names to query; `null` = all task lists |
| `nur_offene` | boolean | no (default `true`) | Exclude completed *and* cancelled tasks (see note below) |
| `faellig_vor` | string (ISO 8601) | no | Only tasks due at or before this point |
| `faellig_nach` | string (ISO 8601) | no | Only tasks due at or after this point |
| `prioritaet` | string enum | no | Filter by priority (`"hoch"`, `"mittel"`, `"niedrig"`) |
| `tag` | string | no | Exact (case-insensitive) match against one `tags` entry |
| `suchtext` | string | no | Case-insensitive substring match over `titel` and `notizen` |
| `limit` | integer | no | Max number of results; must be `> 0` |
| `list_name` | string | no | **Deprecated** alias for `listen_namen`; pass `listen_namen` instead (passing both is an error) |

> **BREAKING CHANGE**: Results are now sorted by `faellig_datum` ascending (tasks without a due date last, then by `titel`), rather than returned in server order.
> **BREAKING CHANGE**: Every task dict now includes a `"liste"` key containing the task list's display name.

`nur_offene=True` (the default) does **not** just exclude `status="erledigt"` -
it excludes `status="abgesagt"` too. This isn't a choice this server layers on
top: it's the underlying `caldav` library's own three-way "pending" query
(`Calendar.todos(include_completed=...)`), which treats any `VTODO` whose
`STATUS` is `COMPLETED` *or* `CANCELLED` (or that has a `COMPLETED` timestamp
set at all) as not-pending. A cancelled task is therefore **not** treated as
"still open" - pass `nur_offene=False` to see it.

Result — one dict per task:

```json
[
  {
    "uid": "0f8ba4a4-...",
    "titel": "Steuererklärung",
    "start_datum": "2026-07-01",
    "faellig_datum": "2026-07-20",
    "prioritaet": "hoch",
    "fortschritt_prozent": 20,
    "status": "offen",
    "ort": "Zuhause",
    "url": "https://example.com/steuer",
    "tags": ["Finanzen", "Wichtig"],
    "erinnerungen": ["-P1D"],
    "notizen": "Belege sammeln",
    "uebergeordnete_uid": null,
    "wiederholung": null,
    "liste": "Privat"
  }
]
```

`erinnerungen` lists all alarms set on the task as relative duration strings (e.g. `"-P1D"`,
`"-PT30M"`) or absolute ISO 8601 datetimes with offset (e.g. `"2026-08-07T09:00:00+02:00"`),
matching the format `create_task`/`update_task` accepts. Absolute values are stored in UTC on
the wire (RFC 5545 requires that) but read back in the server's default timezone, so `"...Z"`
and `"...+00:00"` input come back with the local offset — the same instant, not the same
string.

Two details of an alarm have no slot in this format and are therefore lost when a read-back
value is written again: whether a relative reminder was anchored to the due date or the start
date (writing it back re-derives that from the task's own dates — `faellig_datum` wins), and a
non-display alarm action (`EMAIL`/`AUDIO`) written by another client, which becomes a display
reminder. For reminders this server created itself, the round trip is exact.
`uebergeordnete_uid` is the parent task's UID if this task is a subtask, otherwise `null`.
`wiederholung` is the task's raw RRULE text (e.g. `"FREQ=WEEKLY;BYDAY=MO"`) if it recurs,
otherwise `null` — set via `create_task`/`update_task`, see their `wiederholung` parameter.
`liste` is the display name of the task list containing the task.
Fields not set on the task are `null` (`tags` is `[]`, `fortschritt_prozent` is `0`).

### Filtering and Sorting

- `listen_namen`: pass a list of list names to query specific task lists, or `null` to query all task lists on the account. `list_name` is a deprecated alias that takes a single list name.
- `prioritaet`: filters by task priority (`"hoch"`, `"mittel"`, `"niedrig"`). An unknown priority raises an error (`InvalidTaskDataError`).
- `tag`: exact, case-insensitive match against any entry in `tags`.
- `suchtext`: case-insensitive substring match over `titel` and `notizen` (skipping `null` values).
- If either `faellig_vor` or `faellig_nach` is given, tasks with **no** `faellig_datum` at all
  are excluded from the result — a task without a due date can't be judged "before" or
  "after" anything.
- Both accept the same ISO 8601 date/datetime formats as `create_task`'s `faellig_datum`. A
  date-only bound (e.g. `"2026-07-20"`) is inclusive of the whole day: `faellig_vor` expands
  to the end of that day (`23:59:59`), `faellig_nach` to the start of it (`00:00:00`) — both
  in the server's default timezone (`MCP_DEFAULT_TIMEZONE`), so an all-day task due exactly
  on the boundary date is included by either bound. A datetime bound (with a specific time) is used exactly as given.
- `faellig_vor` and `faellig_nach` can be combined to select a range.
- Tasks are sorted by `faellig_datum` ascending (tasks without a due date sort last), then by `titel`.
- `limit` caps the number of results, applied *last* after merging and sorting across lists. `limit <= 0`
  is an error (`InvalidTaskDataError`).

---

## `get_task(list_name, task_uid)`

Fetch a single task by UID, without listing the whole task list.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list |
| `task_uid` | string | yes | UID of the task to fetch |

Returns the same dict shape as one entry from `list_tasks` (see above), including
`wiederholung`.

---

## `create_task(list_name, titel, ...)`

| Parameter | Type | Required | CalDAV property |
|---|---|---|---|
| `list_name` | string | yes | — (target task list) |
| `titel` | string | yes | `SUMMARY` |
| `start_datum` | string (ISO 8601) | no | `DTSTART` |
| `faellig_datum` | string (ISO 8601) | no | `DUE` |
| `prioritaet` | string enum | no | `PRIORITY` (hoch→1, mittel→5, niedrig→9) |
| `fortschritt_prozent` | integer 0–100 | no | `PERCENT-COMPLETE` |
| `ort` | string | no | `LOCATION` |
| `url` | string | no | `URL` |
| `tags` | list of strings | no | `CATEGORIES` |
| `erinnerungen` | list of strings | no | one `VALARM` per entry |
| `notizen` | string | no | `DESCRIPTION` |
| `sichtbarkeit` | string enum | no | `CLASS` |
| `uebergeordnete_aufgabe` | string (UID) | no | `RELATED-TO;RELTYPE=PARENT` |
| `wiederholung` | string (raw RRULE) | no | `RRULE`, e.g. `"FREQ=WEEKLY;BYDAY=MO"` |

Returns `{"uid": "<new task uid>"}`.

### Recurrence (`wiederholung`)

`wiederholung` is a raw RFC 5545 `RRULE` string, e.g. `"FREQ=DAILY"` or
`"FREQ=WEEKLY;BYDAY=MO;COUNT=10"` — the same format `create_event`'s
`wiederholung` accepts and `list_tasks`/`get_task` return. A value that
doesn't parse as an RRULE is rejected.

A recurring task needs something to recur *from*: a task that ends up with an
`RRULE` but **neither** `start_datum` nor `faellig_datum` is rejected with a
speaking error, rather than silently writing a series no client can resolve.
This is judged on the task's final state, so it also catches clearing the last
anchor of a task that was already recurring — even when the call doesn't
mention `wiederholung` at all.

The value read back is the *canonical* form of what you sent, not a verbatim
echo: part names come back uppercase and in RFC 5545's own order, so
`"byday=mo;freq=weekly"` reads back as `"FREQ=WEEKLY;BYDAY=MO"`. Same rule,
different spelling.

See "Completing a recurring task" under `complete_task` below for what
happens to the series once the task is marked done.

### Reminders (`erinnerungen`)

Each entry is either:

- a **relative** RFC 5545 duration, e.g. `"-P1D"` (1 day before), `"-PT1H"` (1 hour
  before), `"-PT15M"` (15 minutes before). Anchored to `faellig_datum`
  (`TRIGGER;RELATED=END`) when the task has one, otherwise to `start_datum`
  (`RELATED=START`). A relative reminder on a task with neither date is an error.
- an **absolute** ISO 8601 datetime, e.g. `"2026-07-19T09:00:00+02:00"`. Stored as a UTC
  `TRIGGER;VALUE=DATE-TIME` on the wire, as RFC 5545 requires; a value without an offset is
  read in the server's default timezone first. Reading it back yields the same instant
  rendered in the default timezone, so the string may differ from the one sent.

Example call:

```json
{
  "list_name": "Personal",
  "titel": "Steuererklärung abgeben",
  "faellig_datum": "2026-07-20",
  "prioritaet": "hoch",
  "tags": ["Finanzen"],
  "erinnerungen": ["-P1D", "-PT2H"]
}
```

### Subtasks

Pass the UID of an existing task (e.g. from `list_tasks`) as `uebergeordnete_aufgabe`.
The Nextcloud Tasks app then displays the new task nested under its parent. The parent
must be in the same task list.

---

## `update_task(list_name, task_uid, ...)`

Same optional fields as `create_task` (minus `list_name`/`titel`'s "required" status,
plus):

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Task list containing the task |
| `task_uid` | string | yes | UID of the task to change |
| `status` | string enum | no | `"offen"` / `"in-arbeit"` / `"erledigt"` / `"abgesagt"` -> `STATUS` (see below) |
| `felder_leeren` | list of strings | no | Field names to clear (see below) |

Only fields explicitly present in the call are modified; everything else on the task
(including fields this server doesn't model) is preserved. Two things to know:

- Passing `erinnerungen` **replaces all existing reminders** with the new list. Pass
  `[]` to remove all reminders (equivalent to clearing `"erinnerungen"` via
  `felder_leeren`).
- A scalar field left as `None`/omitted is left unchanged. To actually remove a
  property (e.g. delete a due date), list its name in `felder_leeren` instead.
- `wiederholung`'s anchor requirement (see `create_task`'s "Recurrence"
  section) is checked against the task's *final* state after this call, not
  just the fields passed here. So calling `update_task` with only
  `wiederholung` set succeeds as long as the task already has a `start_datum`
  or `faellig_datum` from before — but clearing the task's only anchor
  (`felder_leeren=["faellig_datum"]`) while a recurrence is set or remains is
  rejected the same way.

### Status (`status`)

**BREAKING CHANGE**: `status` used to be an implicit, read-only two-value field
(`"offen"`/`"erledigt"`, derived solely from `complete_task`). It is now settable
directly via `update_task`, with two more values:

- `"erledigt"` behaves exactly like `complete_task`: sets `STATUS:COMPLETED`,
  `PERCENT-COMPLETE:100`, and a `COMPLETED` timestamp.
- `"offen"` is the **reopen** path — for a task completed by mistake, or one you
  want to resume: removes the `COMPLETED` timestamp and resets `PERCENT-COMPLETE`
  to `0`.
- `"in-arbeit"` and `"abgesagt"` set `STATUS` (`IN-PROCESS`/`CANCELLED`
  respectively) and keep whatever `PERCENT-COMPLETE` was recorded. Like
  `"offen"`, they also drop a `COMPLETED` timestamp left over from an earlier
  completion — without that, the task would report its new status while
  staying invisible to `nur_offene=True`, which filters on the presence of
  that timestamp (see the note under `list_tasks`).
- If the same call *also* passes `fortschritt_prozent`, that explicit value wins
  over whatever percentage `status` would otherwise derive — `status` is applied
  first internally, then `fortschritt_prozent` (if given) overwrites it. So
  `{"status": "erledigt", "fortschritt_prozent": 55}` ends up at `55`%, not `100`%.
- An unknown value is a speaking error listing the four accepted labels; nothing is
  written to the task in that case (no partial update).
- `status` is **not** accepted in `felder_leeren` — there is nothing to "clear";
  set `status="offen"` to reopen a task instead.

Note the interaction with `nur_offene` above: setting `status="abgesagt"` removes
the task from `list_tasks`'s default (`nur_offene=True`) results, the same as
`"erledigt"` does.

### Clearing fields (`felder_leeren`)

`felder_leeren` is a list of field names to remove from the task entirely, rather
than change. Accepted values:

`"start_datum"`, `"faellig_datum"`, `"prioritaet"`, `"fortschritt_prozent"`, `"ort"`,
`"url"`, `"tags"`, `"erinnerungen"`, `"notizen"`, `"sichtbarkeit"`,
`"uebergeordnete_aufgabe"`, `"wiederholung"`.

`"titel"` cannot be cleared (a task always needs a title) and is not accepted. Naming
an unknown field, or naming a field in `felder_leeren` that is *also* given a new
value in the same call, is an error.

Example — remove the due date and location, and clear all reminders, while also
setting a new priority:

```json
{
  "list_name": "Personal",
  "task_uid": "0f8ba4a4-...",
  "prioritaet": "niedrig",
  "felder_leeren": ["faellig_datum", "ort", "erinnerungen"]
}
```

Returns `{"uid": "<task_uid>"}`.

---

## `complete_task(list_name, task_uid)`

Marks the task as done: `STATUS:COMPLETED`, `PERCENT-COMPLETE:100`, and a `COMPLETED`
timestamp (current UTC time). Returns `{"uid": "<task_uid>"}`.

A task completed by mistake can be reopened with `update_task(status="offen")` (see
the "Status" section above) — there is no separate "uncomplete" tool.

Completing a parent task does **not** cascade to its subtasks.

### Completing a recurring task (`wiederholung`)

`complete_task` only ever touches `STATUS`/`COMPLETED`/`PERCENT-COMPLETE` on
the task itself — it does **not** roll the series forward to a next
occurrence (no new `DTSTART`/`DUE`, no separate follow-up task object is
created). This is this server's own observed behaviour, verified by test
(`test_mapping.py`'s `test_mark_completed_leaves_wiederholung_intact`,
`test_caldav_client.py`'s `test_complete_task_leaves_rrule_intact`): the
task's `RRULE` survives completion unchanged, and the task simply comes back
from `list_tasks`/`get_task` with `status="erledigt"` while still carrying
its original `wiederholung`.

Practically, **completing a recurring task ends it** as far as this server
is concerned — to keep a recurring series "going", advance `faellig_datum`
(via `update_task`) to the next due date yourself instead of calling
`complete_task`.

Whether the Nextcloud Tasks web app (or another CalDAV client) additionally
materializes/displays a "next" occurrence once a recurring `VTODO` is marked
`COMPLETED` this way is **not verified here** — that would require observing
the app's own client-side behaviour against a live server, which is outside
what this server's code controls or this test suite checks. An opt-in
integration test
(`test_integration.py::test_recurring_task_completion_behaviour_against_a_real_server`,
gated behind `RUN_INTEGRATION_TESTS=1`) creates a recurring task, completes
it, and records what comes back from a real server, so this can be confirmed
independently later.

---

## `delete_task(list_name, task_uid)`

Permanently deletes the task from the server — there is no trash bin at the CalDAV
level. Deleting a parent does not delete its subtasks; they keep a dangling
`RELATED-TO` reference and become top-level tasks in most clients.
Returns `{"uid": "<task_uid>"}`.

---

## Task-list management

### `create_task_list(display_name)`

Creates a new task list (a CalDAV collection supporting VTODO). A URL-safe
collection id is derived from the name (`"Grocery List!"` → `grocery-list`);
if that id is occupied (including by a deleted list still in Nextcloud's
trashbin), `-2`, `-3`, … suffixes are tried automatically. A display-name
conflict with an existing task list fails instead. Returns
`{"name": ..., "url": ...}`.

### `rename_task_list(list_name, new_display_name)`

Changes only the display name; the URL/id stays stable. Fails if another task
list already has the new name.

### `delete_task_list(list_name)`

Permanently deletes the list **and every task inside it**. Returns
`{"list_name": ...}`.

---

## Calendar management (VEVENT)

### `list_calendars()`

No parameters. Returns every VEVENT-supporting calendar:

```json
[
  {
    "name": "Personal",
    "url": "https://cloud.example.com/remote.php/dav/calendars/demo/personal/",
    "farbe": "#00679e",
    "komponenten": ["VEVENT"]
  }
]
```

### `create_calendar(display_name, farbe=None)`

Creates a new VEVENT calendar (CalDAV `MKCALENDAR`), optionally with a color.
Collection-id handling matches `create_task_list` (auto-suffix on occupied
ids). Returns `{"name", "url", "farbe"}`.

### `update_calendar(calendar_name, new_display_name=None, farbe=None)`

Renames and/or recolors a calendar (CalDAV `PROPPATCH`); at least one of the
two optional parameters is required. The URL/id stays stable.

### `delete_calendar(calendar_name)`

Permanently deletes the calendar **and every event inside it**. Returns
`{"calendar_name": ...}`.

---

## `list_events(kalender_namen=None, von=None, bis=None, suchtext=None, tag=None, limit=None, wiederholungen_aufloesen=False)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_namen` | list of strings | no | Calendars to query; `null` = all event calendars |
| `von` | string (ISO 8601) | no | Lower bound; date-only = start of that day |
| `bis` | string (ISO 8601) | no | Upper bound; date-only includes that whole day |
| `suchtext` | string | no | Case-insensitive substring over `titel`, `beschreibung`, `ort` |
| `tag` | string | no | Exact (case-insensitive) match against one `tags` entry |
| `limit` | integer | no | Max results, must be `> 0`; applied last (earliest events win) |
| `wiederholungen_aufloesen` | boolean | no (default `false`) | Expand recurring events into single occurrences within `[von, bis]` (both bounds required) |

The time-range filter runs server-side (CalDAV `time-range` REPORT), so a
recurring event with an occurrence in the window matches even if its master
event started long before. Results are sorted by `start`. One event dict:

```json
{
  "uid": "7f0c9e2a-...",
  "titel": "Team-Meeting",
  "start": "2026-07-20T14:00:00+02:00",
  "ende": "2026-07-20T15:00:00+02:00",
  "ganztaegig": false,
  "ort": "Konferenzraum",
  "beschreibung": "Sprint-Planung",
  "tags": ["Arbeit"],
  "erinnerungen": ["-PT30M"],
  "status": "bestätigt",
  "sichtbarkeit": null,
  "wiederholung": "FREQ=WEEKLY;BYDAY=MO",
  "ausnahme_daten": ["2026-07-27T14:00:00+02:00"],
  "url": null,
  "verknuepfte_aufgaben": [{"uid": "0f8ba4a4-...", "beziehung": "zeitblock"}],
  "wiederholung_von": null,
  "kalender": "Personal",
  "organisator": {"email": "chef@example.com", "name": "Chefin"},
  "teilnehmer": [
    {
      "email": "kollege@example.com",
      "name": "Kollege",
      "status": "zugesagt",
      "rolle": "erforderlich",
      "rsvp": true
    }
  ]
}
```

`wiederholung_von` carries the `RECURRENCE-ID` when `wiederholungen_aufloesen`
materialized a single occurrence of a series. For **all-day** events `start`
and `ende` are date-only strings and `ende` is the **inclusive** last day
(RFC 5545's exclusive `DTEND` is translated on the way in and out).

`verknuepfte_aufgaben` entries' `beziehung` uses exactly the same vocabulary
as `link_task_to_event`'s `beziehung` parameter: a link written as
`"zeitblock"` reads back as `"zeitblock"`, and one written as
`"voraussetzung"` reads back as `"voraussetzung"` - request and response are
the same words, round-trip. `"gleichrangig"` (RFC 5545 `SIBLING`) or a raw
lowercased `RELTYPE` can also appear for links written by other CalDAV
clients that this server didn't create.

`organisator` is the event's `ORGANIZER` ({"email", "name"}), or `null` if
the event has no attendees/organizer. `teilnehmer` lists every `ATTENDEE`
(`[]` if none); `rsvp` reflects whether the attendee's `RSVP` parameter is
`TRUE` (missing `RSVP` reads as `false`, per RFC 5545's default). See
`create_event`'s `teilnehmer` for how to set attendees, and
`respond_to_event` for replying to an invitation.

---

## `get_event(kalender_name, event_uid)`

Fetches a single event by UID; same dict shape as one `list_events` entry.

---

## `create_event(kalender_name, titel, start, ...)`

Required: `kalender_name`, `titel`, `start`. Optional fields and their CalDAV
mapping:

| Parameter | CalDAV property | Notes |
|---|---|---|
| `ende` | `DTEND` | Same type as `start` (both dates or both datetimes); all-day: inclusive last day |
| `ort` | `LOCATION` | |
| `beschreibung` | `DESCRIPTION` | |
| `tags` | `CATEGORIES` | list of strings |
| `status` | `STATUS` | `"bestätigt"`→CONFIRMED, `"vorläufig"`→TENTATIVE, `"abgesagt"`→CANCELLED |
| `sichtbarkeit` | `CLASS` | same values as tasks |
| `wiederholung` | `RRULE` | raw RFC 5545 text, e.g. `"FREQ=WEEKLY;BYDAY=MO;COUNT=10"` |
| `ausnahme_daten` | `EXDATE` | list of ISO dates/datetimes: occurrences of the series to skip |
| `erinnerungen` | `VALARM` | relative durations (e.g. `"-PT30M"`) trigger before `start`; absolute ISO datetimes as-is |
| `url` | `URL` | |
| `verknuepfte_aufgabe` | `RELATED-TO;RELTYPE=PARENT` | UID of a task this event reserves time for |
| `teilnehmer` | `ATTENDEE` (one per entry) | list of attendee dicts, see below |

Returns `{"uid": ...}`.

To move or cancel a **single occurrence** of a recurring event: add its
original date to `ausnahme_daten` (via `update_event`) and, for a move, create
a separate replacement event.

### Attendees (`teilnehmer`)

Each entry:

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `email` | string | yes | — | Attendee's email -> `ATTENDEE:mailto:<email>` |
| `name` | string | no | — | -> `CN` parameter |
| `rolle` | string enum | no | `"erforderlich"` | -> `ROLE` parameter (see enum table above) |
| `rsvp` | boolean | no | `true` | -> `RSVP` parameter |

Every written `ATTENDEE` also gets `PARTSTAT=NEEDS-ACTION` and
`CUTYPE=INDIVIDUAL`. The first time attendees are added to an event that has
none yet, `ORGANIZER` is set automatically to your own account's address (an
event that already has attendees keeps whatever `ORGANIZER` it already has).

**Important — server-side scheduling:** Nextcloud's CalDAV server sends iMIP
invitation emails automatically when an event with `ORGANIZER` and
`ATTENDEE`s is saved by the organizer. This tool does not send any mail
itself; saving the event is what triggers Nextcloud to do so.

Example:

```json
{
  "kalender_name": "Termine",
  "titel": "Sprint-Planung",
  "start": "2026-07-20T14:00:00",
  "ende": "2026-07-20T15:00:00",
  "teilnehmer": [
    {"email": "alice@example.com", "name": "Alice", "rolle": "leitung"},
    {"email": "bob@example.com", "rolle": "optional", "rsvp": false}
  ]
}
```

---

## `update_event(kalender_name, event_uid, ...)`

Same fields as `create_event`, all optional. Only fields you pass are changed;
`erinnerungen` and `ausnahme_daten` replace all existing entries, and so does
`teilnehmer` — passing it **replaces the entire attendee list**, it does not
add to it. `felder_leeren` removes properties entirely — accepted names:
`ende`, `ort`, `beschreibung`, `tags`, `status`, `sichtbarkeit`,
`wiederholung`, `ausnahme_daten`, `erinnerungen`, `url`,
`verknuepfte_aufgabe`, `teilnehmer` (`titel` and `start` cannot be cleared; a
field can't be both set and cleared in one call).

Clearing `"teilnehmer"` removes every `ATTENDEE` and, since an `ORGANIZER`
with no attendees is meaningless, also removes `ORGANIZER` if none remain.

To **respond** to an event you were invited to (set your own RSVP status),
use `respond_to_event` instead of setting `teilnehmer` here — `teilnehmer`
replaces the whole list and would overwrite everyone else's replies too.

---

## `respond_to_event(kalender_name, event_uid, antwort, kommentar=None)`

Replies to a calendar invitation: finds **your own** `ATTENDEE` entry on the
event (matched against your account's CalDAV calendar-user-addresses,
case-insensitive, `mailto:` ignored) and sets its `PARTSTAT`. Fails with a
clear error if you are not listed as an attendee of this event at all.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Calendar containing the event |
| `event_uid` | string | yes | UID of the event to respond to |
| `antwort` | string enum | yes | `"zugesagt"` / `"abgesagt"` / `"vorläufig"` -> `PARTSTAT` |
| `kommentar` | string | no | -> `COMMENT` |

Returns `{"uid": event_uid, "antwort": antwort}`.

Saves the event afterwards; Nextcloud's CalDAV server propagates the reply to
the organizer as an iMIP/iTIP reply mail automatically — same server-side
scheduling mechanism that sends the original invitations, this tool does not
send any mail itself.

---

## `delete_event(kalender_name, event_uid)`

Permanently deletes the event.

---

## `link_task_to_event(list_name, task_uid, kalender_name, event_uid, beziehung="zeitblock")`

Links an existing task (VTODO) to an existing event (VEVENT) via a
cross-component `RELATED-TO` property. The property is written **on the
event** — the Nextcloud Tasks app interprets a task-side `RELATED-TO` as
"subtask of", so a task-side link would garble its task tree, while the
calendar app simply round-trips the property as raw data (it is not shown in
either web UI; it is visible in the `verknuepfte_aufgaben` field of this
server's event dicts).

- `"zeitblock"` — the event reserves time to work on the task (event is the
  task's *child*, `RELTYPE=PARENT` pointing at the task).
- `"voraussetzung"` — the event must happen before the task can be completed
  (event is the task's *parent*, `RELTYPE=CHILD` pointing at the task).

The task must exist; linking is idempotent (re-linking the same pair is a
no-op).

---

## `list_events_for_task(list_name, task_uid, kalender_namen=None)`

The task-side counterpart of `link_task_to_event`: since the `RELATED-TO`
link is only ever written on the event, there is no direct way to find
linked events starting from a task — this tool does the reverse lookup,
scanning events in the given calendars for a `verknuepfte_aufgaben` entry
whose `uid` matches `task_uid`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list containing the task |
| `task_uid` | string (UID) | yes | UID of the task to find linked events for |
| `kalender_namen` | list of strings | no | Calendars to search; `null` = all event calendars |

The task must exist (same check and error as `link_task_to_event`). Returns
event dicts with the same shape as `list_events` entries, each with an added
`"kalender_name"` key, sorted by start:

```json
[
  {
    "uid": "7f0c9e2a-...",
    "titel": "Steuererklärung vorbereiten",
    "start": "2026-07-20T14:00:00+02:00",
    "ende": "2026-07-20T15:00:00+02:00",
    "ganztaegig": false,
    "ort": null,
    "beschreibung": null,
    "tags": [],
    "status": null,
    "sichtbarkeit": null,
    "wiederholung": null,
    "ausnahme_daten": [],
    "url": null,
    "verknuepfte_aufgaben": [{"uid": "0f8ba4a4-...", "beziehung": "zeitblock"}],
    "wiederholung_von": null,
    "kalender_name": "Personal"
  }
]
```

---

## `create_event_from_task(list_name, task_uid, kalender_name, start=None, dauer_minuten=None, ende=None, beschreibung=None, erinnerungen=None, sichtbarkeit=None)`

Timeboxing: creates an event from an existing task and links the two (the
`"zeitblock"` semantics above). `titel`, `ort` and `tags` are always copied
from the task; the task itself is not modified.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `start` | string (ISO 8601) | no | Defaults to the task's `faellig_datum`; fails if the task has neither |
| `dauer_minuten` | integer | no | Event length in minutes from `start`; mutually exclusive with `ende` |
| `ende` | string (ISO 8601) | no | Explicit event end, as an alternative to `dauer_minuten` |
| `beschreibung` | string | no | Event description; `None` (default) inherits the task's `notizen`, `""` sets an empty description |
| `erinnerungen` | list of strings | no | Same format as `create_event`'s `erinnerungen` -> `VALARM` |
| `sichtbarkeit` | string enum | no | Same as tasks/events -> `CLASS` |

- A date-only `start` produces a one-day all-day event; `ende` must then also
  be a date (or omitted) — this is the same start/end consistency check
  `create_event` uses, not a separate rule.
- `ende` and `dauer_minuten` are mutually exclusive — passing both is an error
  naming both parameters. With **neither** given, the event runs 60 minutes
  (unchanged from before this parameter split; `dauer_minuten` now defaults to
  `None` rather than `60` purely so a call can tell "not given" apart from
  "given as 60" — existing calls that only ever passed `dauer_minuten`
  continue to behave identically).
- `dauer_minuten` has no effect on an all-day start — the event stays a
  one-day all-day event unless `ende` gives it a later **date**. A *datetime*
  `ende` on an all-day start is rejected with the same start/end type error
  `create_event` raises (`start and ende must both be all-day dates or both be
  datetimes`), not silently ignored.
- `beschreibung`: use `None` vs. `""` deliberately — `None` inherits the
  task's `notizen` (the original behavior), an explicit `""` clears the
  description instead of inheriting anything.

Returns `{"uid": <event uid>, "task_uid": <task uid>}`.

---

## `get_agenda(datum, kalender_namen=None, listen_namen=None)`

One day's calendar events and due tasks together — CalDAV has no combined
VEVENT+VTODO query, so this is composed server-side. `datum` must be a
date-only `"YYYY-MM-DD"` string; day boundaries are in the server's default timezone
(consistent with the naive-input rule).

```json
{
  "datum": "2026-07-20",
  "termine": [ ... ],
  "aufgaben": [ ... ]
}
```

`termine` are event dicts (recurring events expanded to that day's
occurrences, sorted by start); `aufgaben` are open tasks due that day, each
with an added `"liste"` key naming its task list.

Every entry in both lists also carries a `"quelle_url"` key — the CalDAV URL
of the exact calendar/task list it came from, alongside the display name in
`"kalender"`/`"liste"`. Nextcloud doesn't enforce unique display names, so two
collections can share one; `quelle_url` is what lets a surprising agenda
entry be traced back to one specific collection instead of guessed at.
`list_events`/`list_tasks` do not include this key — it's added here only.

Calendar/task-list listings and resolved names are cached for up to a minute,
so a collection renamed or deleted in the Nextcloud web UI can take that long
to be reflected here — but no longer than that, including when the freed-up
name is immediately given to a different collection. This applies to every
tool that addresses a calendar or list by display name, not just `get_agenda`.

**Breaking change:** `aufgaben` now comes back sorted by `faellig_datum` (then
by `titel`), not in the order the server happened to return each list — the
tasks are fetched through `list_tasks`, so they inherit its sort. With
`listen_namen=None` the tasks of *all* lists are interleaved chronologically
instead of arriving grouped per list.

---

## `get_free_busy(von, bis, benutzer=None)`

Busy time intervals in `[von, bis]`, for yourself or another Nextcloud user.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `von` | string (ISO 8601) | yes | Range start; date-only = start of that day |
| `bis` | string (ISO 8601) | yes | Range end; date-only includes that whole day |
| `benutzer` | string | no | Nextcloud user id or email of another account; `null` = your own availability |

With `benutzer` omitted, busy blocks are computed by aggregating your own
event calendars: non-cancelled (`STATUS` ≠ `CANCELLED`), non-transparent
(`TRANSP` ≠ `TRANSPARENT`) events in range each contribute a busy interval,
which are then merged (overlapping and back-to-back blocks become one) and
sorted.

With `benutzer` set, this sends a CalDAV `RFC 6638` free-busy scheduling
request to the Nextcloud server for that user — **the server resolves
`benutzer`**, not this tool. If the server can't provide free/busy
information for that user (unknown account, scheduling disabled, ...), the
call fails with an error rather than silently returning an empty (looks
"fully free") result.

```json
{
  "von": "2026-07-20T00:00:00+02:00",
  "bis": "2026-07-21T00:00:00+02:00",
  "benutzer": null,
  "belegt": [
    {"von": "2026-07-20T14:00:00+02:00", "bis": "2026-07-20T15:00:00+02:00"}
  ]
}
```

`belegt` ("busy") is the merged, sorted list of busy intervals; empty if the
user is free the whole range.

---

## Calendar sharing

Nextcloud-specific DAV extension (not part of any CalDAV RFC) — these three
tools only work against a real Nextcloud server, not a generic CalDAV
server. All three resolve `kalender_name` across **both** task lists and
event calendars (whichever kind has that display name).

### `share_calendar(kalender_name, empfaenger, gruppe=False, schreibzugriff=False)`

Shares a task list or event calendar with a Nextcloud user or group.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the task list or event calendar |
| `empfaenger` | string | yes | Nextcloud user id, or group id when `gruppe=True` |
| `gruppe` | boolean | no (default `false`) | `empfaenger` names a group instead of a user |
| `schreibzugriff` | boolean | no (default `false`) | Grant read-write instead of read-only access |

Calling this again for the same `empfaenger` updates their access level
rather than creating a duplicate share. Returns:

```json
{"kalender_name": "Privat", "empfaenger": "bob", "schreibzugriff": true}
```

### `unshare_calendar(kalender_name, empfaenger, gruppe=False)`

Removes a user's or group's share of a task list or event calendar. A no-op
(not an error) if `empfaenger` doesn't currently have a share. Returns
`{"kalender_name": ..., "empfaenger": ...}`.

### `list_calendar_shares(kalender_name)`

Lists everyone a task list or event calendar is currently shared with:

```json
[
  {"empfaenger": "bob", "typ": "benutzer", "schreibzugriff": true, "status": "akzeptiert"},
  {"empfaenger": "team", "typ": "gruppe", "schreibzugriff": false, "status": "ausstehend"}
]
```

See the enum table above for `typ`/`status` values; an invite status the
server reports that isn't one of the known ones comes back lowercased
instead of being dropped.

---

## Trash bin

Nextcloud-specific `calendar-trashbin` DAV plugin — deleting a task or event
(`delete_task`/`delete_event`, or deleting a whole list/calendar) moves it
here rather than purging it immediately. There is deliberately no tool to
empty the trash or permanently delete an item; only listing and restoring.

### `list_trash()`

No parameters. Returns every deleted task/event still in the trash bin:

```json
[
  {
    "id": "42.ics",
    "titel": "Einkaufen",
    "typ": "aufgabe",
    "kalender": "personal",
    "geloescht_am": "2026-07-10T14:00:00+02:00"
  }
]
```

`id` is opaque — pass it to `restore_from_trash` verbatim. `titel`/`typ` are
derived from the deleted item's own data and are `null` if that can't be
read; `kalender` is the original calendar's URI if the server reports it, or
`null`. On a server without the trashbin plugin (non-Nextcloud), this fails
with a clean "trash bin not available on this server" error.

### `restore_from_trash(id)`

Restores a deleted task/event to its original calendar.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Trash item id, from `list_trash`'s `"id"` field |

Returns `{"id": ...}` on success. Fails with a clean error if `id` isn't
currently in the trash bin (already restored, or never existed).

---

## Notes

Nextcloud's Notes app, over its own plain JSON REST API — unrelated to
CalDAV/the tools above. A note's living-document use case (current state,
decisions + rationale, open questions, next step) is a convention for how
you write `inhalt`, not something this server enforces.

### `list_notizen(kategorie=None)`

Lists every note without its content, to keep the listing cheap.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kategorie` | string | no | Filter to notes in this category |

```json
[{"id": 42, "titel": "Projekt X", "kategorie": "Arbeit", "favorit": false, "geaendert": "2026-07-20T12:00:00+00:00"}]
```

### `get_notiz(notiz_id)`

Fetches one note, including its full content.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `notiz_id` | integer | yes | Note id, from `list_notizen`/`search_notizen` |

```json
{"id": 42, "titel": "Projekt X", "kategorie": "Arbeit", "inhalt": "...", "favorit": false, "geaendert": "2026-07-20T12:00:00+00:00", "schreibgeschuetzt": false}
```

### `create_notiz(titel, kategorie=None, inhalt=None, favorit=None)`

Creates a note. Only `titel` is required. Returns the created note, same
shape as `get_notiz`.

### `update_notiz(notiz_id, titel=None, kategorie=None, inhalt=None, favorit=None)`

Updates only the fields explicitly given; `inhalt` **replaces** the existing
content wholesale (use `append_notiz` to add to it instead). At least one
field must be given. Returns the updated note, same shape as `get_notiz`.

### `append_notiz(notiz_id, text)`

Appends `text` to a note's existing content (blank-line-separated if the note
already has content). Implemented as a read-then-write — the Notes API has
no atomic append — so a concurrent edit to the same note between the two may
be lost. Returns the updated note, same shape as `get_notiz`.

### `search_notizen(suchtext, kategorie=None)`

Case-insensitive substring search over title and content. The Notes API has
no server-side full-text search, so this fetches the (optionally
category-filtered) notes and filters client-side. Returns matches in the
same shape as `list_notizen` (no content).

### `delete_notiz(notiz_id)`

Permanently deletes a note.

WARNING: this is irreversible from this server's point of view - this server cannot restore a deleted note. Confirm with the user before calling this.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `notiz_id` | integer | yes | Note id, from `list_notizen`/`search_notizen` |

Returns `{"id": notiz_id}` on success.

### Verifying the notes workflow

An opt-in live-server integration test suite in `tests/test_integration.py` verifies the notes workflow end-to-end against a real Nextcloud instance.

#### Running the integration tests

To run the live-server notes suite, set `RUN_INTEGRATION_TESTS=1` along with your Nextcloud instance environment variables:

```bash
RUN_INTEGRATION_TESTS=1 \
NEXTCLOUD_BASE_URL="https://cloud.example.com" \
NEXTCLOUD_USERNAME="your-username" \
NEXTCLOUD_APP_PASSWORD="your-app-password" \
uv run pytest tests/test_integration.py -k "notes or nonexistent_note"
```

The filter has to cover both tests: `-k test_notes` alone silently deselects
`test_get_nonexistent_note_raises_notiz_not_found` and reports success on
partial coverage.

#### Created objects and cleanup

- **Objects created**: A single disposable note titled `"mcp-notes-test"` in category `"mcp-test"`.
- **Cleanup**: The note is automatically cleaned up in a `finally` block using `NotesService.delete_note(notiz_id)`.

#### Manual verification checklist

While automated integration tests verify API round-trips, client rendering and sync across Nextcloud apps require manual verification:

1. **Web UI & Mobile App verification**: After writing or updating a note via `create_notiz` or `update_notiz`, open the note in the Nextcloud web UI and in the Notes mobile app (Android / iOS). Confirm that formatting, special characters (German umlauts), emojis, fenced code blocks, and trailing lines render identically.
2. **App-to-MCP readback**: Edit the note within the Nextcloud Notes mobile app (or web UI), save it, then fetch it back using `get_notiz`. Confirm that both show the exact same updated content without data loss.

---

## ICS import / export

### `export_calendar(kalender_name)`

Exports a task list or event calendar as a single ICS (VCALENDAR) text
containing every task/event in it.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the task list or event calendar |

```json
{"kalender_name": "Privat", "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n..."}
```

Built with a single `PRODID`/`VERSION` header; a recurring event/task and its
override instances are kept together, and `VTIMEZONE` components are
de-duplicated by `TZID`.

### `import_ics(kalender_name, ics)`

Imports ICS text into an existing task list or event calendar.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the target task list or event calendar |
| `ics` | string | yes | Full ICS text; must be a VCALENDAR with at least one VEVENT or VTODO |

Top-level `VEVENT`/`VTODO` components are grouped by `UID`, so a recurring
event/task and its override instances are saved together as one calendar
object (along with any `VTIMEZONE`s from the source ICS). A component whose
kind the target calendar doesn't support (e.g. a `VEVENT` in an ICS file
being imported into a plain task list) is skipped rather than failing the
whole import.

```json
{"kalender_name": "Privat", "importiert": 3, "uebersprungen": 1}
```

`importiert` is the number of calendar objects created; `uebersprungen`
("skipped") the number of UID groups whose component kind wasn't supported
by the target calendar. Malformed ICS text is rejected with a clean error
that includes the parser's detail message.

---

## Errors

All failures come back as short, single-line MCP tool errors, for example:

- `Task list 'Einkuafsliste' was not found.` — typo in the list name; call
  `list_task_lists` to see valid names.
- `Task 'abc-123' was not found.` — stale or wrong UID.
- `Multiple task lists are named 'Personal', which is ambiguous. Rename the task lists
  in Nextcloud so each has a distinct name, or use a different, unambiguous list name.`
  — two calendars share the same display name; the server can't tell which one you
  mean.
- `Nextcloud rejected the CalDAV credentials (check username/app password).`
- `Could not reach the Nextcloud server (connection refused or timed out).`
- `The requested note was not found.` — stale or wrong `notiz_id`; call
  `list_notizen`/`search_notizen` to find the current one.
- `Nextcloud rejected the Notes API credentials (check username/app password).`
- `The task was modified by another client since it was last read (conflicting edit).
  Re-fetch the task and retry.` — another client (e.g. the Nextcloud Tasks app) changed
  this task between your last read and this write; re-fetch it with `list_tasks` and
  retry the change.
- `Unknown prioritaet 'dringend'. Expected one of: hoch, mittel, niedrig.`
- `Unknown status 'fertig'. Expected one of: offen, in-arbeit, erledigt, abgesagt.` —
  `update_task`'s `status` parameter; nothing is written to the task.
- `ende and dauer_minuten cannot both be given; pass at most one to control how long
  the event runs.` — `create_event_from_task`.
- `Could not parse Erinnerung '1 Tag vorher': expected an ISO 8601 duration like '-P1D' / '-PT1H', or an absolute ISO 8601 datetime.`
- `Unknown felder_leeren entry/entries: telefonnummer. Expected one of: start_datum,
  faellig_datum, prioritaet, fortschritt_prozent, ort, url, tags, erinnerungen, notizen,
  sichtbarkeit, uebergeordnete_aufgabe.`
- `Cannot both set and clear the same field in one call: faellig_datum.`
- `limit must be greater than 0, got 0.` — `list_tasks`'s `limit` parameter was `<= 0`.
- `Calendar 'Termine' was not found.` — typo in the calendar name, or the calendar
  supports no VEVENTs; call `list_calendars` to see valid names.
- `Event 'abc-123' was not found.` — stale or wrong event UID.
- `A calendar named 'Termine' already exists.`
- `farbe must look like '#RRGGBB' (or '#RRGGBBAA'), got 'rot'.`
- `Could not parse wiederholung 'jeden Montag' as an RFC 5545 RRULE (e.g. 'FREQ=WEEKLY;BYDAY=MO').`
- `start and ende must both be all-day dates or both be datetimes; got one of each. ...`
- `Expanding recurring events requires both von and bis bounds.`
- `Unknown beziehung 'egal'. Expected one of: zeitblock, voraussetzung.`
- `The task has no faellig_datum (due date); pass an explicit start for the event instead.`
- `datum must be a date-only 'YYYY-MM-DD' string, got '2026-07-20T14:00:00'.`
- `Unknown rolle 'chef'. Expected one of: leitung, erforderlich, optional, keine-teilnahme.`
- `Unknown antwort 'vielleicht'. Expected one of: zugesagt, abgesagt, vorläufig.`
- `You are not listed as an attendee of this event, so there is nothing to respond to.`
- `Nextcloud could not provide free/busy information for 'bob@example.com' (the user may
  be unknown, or scheduling may be disabled on the server).`
- `Calendar or task list 'Ghost' was not found.` — `share_calendar`/`export_calendar`/etc.
  found no task list or event calendar with this name.
- `empfaenger is required to share a calendar.`
- `Nextcloud could not find user/group 'ghost' to share 'Privat' with.` — `empfaenger`
  isn't a real Nextcloud user/group id.
- `Nextcloud denied sharing 'Privat' with 'bob' (permission denied, or the sharing
  backend is disabled).`
- `The trash bin is not available on this server.` — the server isn't Nextcloud, or
  doesn't have the calendar-trashbin plugin.
- `Trash item '42.ics' was not found in the trash bin.` — already restored, or a bad id.
- `ics must be a VCALENDAR.` / `ics must contain at least one VEVENT or VTODO component.`
- `Could not parse ics: ...` — malformed ICS text; the message includes the parser's detail.

Requests without a valid OAuth access token are rejected earlier, at the HTTP level
(`401`), before reaching tool logic — see [Authentication](../README.md#authentication).
