# Contributing

## Branches

- `main` is the released state and the default branch. Do not commit to it directly.
- `dev` is where work happens. Branch from `dev`, open PRs against `dev`.
- Releases are cut by merging `dev` into `main` and tagging `main`.
- A release bumps the version in `pyproject.toml` and in both places in
  `server.json`. After the GitHub release has published to PyPI, publish the
  same version to the MCP Registry with `mcp-publisher login github` and
  `mcp-publisher publish`.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # installs the project + dev dependency group
pre-commit install            # optional but recommended, see below
```

## Running checks locally

These are the exact commands CI runs (`.github/workflows/ci.yml`); run them all
before opening a PR:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-report=term-missing --cov-fail-under=90
```

- `ruff check` / `ruff format --check` — lint and formatting.
- `mypy src tests` — type checking. `src/nextcloud_organizer_mcp/personal_auth.py` is
  excluded (see "Vendored files" below).
- The coverage gate (`--cov-fail-under=90`) applies to `src/nextcloud_organizer_mcp` as
  a whole, with `personal_auth.py` omitted from the measured set for the same
  reason it's excluded from mypy/ruff — see below. If a change drops coverage
  below 90%, add tests rather than lowering the gate.

## Integration tests

### Reverse-proxy regression tests

The default suite checks Uvicorn's forwarded-header trust and consent rate limiting.
To also run through a real Caddy HTTPS proxy, install Caddy and run:

```bash
RUN_PROXY_TESTS=1 uv run pytest tests/test_proxy_deployment.py -q
# If Caddy is not on PATH:
CADDY_BIN=/absolute/path/to/caddy RUN_PROXY_TESTS=1 uv run pytest tests/test_proxy_deployment.py -q
```

These tests use temporary certificates and loopback ports, without modifying system
certificate trust. Nextcloud is mocked. CI runs them in a separate job with a pinned
Caddy binary; an explicitly enabled run fails if Caddy is missing. Public DNS/TLS,
external MCP clients and real Nextcloud still need the
[deployment verification checklist](docs/deployment-verification.md).

### Real Nextcloud integration tests

Unit tests mock the `caldav` library and the Notes REST API (via
`httpx.MockTransport`) entirely - no network access, no real Nextcloud instance
required. Integration tests exercise the full flow against a real Nextcloud
instance (create, list, update, complete, delete a task in a disposable test
list) and are skipped by default. To run them:

```bash
export RUN_INTEGRATION_TESTS=1
export NEXTCLOUD_CALDAV_URL=... NEXTCLOUD_USERNAME=... NEXTCLOUD_APP_PASSWORD=...
export NEXTCLOUD_BASE_URL=https://cloud.example.com  # required by the Notes tests
export INTEGRATION_TEST_LIST="Test"   # an existing task list; tasks are created/deleted in it
uv run pytest -q
```

`.github/workflows/integration.yml` runs these on a weekly schedule (and on manual
dispatch) against a disposable `nextcloud` Docker container, so this path is
exercised against a real server periodically even though it's excluded from
per-PR CI.

## pre-commit

`.pre-commit-config.yaml` wires `ruff check --fix`, `ruff format`, and `mypy` into
`git commit` via local hooks that shell out to `uv run` — so the versions used
match `uv.lock` exactly, with nothing extra to install or keep in sync.

```bash
pre-commit install       # once, per clone
pre-commit run --all-files   # optional: run against the whole tree now
```

## Vendored files

`src/nextcloud_organizer_mcp/personal_auth.py` is vendored verbatim from
[fastmcp-personal-auth](https://github.com/crumrine/fastmcp-personal-auth) (it
ships as a single file to copy in, not an installable package), plus a small
number of documented local security patches — see the "LOCAL PATCHES" header
comment at the top of the file.

Rules for touching it:

- Keep it diffable against upstream: don't reformat or reflow lines that aren't
  part of an intentional patch.
- Any local patch (new or changed) must be logged in the "LOCAL PATCHES" header
  comment, with a short rationale.
- It's deliberately excluded from `ruff`, `ruff format`, `mypy`, and the
  coverage gate (see the `extend-exclude` / `exclude` / `omit` entries in
  `pyproject.toml`) for the same reason — those tools would otherwise want to
  reformat or restructure vendored code. Its own test coverage
  (`tests/test_auth*.py`) is expected to stay high regardless; the omission is
  only from the blanket 90% gate on the rest of the package.

## Commit style

Keep commits scoped and the message focused on *why*, not just *what*. Update
[CHANGELOG.md](CHANGELOG.md)'s `[Unreleased]` section for user-visible changes
(new tools/parameters, config changes, security fixes) — skip it for pure
internal refactors or test-only changes.
