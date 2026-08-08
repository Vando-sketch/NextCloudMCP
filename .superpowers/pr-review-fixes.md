# Plan: 63 Review-Findings über 5 gestapelte PRs abarbeiten

Repo: /home/elias/projects/NextCloudMCP (GitHub: Vando-sketch/NextCloudMCP)
Arbeits-Worktree: /home/elias/projects/NextCloudMCP-prfix

Ein unabhängiger Multi-Runden-Review hat 63 Correctness-Findings (0 Security)
über die PRs #18–#22 gemeldet. Diese fünf PRs sind der untere Teil eines
13-PR-Stapels:

```
main
 └─ feat/reminders-readback        (#18)
     └─ feat/default-timezone      (#19)
         └─ feat/list-tasks-filters (#20)
             └─ fix/agenda-consistency (#21)
                 └─ feat/task-recurrence (#22)
                     └─ feat/timeboxing-and-task-status (#23)
                         └─ feat/optional-caldav-url (#24)
                             └─ test/notes-workflow-verification (#25)
                                 └─ feat/delete-notiz (#26)
                                     └─ feat/move-operations (#27)
                                         └─ feat/list-tags (#28)
                                             └─ feat/batch-event-operations (#29)
                                                 └─ test/live-round-trip-verification (#30)
```

Alle 63 Findings werden bearbeitet. Fixes landen jeweils auf dem Branch des
betroffenen PRs; danach kaskadieren die Änderungen per Merge den ganzen Stapel
hinunter.

## Global Constraints

Diese Regeln binden JEDE Task und JEDEN Reviewer:

1. **Findings verifizieren, nicht blind fixen.** Der Review-Bericht ist eine
   Behauptung, kein Beweis. Für jedes Finding zuerst den tatsächlichen Code
   lesen und bestätigen, dass das beschriebene Verhalten wirklich existiert.
   Ein Finding, das sich als Fehlalarm herausstellt, wird NICHT "gefixt" —
   stattdessen im Report unter "Fehlalarme" mit Begründung und `file:line`
   aufgeführt. Das ist ein erwünschtes Ergebnis, kein Versagen.
2. **Test zuerst.** Für jedes verhaltensändernde Finding einen Test schreiben,
   der ohne den Fix fehlschlägt, und das belegen (kurze Ausgabe des roten
   Laufs im Report). Danach den Fix. Reine Doku-/CHANGELOG-Findings brauchen
   keinen Test.
3. **Commits direkt auf den Branch des jeweiligen PRs.** KEINE neuen Branches,
   KEINE neuen PRs, KEIN Rebase, KEIN Force-Push, KEIN Merge in dieser Task.
   Das ist die bewusste Ausnahme zur Branch-Regel in CLAUDE.md: die Fixes
   gehören in den bestehenden, offenen PR. Nach dem Commit `git push` auf den
   gleichnamigen Remote-Branch.
4. **Grüne Gates vor jedem Commit:**
   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -q --cov=src/nextcloud_task_mcp --cov-report=term-missing --cov-fail-under=90
   ```
   Alle vier müssen sauber sein. `uv` liegt in `~/.local/bin`.
5. **Keine Live-Zugriffe.** Unit-Tests reden nie mit einer echten Nextcloud.
   Integrationstests, die das tun, laufen in einem eigenen Workflow und sind
   in dieser Arbeit nicht auszuführen.
6. **Deutsche API-Oberfläche bleibt deutsch.** Tool-Parameter, Rückgabe-Keys
   und Docstrings des MCP-Servers sind auf Deutsch (`erinnerungen`,
   `faellig_datum`, `listen_namen`, `wiederholung`, ...). Kein Umbenennen ins
   Englische, keine neuen englischen Keys.
7. **Öffentliche Signaturen nicht still verschieben.** Neue Parameter an
   bestehenden Funktionen werden keyword-only (`*`), damit Positional-Caller
   nicht still auf andere Werte binden.
8. **CHANGELOG.md pflegen,** wenn ein Finding einen fehlenden oder falschen
   Eintrag nennt, und immer dann, wenn ein Fix das Verhalten nach außen ändert.
9. **Kein Scope-Creep.** Nur die gelisteten Findings plus die Tests und
   Doku-Änderungen, die sie direkt verlangen. Keine Refactorings nebenbei.
10. **Der Fix darf das Feature des PRs nicht abschaffen.** Wo ein Finding ein
    Design-Problem beschreibt, das nur durch Weglassen des Features "lösbar"
    wäre, ist der kleinste korrekte Fix zu wählen und die verbleibende
    Einschränkung in `docs/tools.md` bzw. der Docstring ehrlich zu
    dokumentieren.

---

## Task 1: PR #18 — Erinnerungen (VALARM) verlustfrei genug machen

**Branch:** `feat/reminders-readback`
**Status:** abgeschlossen (siehe progress.md). Brief liegt in
`task-1-brief.md`, Report in `task-1-report.md`.

---

## Task 2: PR #19 — Default-Zeitzone an Komponenten verankern

**Vorbedingung:** Task 1 ist gemergt in `feat/default-timezone` (der Controller
hat das erledigt, bevor diese Task startet).
**Branch:** `feat/default-timezone`
**PR:** #19 `feat: one configurable default timezone instead of hardcoded UTC`
**Betroffene Dateien:** `src/nextcloud_task_mcp/caldav_client.py`,
`src/nextcloud_task_mcp/event_mapping.py`, `src/nextcloud_task_mcp/mapping.py`,
`src/nextcloud_task_mcp/notes_mapping.py`, `src/nextcloud_task_mcp/admin.py`,
Tests, `CHANGELOG.md`

`MCP_DEFAULT_TIMEZONE` (Default `Europe/Berlin`) ersetzt hartcodiertes UTC.
Kernproblem: die Zone wird pro Wert beim Parsen gewählt statt an die schon
existierende Zone der Komponente verankert.

### Hoch

**2.1 `get_free_busy` schickt eine protokoll-ungültige VFREEBUSY-Anfrage**
(`caldav_client.py` – `_range_bound` → `freebusy_request()`): Liefert
zonen-bewusste Datetimes direkt in die Freebusy-Anfrage
(`DTSTART;TZID=Europe/Berlin:...` ohne VTIMEZONE-Komponente). RFC 5545/6638
verlangen UTC-Bounds für VFREEBUSY.

**2.2 Eine stornierte Instanz kann still nicht mehr storniert werden**
(`event_mapping.py` – `apply_event_fields`, EXDATE): `ausnahme_daten` kann jetzt
in anderer Zone geschrieben werden als das eigene DTSTART — die Ausnahme matcht
keine reale Instanz mehr, und kein Fehler wird gemeldet. EXDATE muss an der
Zone des DTSTART der Komponente verankert werden.

**2.3 Lesen+Zurückspeichern eines wiederkehrenden Events hebt den Sinn dieses
PRs auf** (`event_mapping.py` – `parse_vevent`/`_parse_datetime`): `get_event`
rendert einen TZID-verankerten DTSTART als numerischen Offset — die eine Form,
die sofort wieder zu fixem UTC kollabiert. `get_event` → `update_event` auf
einem wiederkehrenden TZID-Event reintroduziert DST-Drift nach einem einzigen
Round-Trip. Nur ein No-Op-Update bleibt verlustfrei.

**2.4 Gemischte Zonen bei Exception-Dates erzeugen ungültiges EXDATE**
(`event_mapping.py` – `apply_event_fields`): Naiver `ausnahme_daten`-Eintrag
parst zonen-bewusst, numerischer Offset-Eintrag kollabiert zu UTC; icalendar
serialisiert alles unter einem TZID, manche Werte behalten aber ein `Z` —
ungültig nach RFC 5545 §3.2.19 und beim Lesen unsichtbar.

### Mittel

**2.5** Update nur von Start oder nur Ende kann Event-Dauer still ändern
(`event_mapping.py` – `_check_start_end_consistency`, vergleicht über
UTC-Instant).

**2.6** Lokale-Mitternacht-Tagesfenster matchen nicht mehr die eigene
Tagesgrenzen-Regel des Servers (`caldav_client.py` – `_range_bound`); kann
Events aus Nachbartagen in `get_agenda` einschleusen oder verlieren.

**2.7** Zwei weitere Dateien hardcoden weiterhin UTC-Output:
`notes_mapping.py` (`_format_modified`) und `admin.py` (Token-Expiry) —
widerspricht der im PR selbst aufgestellten Regel.

**2.8** Naive Event-Eingabe in einer DST-Lücke/Überlappung wird vor dem
Schreiben nicht aufgelöst (`mapping.py` – `parse_datetime_input`,
`keep_zone=True`-Pfad).

**2.9** Eine naive Freebusy-Antwortperiode wird jetzt falsch interpretiert
(`event_mapping.py` – `extract_freebusy_periods`) — echte Regression.

**2.10** `MCP_DEFAULT_TIMEZONE=UTC` stellt entgegen der Doku NICHT das alte
Verhalten wieder her (`caldav_client.py` – `_sync_vtimezones`): hängt jetzt ein
verwaistes `VTIMEZONE:TZID=UTC` an jedes Event an. Der Test, der das gefangen
hätte, wurde im selben PR verändert statt beibehalten — den Test wiederherstellen.

### Niedrig

**2.11** Ungeschütztes Modul-Level `ZoneInfo()` crasht hässlich statt sauber
(`mapping.py` – `_DEFAULT_TIMEZONE`).

**2.12** Angehängte VTIMEZONE-Regeln enden bei 2037/2038 (`caldav_client.py` –
`_sync_vtimezones`).

**2.13** Ein Server-seitiger UTC-Timestamp kann mit lokaler Zone fehlgestempelt
werden (`caldav_client.py` – `_parse_deleted_at`).

**2.14** Lokale-Mitternacht-Konstruktion nimmt an, Mitternacht existiere immer —
Zonen mit Übergang um 00:00 (Santiago, Beirut, Havanna, Teheran) haben an
Übergangstagen keine (`_local_midnight`-Familie in mehreren Dateien).

**2.15** `create_event_from_task` erzeugt nie ein zonen-verankertes Event
(`caldav_client.py`).

---

## Task 3: PR #20 — listenübergreifende Task-Abfrage reparieren

**Vorbedingung:** Task 2 ist gemergt in `feat/list-tasks-filters`.
**Branch:** `feat/list-tasks-filters`
**PR:** #20 `feat: query tasks across lists and filter them`
**Betroffene Dateien:** `src/nextcloud_task_mcp/caldav_client.py`,
`src/nextcloud_task_mcp/mapping.py`, `src/nextcloud_task_mcp/server.py`, Tests,
`CHANGELOG.md`

Der PR bringt `listen_namen`, `prioritaet`/`tag`/`suchtext`-Filter, Sortierung
und einen `liste`-Key. Zwei Regressionen stechen heraus: ein permanenter Bruch
und eine stille Argument-Verschiebung.

### Hoch

**3.1 Abfrage über alle Listen kann sich nie von einem veralteten
Collection-Cache erholen** (`caldav_client.py` – `list_tasks`/`_task_lists`,
`listen_namen=None`-Pfad): Umgeht den Stale-Cache-Retry von `_with_collection`
komplett. Sobald ein gecachtes Collection-Objekt 404t (z. B. Liste server-seitig
gelöscht), werfen `list_tasks()`/`get_agenda(listen_namen=None)` für den Rest
des Prozesslebens `TaskListNotFoundError`. Kein Self-Heal mehr.
*Achtung:* `tests/test_caldav_client.py` enthält einen Test, der genau dieses
Design als beabsichtigt verteidigt (Finding 3.19) — der Test ist mit zu
korrigieren, nicht das Finding zu verwerfen.

**3.2 Neue Filter-Parameter verschieben still das `limit` eines
Positional-Callers** (`caldav_client.py`/`server.py` – `list_tasks`-Signaturen):
`prioritaet`/`tag`/`suchtext` wurden vor `limit` als normale
positional-or-keyword-Parameter eingefügt (nicht keyword-only). Ein Caller, der
`limit` bisher als 5. Positional-Argument übergeben hat, bindet den Wert jetzt
still an `prioritaet`. Unentdeckt, weil kein Call-Site im Repo 5 Positional-
Argumente nutzt und der einzige Test, der die Arity gepinnt hätte, im selben
Diff gelockert wurde. Zusätzlich wurde `list_name` zu `list_names` umbenannt —
das bricht jeden `list_name=`-Keyword-Call. Signaturen aufräumen (keyword-only,
siehe Global Constraint 7), Arity-Test wiederherstellen, und für das
Umbenennen entweder Rückwärtskompatibilität herstellen oder den Bruch im
CHANGELOG als Breaking Change ausweisen.

### Mittel

**3.3** Ein einziges fehlerhaftes Fälligkeitsdatum vergiftet ein ganzes, sonst
gesundes Listing (`mapping.py` – `_task_sort_key`/`filter_tasks`; die Sortierung
parst jetzt unbedingt jedes `faellig_datum`).

**3.4** Doppelte Listennamen zählen Aufgaben still doppelt (`caldav_client.py` –
`_task_lists`).

**3.5** Der neue `liste`-Key ist oft genau dann unbrauchbar, wofür er benannt
ist (`caldav_client.py` – `_task_lists`): Teilen sich zwei Listen einen
Anzeigenamen, bekommen beide denselben mehrdeutigen Wert.

**3.6** Ein leerer Listen-Scope umgeht die gesamte Filter-Validierung
(`caldav_client.py` – `list_tasks` Early-Return bei `listen_namen=[]`).

**3.7** `tag`/`suchtext` sind nicht Unicode-normalisiert, in einer
deutschsprachigen API (`mapping.py` – `filter_tasks`, `.lower()` statt
`.casefold()` plus Normalisierung).

**3.8** Jede benannte Liste wird pro Call zweimal aufgelöst — unnötig, nicht
langsamer (`caldav_client.py`).

**3.9** Auth-/Netzwerkfehler bei der Auflösung entkommen jetzt als
undurchsichtiger interner Fehler (`caldav_client.py` – `list_tasks`, die
Auflösung wurde aus dem `try/except` herausverschoben).

**3.10** Ein bare Call fegt jetzt unbegrenzt über alle Listen (`server.py` –
`list_tasks`-Tool, kein Default-`limit`, keine Warnung in der Docstring).

### Niedrig

**3.11** Docstring behauptet fälschlich, der All-Listen-Zweig sei "frisch
gelistet" (`caldav_client.py`).

**3.12** Leerer String als Listenname wird anders behandelt als jeder andere
unbekannte Name (`caldav_client.py`).

**3.13** Fragiler Control-Flow beim Alias-Konflikt-Check (`server.py`).

**3.14** Titel-Tiebreak-Sortierung ist nicht locale-aware — rohe
Codepoint-Ordnung, Umlaute sortieren zuletzt (`mapping.py` – `_task_sort_key`).

**3.15** Der Service-Lock wird über die gesamte All-Listen-Fetch-Schleife
gehalten und serialisiert alle anderen gleichzeitigen Tool-Calls
(`caldav_client.py`).

**3.16** `get_task`-Docstring behauptet fälschlich Shape-Parität mit
`list_tasks` (`server.py`).

**3.17** Kein CHANGELOG-Eintrag trotz zwei selbst erklärter Breaking Changes.

**3.18** `suchtext=""` und `prioritaet=""` werden inkonsistent behandelt
(`mapping.py`).

**3.19** Ein Test verteidigt explizit das Design, das Finding 3.1 verursacht,
als beabsichtigt (`tests/test_caldav_client.py`).

---

## Task 4: PR #21 — Cache-TTL scharfstellen

**Vorbedingung:** Task 3 ist gemergt in `fix/agenda-consistency`.
**Branch:** `fix/agenda-consistency`
**PR:** #21 `fix: bound how long stale collections can feed get_agenda`
**Betroffene Dateien:** `src/nextcloud_task_mcp/caldav_client.py`, Tests,
`CHANGELOG.md`

Der PR setzt eine 60s-TTL auf die Collection-Caches und ergänzt
`quelle_url`-Provenance in `get_agenda`. Nach drei Review-Runden konvergiert;
vier Findings offen. Lock-Reentrancy wurde unabhängig als sicher bestätigt
(`threading.RLock`, kein Deadlock) — daran nichts ändern.

### Mittel

**4.1 Das reale Staleness-Fenster kann fast doppelt so lang sein wie die
beworbene TTL** (`caldav_client.py` – `_get_collection`/`_cache_collection`):
Cache-Einträge werden zum Auflösungszeitpunkt aus einem Snapshot gestempelt,
der schon fast 60s alt sein kann — bis zu ~119s statt "maximal eine Minute".

**4.2 Die zwei Collection-Caches laufen auf unabhängigen Uhren**
(`_collections_fetched_at`/`_collection_meta_fetched_at`) — Skew-Fenster mit
einem frischen und einem veralteten Cache.

### Niedrig

**4.3** Create/Rename-Konflikt-Check erzwingt kein Refresh der Metadaten, nur
der Collection-Liste (`caldav_client.py`, `fresh=True`).

**4.4** `get_agenda` ist nicht atomar über eine TTL-Grenze bei einem langsamen
Call.

**4.5** Der CHANGELOG-Eintrag für diesen PR fehlt.

---

## Task 5: PR #22 — wiederkehrende Tasks lesbar machen

**Vorbedingung:** Task 4 ist gemergt in `feat/task-recurrence`.
**Branch:** `feat/task-recurrence`
**PR:** #22 `feat: let tasks be created and updated as a recurring series`
**Betroffene Dateien:** `src/nextcloud_task_mcp/mapping.py`,
`src/nextcloud_task_mcp/caldav_client.py`, `src/nextcloud_task_mcp/server.py`,
`tests/test_integration.py`, weitere Tests, `README.md`, `docs/tools.md`,
`CHANGELOG.md`

`wiederholung` (RRULE) wird für Tasks schreibbar. Kernproblem: Wiederholung
kann geschrieben, aber nie als Serie zurückgelesen werden.

### Hoch

**5.1 Wiederholung ist write-only — nichts expandiert je die zukünftigen
Instanzen eines wiederkehrenden Tasks** (`caldav_client.py` –
`get_agenda`/`list_tasks`, kein `expand=True`-Analog für Tasks): Ein wöchentlich
wiederkehrender Task erscheint genau einmal, am ursprünglichen Fälligkeitsdatum,
in jedem Listing — nie wieder. Zusammen mit `complete_task` (rollt die Serie
nicht vor) und fehlendem EXDATE/RDATE-Support funktioniert Ende-zu-Ende nur ein
Use-Case: eine Wiederholungsregel für einen anderen, vollwertigen CalDAV-Client
bereitstellen.
*Erwartete Richtung:* Instanz-Expansion für wiederkehrende Tasks in den
Listings (client-seitig über die RRULE, da CalDAV-Server VTODO-Expansion nicht
zuverlässig liefern), begrenzt auf das abgefragte Zeitfenster und mit einem
harten Instanz-Limit gegen unbegrenzte Regeln. Wenn sich das im Rahmen dieser
Task nicht sauber umsetzen lässt, ist das explizit als BLOCKED zu melden statt
halb zu implementieren.

### Mittel

**5.2 Der Anchor-Check kann unabhängige Edits dauerhaft verunmöglichen**
(`mapping.py` – `_check_rrule_anchor`): validiert bei jedem
`apply_task_fields`-Call, nicht nur bei Änderungen an `wiederholung`. Ein via
`import_ics` angelegter ankerloser wiederkehrender Task wird über `update_task`
dauerhaft uneditierbar, selbst für eine reine Titeländerung.

**5.3 RRULE-Validierung ist nur Grammatik, nicht Semantik** (`mapping.py` –
`parse_rrule_text`): kein Pflicht-FREQ, keine Ablehnung doppelter Teile, keine
Wertebereichsprüfung (`INTERVAL=0`, negativer COUNT, `BYMONTHDAY=0`,
`BYMONTH=13`, `BYHOUR=99`), akzeptiert das verbotene UNTIL+COUNT zusammen,
akzeptiert UNTIL vor dem Anker, lässt unbekannte Teilnamen durch.

**5.4 Ein reiner DUE-Anker erzeugt trotzdem eine RFC-unauflösbare Serie**
(`mapping.py` – `_check_rrule_anchor`): RFC 5545 generiert das Recurrence-Set
aus DTSTART, nicht DUE.

**5.5 Eine eingefügte vollständige RRULE-Zeile wird doppelt vorangestellt zu
einer korrupten Property** (`mapping.py` – `parse_rrule_text`):
`"RRULE:FREQ=DAILY"` (z. B. aus `export_calendar` kopiert) wird als
`RRULE:RRULE:FREQ=DAILY` geschrieben.

**5.6 Eine duplizierte RRULE-Property crasht jedes Lesen der gesamten Liste,
die sie enthält** (`mapping.py` – `_extract_rrule`, erreichbar via
`import_ics`): `.to_ical()` auf der resultierenden Python-Liste wirft
`AttributeError`.

**5.7 Wiederkehrende Tasks sind an einen fixen UTC-Instant verankert**, im
Gegensatz zu wiederkehrenden Events (`mapping.py` – `parse_datetime_input`,
`keep_zone=False` für Tasks): aktuell nur latent, driftet aber bei jedem
DST-Übergang, sobald 5.1 das Lesen nachzieht.

**5.8 Tasks haben gar keinen EXDATE/RDATE-Support:** eine einzelne Instanz kann
nicht übersprungen werden, und `wiederholung` zu leeren lässt existierende
EXDATE/RDATE verwaist zurück. Mindestens das Verwaisen beim Leeren beheben.

**5.9 Der Integrationstest kann das Verhalten, das er dokumentieren soll, gar
nicht beobachten** (`tests/test_integration.py`): hard-assertet ein konkretes
Ergebnis, obwohl er als "aufgezeichnet, nicht als korrekt behauptet" geframed
ist.

### Niedrig

**5.10** `update_task` kann RRULE auf eine Override-Subkomponente statt den
Master schreiben (`caldav_client.py`).

**5.11** `create_event_from_task` verwirft `wiederholung` stillschweigend
(`caldav_client.py`).

**5.12** Der Hinweis "Abhaken beendet die Serie" steht nicht im tatsächlichen
Tool-Docstring, nur in README/docs (`server.py` – `complete_task`); CHANGELOG
ebenfalls nicht aktualisiert.

---

## Task 6: Kaskade durch den restlichen Stapel

**Vorbedingung:** Tasks 1–5 sind fertig und gepusht.

Die Fixes aus `feat/task-recurrence` (#22) durch die verbleibenden acht Branches
mergen, in genau dieser Reihenfolge:

```
feat/task-recurrence          → feat/timeboxing-and-task-status   (#23)
feat/timeboxing-and-task-status → feat/optional-caldav-url        (#24)
feat/optional-caldav-url      → test/notes-workflow-verification  (#25)
test/notes-workflow-verification → feat/delete-notiz              (#26)
feat/delete-notiz             → feat/move-operations              (#27)
feat/move-operations          → feat/list-tags                    (#28)
feat/list-tags                → feat/batch-event-operations       (#29)
feat/batch-event-operations   → test/live-round-trip-verification (#30)
```

Regeln:
- Merge-Commits, kein Rebase (die Branches sind gepusht und haben offene PRs).
- Nach JEDEM Merge auf dem Ziel-Branch die vier Gates aus Global Constraint 4
  laufen lassen. Erst grün, dann pushen, dann den nächsten Merge.
- Konflikte inhaltlich auflösen, nicht per `--ours`/`--theirs` wegdrücken. Wenn
  ein Downstream-Branch dieselbe Funktion anders geändert hat, gewinnt die
  Kombination beider Absichten; im Zweifel eskalieren statt raten.
- Wenn ein Downstream-Test durch einen Upstream-Fix fehlschlägt: prüfen, ob der
  Test eine Annahme pinnt, die der Fix bewusst geändert hat. Dann den Test
  anpassen und das im Commit begründen. Nie einen Test löschen oder skippen,
  um die Kaskade grün zu bekommen.
