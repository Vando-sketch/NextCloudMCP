# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Git workflow

Follow this for every implementation task, without waiting to be asked:

1. **Classify the task** as `feat` (new capability), `fix` (bug fix), or
   another conventional type (`refactor`, `chore`, `docs`, `test`) based on
   what the diff actually does.
2. **Create a branch** named `<type>/<short-kebab-description>` (e.g.
   `feat/wardrobe-photo-batching`, `fix/dashboard-auth-bypass`) off the current
   base branch before making changes. Always create a branch — never commit
   directly to `main` or to whatever branch you started on.
3. **Implement, test, and commit** on that branch.
4. **Open a pull request** (`gh pr create`) once the work is done and verified
   (tests + lint pass), targeting the branch you branched from.

### Breaking down larger tasks with subagents

For a task large enough to naturally split into independent subtasks:

1. Create your own `<type>/<description>` branch first, as above — this is the
   integration branch.
2. Split the work into subtasks and dispatch each to a subagent, picking the
   model per subtask's complexity (e.g. a small, mechanical change → a
   cheaper/faster model; a subtask requiring deep design or judgment → a
   stronger model).
3. Each subagent creates its own branch off the integration branch (same
   `<type>/<description>` naming convention), and independently follows the
   full workflow above on that branch: implement, test, commit. Subagents do
   not open their own pull requests and do not merge — only the main agent
   does.
4. Once a subagent's branch is done, the main agent merges it into its own
   integration branch, resolving any conflicts.
5. After all subagent branches are merged, the main agent runs the full test
   suite and lint on the integration branch, then opens the single pull request
   for the whole task.

## Timezone & Date Conventions

> **BREAKING CHANGE**: Timezone handling changed from hardcoded UTC to configurable server default timezone (`MCP_DEFAULT_TIMEZONE`, default `Europe/Berlin`).

- **Server Default Timezone**: Configured via `MCP_DEFAULT_TIMEZONE` (default `Europe/Berlin`). `MCP_DEFAULT_TIMEZONE=UTC` restores previous UTC behavior.
- **Naive Inputs**: Any naive datetime input (no UTC offset) is interpreted in the server's default timezone.
- **Day Windows**: Day bounds (agenda, `due_before`/`due_after`, `start`/`end`) are constructed in the default timezone.
- **Output Timestamps**: Timestamps returned to callers are formatted in the server's default timezone with offset (e.g. `+02:00`). All-day dates remain bare `YYYY-MM-DD` strings.
