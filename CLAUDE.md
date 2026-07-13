# CLAUDE.md - working rules for agents in this repo

These rules are binding for every Claude Code session. They exist to keep a
mobile-driven, agent-built project from drifting. Read them before touching code.

## Writing conventions (hard rules, top priority)

- **Never use em dashes or en dashes** (the `U+2014` and `U+2013` characters)
  anywhere: not in commit messages, PR/MR titles or bodies, code, comments,
  docstrings, docs, or config. Use a spaced hyphen (` - `) for a clause break,
  or reword; use a plain hyphen (`-`) for numeric or section ranges. (Section
  marks `§`, arrows, and the like are fine - this rule is only about dashes.
  Genuine external data, e.g. real feed names or recorded fixture payloads, is
  left verbatim.)
- **Never add AI/agent authorship attribution.** No `Co-Authored-By:` trailer
  (any name), `Claude-Session:`, "Generated with Claude Code", `claude.ai/code`
  link, or anything similar - in commit messages, PR/MR titles/bodies, or code.
  Every message is about the change itself, nothing else.
- **Clean up before a PR is ready.** Before opening OR updating a PR, sweep the
  branch and remove anything that slipped in: `git grep -nP "\x{2014}|\x{2013}"`
  for em/en dashes, and `git log origin/main..HEAD` for attribution trailers in
  commit messages. A PR is not ready until both are clean.

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
  Kyle decide - do not quietly change scope, schema, or decisions.
- Architecture decisions in PRD §5 are locked (JSONL truth + SQLite cache, Jinja
  SSG, named LLM profiles + budget gates). No architecture changes without asking.

## Scope discipline

- **No features beyond the issues in scope for the session.** The Non-Goals
  (PRD §2) and the issue plan (PRD §12) are the contract. Parking-lot ideas stay
  parked. No silent behavior changes.
- If an issue's AC is ambiguous, state the assumption in the MR and proceed;
  ask only if truly blocked.

## Security (public repo)

- The repo is **public** - treat every workflow file, log line, and committed
  artifact as visible to anyone.
- `validate`/PR-triggered workflows must **never reference `LLM_API_KEY` or
  any other secret**. Secrets are only ever consumed by the `pipeline`
  workflow (schedule/`workflow_dispatch`, never `pull_request`), and only in
  steps that don't echo them.
- Never log request headers or credential-bearing config (API keys, tokens,
  session cookies) - not even truncated/masked by hand; rely on GitHub's
  built-in secret masking, don't build a parallel one.
- Keep the repo's default fork-PR approval settings (workflow runs from
  first-time/outside contributors require maintainer approval) - do not
  relax them for convenience.

## Parallel work

- No hard cap on active work streams (the old max-2 rule is retired,
  2026-07-13, Kyle's call). Run as many parallel sessions/agents as the work
  supports, with one constraint: **two concurrent streams must not touch the
  same files or the same PRD decision.** Check open PRs and claimed issues
  before starting; if your issue overlaps an in-flight branch, wait or pick
  another. Issues that share modules stay sequential; blocked-by lines in
  issue bodies are binding.

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues in this repo (`github.com/Datarogie/grepify`). See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout: a root `CONTEXT.md` (created lazily by `/domain-modeling` when needed) plus the existing `docs/adr/`. See `docs/agents/domain.md`.

## Engineering standards (enforced by `make check`)

- Python 3.12, `uv` for env. `ruff` + `mypy --strict` on the core package,
  `pytest` for tests.
- SQL is lowercase; never `select *`. Interfaces stay Postgres-swappable - no
  SQLite-specific types in `Repository` / `ConfigProvider` signatures.
- Every module documents its **failure modes** in its docstring.
- Definition of done per issue: code + tests + fixtures + docstring failure
  modes + `make check` green.
