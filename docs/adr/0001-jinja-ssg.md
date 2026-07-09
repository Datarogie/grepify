# ADR 0001 — Jinja SSG for the v1 site

- **Status**: Accepted (locked, PRD §5 "Frontend (locked)")
- **Date**: 2026-07-09 (recorded at GRP-30)
- **Deciders**: Kyle
- **Context PRD**: §5 (Frontend), §8 F-SIT-01..08, §9 (performance/security), §15 (v2 boundary)

## Context

The v1 site is a **zero-server static build** (PRD §5 "Zero-server v1"): a CI
cron renders `public/` and GitHub/GitLab Pages serves it. There is no serving
process, no LLM in the serving path, and the build must be reproducible for
snapshot testing (F-SIT-08). The repo is mobile-driven and agent-built, so the
toolchain has to be installable and runnable from a phone-triggered CI session
with no local dev machine in the loop.

## Decision

Render the site with **Jinja2** templates from Python, at build time. No Node
toolchain, no client framework, no bundler. Interactivity is deliberately tiny
and stays that way:

- **Keyword cloud** — CSS (log-scaled font sizes), links to keyword pages.
- **Sparklines** — inline SVG generated in Python at build time (`grepify.site.sparkline`).
- **Items filters** — ~100 lines of vanilla JS over an emitted JSON index
  (GRP-33), no framework.

Style is a set of **design tokens** (`grepify.site.tokens`) rendered into CSS
custom properties — the single source of truth for palette/spacing/type. The
site is **dark, mobile-first**, with **no external fonts and no trackers**
(F-SIT-07): a system font stack only.

## Consequences

- **Single-language repo** — everything an agent touches is Python + templates;
  no `npm`/`node_modules` in mobile sessions.
- **Byte-stable builds** (F-SIT-08) — the build is a pure function of the
  SQLite cache + config; the clock is injected (`grepify.clock`), all dict/set
  iteration is sorted before templating, so snapshot tests can assert
  byte-identical output across two consecutive renders (S8 determinism rule).
- **Cheap CI** — `pip`/`uv` install of Jinja2 only; build target is `< 2 min`
  at 50k items via the trailing-90d emission rule (§9, GRP-35).
- **v2 boundary is clean** — PRD §15 replaces the SSG serving layer wholesale
  with FastAPI (or Phoenix) + Postgres. The v1 site code (`grepify.site`) is
  therefore intentionally SQLite-aware and thrown away at v2; only the pipeline,
  `Repository`, and JSONL truth cross the boundary. Trend queries reading the
  cache directly (not via `Repository`) is a consequence of this, not a leak.

## Alternatives considered

- **Next.js / static export** — matches trendcloud's stack, but pulls in a Node
  toolchain (breaks single-language + mobile-session goals) and a non-trivial
  bundler for what is a mostly-static surface. Rejected in PRD §5.
- **A live app in v1 (FastAPI/Phoenix)** — that is the v2 design (§15); it needs
  always-on infra and metered serving cost, which v1 explicitly avoids.
- **Hand-written HTML strings in Python** — no template inheritance, no
  autoescaping; worse maintainability and XSS-safety than Jinja for no gain.
