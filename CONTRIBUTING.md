# Contributing

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
uv run pytest -q --cov=src/nextcloud_task_mcp --cov-report=term-missing --cov-fail-under=90
```

- `ruff check` / `ruff format --check` — lint and formatting.
- `mypy src tests` — type checking. `src/nextcloud_task_mcp/personal_auth.py` is
  excluded (see "Vendored files" below).
- The coverage gate (`--cov-fail-under=90`) applies to `src/nextcloud_task_mcp` as
  a whole, with `personal_auth.py` omitted from the measured set for the same
  reason it's excluded from mypy/ruff — see below. If a change drops coverage
  below 90%, add tests rather than lowering the gate.

Integration tests against a real Nextcloud instance are skipped by default and
not part of the above; see the "Testing" section of [README.md](README.md) for
how to run them locally, and `.github/workflows/integration.yml` for how CI runs
them on a schedule.

## pre-commit

`.pre-commit-config.yaml` wires `ruff check --fix`, `ruff format`, and `mypy` into
`git commit` via local hooks that shell out to `uv run` — so the versions used
match `uv.lock` exactly, with nothing extra to install or keep in sync.

```bash
pre-commit install       # once, per clone
pre-commit run --all-files   # optional: run against the whole tree now
```

## Vendored files

`src/nextcloud_task_mcp/personal_auth.py` is vendored verbatim from
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
