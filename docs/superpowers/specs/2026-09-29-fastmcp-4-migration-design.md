# Migrate to FastMCP 4.x (release 0.2.0)

Status: design approved 2026-09-29, spec awaiting review.

## Goal

Release 0.2.0 on `fastmcp>=4,<5`, replacing `fastmcp>=2.9,<3`. This removes the
FastMCP advisories that affect `fastmcp < 3.2.0` (OpenAPI provider SSRF, OAuth
proxy confused-deputy, Gemini CLI command injection) from the dependency range
of everyone who installs this package, and drops `diskcache` (unfixed pickle
advisory, previously pulled in via `fastmcp[disk]` -> `py-key-value-aio`).
None of the affected features are used here, but users pinned to `<3` keep
getting flagged.

## Constraints

- The OAuth 2.1 flow (DCR + PKCE + password-gated `/consent`, token persistence
  in `MCP_OAUTH_STATE_DIR`) must keep working with Claude custom connectors,
  including connectors that are already connected.
- CI stays green: ruff, `mypy src tests`, pytest with >= 90% coverage, on
  Python 3.10 and 3.12.
- Branch `feat/fastmcp-4`, PR into `main`. Dependabot PR #54 is closed once
  this PR supersedes it.
- Nothing is tagged or published without the maintainer's manual smoke test.

## Decisions

- **2.x -> 4.x directly, not via 3.x.** 3.x and 4.x sit on different MCP SDKs
  (`mcp` 1.x vs 2.x) with different `ToolAnnotations` keyword names, so a stop
  at 3.x would be a second migration.
- **Version range `fastmcp>=4,<5`** (chosen by the maintainer over
  `>=3.2,<5`). One code path; anyone who must stay on FastMCP < 4 pins
  `nextcloud-organizer-mcp<0.2`.
- **Keep the vendored `PersonalAuthProvider`.** FastMCP 4.0.10 has no native
  provider with DCR + password consent and no external IdP (providers are jwt,
  introspection, IdP-backed ones, and `in_memory`). `personal_auth.py` stays
  excluded from ruff/mypy/coverage.

## Evidence (throwaway spike, not committed)

Copy of the repo with `fastmcp==4.0.10` (pulls `mcp` 2.2.0, was 1.28.1):

- No code changes: 1059 passed, 137 errors, all from the `tools` fixture in
  `tests/test_server.py` calling `mcp.get_tools()`.
- After two mechanical edits: 1196 passed, 18 skipped (integration), ruff and
  `mypy src tests` clean, coverage 95.4%.
- `personal_auth.py` needed no edits. `InMemoryOAuthProvider` differs from
  2.14.7 only by new optional kwargs (`resource_base_url`, `issuer_url`).
- An `oauth_tokens.json` written by 2.14.7 loads under 4.0.10 and its access
  token verifies.
- `diskcache` is absent from the 4.0.10 install. `httpx` is replaced by
  `httpx2` transitively.
- `run_http_async` gained `host_origin_protection`; it defaults to `False`, so
  Funnel/Tailscale host names are not rejected.
- 4.x `TokenHandler` now turns `400 invalid_grant` into `401`.

Release notes: the FastMCP v4.0.0 and v3.0.0 GitHub release bodies were read
after the spike (`gh release view v4.0.0 -R PrefectHQ/fastmcp`). Relevant
findings:

- 4.0 is built on MCP protocol revision `2026-07-28` and MCP SDK 2.x. A server
  negotiates the best protocol per connection; older clients keep working. The
  end-to-end tests therefore include a raw `2025-06-18` session handshake, since
  `fastmcp.Client` always negotiates the newest protocol.
- "MCP model fields are snake_case (with a warning compatibility bridge for the
  old names)": explains the `ToolAnnotations` change; the JSON on the wire keeps
  camelCase, which the end-to-end tests assert.
- "Honor OAuth application_type in DCR (SEP-837)": covered by a DCR test that
  registers with `application_type` `web` and `native`.
- Server-initiated sampling/roots and 3.x deprecated APIs are removed; none are
  used here. OAuth hardening in the notes concerns `OAuthProxy`, which this
  project does not use.
- FastMCP performs an update check against pypi.org at startup in both 2.14 and
  4.x (`FASTMCP_CHECK_FOR_UPDATES` controls it); not a change, so not documented
  as one.

An independent review via `agy` failed twice (no output beyond a preamble) and
was skipped at the maintainer's direction.

## Design

### 1. Code changes

- `pyproject.toml`: `fastmcp>=4,<5`, version `0.2.0`. Keep the `anyio` and
  `cryptography` floors; confirm they are still needed and consistent with what
  FastMCP 4 resolves. Correct the `httpx` comment (no longer transitive via
  fastmcp).
- `server.py`: `ToolAnnotations` keywords to snake_case (`read_only_hint`,
  `destructive_hint`, `idempotent_hint`, `open_world_hint`). Update the
  "fastmcp (<3)" PEP 563 comment.
- `tests/test_server.py`: `tools` fixture builds `{t.name: t for t in
  asyncio.run(mcp.list_tools())}`; annotation assertions read snake_case
  attributes (removes the `FastMCPDeprecationWarning`s). The annotations
  wire-format test must keep passing unchanged (clients still receive
  camelCase JSON).
- Re-check whether the PEP 563 `__kwdefaults__` bug still exists in 4.x. If
  gone, reword the comment and test docstring; if present, keep the guard.
  Either way nothing may still say `<3`.
- `personal_auth.py`: no functional change.

### 2. Dependencies

- Regenerate `uv.lock`; verify resolution for Python 3.10 and 3.12.
- Confirm `diskcache` and `py-key-value-aio[disk]` are gone from the lock.
- Audit the lockfile for known advisories before release.
- Close #54 with a comment linking the superseding PR.

### 3. OAuth end-to-end test (new, runs in CI)

The existing `tests/test_auth.py` deliberately does not drive the full flow.
Add a test that boots the real app on an ephemeral port (uvicorn in a thread)
and drives it over HTTP:

1. Discovery (`/.well-known/*`).
2. Dynamic Client Registration.
3. `/authorize` with PKCE; redirect to `/consent`, no code minted yet.
4. `POST /consent` with a wrong password (rejected, rate limit counted) and the
   right password (redirect carrying the code).
5. `/token` exchange with the PKCE verifier; wrong verifier rejected.
6. `/mcp` with the bearer token via the MCP client: `initialize`, `tools/list`.
7. Refresh-token grant with rotation; old refresh token unusable.
8. Expired/invalid grant returns `401` (new 4.x behaviour).
9. Provider restarted on the same state dir: previously issued token still
   valid.
10. A committed `oauth_tokens.json` fixture in the 0.1.1 format loads and
    verifies.

The test contributes to the 90% coverage gate via `server.py`;
`personal_auth.py` stays omitted. It must pass on both Python 3.10 and 3.12.

Manual pre-release smoke test (maintainer, real Tailscale Funnel URL): add the
0.2.0 build as a fresh Claude custom connector; reconnect an existing
connector; run one read tool and one write tool. This is the only check of
real claude.ai behaviour, including the `invalid_grant` -> `401` change.

### 4. Release and docs

- `CHANGELOG.md` 0.2.0: Changed (requires FastMCP 4 / MCP SDK 2), Security
  (fastmcp and diskcache advisories no longer reachable through this
  package's dependency range), Notes (pin `<0.2` to stay on FastMCP 2).
- `docs/deployment.md` gets an "Upgrading to 0.2.0" section only if a real
  deployment or config change turns up. Current evidence: no env-var change,
  no state migration, existing tokens keep working; the changelog says so
  explicitly.
- Update `docs/architecture.md` and README where they mention FastMCP
  versions or `InMemoryOAuthProvider`.
- Release through the existing GitHub Release -> PyPI workflow, only after the
  manual smoke test passes.

### 5. Risks

- `invalid_grant` 400 -> 401: covered by the E2E test and the manual smoke
  test.
- Protocol-era negotiation (see Release notes above): a connector that speaks the
  previous protocol version must keep working; covered by a raw `2025-06-18`
  handshake test.
- Python 3.10 resolution of `mcp` 2.x is unverified locally (Dependabot's CI
  did reach mypy on 3.10, so installs work).
- Optional hardening (`host_origin_protection`) is out of scope; may be noted
  in `docs/deployment.md`.

## Out of scope

Replacing or refactoring `PersonalAuthProvider`; adopting CIMD or ID-JAG;
enabling `host_origin_protection`; supporting FastMCP 3.x.

## Success criteria

- `uv sync --locked`, ruff, `mypy src tests`, pytest (>= 90% coverage) pass on
  Python 3.10 and 3.12.
- The new E2E test passes and would fail if the consent gate, PKCE check, or
  refresh rotation were broken.
- `uv.lock` contains no `diskcache`.
- Manual smoke test with a fresh and an existing Claude connector succeeds.
- 0.2.0 changelog is accurate; #54 is closed.
