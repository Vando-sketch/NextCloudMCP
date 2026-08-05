# Design: Issues #1–#9

Entwurf vor Implementierung, am 2026-08-05 bestätigt. Ein Issue = ein Branch =
ein PR. Reihenfolge nach Priorität und Abhängigkeit:
#1 → #3 → #9 → #4 → #6 → #2 → #5 → #7 → #8
(#9 baut auf der neuen `list_tasks`-Signatur aus #3 auf).

Bestätigte Entscheidungen: Batch-Operationen (#7) werden umgesetzt; der
vierwertige Task-`status` (#6) ist als Breaking Change akzeptiert;
`get_agenda` rechnet künftig standardmäßig in `Europe/Berlin` (#9);
Live-Zugangsdaten für #8/#9 werden nachgereicht, bis dahin gilt alles
unit-getestet und die Live-Verifikation läuft separat nach.

Gemeinsame Regeln: deutsche Parameter- und Rückgabenamen, Datums-/Zeitsemantik
unverändert, sprechende Fehler aus `errors.py`, jedes geänderte Tool bekommt
einen Round-Trip-Test (schreiben → lesen → gleicher Wert).

---

## #1 Erinnerungen (VALARM) zurückgeben — Prio hoch

**Branch** `feat/reminders-readback`

**Dateien**
- `mapping.py`: neue Funktion `extract_alarms(component) -> list[str]`,
  Aufruf in `parse_vtodo`.
- `event_mapping.py`: Aufruf derselben Funktion in `parse_vevent`.
- `server.py`: Docstrings von `list_tasks`/`get_task`/`list_events`/`get_event`.
- `docs/tools.md`, `README.md`.

**Signatur**

```python
def extract_alarms(component) -> list[str]:
    """Alle VALARM-TRIGGER als Strings im Eingabeformat von create_*."""
```

**Rückgabe-Shape** — neuer Schlüssel `erinnerungen: list[str]` in jedem Task-
und Event-Dict (leere Liste, wenn keine VALARM vorhanden):

- relativer Trigger (`VALUE=DURATION`) → RFC-5545-Dauer, z. B. `"-PT30M"`,
  `"-P1D"` (Serialisierung über `icalendar.vDuration`).
- absoluter Trigger (`VALUE=DATE-TIME`) → ISO 8601 mit Offset, z. B.
  `"2026-08-07T09:00:00+00:00"`.

**Edge-Cases**
- Mehrere VALARMs: Reihenfolge wie im Objekt gespeichert.
- VALARM ohne TRIGGER oder mit unparsebarem Wert: wird übersprungen, kein
  Fehler — ein fremder Client darf ein Listing nicht sprengen.
- `ACTION=EMAIL/AUDIO` (von anderen Clients): Trigger wird trotzdem
  ausgegeben; dieser Server schreibt selbst nur `DISPLAY`.
- `RELATED=END` (Task, relativ zu DUE) vs. `RELATED=START`: die Ausgabe ist
  in beiden Fällen die nackte Dauer — genau das nimmt `create_task` auch
  entgegen, und `build_alarm` wählt beim Schreiben END, wenn ein
  `faellig_datum` existiert, sonst START. Round-Trip bleibt damit stabil.
- `"…Z"`-Eingabe kommt als `"+00:00"` zurück: semantisch identisch, nicht
  bytegleich. Wird dokumentiert.
- `DURATION`/`REPEAT` innerhalb einer VALARM (Wiederholung des Alarms selbst)
  wird nicht abgebildet — dokumentiert, nicht stillschweigend verschluckt.

**Testfälle** (`test_mapping.py`, `test_event_mapping.py`, `test_caldav_client.py`)
- relative Dauer setzen → lesen → identischer String (Task mit DUE, Task nur
  mit DTSTART, Event).
- absoluter Trigger setzen → lesen → identischer ISO-String.
- mehrere Erinnerungen, Reihenfolge stabil.
- keine VALARM → `[]`.
- kaputte VALARM (kein TRIGGER) → übersprungen, restliche bleiben.
- `list_tasks`/`list_events` liefern das Feld durch (Service-Ebene).

---

## #2 move_event / move_task — Prio hoch

**Branch** `feat/move-operations`

**Dateien** `caldav_client.py` (neue Methoden + `_move_object`-Helfer),
`server.py` (zwei Tools), `docs/tools.md`, `README.md`.

**Signaturen** — Parameternamen wie im Issue; Reihenfolge folgt der
Hausordnung (Collection zuerst, wie bei `get_event`/`get_task`):

```python
# Tool
async def move_event(kalender_name: str, event_uid: str, ziel_kalender: str) -> dict[str, str]
async def move_task(list_name: str, task_uid: str, ziel_liste: str) -> dict[str, str]

# Service
def move_event(self, calendar_name: str, event_uid: str, target_calendar: str) -> dict[str, str]
def move_task(self, list_name: str, task_uid: str, target_list: str) -> dict[str, str]
```

**Rückgabe-Shape**

```python
{"uid": ..., "von": "<Quellname>", "nach": "<Zielname>", "methode": "MOVE" | "kopiert"}
```

**Vorgehen**
1. Quelle und Ziel auflösen; Ziel muss die Komponentenart unterstützen
   (`_supports_component`), sonst sprechender Fehler
   („Der Zielkalender 'X' nimmt keine Aufgaben auf …“).
2. Quelle == Ziel → No-op-Erfolg mit `"methode": "MOVE"` und unverändertem UID.
3. Objekt-URL holen (`event_by_uid(...).url`), CalDAV `MOVE` mit
   `Destination: <Ziel-Collection-URL><Ressourcenname>` und
   `Overwrite: F` senden.
4. Antwortet der Server 403/405/409/501/502 → Fallback: komplettes
   `icalendar_instance` (inkl. VTIMEZONE, Override-Instanzen, VALARM, RRULE,
   EXDATE, RELATED-TO) 1:1 mit `save_event`/`save_todo` im Ziel anlegen,
   Erfolg durch Re-Fetch prüfen, **erst danach** Quelle löschen.

**Edge-Cases**
- UID existiert bereits im Ziel → Abbruch vor jedem Schreibvorgang
  (`Overwrite: F` bzw. Vorab-Lookup im Fallback), damit nichts überschrieben wird.
- Fallback-PUT schlägt fehl → Quelle bleibt unangetastet, Fehler mit Ursache.
- Löschen nach erfolgreichem PUT schlägt fehl → Fehler nennt beide Orte
  („Kopie liegt in 'Y', Original in 'X' konnte nicht gelöscht werden“),
  damit kein stiller Duplikat-Zustand entsteht.
- Serie mit Override-Instanzen: MOVE bewegt das ganze Kalenderobjekt; der
  Fallback kopiert `icalendar_instance` als Ganzes, nicht einzelne Komponenten.
- ETag-Konflikt (412) → `TaskConflictError` wie anderswo.
- Kein Cache-Invalidieren nötig (Collections ändern sich nicht), Objektcaches
  gibt es nicht.

**Testfälle**
- MOVE-Happy-Path: Methode, Ziel-URL und `Overwrite`-Header werden geprüft.
- 405 → Fallback: `save_*` wird vor `delete` aufgerufen (Reihenfolge asserted).
- Fallback-PUT wirft → `delete` wird **nicht** aufgerufen.
- Ziel unterstützt Komponentenart nicht → sprechender Fehler, kein Request.
- Ziel unbekannt → `CalendarNotFoundError` / `TaskListNotFoundError`.
- Quelle == Ziel → No-op.
- Round-Trip: Event mit VALARM + RRULE + EXDATE + RELATED-TO verschieben →
  im Ziel gelesen identisch, UID gleich.

---

## #3 list_tasks listenübergreifend + Filter — Prio hoch

**Branch** `feat/list-tasks-filters`

**Dateien** `server.py`, `caldav_client.py`, `mapping.py` (`filter_tasks`),
`docs/tools.md`, `README.md`.

**Signaturen**

```python
# Tool
async def list_tasks(
    listen_namen: list[str] | None = None,
    nur_offene: bool = True,
    faellig_vor: str | None = None,
    faellig_nach: str | None = None,
    prioritaet: str | None = None,
    tag: str | None = None,
    suchtext: str | None = None,
    limit: int | None = None,
    list_name: str | None = None,   # veralteter Alias für listen_namen
) -> list[dict[str, Any]]

# Service
def list_tasks(self, list_names: list[str] | None = None, only_open: bool = True,
               due_before=None, due_after=None, prioritaet=None, tag=None,
               suchtext=None, limit=None) -> list[dict[str, Any]]
```

**Rückgabe-Shape** — bestehende Task-Dicts plus `"liste": "<Anzeigename>"`.

**Verhalten**
- `listen_namen=None` → alle VTODO-Collections (wie `list_events.kalender_namen`).
- `list_name` allein → wie `listen_namen=[list_name]`; Docstring markiert ihn
  als veraltet.
- Beides gesetzt → Fehler („`list_name` ist der veraltete Alias von
  `listen_namen`; bitte nur eines angeben“).
- `prioritaet`: `hoch`/`mittel`/`niedrig`, validiert gegen `PRIORITY_LABELS`,
  Vergleich gegen das geparste Label.
- `tag`: exakt, case-insensitiv (wie bei `list_events`).
- `suchtext`: case-insensitiv als Teilstring über `titel` und `notizen`.
- Sortierung: `faellig_datum` aufsteigend, Aufgaben ohne Fälligkeit zuletzt,
  danach `titel`. `limit` greift zuletzt, über alle Listen hinweg.

**Breaking Change** (im PR explizit): Ergebnisse sind jetzt sortiert statt in
Server-Reihenfolge, und jedes Dict trägt zusätzlich `liste`.

**Edge-Cases**
- Zwei Listen mit gleichem Anzeigenamen: `listen_namen=None` fragt die
  Collection-Objekte direkt ab (wie `list_events`), beide bleiben erreichbar;
  ein *expliziter* mehrdeutiger Name bleibt ein Fehler.
- Unbekannter Name in `listen_namen` → `TaskListNotFoundError` (kein stilles
  Überspringen).
- `listen_namen=[]` → leeres Ergebnis, kein Request (unterscheidet sich
  bewusst von `None`).
- `nur_offene` bleibt Serverfilter (`include_completed`).

**Testfälle**
- ohne Argumente → alle Listen, `liste` gesetzt.
- explizite Listenauswahl; unbekannter Name → Fehler.
- Alias `list_name` funktioniert; beide gesetzt → Fehler.
- je ein Test pro Filter + Kombination.
- `limit` schneidet erst nach dem Merge über Listen.
- `get_agenda` nutzt den neuen Weg und setzt `liste` nicht mehr doppelt.

---

## #4 Wiederholung in create_task/update_task — Prio mittel

**Branch** `feat/task-recurrence`

**Dateien** `mapping.py` (`TaskFields.wiederholung`, `_CLEAR_SPECS`,
`apply_task_fields`, RRULE-Parser gemeinsam nutzbar machen),
`event_mapping.py` (`_parse_rrule` delegiert), `server.py`, Docs.

**Signaturen** — `wiederholung: str | None = None` (rohe RRULE, wie bei
`create_event`) in `create_task` und `update_task`; `"wiederholung"` wird in
`felder_leeren` akzeptiert (`_CLEAR_SPECS["wiederholung"] = ("wiederholung", "rrule")`).

Der RRULE-Parser wandert nach `mapping.parse_rrule_text()` und wirft
`InvalidTaskDataError`; `event_mapping` übersetzt weiterhin in
`InvalidEventDataError`, damit Event-Aufrufer nur Event-Fehler sehen.

**Rückgabe-Shape** unverändert — `wiederholung` wird bereits gelesen; der
Docstring-Hinweis „read-only“ verschwindet aus `server.py` und `docs/tools.md`.

**Edge-Cases**
- Ungültige RRULE → sprechender Fehler (leeres `vRecur` gilt als ungültig).
- Task ohne DTSTART/DUE mit RRULE: iCalendar verlangt einen Anker; ohne beides
  wird abgelehnt statt eine unauflösbare Serie zu schreiben.
- Setzen und Leeren gleichzeitig → bestehende Konfliktprüfung greift.
- `complete_task` auf einer Serie: Verhalten von Nextcloud Tasks wird
  dokumentiert. Erwartung (im Integrationstest zu bestätigen): STATUS wird auf
  dem Master gesetzt, es entsteht **keine** neue Instanz — die RRULE bleibt
  erhalten. Empfehlung in der Doku: `faellig_datum` vorrücken statt abhaken,
  wenn die Serie weiterlaufen soll.

**Testfälle**
- create mit RRULE → `get_task` liefert denselben String (Round-Trip).
- update setzt/ändert RRULE; `felder_leeren=["wiederholung"]` entfernt sie.
- ungültige RRULE → Fehler, kein Request.
- `complete_task` lässt RRULE unangetastet (Unit-Test).
- optionaler Integrationstest gegen den echten Server (Standard: skip).

---

## #5 list_tags — Prio mittel

**Branch** `feat/list-tags`

**Dateien** `caldav_client.py` (`list_tags`), `server.py`, Docs.

**Signatur**

```python
async def list_tags(kalender_namen: list[str] | None = None,
                    listen_namen: list[str] | None = None) -> list[dict[str, Any]]
```

**Rückgabe-Shape** `[{"tag": "CLI-Tool", "anzahl": 6}, …]`, absteigend nach
`anzahl`, bei Gleichstand alphabetisch nach `tag`.

**Verhalten**
- Beide `None` → alle Event-Kalender **und** alle Aufgabenlisten.
- Aggregation über VEVENT und VTODO gemeinsam; erledigte Aufgaben zählen mit
  (`include_completed=True`), damit ein Tag nicht verschwindet, sobald alles
  abgehakt ist.
- Groß-/Kleinschreibung wird zusammengefasst; ausgegeben wird die zuerst
  gefundene Schreibweise.

**Edge-Cases**
- Collection, die beide Komponentenarten kann, wird nur einmal besucht
  (Dedup über die Collection-URL).
- Leere Liste `[]` heißt „von dieser Art keine“, `None` heißt „alle“.
- Unbekannter Name → not-found-Fehler.
- Kein Zeitfenster: der Aufruf liest jede Collection vollständig — in Doc-
  string und Doku als teurer Aufruf gekennzeichnet.

**Testfälle**
- Aggregation über Events + Tasks, Sortierung, Case-Folding.
- nur Kalender / nur Listen / gemischt.
- keine Tags → `[]`.
- unbekannter Name → Fehler.
- Round-Trip: `create_task(tags=[…])` + `create_event(tags=[…])` → Zählung stimmt.

---

## #6 create_event_from_task vollständig, update_task mit Status — Prio mittel

**Branch** `feat/timeboxing-and-task-status`

**Dateien** `caldav_client.py`, `mapping.py`, `server.py`, Docs.

**Signaturen**

```python
async def create_event_from_task(
    list_name: str, task_uid: str, kalender_name: str,
    start: str | None = None,
    dauer_minuten: int | None = None,     # Default 60, wenn auch `ende` fehlt
    ende: str | None = None,
    beschreibung: str | None = None,
    erinnerungen: list[str] | None = None,
    sichtbarkeit: str | None = None,
) -> dict[str, str]

async def update_task(..., status: str | None = None, ...)
```

**Verhalten**
- `ende` **und** `dauer_minuten` gleichzeitig → Fehler. Deshalb wird
  `dauer_minuten` auf `None` defaultet und intern zu 60 aufgelöst, wenn beides
  fehlt; bestehende Aufrufe mit `dauer_minuten` bleiben gültig.
- `beschreibung`/`sichtbarkeit`/`erinnerungen` werden durchgereicht;
  `beschreibung=None` erbt weiterhin `notizen` der Aufgabe, `""` setzt
  bewusst leer.
- `status`: `offen` → `NEEDS-ACTION`, `in-arbeit` → `IN-PROCESS`,
  `erledigt` → `COMPLETED`, `abgesagt` → `CANCELLED` (neue Map
  `TASK_STATUS_LABELS` in `mapping.py`).
  - `erledigt` setzt zusätzlich `PERCENT-COMPLETE=100` und `COMPLETED`
    (gleiches Ergebnis wie `complete_task`).
  - `offen` entfernt `COMPLETED` und setzt `PERCENT-COMPLETE` auf 0 —
    das ist der Weg, eine versehentlich erledigte Aufgabe wieder zu öffnen.
  - `abgesagt` setzt nur STATUS.

**Breaking Change** (im PR explizit): `parse_vtodo` gibt `status` künftig in
vier Ausprägungen zurück (`offen`/`in-arbeit`/`erledigt`/`abgesagt`) statt nur
`offen`/`erledigt` — nötig, damit `status` round-trippt.

**Edge-Cases**
- `status` und `fortschritt_prozent` in einem Aufruf: der explizite
  `fortschritt_prozent`-Wert gewinnt (wird nach dem Status angewandt).
- `nur_offene=True` filtert serverseitig über `include_completed`; eine
  `CANCELLED`-Aufgabe gilt dabei als offen — dokumentiert.
- Ganztägige Fälligkeit als `start` → weiterhin ganztägiger Termin;
  `ende` muss dann ebenfalls ein Datum sein (bestehende Konsistenzprüfung).
- Unbekanntes Status-Label → sprechender Fehler mit Aufzählung.

**Testfälle**
- je ein Test pro Status; `erledigt` → `offen` entfernt COMPLETED und setzt
  Prozent zurück; Round-Trip `update_task(status=…)` → `get_task`.
- `create_event_from_task` überträgt `erinnerungen`, `sichtbarkeit`,
  `beschreibung`, `ende`.
- `ende` + `dauer_minuten` → Fehler; nur `ende`; keins von beidem → 60 Minuten.
- bestehende Aufrufe ohne neue Felder verhalten sich unverändert.

---

## #7 Batch-Operationen — Prio niedrig

**Branch** `feat/batch-event-operations`

**Empfehlung: umsetzen**, aber auf genau zwei Tools begrenzt. Beide sind
dünne Schleifen über vorhandene Einzeloperationen; `import_ics` deckt nur das
Anlegen ab, für Patch und Löschen gibt es keinen Bulk-Weg.

**Signaturen**

```python
async def delete_events(kalender_name: str, event_uids: list[str]) -> dict[str, Any]
async def update_events(kalender_name: str, event_uids: list[str],
                        titel=None, start=None, ende=None, ort=None,
                        beschreibung=None, tags=None, status=None,
                        sichtbarkeit=None, wiederholung=None, ausnahme_daten=None,
                        erinnerungen=None, url=None, verknuepfte_aufgabe=None,
                        teilnehmer=None, felder_leeren=None) -> dict[str, Any]
```

**Rückgabe-Shape**

```python
{
  "kalender_name": "...",
  "erfolgreich": 58,
  "fehlgeschlagen": 2,
  "ergebnisse": [{"uid": "...", "status": "ok"},
                 {"uid": "...", "status": "fehler", "fehler": "Termin '…' wurde nicht gefunden."}],
}
```

**Edge-Cases**
- Teilfehler brechen den Aufruf nicht ab; jede UID bekommt ihr eigenes Ergebnis.
- Leere `event_uids` → Fehler (kein stiller No-op).
- Doppelte UIDs → dedupliziert unter Beibehaltung der Reihenfolge.
- Obergrenze 200 UIDs pro Aufruf, darüber sprechender Fehler mit Verweis auf
  mehrere Aufrufe.
- `update_events` validiert den Feld-Patch **einmal** vorab (ungültige RRULE,
  unbekanntes `felder_leeren`) → harter Fehler, bevor irgendetwas geschrieben wird.
- Ein leerer Patch (nichts gesetzt, nichts geleert) → Fehler.

**Testfälle**
- alle erfolgreich; ein unbekanntes UID → nur dieser Eintrag ist `fehler`.
- Reihenfolge der `ergebnisse` entspricht der Eingabe.
- Duplikate; leere Liste; über 200.
- ungültiger Patch → kein einziger Schreibvorgang.
- Round-Trip: Patch über drei UIDs → alle drei zurückgelesen mit neuem Wert.

---

## #8 Notiz-Workflow (Regel-Notiz id 208) im Praxistest — Prio mittel

**Branch** `test/notes-workflow-verification`

Kein Feature, sondern Verifikation. Zwei Teile:

1. **Opt-in-Integrationstests** in `tests/test_integration.py` (laufen nur mit
   `RUN_INTEGRATION_TESTS=1`), die gegen eine **eigens angelegte** Testnotiz
   arbeiten — die echte Regel-Notiz 208 wird nie geschrieben, höchstens
   lesend geprüft:
   - `create_notiz` → `get_notiz` → Inhalt identisch.
   - `update_notiz(inhalt=…)` ersetzt den kompletten Inhalt; mehrzeiliger
     Markdown-Text mit Umlauten, Emojis und Codeblöcken kommt Byte-für-Byte
     zurück (kein Datenverlust, keine Zeilenende-Verfälschung).
   - `append_notiz` hängt an, ohne Bestehendes zu verlieren; zweimal
     ausgeführt entstehen zwei Absätze.
   - `list_notizen` und `search_notizen` finden die Notiz (auch mit
     `kategorie`-Filter).
   - Aufräumen im `finally`.
2. **Manuelle Checkliste** in `docs/tools.md`: Notiz nach Schreiben in der
   Nextcloud-Web-UI und in der Notes-App öffnen und vergleichen; Änderung aus
   der App zurücklesen. Das kann kein Test abdecken.

**Zu prüfen bei der Umsetzung** (Ergebnis wandert in den PR-Text):
- Ob `update_note` ein `If-Match`/ETag mitschickt; ohne das überschreibt ein
  Update stillschweigend eine parallele Änderung aus der App.
- Ob `append_notiz`' bekannte Nicht-Atomarität in der Praxis auffällt.

Braucht Zugangsdaten. Ohne sie liefert dieses Issue nur die (übersprungenen)
Tests plus die Checkliste — das steht dann so im PR.

---

## #9 get_agenda liefert unzuverlässige Ergebnisse — Prio hoch

**Branch** `fix/agenda-consistency`

Zuerst reproduzieren, dann fixen. Drei Hypothesen, alle einzeln testbar:

**H1 — UTC-Tagesgrenze (erklärt die fehlende Aufgabe).**
`get_agenda` bildet das Tagesfenster in UTC, sowohl für Termine
(`von=datum, bis=datum`) als auch für Aufgaben (`due_before`/`due_after`).
Eine Aufgabe mit `faellig_datum = 2026-08-07T00:30:00+02:00` liegt in UTC am
**06.08.** und fällt aus der Agenda des 07.08. heraus; umgekehrt rutscht ein
Termin vom Abend des 06.08. (lokal) in den 07.08.
→ Erklärt „Immich aufsetzen“ (Liste „Home Server (Proxmox)“) exakt.

**H2 — Namenskollision Kalender ↔ Liste.**
`get_agenda` löst Aufgabenlisten über *Anzeigenamen* auf
(`list_task_lists()` → `list_tasks(name)`), nicht über Collection-URLs. Ein
Kalender „CSGO“ und eine Liste „CSGO“ sind zwei Collections mit demselben
Namen; wenn die gecachte Komponenten-Metadaten-PROPFIND fehlschlägt, gilt
`_supports_component` als „kann alles“ (fail-open), und die Auflösung greift
auf die falsche Collection zu oder wird mehrdeutig.

**H3 — Prozessweite Caches ohne Invalidierung von außen.**
`_collections`, `_collection_meta` und `_calendar_cache` leben für die gesamte
Prozesslaufzeit und werden nur invalidiert, wenn *dieser* Server eine
Collection anlegt/löscht/umbenennt. Wird in der Web-UI umbenannt oder
gelöscht, serviert der Server weiterhin alte Namen und alte Collection-Objekte
— genau das Muster „get_agenda zeigt etwas, das list_tasks/export_calendar
nicht mehr finden“, wenn die Aufrufe zeitlich auseinanderliegen oder
verschiedene Auflösungspfade nehmen.

**Fix**
- `get_agenda` löst Collections **einmal** auf und arbeitet danach auf den
  Collection-Objekten (URL-identifiziert), nicht auf Namen — beseitigt H2.
- Neue TTL (60 s, Konstante) auf `_collections`/`_collection_meta`, damit
  externe Änderungen spätestens nach einer Minute sichtbar sind; die
  bestehende sofortige Invalidierung bei eigenen Änderungen bleibt — beseitigt H3.
- Neuer Parameter `zeitzone: str = "Europe/Berlin"` für `get_agenda`
  (IANA-Name, `"UTC"` stellt das alte Verhalten wieder her). Das Tagesfenster
  wird in dieser Zone gebildet und für Termine *und* Aufgaben identisch
  angewandt — beseitigt H1.
  **Breaking Change** (im PR explizit): die Tagesgrenzen von `get_agenda` sind
  nicht mehr UTC. Betroffen ist nur dieses eine Tool; die allgemeine
  Datums-/Zeitsemantik (Eingabe-Parsing) bleibt unangetastet.
- Jeder Agenda-Eintrag trägt seine Herkunft: Termine bereits `kalender`,
  Aufgaben `liste` — zusätzlich `quelle_url` (Collection-URL), damit ein
  Phantom künftig sofort einer Collection zuzuordnen ist.

**Regressionstest (Cross-Check)**
Ein Test baut einen Fake-Server mit: zwei gleichnamigen Collections
(Kalender „CSGO“ + Liste „CSGO“), einer Aufgabe mit lokal-mitternächtlicher
Fälligkeit, einem ganztägigen Termin und einer Serie mit EXDATE. Danach gilt:

```
get_agenda(datum)["termine"]  == list_events(von=datum, bis=datum, expand=True)
get_agenda(datum)["aufgaben"] == list_tasks(faellig_vor=datum, faellig_nach=datum, nur_offene=True)
```

für dasselbe Tagesfenster (`Europe/Berlin` als Default und `UTC` explizit),
und `export_calendar`
enthält jedes zurückgegebene UID. Zusätzlich: Cache-TTL-Test (nach Ablauf wird
neu aufgelöst) und ein Test, dass eine in der Web-UI verschwundene Collection
nicht mehr in der Agenda auftaucht.

**Offen:** Bestätigung der Ursache am Live-System (Kalender-/Listennamen,
tatsächliches `faellig_datum` von „Immich aufsetzen“). Ohne Zugangsdaten
werden alle drei Hypothesen gefixt und per Unit-Test abgesichert, aber die
Bestätigung am Produktivsystem fehlt und steht so im PR.
