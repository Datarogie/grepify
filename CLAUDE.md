# CLAUDE.md — working rules for agents in this repo

These rules are binding for every Claude Code session. They exist to keep a
mobile-driven, agent-built project from drifting. Read them before touching code.

## Branch & review

- **Never commit or push to `main`.** Always work on a feature branch and open an
  MR/PR. `main` moves only through reviewed merges.
- Every session ends with `make check` green and an MR whose title carries the
  relevant issue IDs (e.g. `GRP-03: storage layer`).
- **No AI-authorship attribution in the repo.** Commit messages and MR/PR bodies
  must not contain `Co-Authored-By: Claude`, `Claude-Session:`, "Generated with
  Claude Code", `claude.ai/code` session links, or any similar tool/agent
  attribution. Keep messages about the change itself.

## Source of truth

- **`docs/prd.md` is the source of truth.** Never edit it silently. If something
  discovered mid-session changes the PRD, **propose the diff in the MR** and let
  Kyle decide — do not quietly change scope, schema, or decisions.
- Architecture decisions in PRD §5 are locked (JSONL truth + SQLite cache, Jinja
  SSG, named LLM profiles + budget gates). No architecture changes without asking.

## Scope discipline

- **No features beyond the issues in scope for the session.** The Non-Goals
  (PRD §2) and the issue plan (PRD §12) are the contract. Parking-lot ideas stay
  parked. No silent behavior changes.
- If an issue's AC is ambiguous, state the assumption in the MR and proceed;
  ask only if truly blocked.

## Work-in-progress cap

- **Max 2 active work streams** at any time. `[P]`-marked issues are the only
  safe parallels; everything else is sequential within an epic.

## Engineering standards (enforced by `make check`)

- Python 3.12, `uv` for env. `ruff` + `mypy --strict` on the core package,
  `pytest` for tests.
- SQL is lowercase; never `select *`. Interfaces stay Postgres-swappable — no
  SQLite-specific types in `Repository` / `ConfigProvider` signatures.
- Every module documents its **failure modes** in its docstring.
- Definition of done per issue: code + tests + fixtures + docstring failure
  modes + `make check` green.
