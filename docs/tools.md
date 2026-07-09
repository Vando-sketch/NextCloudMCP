# Tool reference

Detailed reference for all six MCP tools, including argument/result examples.
Parameter names are the literal MCP schema names — including the German umlauts.

Values for enum-like fields:

| Field | Allowed values |
|---|---|
| `priorität` | `"hoch"`, `"mittel"`, `"niedrig"` |
| `sichtbarkeit` | `"öffentlich"`, `"privat"`, `"vertraulich"` |
| `status` (in results) | `"offen"`, `"erledigt"` |

Dates are ISO 8601 strings. Date-only values (`"2026-07-20"`) and datetimes
(`"2026-07-20T14:00:00"`, `"2026-07-20T14:00:00+02:00"`) are both accepted.

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

## `list_tasks(list_name, nur_offene=True)`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Display name of the task list |
| `nur_offene` | boolean | no (default `true`) | Exclude completed tasks |

Result — one dict per task:

```json
[
  {
    "uid": "0f8ba4a4-...",
    "titel": "Steuererklärung",
    "start_datum": "2026-07-01T00:00:00",
    "fällig_datum": "2026-07-20T00:00:00",
    "priorität": "hoch",
    "fortschritt_prozent": 20,
    "status": "offen",
    "ort": "Zuhause",
    "url": "https://example.com/steuer",
    "tags": ["Finanzen", "Wichtig"],
    "notizen": "Belege sammeln",
    "übergeordnete_uid": null
  }
]
```

`übergeordnete_uid` is the parent task's UID if this task is a subtask, otherwise `null`.
Fields not set on the task are `null` (`tags` is `[]`, `fortschritt_prozent` is `0`).

---

## `create_task(liste, titel, ...)`

| Parameter | Type | Required | CalDAV property |
|---|---|---|---|
| `liste` | string | yes | — (target task list) |
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
  "liste": "Personal",
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

Same optional fields as `create_task` (minus `liste`, plus):

| Parameter | Type | Required | Description |
|---|---|---|---|
| `list_name` | string | yes | Task list containing the task |
| `task_uid` | string | yes | UID of the task to change |

Only fields explicitly present in the call are modified; everything else on the task
(including fields this server doesn't model) is preserved. Two things to know:

- Passing `erinnerungen` **replaces all existing reminders** with the new list. Pass
  `[]` to remove all reminders.
- There is no way to *unset* a scalar field (e.g. remove a due date) — omitting it
  leaves it unchanged. This is a deliberate trade-off to keep "not passed" and "clear
  this" unambiguous for the model.

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
- `Nextcloud rejected the CalDAV credentials (check username/app password).`
- `Could not reach the Nextcloud server (connection refused or timed out).`
- `Unknown priorität 'dringend'. Expected one of: hoch, mittel, niedrig.`
- `Could not parse Erinnerung '1 Tag vorher': expected an ISO 8601 duration like '-P1D' / '-PT1H', or an absolute ISO 8601 datetime.`

Requests without a valid OAuth access token are rejected earlier, at the HTTP level
(`401`), before reaching tool logic — see [Authentication](../README.md#authentication).
