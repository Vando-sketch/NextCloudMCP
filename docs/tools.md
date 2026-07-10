# Tool reference

Detailed reference for all seven MCP tools, including argument/result examples.
Parameter names are the literal MCP schema names — including the German umlauts.

Values for enum-like fields:

| Field | Allowed values |
|---|---|
| `priorität` | `"hoch"`, `"mittel"`, `"niedrig"` |
| `sichtbarkeit` | `"öffentlich"`, `"privat"`, `"vertraulich"` |
| `status` (in results) | `"offen"`, `"erledigt"` |

Dates are ISO 8601 strings. Two rules apply everywhere a date/datetime is
accepted (`start_datum`, `fällig_datum`, and absolute `erinnerungen` entries):

- A value that is exactly `"YYYY-MM-DD"` (e.g. `"2026-07-20"`) creates an
  **all-day** entry (iCalendar `VALUE=DATE`) — it comes back from `list_tasks`
  / `get_task` as `"2026-07-20"`, not a midnight datetime.
- Any other ISO 8601 datetime (e.g. `"2026-07-20T14:00:00"`,
  `"2026-07-20T14:00:00+02:00"`) is stored as a datetime. A **naive**
  datetime (no UTC offset) is interpreted as UTC.

---

## `list_task_lists()`

No parameters. Returns every calendar on the account:

```json
[
  {"name": "Personal", "url": "https://cloud.example.com/remote.php/dav/calendars/demo/personal/"},
  {"name": "Arbeit",   "url": "https://cloud.example.com/remote.php/dav/calendars/demo/arbeit/"}
]
```

Note: Nextcloud exposes task lists and event calendars through the same CalDAV
collection listing, so event-only calendars appear here too. Filtering them out would
cost one extra CalDAV property request per calendar, so it's deliberately not done.

---

## `list_tasks(list_name, nur_offene=True, fällig_vor=None, fällig_nach=None, limit=None)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list |
| `nur_offene` | boolean | no (default `true`) | Exclude completed tasks |
| `fällig_vor` | string (ISO 8601) | no | Only tasks due at or before this point |
| `fällig_nach` | string (ISO 8601) | no | Only tasks due at or after this point |
| `limit` | integer | no | Max number of results; must be `> 0` |

Result — one dict per task:

```json
[
  {
    "uid": "0f8ba4a4-...",
    "titel": "Steuererklärung",
    "start_datum": "2026-07-01",
    "fällig_datum": "2026-07-20",
    "priorität": "hoch",
    "fortschritt_prozent": 20,
    "status": "offen",
    "ort": "Zuhause",
    "url": "https://example.com/steuer",
    "tags": ["Finanzen", "Wichtig"],
    "notizen": "Belege sammeln",
    "übergeordnete_uid": null,
    "wiederholung": null
  }
]
```

`übergeordnete_uid` is the parent task's UID if this task is a subtask, otherwise `null`.
`wiederholung` is the task's raw RRULE text (e.g. `"FREQ=WEEKLY;BYDAY=MO"`) if it recurs,
otherwise `null` — **read-only**: this server has no tool to create or edit recurrence, it
only surfaces whether/how an existing task recurs.
Fields not set on the task are `null` (`tags` is `[]`, `fortschritt_prozent` is `0`).

### Filtering (`fällig_vor` / `fällig_nach` / `limit`)

- If either `fällig_vor` or `fällig_nach` is given, tasks with **no** `fällig_datum` at all
  are excluded from the result — a task without a due date can't be judged "before" or
  "after" anything.
- Both accept the same ISO 8601 date/datetime formats as `create_task`'s `fällig_datum`. A
  date-only bound (e.g. `"2026-07-20"`) is inclusive of the whole day: `fällig_vor` expands
  to the end of that day (`23:59:59` UTC), `fällig_nach` to the start of it (`00:00:00`
  UTC) — so an all-day task due exactly on the boundary date is included by either bound.
  A datetime bound (with a specific time) is used exactly as given.
- `fällig_vor` and `fällig_nach` can be combined to select a range.
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
| `fällig_datum` | string (ISO 8601) | no | `DUE` |
| `priorität` | string enum | no | `PRIORITY` (hoch→1, mittel→5, niedrig→9) |
| `fortschritt_prozent` | integer 0–100 | no | `PERCENT-COMPLETE` |
| `ort` | string | no | `LOCATION` |
| `url` | string | no | `URL` |
| `tags` | list of strings | no | `CATEGORIES` |
| `erinnerungen` | list of strings | no | one `VALARM` per entry |
| `notizen` | string | no | `DESCRIPTION` |
| `sichtbarkeit` | string enum | no | `CLASS` |
| `übergeordnete_aufgabe` | string (UID) | no | `RELATED-TO;RELTYPE=PARENT` |

Returns `{"uid": "<new task uid>"}`.

### Reminders (`erinnerungen`)

Each entry is either:

- a **relative** RFC 5545 duration, e.g. `"-P1D"` (1 day before), `"-PT1H"` (1 hour
  before), `"-PT15M"` (15 minutes before). Anchored to `fällig_datum`
  (`TRIGGER;RELATED=END`) when the task has one, otherwise to `start_datum`
  (`RELATED=START`). A relative reminder on a task with neither date is an error.
- an **absolute** ISO 8601 datetime, e.g. `"2026-07-19T09:00:00+02:00"`. Stored as a UTC
  `TRIGGER;VALUE=DATE-TIME` (values without an offset are assumed to already be UTC).

Example call:

```json
{
  "list_name": "Personal",
  "titel": "Steuererklärung abgeben",
  "fällig_datum": "2026-07-20",
  "priorität": "hoch",
  "tags": ["Finanzen"],
  "erinnerungen": ["-P1D", "-PT2H"]
}
```

### Subtasks

Pass the UID of an existing task (e.g. from `list_tasks`) as `übergeordnete_aufgabe`.
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

`"start_datum"`, `"fällig_datum"`, `"priorität"`, `"fortschritt_prozent"`, `"ort"`,
`"url"`, `"tags"`, `"erinnerungen"`, `"notizen"`, `"sichtbarkeit"`,
`"übergeordnete_aufgabe"`.

`"titel"` cannot be cleared (a task always needs a title) and is not accepted. Naming
an unknown field, or naming a field in `felder_leeren` that is *also* given a new
value in the same call, is an error.

Example — remove the due date and location, and clear all reminders, while also
setting a new priority:

```json
{
  "list_name": "Personal",
  "task_uid": "0f8ba4a4-...",
  "priorität": "niedrig",
  "felder_leeren": ["fällig_datum", "ort", "erinnerungen"]
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
- `Unknown priorität 'dringend'. Expected one of: hoch, mittel, niedrig.`
- `Could not parse Erinnerung '1 Tag vorher': expected an ISO 8601 duration like '-P1D' / '-PT1H', or an absolute ISO 8601 datetime.`
- `Unknown felder_leeren entry/entries: telefonnummer. Expected one of: start_datum,
  fällig_datum, priorität, fortschritt_prozent, ort, url, tags, erinnerungen, notizen,
  sichtbarkeit, übergeordnete_aufgabe.`
- `Cannot both set and clear the same field in one call: fällig_datum.`
- `limit must be greater than 0, got 0.` — `list_tasks`'s `limit` parameter was `<= 0`.

Requests without a valid OAuth access token are rejected earlier, at the HTTP level
(`401`), before reaching tool logic — see [Authentication](../README.md#authentication).
