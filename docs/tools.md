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

## `list_tasks(listen_namen=None, nur_offene=True, faellig_vor=None, faellig_nach=None, limit=None, prioritaet=None, tag=None, suchtext=None, list_name=None)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `listen_namen` | list of strings | no | Task list display names to query; `null` = all task lists |
| `nur_offene` | boolean | no (default `true`) | Exclude completed *and* cancelled tasks (see note below) |
| `faellig_vor` | string (ISO 8601) | no | Only tasks due at or before this point |
| `faellig_nach` | string (ISO 8601) | no | Only tasks due at or after this point |
| `limit` | integer | no | Max number of results; must be `> 0` |
| `prioritaet` | string enum | no | Filter by priority (`"hoch"`, `"mittel"`, `"niedrig"`) |
| `tag` | string | no | Exact (case-insensitive) match against one `tags` entry |
| `suchtext` | string | no | Case-insensitive substring match over `titel` and `notizen` |
| `list_name` | string | no | **Deprecated** alias for `listen_namen`; pass `listen_namen` instead (passing both is an error) |

> **BREAKING CHANGE**: Results are now sorted by `faellig_datum` ascending (tasks without a due date last, then by `titel`), rather than returned in server order.
> **BREAKING CHANGE**: Every task dict now includes a `"liste"` key containing the task list's display name.

A bare `list_tasks()` queries **every** task list on the account — one request
per list — and returns every open task in all of them. Narrow it with
`listen_namen`, a due-date bound or `limit` unless the whole account really is
what you want.

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
    "ausnahme_daten": [],
    "wiederholung_von": null,
    "serie_uid": null,
    "liste": "Privat"
  }
]
```

`erinnerungen` lists the task's alarms as relative duration strings (e.g. `"-P1D"`,
`"-PT30M"`) or absolute ISO 8601 datetimes with offset (e.g. `"2026-08-07T09:00:00+02:00"`),
matching the format `create_task`/`update_task` accepts. Absolute values are stored in UTC on
the wire (RFC 5545 requires that) but read back formatted in the server's default timezone
(`MCP_DEFAULT_TIMEZONE`), so `"...Z"` and `"...+00:00"` input come back with the local offset —
the same instant, not the same string. Reading a reminder and writing it back is safe: the
alarm is recognized as already present and left exactly as it is.

Two things about the strings themselves:

- Durations come back in their canonical spelling, so `"-P1W"` reads back as `"-P7D"` and
  `"-PT90M"` as `"-PT1H30M"` — the same trigger, a different string.
- Absolute values are rendered in the server's default timezone regardless of the zone (or
  offset) they were written or stored in — the same convention `start_datum`/`faellig_datum`
  follow — and `"...Z"` input reads back with the local offset — again the same instant, not
  the same characters.

**What is *not* listed.** Only alarms whose trigger this format can express appear here.
An alarm is left out when

- it is anchored to a date the task really has, but not the one a write would anchor it to —
  a start-anchored alarm on a task that has *both* `start_datum` and `faellig_datum`, since
  writing `"-PT30M"` back anchors it to the due date. (An alarm naming an anchor the task
  does *not* have — the common `TRIGGER:-PT30M` with no anchor named at all — is listed
  normally: there is only one date it can mean.)
- it is relative but the task has neither `faellig_datum` nor `start_datum`, so there is no
  date to be relative to and the string could not be written back at all;
- its trigger is a bare date, a repeated property, or missing entirely;
- its timezone name resolves to no zone this server knows (common Windows and Evolution zone
  names do resolve).

Such alarms still exist on the task and are **never touched by a write** — passing
`erinnerungen` replaces only the reminders you can see.

**Important — a hidden alarm keeps firing.** If a task carries one, writing `erinnerungen`
can leave the user with *two* notifications: the hidden alarm plus the one you just wrote.
This is easy to hit by accident — adding a `faellig_datum` and `erinnerungen: ["-PT30M"]` in one
`update_task` call turns the task's existing start-anchored reminder into a hidden one and
adds a due-anchored one beside it, and `erinnerungen` then reads back as a single
`["-PT30M"]`. The extra alarm cannot be removed through `erinnerungen`; clear
`"erinnerungen"` via `felder_leeren` (which removes *every* alarm) and write the list you
want afterwards, or use `export_calendar`/`import_ics` to see and edit the alarms in full.

**What a write does not carry over.** For the alarms that *are* listed, the string form has
no slot for a non-display action (`EMAIL`/`AUDIO`), an `ATTACH`, or `DURATION`/`REPEAT`
(alarm self-repetition). Those survive as long as that reminder stays in the list you write
back; changing a reminder's *time* replaces the alarm with a plain display one. For a
guaranteed verbatim round trip of everything — including a foreign client's alarms and their
dismissed state — use `export_calendar`/`import_ics`.

`uebergeordnete_uid` is the parent task's UID if this task is a subtask, otherwise `null`.
`wiederholung` is the task's raw RRULE text (e.g. `"FREQ=WEEKLY;BYDAY=MO"`) if it recurs,
otherwise `null` — set via `create_task`/`update_task`, see their `wiederholung` parameter.
`ausnahme_daten` lists the occurrences the series skips (`EXDATE`), `[]` if it has none.
`wiederholung_von` and `serie_uid` are `null` on a stored task and set only on an
expanded occurrence — see "Recurring tasks in listings" below.

### Recurring tasks in listings

A recurring task is stored once but is due many times. CalDAV servers do not
expand `VTODO` series the way they expand `VEVENT`s, so a weekly task used to
appear exactly once — at its original due date — in every listing, and never
again: "what is due next week" could not include a series started in March.

When `faellig_vor` is given, `list_tasks` therefore expands each recurring task
into one row per occurrence due inside the window, computed client-side from
its `RRULE` (its `ausnahme_daten` are skipped). `get_agenda` does the same for
its day. **Without `faellig_vor` there is no window to expand into**, and the
series is returned as the single stored row it is, `wiederholung` intact.
At most 100 occurrences per task are produced, whatever the window.

An expanded row is a **read-only view of one date in a series**:

| Key | On a stored task | On an expanded occurrence |
|---|---|---|
| `uid` | the task's UID | `"<serie_uid>#<occurrence>"` — **not** a task any tool accepts |
| `serie_uid` | `null` | the stored task's UID |
| `wiederholung_von` | `null` | the occurrence this row stands for |
| `wiederholung` | the `RRULE` | `null` — nothing about one occurrence recurs |
| `start_datum` / `faellig_datum` | as stored | this occurrence's, keeping the stored distance between the two |

Passing an expanded row's `uid` to `update_task`, `complete_task`,
`delete_task` or `get_task` is an error naming the series, not a silent edit of
the whole series. To act on the series, pass `serie_uid` — but note that
`update_task` changes *every* occurrence and `complete_task` **ends** the
series (see "Completing a recurring task" below). To make a series skip a
single date, add that date to its `ausnahme_daten`.

Occurrences keep their wall-clock time across daylight-saving transitions: a
task due "every Monday 09:00" is reported at `09:00+02:00` in summer and
`09:00+01:00` in winter, not at a fixed UTC instant. A series a foreign client
anchored to some *other* timezone is expanded in the server's default timezone,
which differs only between the two zones' transition dates.

Where the expansion cannot be trusted it degrades to the single stored row
rather than guessing: a series with no due date, an unreadable anchor, a
`start_datum` and `faellig_datum` of different value kinds, or a rule
`dateutil` refuses.
`liste` is the display name of the task list containing the task — the name every
other task tool takes. Nextcloud does allow two task lists to carry the *same*
display name. `liste` cannot tell them apart, but `liste_url` alongside it
carries the list's unique collection URL, which you can match against
`list_task_lists`. However, because no tool accepts a URL to act on, you still
cannot address such a list by name: it is reported as ambiguous. Renaming one
of them in Nextcloud is the only way to make those tasks addressable again.
Fields not set on the task are `null` (`tags` is `[]`, `fortschritt_prozent` is `0`).

### Filtering and Sorting

- `listen_namen`: pass a list of list names to query specific task lists, or `null` to query all task lists on the account. `list_name` is a deprecated alias that takes a single list name. Naming the same list twice queries it once. An empty list (`[]`) is an empty scope: no request, no results — the other filter arguments are still validated.
- `prioritaet`: filters by task priority (`"hoch"`, `"mittel"`, `"niedrig"`). An unknown priority raises an error (`InvalidTaskDataError`).
- `tag`: exact match against any entry in `tags`.
- `suchtext`: substring match over `titel` and `notizen` (skipping `null` values).
- `tag` and `suchtext` compare case-insensitively *and* independently of Unicode
  spelling: either encoding of `"ü"` matches the other, and `"STRASSE"` matches
  `"Straße"`.
- An empty string means "no filter" for every filter that takes one — `prioritaet`,
  `tag`, `suchtext`, `faellig_vor` and `faellig_nach` alike. `limit` is the one
  exception and still rejects `0`: `null` is how an integer parameter says "no
  limit", so `0` reads as a caller asking for zero results, which is an error.
- If either `faellig_vor` or `faellig_nach` is given, tasks with **no** readable `faellig_datum`
  are excluded from the result — a task without a due date can't be judged "before" or
  "after" anything, and neither can one whose stored due value this server cannot
  parse (a foreign client's `DUE` holding a bare time or a period). Such a task is
  listed normally when no due bound is given, sorting with the ones that have no
  due date.
- Both accept the same ISO 8601 date/datetime formats as `create_task`'s `faellig_datum`. A
  date-only bound (e.g. `"2026-07-20"`) is inclusive of the whole day: `faellig_vor` expands
  to the end of that day (`23:59:59`), `faellig_nach` to the start of it (`00:00:00`) — both
  in the server's default timezone (`MCP_DEFAULT_TIMEZONE`), so an all-day task due exactly
  on the boundary date is included by either bound. A datetime bound (with a specific time) is used exactly as given.
- `faellig_vor` and `faellig_nach` can be combined to select a range.
- Tasks are sorted by `faellig_datum` ascending (tasks without a readable due date sort last), then by `titel` — case-insensitively and with umlauts filed under their base letter (`"Ärztin"` between `"Apotheke"` and `"Zahnarzt"`, not behind both), not in raw codepoint order.
- `limit` caps the number of results, applied *last* after merging and sorting across lists. `limit <= 0`
  is an error (`InvalidTaskDataError`).

---

## `get_task(list_name, task_uid)`

Fetch a single task by UID, without listing the whole task list.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list |
| `task_uid` | string | yes | UID of the task to fetch |

Returns what one entry from `list_tasks` holds (see above), including
`wiederholung` — minus its `"liste"` key, since the list is `list_name`, which
you passed in.

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
| `ausnahme_daten` | list of strings (ISO 8601) | no | one `EXDATE` holding every entry |

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

Recurring tasks are anchored to the timezone they are written in, not to a
fixed UTC instant: a task due "every Monday 09:00" stays at 09:00 local across
a daylight-saving transition. A naive value is anchored to the server's default
timezone, a value that names an IANA zone (`"2026-07-20T09:00:00 Europe/Berlin"`)
to that one, and a value carrying only a numeric offset — which is what
`list_tasks`/`get_task` hand back — keeps the zone the task already has, so
reading a task and writing it back never re-anchors it.

### Exception dates (`ausnahme_daten`)

`ausnahme_daten` lists occurrences the series should skip, written as one
`EXDATE` property. Setting it **replaces** the task's whole exception set;
clearing `"wiederholung"` via `felder_leeren` drops `ausnahme_daten` (and any
`RDATE`) with it, since neither means anything without a recurrence rule.

Two rules, identical to `create_event`'s field of the same name:

- Every entry must be the same *value kind* as the task's own start: date-only
  `"YYYY-MM-DD"` values for an all-day task, full datetimes otherwise. A mixed
  set, or the wrong kind, is rejected.
- An entry that names no occurrence of the task's `wiederholung` at all — wrong
  day, wrong hour, or a naive value read in the server's default timezone while
  the series runs in another — is rejected rather than stored to cancel
  nothing. Pass the occurrence exactly as `list_tasks`/`get_task` reported its
  `start_datum`.

### Reminders (`erinnerungen`)

Each entry is either:

- a **relative** RFC 5545 duration, e.g. `"-P1D"` (1 day before), `"-PT1H"` (1 hour
  before), `"-PT15M"` (15 minutes before). Anchored to `faellig_datum`
  (`TRIGGER;RELATED=END`) when the task has one, otherwise to `start_datum`
  (`RELATED=START`). A relative reminder on a task with neither date is an error.
  The leading `-` is what makes it fire *before* the date: a positive duration
  (`"PT30M"`) is valid and means half an hour *after* it.
- an **absolute** ISO 8601 datetime, e.g. `"2026-07-19T09:00:00+02:00"`. Stored as a UTC
  `TRIGGER;VALUE=DATE-TIME` on the wire, as RFC 5545 requires; a value without an offset is
  read in the server's default timezone (`MCP_DEFAULT_TIMEZONE`) first, then converted to
  UTC. Reading it back yields the same instant rendered in the default timezone, so the
  string may differ from the one sent.

Passing the same reminder twice — including two spellings of the same trigger, e.g.
`"-P1W"` and `"-P7D"` — creates one alarm, not two.

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

- Passing `erinnerungen` **replaces the reminders `list_tasks` shows** with the new list.
  A reminder that is already there is left untouched (keeping the dismissed state and any
  detail this format has no slot for), one that is missing from the list is removed, and an
  alarm the task carries but `erinnerungen` cannot express (see `list_tasks`) is never
  touched. Pass `[]` to remove the visible reminders; clearing `"erinnerungen"` via
  `felder_leeren` removes *every* alarm, including the ones that were never listed.
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
`"uebergeordnete_aufgabe"`, `"wiederholung"`, `"ausnahme_daten"`.

Clearing `"wiederholung"` also drops the task's `ausnahme_daten` (`EXDATE`) and
any `RDATE`: they cancel and add nothing once the series is gone, and would
silently come back to life the day the task is made recurring again.

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

## `move_task(list_name, task_uid, ziel_liste)`

Moves a task (VTODO) from one task list to another.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the source task list |
| `task_uid` | string | yes | UID of the task to move |
| `ziel_liste` | string | yes | Display name of the target task list |

Behaviour:
- Resolves source and target task lists. Target must support VTODO tasks; if the target exists but does not accept tasks (e.g. an events-only calendar), an error is raised naming both target and component kind before any object is touched.
- Target is resolved BEFORE touching the source task object.
- If source == target (same collection URL), returns no-op success immediately with `"methode": "MOVE"`.
- Preferred path: issues a CalDAV `MOVE` request on the object's URL with `Destination` and `Overwrite: F`, preserving server-side URL identity, UID, ETags, and all properties.
- Fallback path: if the server rejects `MOVE` with HTTP 403, 405, 409, 501, or 502, the entire calendar object (carrying UID, VTIMEZONEs, VALARMs, RRULE, EXDATE, RELATED-TO, etc.) is copied into the target list with `save_todo` (guarded with `no_overwrite`), verified by re-fetching, and only then is the source task deleted. The fallback NEVER deletes the source before a verified write.
- If an object with that UID already exists in the target collection, the operation is refused (HTTP 412 or pre-check on fallback) with a speaking error.- The read-back check compares instances, not just the UID: for a recurring object the master and every `RECURRENCE-ID` override must be present in the target, otherwise the copy is reported as incomplete and the original is kept.

Result shape:

```json
{
  "uid": "0f8ba4a4-...",
  "von": "Privat",
  "nach": "Arbeit",
  "methode": "MOVE"
}
```

(`"methode"` is `"MOVE"` if CalDAV MOVE was used, or `"kopiert"` if copy+delete fallback was executed.)

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
| `erinnerungen` | `VALARM` | relative durations (e.g. `"-PT30M"`) trigger before `start`; absolute ISO datetimes as-is. Same read/write rules as tasks — see `list_tasks` |
| `url` | `URL` | |
| `verknuepfte_aufgabe` | `RELATED-TO;RELTYPE=PARENT` | UID of a task this event reserves time for |
| `teilnehmer` | `ATTENDEE` (one per entry) | list of attendee dicts, see below |

Returns `{"uid": ...}`.

To move or cancel a **single occurrence** of a recurring event: add its
original date to `ausnahme_daten` (via `update_event`) and, for a move, create
a separate replacement event. Pass the occurrence exactly as `list_events` /
`get_event` reported its `start` — exception dates are stored in the timezone
the event's own start is anchored to, so the value names the same moment the
series produced. A **naive** exception date still means the server's default
timezone, like every other naive input: for an event anchored in a *foreign*
zone, name that zone (`"2026-07-27T09:00:00 Asia/Tokyo"`) or pass the reported
value back.

Every entry must be the same kind as the event's own start — date-only
`"YYYY-MM-DD"` values for an all-day event, full datetimes otherwise. A mixed
(or mismatched) set is rejected instead of written: iCalendar puts every value
under one property with one set of parameters, which RFC 5545 §3.8.5.1 allows
for a single value type only, and a date-only exception names no occurrence of
a timed series in any case.

An entry that names no occurrence of the event's `wiederholung` at all — wrong
day, wrong time, or a naive value read in the server's timezone while the
series runs in another — is **rejected** rather than stored, since it would
cancel nothing and report nothing. The check is best-effort and never guesses:
an event without an `RRULE`, a rule that cannot be expanded, and a series long
enough that finding the occurrence would mean walking more than 10 000 of them
are all accepted unchecked. `RDATE` dates count as occurrences too.

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
`ausnahme_daten` replaces all existing entries, and so does `teilnehmer` —
passing it **replaces the entire attendee list**, it does not add to it.
`erinnerungen` replaces the reminders `list_events` shows, on the same terms
as `update_task` (reminders already present stay untouched, alarms the format
cannot express are never touched). A reminder anchored to the *end* of an
event is one of those: it has no `erinnerungen` spelling, since writing
`"-PT30M"` back would anchor it to the start and move it by the event's whole
duration. `felder_leeren` removes properties entirely — accepted names:
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

## `update_events(kalender_name, event_uids, ...)`

Batch updates up to 200 events in a single calendar with the same field patch.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the calendar containing the events |
| `event_uids` | array of strings | yes | UIDs of events to update (max 200) |
| (all other parameters) | | no | Same optional fields and `felder_leeren` options as `update_event` |

Returns:
```json
{
  "kalender_name": "Termine",
  "erfolgreich": 58,
  "fehlgeschlagen": 2,
  "ergebnisse": [
    {"uid": "uid1", "status": "ok"},
    {"uid": "uid2", "status": "fehler", "fehler": "Event 'uid2' was not found."}
  ]
}
```

Contract and behaviour:
- **Up-front patch validation guarantee**: The field patch is validated ONCE up front (invalid RRULE, unknown `felder_leeren` entry, invalid status, empty patch, etc.) and fails hard before modifying any event. If the patch is invalid, zero events are written.
- **Patch vs. individual event**: Whether the patch *fits* a given event is per-event - a timed `ende` cannot apply to an all-day event, for instance. Such an event is reported as a failed entry; the batch is not aborted.
- **Calendar resolution**: Resolved once for the entire call, not per UID.
- **Deduplication**: Duplicate UIDs in `event_uids` are deduplicated while preserving the order of first occurrence.
- **Limit**: Maximum 200 UIDs per call. Passing more than 200 UIDs or an empty list raises an error.
- **Partial-failure contract**: A failure on a single UID (e.g. event not found or ETag conflict) does not abort the batch; each UID gets its own entry in `ergebnisse`, and the order matches `event_uids` after deduplication. Server-wide errors (auth failure, missing calendar, connection error) propagate as exceptions immediately.
- **Stale cache**: If the cached calendar has gone stale, resolution is refreshed once for the whole batch instead of reporting every UID as missing.

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

## `delete_events(kalender_name, event_uids)`

Batch deletes up to 200 events from a single calendar.

> **Irreversible**, and a batch multiplies the damage a wrong UID list does. Confirm the list with the user before calling this.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the calendar containing the events |
| `event_uids` | array of strings | yes | UIDs of events to delete (max 200) |

Returns:
```json
{
  "kalender_name": "Termine",
  "erfolgreich": 2,
  "fehlgeschlagen": 0,
  "ergebnisse": [
    {"uid": "uid1", "status": "ok"},
    {"uid": "uid2", "status": "ok"}
  ]
}
```

Contract and behaviour:
- **Calendar resolution**: Resolved once for the entire call.
- **Deduplication**: Duplicate UIDs in `event_uids` are deduplicated so each event is deleted at most once, preserving the order of first occurrence.
- **Limit**: Maximum 200 UIDs per call. Passing more than 200 UIDs or an empty list raises an error.
- **Partial-failure contract**: Each UID gets its own entry in `ergebnisse`. Per-UID errors (event not found) are recorded in `ergebnisse`, while server-wide errors (auth failure, missing calendar, connection error) propagate as exceptions immediately.
- **Stale cache**: If the cached calendar has gone stale, resolution is refreshed once for the whole batch instead of reporting every UID as missing.

---

## `move_event(kalender_name, event_uid, ziel_kalender)`

Moves an event (VEVENT) from one calendar to another.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_name` | string | yes | Display name of the source calendar |
| `event_uid` | string | yes | UID of the event to move |
| `ziel_kalender` | string | yes | Display name of the target calendar |

Behaviour:
- Resolves source and target calendars. Target must support VEVENT events; if the target exists but does not accept events (e.g. a tasks-only list), an error is raised naming both target and component kind before any object is touched.
- Target is resolved BEFORE touching the source event object.
- If source == target (same collection URL), returns no-op success immediately with `"methode": "MOVE"`.
- Preferred path: issues a CalDAV `MOVE` request on the object's URL with `Destination` and `Overwrite: F`, preserving server-side URL identity, UID, ETags, and all properties.
- Fallback path: if the server rejects `MOVE` with HTTP 403, 405, 409, 501, or 502, the entire calendar object (carrying UID, VTIMEZONEs, VALARMs, RRULE, EXDATE, RELATED-TO, etc.) is copied into the target calendar with `save_event` (guarded with `no_overwrite`), verified by re-fetching, and only then is the source event deleted. The fallback NEVER deletes the source before a verified write.
- If an object with that UID already exists in the target collection, the operation is refused (HTTP 412 or pre-check on fallback) with a speaking error.- The read-back check compares instances, not just the UID: for a recurring object the master and every `RECURRENCE-ID` override must be present in the target, otherwise the copy is reported as incomplete and the original is kept.

Result shape:

```json
{
  "uid": "event-uid",
  "von": "QuellKalender",
  "nach": "ZielKalender",
  "methode": "MOVE"
}
```

(`"methode"` is `"MOVE"` if CalDAV MOVE was used, or `"kopiert"` if copy+delete fallback was executed.)

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

- `start` defaults to the task's `faellig_datum`; if the task has none, the
  call fails and you must pass `start` explicitly.
- A date-only `start` produces a one-day all-day event; `ende` must then also
  be a date (or omitted) — this is the same start/end consistency check
  `create_event` uses, not a separate rule. A datetime `start` produces an
  event of `dauer_minuten` length instead.
- `dauer_minuten` is a real duration: a block that spans a daylight-saving
  change stays that many minutes long, rather than following the wall clock.
  It has no effect on an all-day start — the event stays a one-day all-day
  event unless `ende` gives it a later **date**. A *datetime* `ende` on an
  all-day start is rejected with the same start/end type error `create_event`
  raises (`start and ende must both be all-day dates or both be datetimes`),
  not silently ignored.
- `ende` and `dauer_minuten` are mutually exclusive — passing both is an error
  naming both parameters. With **neither** given, the event runs 60 minutes
  (unchanged from before this parameter split; `dauer_minuten` now defaults to
  `None` rather than `60` purely so a call can tell "not given" apart from
  "given as 60" — existing calls that only ever passed `dauer_minuten`
  continue to behave identically).
- The event is anchored to a timezone the same way `create_event` anchors one:
  a `start` naming an IANA zone keeps it, a numeric offset is stored as UTC,
  and a start taken from the task's `faellig_datum` (which is a bare instant —
  tasks store no zone) is anchored in the server's default timezone.
- `beschreibung`: use `None` vs. `""` deliberately — `None` inherits the
  task's `notizen` (the original behavior), an explicit `""` clears the
  description instead of inheriting anything.

Returns `{"uid": <event uid>, "task_uid": <task uid>}`.

---

## `get_agenda(datum, kalender_namen=None, listen_namen=None)`

One day's calendar events and due tasks together — CalDAV has no combined
VEVENT+VTODO query, so this is composed server-side. `datum` must be a
date-only `"YYYY-MM-DD"` string; day boundaries are in the server's default timezone
(consistent with the naive-input rule), for the events and the tasks alike —
including days that a daylight-saving change makes 23 or 25 hours long.

Nextcloud resolves all-day and floating (zone-less) values against the
*calendar's own* timezone when it answers a time-range query, which need not be
`MCP_DEFAULT_TIMEZONE`. To keep the agenda's day the one this server promises,
the neighbouring days are queried too and the result is cut back to the local
day here. A recurring event that the server returns unexpanded is kept in any
case — its start is the series' first occurrence, not the one that matched.

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

## `list_tags(kalender_namen=None, listen_namen=None)`

Aggregated list of tags (`CATEGORIES`) and their counts across calendars and task lists.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `kalender_namen` | list of strings | no | Event calendar display names to include; `null` = all event calendars, `[]` = no calendars |
| `listen_namen` | list of strings | no | Task list display names to include; `null` = all task lists, `[]` = no task lists |

> **Cost warning**: This call reads each target collection completely without a time window, which makes it an expensive operation. It holds the CalDAV service lock while it runs, so other tool calls wait for it to finish.

Return shape:

```json
[
  {"tag": "CLI-Tool", "anzahl": 6},
  {"tag": "Arbeit", "anzahl": 3}
]
```

- **Aggregation**: VEVENT events and VTODO tasks are aggregated together. Completed tasks count too (`include_completed=True`), so a tag does not vanish when all associated tasks are finished.
- **Case folding**: Aggregation is case-insensitive (e.g. `"Arbeit"` and `"arbeit"` collapse into one entry). The spelling reported is the most common one, alphabetically first on a tie - not the first one encountered, so two identical calls cannot disagree because the server returned collections in a different order.
- **Sorting**: Sorted by `anzahl` descending. Ties are broken alphabetically (ascending, case-insensitive) by `tag`.
- **Deduplication**: A mixed VEVENT+VTODO collection contributes its events and its tasks, each counted once - the two queries select disjoint component kinds. A name listed twice in the same argument is deduplicated before querying, so it cannot inflate the counts.
- **Filtering**: `kalender_namen=None` / `listen_namen=None` queries all collections of that component kind, while `[]` excludes that component kind entirely. An unknown collection name raises `CalendarNotFoundError` or `TaskListNotFoundError`.

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

This pair is the lossless path for anything the German field names don't
model. `VALARM`s in particular go out and come back verbatim — action,
`ATTACH`, `DURATION`/`REPEAT`, the `RELATED` anchor and the dismissed state
(`ACKNOWLEDGED`) included — where `erinnerungen` only carries a trigger time.

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
