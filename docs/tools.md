# Tool reference

Detailed reference for all MCP tools (task, task-list, calendar and event
tools), including argument/result examples. Parameter names are the literal
MCP schema names (German field names in ASCII transliteration).

Values for enum-like fields:

| Field | Allowed values |
|---|---|
| `prioritaet` | `"hoch"`, `"mittel"`, `"niedrig"` |
| `sichtbarkeit` | `"öffentlich"`, `"privat"`, `"vertraulich"` |
| `status` (in task results) | `"offen"`, `"erledigt"` |
| `status` (events) | `"bestätigt"`, `"vorläufig"`, `"abgesagt"` |
| `beziehung` (`link_task_to_event`) | `"zeitblock"`, `"voraussetzung"` |
| `farbe` | `"#RRGGBB"` or `"#RRGGBBAA"` |
| `teilnehmer[].status` (read-only, in event results) | `"ausstehend"`, `"zugesagt"`, `"abgesagt"`, `"vorläufig"`, `"delegiert"` |
| `teilnehmer[].rolle` | `"leitung"`, `"erforderlich"` (default), `"optional"`, `"keine-teilnahme"` |
| `antwort` (`respond_to_event`) | `"zugesagt"`, `"abgesagt"`, `"vorläufig"` |

Dates are ISO 8601 strings. Two rules apply everywhere a date/datetime is
accepted (`start_datum`, `faellig_datum`, `start`, `ende`, `von`, `bis`,
`ausnahme_daten` and absolute `erinnerungen` entries):

- A value that is exactly `"YYYY-MM-DD"` (e.g. `"2026-07-20"`) creates an
  **all-day** entry (iCalendar `VALUE=DATE`) — it comes back from `list_tasks`
  / `get_task` as `"2026-07-20"`, not a midnight datetime.
- Any other ISO 8601 datetime (e.g. `"2026-07-20T14:00:00"`,
  `"2026-07-20T14:00:00+02:00"`) is stored as a datetime. A **naive**
  datetime (no UTC offset) is interpreted as UTC.

---

## `list_task_lists()`

No parameters. Returns every VTODO-supporting calendar (task list) on the account:

```json
[
  {"name": "Privat", "url": "https://cloud.example.com/remote.php/dav/calendars/elias/privat/"},
  {"name": "Arbeit", "url": "https://cloud.example.com/remote.php/dav/calendars/elias/arbeit/"}
]
```

Note: Nextcloud keeps task lists and event calendars in the same CalDAV
namespace. Event-only calendars (e.g. the default "Personal" calendar) are
excluded here — they can't hold tasks; `list_calendars` is their counterpart.
A mixed calendar supporting both VEVENT and VTODO appears in both listings.

---

## `list_tasks(list_name, nur_offene=True, faellig_vor=None, faellig_nach=None, limit=None)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list |
| `nur_offene` | boolean | no (default `true`) | Exclude completed tasks |
| `faellig_vor` | string (ISO 8601) | no | Only tasks due at or before this point |
| `faellig_nach` | string (ISO 8601) | no | Only tasks due at or after this point |
| `limit` | integer | no | Max number of results; must be `> 0` |

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
    "notizen": "Belege sammeln",
    "uebergeordnete_uid": null,
    "wiederholung": null
  }
]
```

`uebergeordnete_uid` is the parent task's UID if this task is a subtask, otherwise `null`.
`wiederholung` is the task's raw RRULE text (e.g. `"FREQ=WEEKLY;BYDAY=MO"`) if it recurs,
otherwise `null` — **read-only**: this server has no tool to create or edit recurrence, it
only surfaces whether/how an existing task recurs.
Fields not set on the task are `null` (`tags` is `[]`, `fortschritt_prozent` is `0`).

### Filtering (`faellig_vor` / `faellig_nach` / `limit`)

- If either `faellig_vor` or `faellig_nach` is given, tasks with **no** `faellig_datum` at all
  are excluded from the result — a task without a due date can't be judged "before" or
  "after" anything.
- Both accept the same ISO 8601 date/datetime formats as `create_task`'s `faellig_datum`. A
  date-only bound (e.g. `"2026-07-20"`) is inclusive of the whole day: `faellig_vor` expands
  to the end of that day (`23:59:59` UTC), `faellig_nach` to the start of it (`00:00:00`
  UTC) — so an all-day task due exactly on the boundary date is included by either bound.
  A datetime bound (with a specific time) is used exactly as given.
- `faellig_vor` and `faellig_nach` can be combined to select a range.
- `limit` caps the number of results, applied *after* any due-date filtering. `limit <= 0`
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

Returns `{"uid": "<new task uid>"}`.

### Reminders (`erinnerungen`)

Each entry is either:

- a **relative** RFC 5545 duration, e.g. `"-P1D"` (1 day before), `"-PT1H"` (1 hour
  before), `"-PT15M"` (15 minutes before). Anchored to `faellig_datum`
  (`TRIGGER;RELATED=END`) when the task has one, otherwise to `start_datum`
  (`RELATED=START`). A relative reminder on a task with neither date is an error.
- an **absolute** ISO 8601 datetime, e.g. `"2026-07-19T09:00:00+02:00"`. Stored as a UTC
  `TRIGGER;VALUE=DATE-TIME` (values without an offset are assumed to already be UTC).

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
| `felder_leeren` | list of strings | no | Field names to clear (see below) |

Only fields explicitly present in the call are modified; everything else on the task
(including fields this server doesn't model) is preserved. Two things to know:

- Passing `erinnerungen` **replaces all existing reminders** with the new list. Pass
  `[]` to remove all reminders (equivalent to clearing `"erinnerungen"` via
  `felder_leeren`).
- A scalar field left as `None`/omitted is left unchanged. To actually remove a
  property (e.g. delete a due date), list its name in `felder_leeren` instead.

### Clearing fields (`felder_leeren`)

`felder_leeren` is a list of field names to remove from the task entirely, rather
than change. Accepted values:

`"start_datum"`, `"faellig_datum"`, `"prioritaet"`, `"fortschritt_prozent"`, `"ort"`,
`"url"`, `"tags"`, `"erinnerungen"`, `"notizen"`, `"sichtbarkeit"`,
`"uebergeordnete_aufgabe"`.

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

Completing a parent task does **not** cascade to its subtasks.

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
    "url": "https://cloud.example.com/remote.php/dav/calendars/elias/personal/",
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
  "start": "2026-07-20T14:00:00+00:00",
  "ende": "2026-07-20T15:00:00+00:00",
  "ganztaegig": false,
  "ort": "Konferenzraum",
  "beschreibung": "Sprint-Planung",
  "tags": ["Arbeit"],
  "status": "bestätigt",
  "sichtbarkeit": null,
  "wiederholung": "FREQ=WEEKLY;BYDAY=MO",
  "ausnahme_daten": ["2026-07-27T14:00:00+00:00"],
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
    "start": "2026-07-20T14:00:00+00:00",
    "ende": "2026-07-20T15:00:00+00:00",
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

## `create_event_from_task(list_name, task_uid, kalender_name, start=None, dauer_minuten=60)`

Timeboxing: creates an event from an existing task and links the two (the
`"zeitblock"` semantics above). `titel`, `notizen`→`beschreibung`,
`ort` and `tags` are copied; the task itself is not modified.

- `start` defaults to the task's `faellig_datum`; if the task has none, the
  call fails and you must pass `start` explicitly.
- A datetime start produces an event of `dauer_minuten` length; a date-only
  start produces a one-day all-day event (`dauer_minuten` is ignored).

Returns `{"uid": <event uid>, "task_uid": <task uid>}`.

---

## `get_agenda(datum, kalender_namen=None, listen_namen=None)`

One day's calendar events and due tasks together — CalDAV has no combined
VEVENT+VTODO query, so this is composed server-side. `datum` must be a
date-only `"YYYY-MM-DD"` string; day boundaries are UTC (consistent with the
naive-input-is-UTC rule).

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
  "von": "2026-07-20T00:00:00+00:00",
  "bis": "2026-07-21T00:00:00+00:00",
  "benutzer": null,
  "belegt": [
    {"von": "2026-07-20T14:00:00+00:00", "bis": "2026-07-20T15:00:00+00:00"}
  ]
}
```

`belegt` ("busy") is the merged, sorted list of busy intervals; empty if the
user is free the whole range.

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
- `The task was modified by another client since it was last read (conflicting edit).
  Re-fetch the task and retry.` — another client (e.g. the Nextcloud Tasks app) changed
  this task between your last read and this write; re-fetch it with `list_tasks` and
  retry the change.
- `Unknown prioritaet 'dringend'. Expected one of: hoch, mittel, niedrig.`
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

Requests without a valid OAuth access token are rejected earlier, at the HTTP level
(`401`), before reaching tool logic — see [Authentication](../README.md#authentication).
