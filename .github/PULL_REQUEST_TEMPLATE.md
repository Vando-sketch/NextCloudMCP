## What and why

<!-- What does this change, and why? Link the issue it closes, if any. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy src tests`
- [ ] `uv run pytest -q --cov=src/nextcloud_organizer_mcp --cov-fail-under=90`
- [ ] `CHANGELOG.md` `[Unreleased]` updated for user-visible changes
- [ ] Docs updated (`docs/tools.md`, `.env.example`) if tools or settings changed
