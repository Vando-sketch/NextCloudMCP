# Improvement Plan

A consolidated review of this repository and a step-by-step plan to implement the
findings. The review was produced by three parallel Claude Sonnet subagents —
(1) code quality & architecture, (2) security & auth, (3) testing, CI, packaging
& docs — and every finding below was verified against the actual source (and, where
noted, against the pinned versions of `fastmcp` and `caldav`) before being included.
The implementation itself is designed to be executed by Sonnet subagents, one work
package at a time (see "Execution model" at the end).

Current baseline: 56 tests passing, 2 skipped (integration), 79% coverage,
`ruff check` and `ruff format --check` clean.

---

## Findings inventory

### A. Reliability & architecture

| # | Severity | Finding |
|---|----------|---------|
| A1 | High | Every CalDAV call blocks the asyncio event loop. All six tools in `server.py` are sync `def` functions; fastmcp 2.14.7 invokes sync tools inline on the event loop (verified in `fastmcp/tools/tool.py`), and `caldav.DAVClient` does blocking HTTP. One slow request stalls the whole server for all clients. |
| A2 | High | No HTTP timeout on CalDAV requests. `CalDavService.__init__` (`caldav_client.py:54`) constructs `DAVClient` without `timeout=`; caldav 3.2.1 then issues requests with `timeout=None`. If Nextcloud hangs, the server hangs forever (compounded by A1). |
| A3 | Medium | Task lists are resolved by display name on every call — `principal.calendar(name=...)` does a full PROPFIND + linear scan each time (verified in caldav 3.2.1 `collection.py`). No caching, and duplicate display names are silently resolved to whichever calendar comes first. |
| A4 | Medium | Concurrent-edit conflicts aren't surfaced usefully. caldav raises `ETagMismatchError` on HTTP 412 (it does send `If-Match`), but `_translate` (`caldav_client.py:31-47`) folds it into the generic `DAVError` branch, so callers get no "re-fetch and retry" signal. |
| A5 | Low | No retry/backoff for transient failures; `DAVClient`'s `rate_limit_*` options are unused, so 429/503 fail immediately. |
| A6 | Low | `CalDavService` thread-safety is partial: `_principal` is locked, but the shared `DAVClient`/HTTP session is not — becomes a live concern once A1 moves calls to a thread pool. |

### B. Correctness (date/time & mapping)

| # | Severity | Finding |
|---|----------|---------|
| B1 | High | Date-only input is silently stored as a midnight DATE-TIME. `parse_datetime_input` (`mapping.py:57-70`) tries `datetime.fromisoformat` first, which already accepts `"2026-07-20"` and returns midnight — the `date.fromisoformat` fallback is dead code (verified). All-day due dates become floating midnight datetimes instead of `VALUE=DATE`, which Nextcloud Tasks treats differently. |
| B2 | Medium | Naive datetimes get inconsistent timezone semantics: DTSTART/DUE keep them floating, while absolute VALARM triggers coerce them to UTC (`mapping.py:85-96`). The same-looking input on the same call is interpreted two different ways. |
| B3 | Medium | No way to clear a field once set. `apply_task_fields` treats `None` as "leave unchanged", so a due date, priority, or parent link can never be removed via `update_task`. |

### C. MCP tool API design

| # | Severity | Finding |
|---|----------|---------|
| C1 | Medium | Inconsistent parameter naming: `create_task` uses `liste` while the other five tools use `list_name` (`server.py:81` vs `65/138/184/198`). Tool parameter names are the contract the LLM client reasons about; the asymmetry invites wrong calls. |
| C2 | Medium | No `get_task` tool — reading one task requires fetching the whole list through the LLM context. `_get_todo` + `parse_vtodo` already do the work internally. |
| C3 | Medium | The 13-field task parameter list is hand-duplicated across five places (two tools, two service methods, `apply_task_fields`), including the umlaut→ASCII kwarg translation — one typo away from a silent field drop. |
| C4 | Low | `list_tasks` has no filtering (due-date range, search) or limit; every call ships the entire list. |
| C5 | Low | Recurring tasks (RRULE) are invisible: `parse_vtodo` doesn't surface recurrence at all, so callers can't even tell a task recurs. |

### D. Security

| # | Severity | Finding |
|---|----------|---------|
| D1 | High | `.env.example:29` ships `MCP_OAUTH_PASSWORD=change-me-to-a-long-random-password` — a non-empty value that passes the "password must be set when public" gate in `config.py:39-45`. A copy-paste deploy runs publicly with a password that is public knowledge. |
| D2 | High | The password's delivery path via the OAuth `state` parameter is unverified against the real claude.ai connector flow (the README says so itself). If claude.ai doesn't let users influence `state`, operators are pushed to disable the only real gate. Needs empirical verification and possibly a consent-page mechanism instead. *Resolved 2026-07-10: verified against production claude.ai - `state` only ever carries Claude's own CSRF token, so the gate blocked every legitimate flow; replaced by an interactive consent page (LOCAL PATCH 5 in `personal_auth.py`, see `docs/deployment.md`).* |
| D3 | Medium | The "password required" gate checks `PUBLIC_BASE_URL`'s hostname, not the actual bind address. `MCP_HOST=0.0.0.0` with a stale localhost `PUBLIC_BASE_URL` (typical Docker mistake) bypasses the check entirely. |
| D4 | Medium | `oauth_tokens.json` (plaintext bearer + refresh tokens) is written with default umask permissions — commonly world-readable — and the state dir is created without `mode=0o700` (`personal_auth.py:153,198`). |
| D5 | Medium | Refresh tokens never expire (`expires_at=None`, `personal_auth.py:269-273`) and there is no operator-facing revocation path; one leak of the state file grants indefinite access. |
| D6 | Low-Med | Password check is a non-constant-time substring test (`self.password in params.state`, `personal_auth.py:235`). *Resolved 2026-07-10 together with D2: the consent page compares with `secrets.compare_digest`.* |
| D7 | Low-Med | `_translate` embeds raw caldav/HTTP exception text into "safe" errors that `server.py` forwards verbatim to clients — inconsistent with the scrubbing applied to unexpected exceptions. |
| D8 | Low-Med | No HTTPS-scheme enforcement on `NEXTCLOUD_CALDAV_URL`; a `http://` misconfiguration sends the app password in cleartext Basic Auth. |
| D9 | Low | `localhost` stays in the OAuth redirect allow-list by default even for public deployments. |
| D10 | Low | `icalendar` has no upper version bound (`>=6.0`), while the injection-safety of user-supplied fields depends on its escaping behavior. |

Verified sound (no action needed): iCalendar injection escaping, path traversal via
`list_name`, PKCE handling (in the `mcp` SDK), token entropy, the access-log
mitigation, absence of TLS-verify bypasses.

### E. Tests, CI, packaging, docs

| # | Severity | Finding |
|---|----------|---------|
| E1 | High | `Settings.from_env()` is entirely untested (config.py 57% coverage): missing-var, bad-int, and CSV-domain parsing paths never run. |
| E2 | High | The OAuth `/token` exchange — the code path that mints the bearer tokens used in production — is never tested (`personal_auth.py` 59%); tests stop at the `/authorize` redirect. Auth-code single-use/replay rejection and token expiry are also unverified. |
| E3 | High | `PersonalAuthProvider._load_state` (restart persistence, the whole point of the state dir) and its corrupt-file branch are untested. |
| E4 | Medium | `_translate`'s generic branches (`NotFoundError`, `DAVError`, `Timeout`, `RequestException`, catch-all) and all `save`/`delete` failure paths are untested — it's a pure function, trivially parametrizable. |
| E5 | Medium | No coverage measurement or gate in CI; no `pytest-cov` in dev deps. |
| E6 | Medium | No type checking in CI despite a fully type-hinted codebase. |
| E7 | Medium | No regression test guards `uvicorn_config={"access_log": False}` in `main()` — a documented, load-bearing security control that a refactor could silently drop. |
| E8 | Medium | Packaging metadata gaps: no `[project.urls]`, no classifiers, no `py.typed` marker (PEP 561 — downstream type-checkers treat the package as untyped). |
| E9 | Low | No uv dependency caching in CI (`enable-cache: true`). |
| E10 | Low | No pre-commit config, CONTRIBUTING.md, or CHANGELOG.md. |
| E11 | Low | Integration tests never run anywhere automatically; consider a scheduled workflow against a Nextcloud Docker container. |

Positive finding worth preserving: `docs/tools.md` and the README tool table match
`server.py` exactly today — keep them in sync as tools change (WP3 touches them).

---

## Step-by-step implementation plan

Ordered into six work packages (WP). Each WP is scoped to be handed to one Sonnet
subagent as a self-contained task. Within a WP, steps are ordered; across WPs,
dependencies are noted. WP1–WP2 are the highest-value, lowest-risk starting points.

### WP1 — Security hardening (D1, D3, D4, D8, D9, D10)

Independent of everything else; do first.

1. **`.env.example`**: comment out `MCP_OAUTH_PASSWORD` (leave it blank with an
   explanatory comment) so the existing non-localhost gate fails loudly instead of
   accepting the placeholder. Additionally reject the literal placeholder string in
   `Settings.__post_init__`.
2. **`config.py`**: extend the password gate to also trigger when `settings.host`
   is not in `_LOCAL_HOSTS` (not just `PUBLIC_BASE_URL`'s hostname). (D3)
3. **`config.py`**: require `https://` scheme on `NEXTCLOUD_CALDAV_URL`, with an
   explicit `NEXTCLOUD_ALLOW_INSECURE_HTTP=1` escape hatch for local testing. (D8)
4. **`personal_auth.py`** (documented local patch #3, keep the vendored-diff header
   up to date): create the state dir with `mode=0o700` and write
   `oauth_tokens.json` via `os.open(..., 0o600)`. (D4)
5. **`server.py` / `build_server`**: when `PUBLIC_BASE_URL` is non-local, drop
   `localhost` from the default redirect allow-list unless explicitly configured. (D9)
6. **`pyproject.toml`**: cap `icalendar>=6.0,<8`. (D10)
7. Add regression tests for each of the above (placeholder-password rejection,
   bind-host gate, https enforcement, state-file permissions).

Acceptance: all new tests pass; existing 56 tests still pass; ruff clean.

### WP2 — Reliability: timeouts, non-blocking I/O, error fidelity (A1, A2, A4, A6, D7)

Independent of WP1. The most user-visible robustness win.

1. **`caldav_client.py`**: pass `timeout=` to `DAVClient` (new
   `NEXTCLOUD_HTTP_TIMEOUT_SECONDS` setting, default ~30s). (A2)
2. **`server.py`**: make all tool functions `async def` and offload
   `CalDavService` calls through `anyio.to_thread.run_sync` (wrap in the existing
   `_call` helper so error translation stays in one place). (A1)
3. **`caldav_client.py`**: add a lock (or thread-local sessions) around the shared
   `DAVClient`, since calls now genuinely run concurrently. (A6)
4. **`caldav_client.py`**: special-case `caldav_error.ETagMismatchError` in
   `_translate` → a distinct `TaskConflictError` ("task was modified by another
   client; re-fetch and retry"). (A4)
5. **`caldav_client.py` / `server.py`**: stop embedding raw exception text in
   client-facing messages for the `DAVError`/`RequestException` branches; log the
   detail server-side, return a categorized generic message. (D7)
6. Tests: timeout is passed through; a blocked service call doesn't stall a second
   concurrent tool call; `_translate` parametrized over every branch (also closes E4).

Acceptance: all tools still function against the mocked service; new concurrency
test passes; `_translate` fully covered.

### WP3 — Correctness & API design (B1, B2, B3, C1, C2, C3, A3)

Depends on WP2 (touches the same files; avoid conflicts by sequencing after it).

1. **`mapping.py`**: fix `parse_datetime_input` so date-only strings return a
   `date` (try `date.fromisoformat` first, or detect length-10 input), producing
   `VALUE=DATE` properties for all-day tasks. (B1)
2. **`mapping.py`**: unify naive-datetime semantics between DTSTART/DUE and alarm
   triggers; document the chosen rule in the tool docstrings. (B2)
3. **Field model**: introduce a single `TaskFields` dataclass shared by the tool
   layer, `CalDavService`, and `apply_task_fields`, eliminating the five hand-copied
   13-field parameter lists. (C3)
4. **Clearing fields**: accept the sentinel string `""` (empty) — or a dedicated
   `felder_leeren: list[str]` parameter — in `update_task` to unset a property;
   wire through to a `del component[name]` path in `apply_task_fields`. (B3)
5. **`server.py`**: rename `create_task`'s `liste` → `list_name` (or all →
   `liste`; pick one, apply everywhere) and add a `get_task(list_name, task_uid)`
   tool reusing `_get_todo` + `parse_vtodo`. (C1, C2)
6. **`caldav_client.py`**: cache calendars by name (invalidate on
   `TaskListNotFoundError`); raise a clear error when two calendars share a display
   name. (A3)
7. Update `docs/tools.md` and the README tool table to match the new/changed
   signatures (they are exactly in sync today — keep it that way).
8. Tests for every behavior above, including all-day round-trips and field clearing.

Acceptance: full suite passes; docs match `server.py` signatures.

### WP4 — Auth test depth & lifecycle (E2, E3, D5, D6 verification)

Depends on WP1 (state-file changes land first). Vendored-file changes must keep the
patch log in `personal_auth.py`'s header current.

1. Unit-level tests instantiating `PersonalAuthProvider` directly:
   `authorize()` → `exchange_authorization_code()` happy path; replayed code
   rejected; expired access token refused; refresh-token exchange; revocation. (E2)
2. Persistence tests: issue a token, build a second provider on the same
   `state_dir`, assert clients/tokens survive; corrupt `oauth_tokens.json` and
   assert graceful degradation. (E3)
3. Bounded refresh-token lifetime (new setting, default e.g. 180 days) and a tiny
   `nextcloud-task-mcp-admin` script (or documented procedure) to list/revoke
   entries in `oauth_tokens.json`. (D5)
4. Dedicated test pinning the substring-vs-equality semantics of the password
   check, so any future change to it is deliberate. (D6)
5. **Manual/documented step (not automatable here):** verify against a real
   claude.ai connector registration whether `state` can carry the password (D2);
   record the result in `docs/deployment.md`. If it cannot, design the consent-page
   replacement as a follow-up WP.

Acceptance: `personal_auth.py` coverage well above the current 59%; token
lifecycle fully exercised.

### WP5 — Test coverage & CI (E1, E4 remainder, E5, E6, E7, E9)

Independent; can run in parallel with WP3/WP4 except where files overlap.

1. `tests/test_config.py`: cover `Settings.from_env()` — missing required vars,
   bad `MCP_PORT`/expiry ints, CSV domain parsing (use `monkeypatch`). (E1)
2. Test that `main()` passes `uvicorn_config={"access_log": False}` (patch
   `FastMCP.run`). (E7)
3. Add `pytest-cov` to dev deps; CI runs
   `pytest --cov=src/nextcloud_task_mcp --cov-report=term-missing --cov-fail-under=80`
   (raise the bar after WP4). (E5)
4. Add `mypy` to dev deps with `[tool.mypy]` excluding the vendored
   `personal_auth.py` (mirroring the ruff exclude); add a CI step; fix any errors
   it surfaces (add the missing return annotation on `_get_todo`, A/13). (E6)
5. `ci.yml`: `enable-cache: true` + `cache-dependency-glob: "uv.lock"` on
   `setup-uv`. (E9)

Acceptance: CI green with coverage gate and mypy step on both matrix versions.

### WP6 — Packaging, DX, docs polish (E8, E10, C4, C5, A5, E11)

Lowest priority; purely additive.

1. `pyproject.toml`: `[project.urls]`, classifiers; add `src/nextcloud_task_mcp/py.typed`
   and include it in the wheel. (E8)
2. `.pre-commit-config.yaml` (ruff check --fix, ruff format, mypy) and a short
   CONTRIBUTING.md; start a CHANGELOG.md. (E10)
3. `list_tasks` filtering: optional due-date range and limit parameters. (C4)
4. Surface `RRULE` read-only in `parse_vtodo` (`wiederholung` field). (C5)
5. Pass `rate_limit_*` options to `DAVClient` for 429/503 backoff. (A5)
6. Optional: scheduled GitHub Actions job running the integration suite against a
   `nextcloud` Docker container. (E11)

---

## Execution model

Each work package is dispatched to one **Claude Sonnet subagent** with: the WP's
step list verbatim, the relevant finding rows from the inventory, and the
acceptance criteria. Recommended order and parallelism:

1. **Wave 1 (parallel):** WP1, WP2 — independent file sets apart from small
   `config.py`/`server.py` touches; if run truly in parallel, use worktrees and
   merge WP1 first.
2. **Wave 2 (parallel):** WP3 (after WP2), WP5.
3. **Wave 3:** WP4 (after WP1), then WP6.

Every WP ends with: `uv run ruff check . && uv run ruff format --check . &&
uv run pytest -q` green before commit. Changes to the vendored
`personal_auth.py` must append to the LOCAL PATCHES log in its header so the file
stays diffable against upstream. D2 (claude.ai `state` verification) is the one
step requiring a live environment and operator involvement — schedule it early,
since its outcome may reprioritize the auth design.
