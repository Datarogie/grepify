# Grepify - PRD and Agent Issue Plan

> Working name: **Grepify** (grep the firehose; -ify because it's a product, not a script). Runners-up if it sours: `newsgrep`, `rollup`. Note: a defunct regex GUI tool once used the name - check domain/pypi at scaffold; final call deferred to GRP-64 (M4). Rename stays a find/replace.

Version: 0.2 draft | Owner: Kyle | Status: pre-M0

Release versioning: 0.x during build (M0-M3 = 0.1-0.4); tag **v1.0.0 when M4 ships** (daily-usable). v1.5 = Slack digest push. v2 = multi-user in remote-labs (Postgres + serving layer + Okta). Mechanics: git tags + conventional commits; agents generate changelog.

---

## 1. Summary

Personal, configurable news/video/social aggregator inspired by trendcloud.io. Ingests RSS feeds, YouTube channels (incl. transcripts), Reddit subs, and specific X accounts; extracts keywords with a small/cheap LLM; computes trends; generates daily/weekly digests; renders a static site with keyword cloud, article browser, keyword detail pages, and digest archive.

Differences vs TrendCloud:

- **User-owned source registry**: source *groups* (bundles) that can be enabled/disabled, overridden, extended with own sources. TrendCloud's public source list is the seed default set (AI category only - crypto excluded by requirement).
- **X account watching** (specific people, not firehose) via twscrape - reuse xfilter patterns.
- **YouTube transcripts**, not just video titles, feed keyword extraction and digests.
- **Deterministic-first**: LLM only where it earns its keep (keyword extraction, digest prose). Everything else is plain code over local storage. No LLM in the serving path.
- **Zero-server v1**: scheduled CI (GH Actions → GitLab CI) + static site. No always-on infra, no metered serving cost. v1 is a **cloneable template repo** anyone can self-host; v2 is the multi-user remote-labs deployment on real infra (see §15).

## 2. Goals / Non-Goals

Goals

1. Replace manual feed-checking with one daily digest + browsable trend surface.
2. Fully operable and buildable from mobile (agents do the work; Kyle reviews MRs and the deployed site from phone).
3. Sustainable: < ~1 EUR/mo run cost target (free CI tiers + free/cheap LLM tier), graceful degradation when any source or the LLM is down.
4. Portable: GitHub first → GitLab (CI + Pages) with near-zero rework → optionally remote-labs later.
5. Reliable: idempotent pipelines, explicit failure modes, per-source health tracking, no silent data loss.

Non-Goals (v1)

- Multi-user accounts, auth, personalization-per-visitor. Single-tenant; "user config" = repo config files. (v2 scope - §15.)
- Per-user LLM digests, ever cheaply: digests are keyed to **category**, not user (§8, F-DIG). Only feature whose cost scales with users, so it stays category-level even in v2; personal digests are a capped v2.x option.
- Crypto category. Excluded entirely.
- Real-time updates. Batch cadence (2-4 fetches/day) is fine.
- Full-text article archival/search. Store title + summary/description + metadata + extracted keywords only (copyright + storage hygiene).
- Newsletter/email delivery (Slack digest push is v1.5 - F-SLK).
- Monetization of any kind.

## 3. Reference analysis (trendcloud.io)

What it does (verified by exploration):

| Surface | Behavior |
|---|---|
| Home | Keyword cloud for a date window + category, sized by mention count, each keyword links to detail page. Stats block: top keyword, top source, article/source/keyword/mention counts. Latest digests, latest articles, top sources. |
| Digests | Daily + weekly. LLM-written 2-4 paragraph narrative ("what mattered and why") with top-keyword chips (with counts). URL slugs like `/digest/daily/2026-07-07-<slug>` and `/digest/weekly/2026-W27-<slug>`. Filterable all/daily/weekly + by keyword. |
| Articles | Paginated list (20/page), filter by source and keyword. Each: source name, keyword tags, title, teaser, timestamp. Dedup imperfect (observed exact-duplicate entries). |
| Keyword pages | Date-windowed mention count, distinct source count, mention timeline, related keywords (co-occurrence with counts), latest content tabbed Articles / Reddit / YouTube. |
| Sources | Table (name, feed URL, type: RSS / YouTube / Reddit), 118 AI sources (147 total incl. crypto/cyber), paginated, "request a source" form. YouTube handled via `youtube.com/feeds/videos.xml?channel_id=...` (RSS - no API key). |
| About | Totals (45k signals / 90 days), GitHub-style fetch-activity heatmap, category coverage. |
| Stack signals | Next.js-style meta tags; gpt-4.1-nano for keyword extraction, nano+mini for digests (from the author's Reddit post); ~8 digests/wk keeps LLM cost trivial. |

Lessons to adopt: YouTube-via-RSS trick, digest-first value prop, keyword pages with co-occurrence, cheap LLM for extraction. Lessons to fix: visible duplicate articles (dedup is a first-class requirement), no user configurability, no transcripts.

## 4. Users and core flows

Single user (Kyle). Flows:

1. **Morning digest**: open site (or Slack push from v1.5), read daily digest, tap keywords of interest → keyword page → source items.
2. **Configure**: edit `sources/*.yml` in repo from mobile (GitLab/GitHub app or Claude Code session): enable a group, add an RSS URL, add an X handle, mute a keyword. Merge → next pipeline run picks it up.
3. **Investigate a trend**: keyword page → timeline + related keywords + items across article/reddit/youtube/x tabs.
4. **Operate**: pipeline failures surface as CI job failures + a `/health` page (per-source last-success, consecutive-failure count). No pager; check when convenient.

## 5. System architecture

```
                    ┌────────────── CI cron (2-4x/day) ───────────────┐
 sources/*.yml ──▶  ingest ──▶ normalize+dedup ──▶ SQLite (data/grepify.db)
                      │                                   │
                      │ (yt transcripts, x via twscrape)  ├──▶ extract (LLM keywords, batched, cached)
                      │                                   ├──▶ trends (pure SQL/python, deterministic)
                      │                                   ├──▶ digest (daily 1x, weekly 1x, LLM)
                      │                                   └──▶ build (SSG → public/) ──▶ Pages deploy
                      └──▶ health snapshot (json)
```

Decisions (and why):

- **Language**: Python 3.12, `uv` for env. Matches Kyle's stack and every library needed (feedparser, twscrape, youtube-transcript-api).
- **Storage (v0.2 - revised)**: **append-only JSONL is the source of truth**, SQLite is a derived query cache.
  - Truth: `data/items/YYYY/MM/DD.jsonl`, `data/keywords/YYYY/MM/DD.jsonl`, `data/digests/*.json`, committed to a dedicated **`data` branch** (not `main`, which carries a ruleset requiring PRs with no bypass - GRP-06 revision). Readable diffs, no binary blobs in git history, retroactive reprocessing = rerun over files.
  - Cache: `grepify.db` rebuilt in CI each run (seconds at this scale), never committed. All trend/related-keyword/timeline queries hit SQLite.
  - Concurrency: Actions concurrency group on the pipeline + rebase-retry on data commits (cron runs can't race).
  - Migration payoff: JSONL **is** the v2 migration - `COPY FROM` into Postgres and v2 starts with full history. Nothing here is throwaway.
  - Guardrails unchanged: metadata-only rows, no article bodies, transcripts compressed+capped, size check in CI (GRP-63).
- **Repository interface (v2-proofing)**: all storage access behind one `Repository` interface. v1 impl = JSONL+SQLite; v2 impl = Postgres. Same for config: `ConfigProvider` interface, filesystem-YAML impl in v1, DB-backed in v2. Pipeline, trend queries, digest assembler never know which.
- **LLM (v0.2 - named profiles)**: provider adjustable by config switch, hard budget gate per profile (CSR retry-loop lesson - bounded retries, circuit breaker, no unbounded loops, ever). Model + prompt version recorded per row for mixed-provenance auditability. Eval harness (GRP-24) is the regression check on any model/prompt switch. Rule: personal v1 never touches work seats; `workspace`/work-Claude profiles activate only in the remote-labs fork.

  ```yaml
  llm:
    active_profile: gemini-free
    profiles:
      gemini-free:  {endpoint: openai-compat, model: gemini-3.1-flash-lite, max_calls_per_run: 40}
      workspace:    {endpoint: vertex, model: tbd}            # Google Workspace seat, remote-labs era
      claude:       {endpoint: anthropic, model: claude-haiku-4-5}
      claude-local: {endpoint: cli, cmd: "claude -p"}          # xfilter pattern
  ```
- **Deterministic fallback**: if LLM unavailable/over budget → YAKE local extraction, items flagged `extraction_method='fallback'` for later re-extraction. Site still builds.
- **Frontend (locked)**: **Jinja SSG**. Kyle knows it from dbt, single-language repo for agents, no node toolchain in mobile sessions, byte-stable builds for snapshot tests. Interactivity is small and stays that way: sparklines = inline SVG generated in Python at build; cloud = CSS; filters = ~100 lines vanilla JS over emitted JSON. GRP-30 becomes skeleton work, not a spike.
- **Scheduling/runtime**: GitHub Actions cron initially; GitLab CI schedule later (same shell entrypoints: `make ingest extract trends digest build`). All commands runnable locally too - CI is just a scheduler.
- **Timezone**: digest day boundary and cron gating pinned to **America/Edmonton**, not UTC, so "yesterday's digest" matches lived days.
- **Secrets**: LLM key, X session cookies (twscrape accounts), Slack webhook/bot token in CI secrets/masked vars. Never in repo.
- **Cloneability**: template-repo hygiene from M0 - `settings.example.yml`, no hardcoded personal paths, `docs/setup.md`; anyone can fork + add secrets + enable cron.

## 6. Data model (logical schema - SQLite cache in v1, Postgres in v2)

Truth lives in JSONL (§5); this schema is what the derived cache exposes to queries. Column set is the contract either backend implements.

```sql
-- lowercase keywords per style; explicit names; no select *
create table sources (
  source_id      text primary key,          -- slug, e.g. 'ai-insider'
  name           text not null,
  kind           text not null check (kind in ('rss','youtube','reddit','x')),
  url            text not null,             -- feed url / channel rss / subreddit json / x handle
  url_hash       text not null unique,      -- canonical feed identity: two users adding same feed = one source (v2)
  group_id       text not null,             -- fk source_groups
  enabled        integer not null default 1,
  added_at       text not null,
  config_json    text                       -- per-source overrides (fetch depth, transcript on/off)
);

create table source_groups (
  group_id       text primary key,          -- 'ai-research', 'ai-tooling-dev', 'x-watchlist'
  name           text not null,
  category       text not null,             -- digest unit: 'ai', 'data-eng', ... (crypto never)
  enabled        integer not null default 1,
  builtin        integer not null default 0 -- seeded/curated vs user-added
);

create table items (
  item_id        text primary key,          -- stable hash: kind + canonical_url|external_id
  source_id      text not null references sources(source_id),
  kind           text not null,             -- rss|youtube|reddit|x
  external_id    text,                      -- guid / video_id / reddit id / tweet id
  canonical_url  text not null,
  title          text not null,
  summary        text,                      -- feed-provided description, truncated 2k chars
  author         text,
  published_at   text not null,
  fetched_at     text not null,
  content_hash   text not null,             -- for near-dup detection
  transcript_ref text,                      -- path to compressed transcript blob, youtube only
  lang           text
);
create index idx_items_published on items(published_at);
create index idx_items_source on items(source_id, published_at);
create unique index idx_items_dedup on items(kind, external_id);

create table item_keywords (
  item_id        text not null references items(item_id),
  keyword        text not null,             -- normalized: lowercase, trimmed, singularized where safe
  rank           integer not null,          -- position in extraction output (1 = most salient)
  method         text not null,             -- 'llm' | 'fallback'
  model          text,                      -- provenance
  extracted_at   text not null,
  primary key (item_id, keyword, method)    -- GRP-25 revision (Kyle-approved): method joins the
                                             -- key so a later llm re-extraction can coexist with
                                             -- an existing fallback row of the same keyword text
                                             -- instead of being silently dropped as a duplicate
                                             -- (closes the backfill convergence gap PR #8 flagged)
);
create index idx_kw_keyword on item_keywords(keyword);

create table keyword_aliases (
  alias          text primary key,          -- 'gen ai'
  canonical      text not null              -- 'genai'  (user-curated merge map)
);

create table digests (
  digest_id      text primary key,          -- 'daily-ai-2026-07-07' | 'weekly-ai-2026-W27'
  kind           text not null check (kind in ('daily','weekly')),
  category       text not null,             -- digest = f(category, window); never per-user
  period_start   text not null,
  period_end     text not null,
  title          text not null,
  body_md        text not null,
  top_keywords   text not null,             -- json [{keyword, count}]
  model          text not null,
  prompt_version text not null,             -- GRP-41 revision (Kyle-approved in
                                            -- PR #13): F-DIG-04 wants "model +
                                            -- prompt version recorded"; the
                                            -- prompt id ('digest-v1', or 'none'
                                            -- when the LLM-down template path
                                            -- ran) is stored per row so a
                                            -- mixed-provenance archive is
                                            -- auditable. Unlike item_keywords
                                            -- (model-only), digests carry the
                                            -- version explicitly.
  created_at     text not null
);

create table fetch_log (
  source_id      text not null,
  run_id         text not null,
  started_at     text not null,
  status         text not null check (status in ('ok','empty','error','skipped')),
  items_new      integer not null default 0,
  error          text,
  duration_ms    integer
);

create table llm_log (
  run_id         text not null,
  purpose        text not null,             -- 'extract' | 'digest'
  model          text not null,
  input_items    integer,
  tokens_in      integer, tokens_out integer,
  status         text not null,
  created_at     text not null
);
```

Notes:

- Trends are **not** materialized: keyword cloud / timelines / related keywords are computed at build time by SQL over `item_keywords` joined to `items` for the window. Deterministic, testable, no cache invalidation problem.
- `keyword_aliases` is the human-in-the-loop lever for merge noise ("gpt5" vs "gpt-5"). Applied at trend-computation time, not extraction time, so remaps are retroactive and non-destructive.
- Dedup layers: (1) unique `(kind, external_id)`; (2) `content_hash` = simhash/minhash of normalized title for near-dups across sources reposting the same wire story (grouped in UI, not deleted).

## 7. Source configuration (the differentiator)

Repo-native config via `ConfigProvider` (filesystem YAML in v1, DB-backed with UI in v2), no admin UI in v1:

```
sources/
  groups/                      # curated category bundles - the future checkbox UI
    ai-research.yml            # scrubbed from trendcloud list (Ahead of AI, MIT News, ...)
    ai-business.yml            # (AI Business, AI Insider, TechCrunch AI, ...)
    ai-tooling-dev.yml         # (InfoWorld AI, Towards Data Science, NVidia Blog, ...)
    youtube-ai.yml
    reddit-ai.yml
    data-engineering.yml       # Kyle-added, not from trendcloud
    x-watchlist.yml
  keywords.yml                 # aliases, mutes, pins
  settings.yml                 # cadence, windows, llm profiles, budgets
```

Group semantics: each group carries a `category`; **digests are generated per category** (trendcloud does the same - digests per AI/crypto/cyber, never per user). Users (v1: Kyle via yaml; v2: checkboxes) compose their view from curated groups + personal groups. Custom sources join a category, so they flow into that category's digest automatically - customization changes *what feeds a category*, not the digest unit.

```yaml
# groups/x-watchlist.yml
group: x-watchlist
name: X accounts
category: ai
enabled: true
sources:
  - id: x-karpathy
    kind: x
    handle: karpathy
  - id: x-simonw
    kind: x
    handle: simonw

# keywords.yml
aliases:
  "gen ai": genai
  "google i/o 2026": "google i/o"
mute:
  - webinar
  - sponsored
pin:            # always shown in cloud if any mentions
  - anthropic
  - dbt
```

```yaml
# settings.yml (excerpt)
llm:
  active_profile: gemini-free        # profiles defined in §5
  max_items_per_call: 25             # batch titles+summaries per call
windows:
  cloud_days: 7
  digest_daily_hours: 24
  digest_weekly: iso_week
limits:
  transcript_max_chars: 60000
  transcript_langs: [en]
```

Validation: `grepify validate` (also a CI check on every MR) - schema-validates YAML, pings each new feed once, rejects duplicates. This is the "request a source" form replaced by an MR.

## 8. Functional requirements

### Ingestion

- **F-ING-01** RSS: fetch enabled RSS sources with per-source timeout (10s), ETag/Last-Modified conditional GET, parse via feedparser, tolerate malformed feeds (log + continue).
- **F-ING-02** YouTube: channel RSS (`feeds/videos.xml?channel_id=`) for video metadata - no API key.
- **F-ING-03** YouTube transcripts: for new videos, attempt transcript via `youtube-transcript-api`; store compressed under `data/transcripts/`; absence is not an error (flag `transcript_ref=null`).
- **F-ING-04** Reddit: subreddit JSON endpoints (`/r/<sub>/new.json`) with UA header + backoff; store post title, selftext excerpt (2k cap), score, permalink. **Fallback**: if JSON blocked from CI IPs, drop to subreddit `.rss` endpoint at reduced cadence (explicit fallback path in fetcher).
- **F-ING-05** X: twscrape against configured handles; new tweets since last seen id per handle; store text, url, metrics. Session/account management identical to xfilter (documented failure modes: login challenge, rate limit → mark source `error`, continue run).
- **F-ING-06** Caps: per-run per-source new-item cap (default 50) to bound first-run backfills and pathological feeds.
- **F-ING-07** Idempotency: re-running ingest never duplicates rows or re-fetches transcripts already stored.
- **F-ING-08** Health: every source attempt logged to `fetch_log`; 5 consecutive errors → source flagged on health page (still retried; auto-disable is out of scope v1).

### Extraction

- **F-EXT-01** Batch untagged items (title + summary + transcript-excerpt≤1500 chars for youtube) into LLM calls, ≤25 items/call, strict JSON out: `[{item_id, keywords: [..max 8..]}]`.
- **F-EXT-02** Response validation: JSON parse, item_id echo check, keyword sanity (len 2-60, no urls). Invalid → one retry, then fallback extractor for that batch. Total retries bounded; circuit breaker per F-config `max_extract_calls_per_run`.
- **F-EXT-03** Normalization: lowercase, trim, collapse whitespace, strip trailing punctuation; alias map applied downstream.
- **F-EXT-04** Cache: extraction keyed by item_id - never re-extract unless `--force` (re-extraction backfill job exists for method='fallback' rows).
- **F-EXT-05** Crypto exclusion: category never ingested; additionally a mute list drops keyword rows matching configured mutes.

### Trends and digests

- **F-TRD-01** Cloud dataset: top N keywords by mention count in window, after alias merge + mutes, with per-keyword counts and deltas vs previous window.
- **F-TRD-02** Keyword detail dataset: daily mention timeline (window), distinct sources, top co-occurring keywords (same-item co-occurrence, count-ranked), latest items grouped by kind.
- **F-TRD-03** Rising detection (deterministic): keywords with count ≥ k (default 3) and window-over-window ratio ≥ r (default 3x) flagged "rising" - feeds digest prompt, badge in cloud.
- **F-DIG-01** Daily digest **per category**: input = category's top/rising keywords + their top item titles/summaries for prior 24h (America/Edmonton day boundary); output = title + 2-4 paragraph markdown narrative + TL;DR bullets + top-keyword chips. One LLM call per category. Stored in `digests`. Never per-user (cost scales with categories, not users).
- **F-DIG-02** Weekly digest: same, ISO week, slightly longer.
- **F-DIG-03** Digest is skipped (not failed) if < minimum item threshold (default 10 items in category) - logged.
- **F-DIG-04** Digest prompt includes only data derived from stored items (no browsing); model + prompt version recorded.

### Slack push (v1.5)

- **F-SLK-01** After digest generation, post daily/weekly digest to Slack. v1.5 mechanism (pick at build time, both supported): (a) incoming webhook → private channel with just Kyle (simplest, one secret), or (b) bot token + `chat.postMessage` to Kyle's DM (needs a workspace app but is the v2-correct shape).
- **F-SLK-02** Payload: digest title, TL;DR bullets, top keywords, link to full digest page. Block Kit formatting; graceful truncation at Slack limits.
- **F-SLK-03** Push failure never fails the pipeline; logged + surfaced on health page.
- **F-SLK-04** v2: proper Slack app in the Remote workspace - per-user DM subscriptions to category digests (subscription table), same generated digest fan-out, no extra LLM calls.

### Site

- **F-SIT-01** Home: keyword cloud (log-scaled sizes, links to keyword pages), stats block, latest digests (5), latest items (10), top sources.
- **F-SIT-02** Digest index + detail pages, daily/weekly filter; URL scheme `/digest/daily/YYYY-MM-DD-slug/`.
- **F-SIT-03** Items browser: paginated, client-side filter by source/kind/keyword within emitted JSON pages; near-dup groups collapsed with "n similar" expander.
- **F-SIT-04** Keyword pages for every keyword above threshold (≥3 mentions in trailing 30d) - timeline sparkline, related keywords, tabbed content by kind.
- **F-SIT-05** Sources page: rendered from config + `fetch_log` health (last success, items last 7d).
- **F-SIT-06** Health page: per-source status, last run summary, LLM budget usage.
- **F-SIT-07** Mobile-first layout (Kyle's primary device), dark mode default, no trackers, no external fonts.
- **F-SIT-08** Build is pure function of DB + config: same inputs → byte-stable output (except timestamps) for snapshot testing.

### Ops

- **F-OPS-01** Single entrypoint CLI: `grepify ingest|extract|trends|digest|build|validate|health|backfill`.
- **F-OPS-02** CI workflows: `pipeline` (cron 3x/day: ingest→extract→build+deploy, including the Pages deploy in the same job since it needs that run's build artifact - GRP-06 revision, folds the originally-separate `deploy` workflow into `pipeline`; digest steps gated by time-of-day), `validate` (on MR).
- **F-OPS-03** GitLab portability: no GH-only features in app code; CI logic lives in `make` targets; `.gitlab-ci.yml` written and kept green from M3.
- **F-OPS-04** Run manifest: every run writes `data/runs/<run_id>.json` (counts, durations, budget usage) - powers health page and debugging from phone.

## 9. Non-functional requirements

- **Cost (v1)**: LLM ≤ free tier (budget gate enforces); hosting free (Pages); CI within free minutes (target < 15 min/day total). **Cost (v2, remote-labs)**: infra cost is not the constraint; governance shifts to agent/LLM token-spend controls (budget gates stay, tuned rather than free-tier-capped).
- **Reliability**: any single source failing never fails the run; LLM failing never blocks site build; pipeline exit non-zero only on systemic failure (DB corruption, build error).
- **Performance**: full pipeline run < 10 min at 150 sources; site build < 2 min at 50k items (paginate/emit only trailing 90d to pages; older data queryable via DB only).
- **Security**: secrets only in CI vars; twscrape session files encrypted at rest in CI cache or re-authed per run; no PII beyond public posts.
- **Maintainability**: ruff + mypy (strict on core), 100% of pipeline logic unit-testable without network (all fetchers behind interfaces with fixture-based fakes).

## 10. Testing strategy

Layers (all runnable locally and in CI on every MR):

1. **Unit** (pytest, no network): parsers (feedparser edge fixtures: bad dates, missing guids, html-in-title), normalizers, dedup hashing, alias merge, rising-detection math, LLM response validators (valid/malformed/truncated JSON fixtures), budget circuit breaker.
2. **Contract fixtures**: recorded real payloads per source kind (RSS xml, YT rss, YT transcript, reddit json, twscrape objects) checked into `tests/fixtures/`; ingestion asserted against them. When a live format drifts, add the new payload as a fixture - regression suite grows.
3. **Pipeline integration**: end-to-end run against fixture fakes into a temp SQLite → assert row counts, idempotency (run twice, same counts), fallback path (LLM fake returns 500 → items get fallback keywords → build succeeds).
4. **Golden/snapshot**: `build` from a canned DB fixture → snapshot key pages' HTML/JSON (F-SIT-08). Any rendering change is an explicit snapshot update in the MR diff.
5. **LLM eval (lightweight, offline)**: 30-item labeled set (titles → expected keyword sets); scored on jaccard overlap; run manually/`make eval` when changing extract prompt or model - not in CI gate, but result pasted in MR description (no silent prompt regressions).
6. **Smoke (live, scheduled only)**: nightly job hits 3 canary sources for real + 1 real LLM call; failures notify but don't block deploys.
7. **Data quality checks in-pipeline**: post-extract assertions (every new item has ≥1 keyword or an explicit `no_keywords` flag; no keyword > 60 chars; digest references only existing keywords). Violations fail the run loudly - no silent behavior changes.

Definition of done per issue: code + tests + fixtures + docstring failure modes + `make check` green.

## 11. Milestones

- **M0 - Skeleton** (repo, CI, CLI, storage + config interfaces, validate, cloneability hygiene). Site says "hello" on Pages.
- **M1 - Ingest core** (RSS + YouTube metadata + Reddit; dedup; health; fetch_log). Data filling on cron.
- **M2 - Extraction** (LLM batch extract + fallback + budget gate + cache + eval harness).
- **M3 - Site v1** (home cloud, items browser, sources, health; snapshot tests; GitLab CI file green).
- **M4 - Digests + keyword pages** (per-category daily/weekly digests, keyword detail pages, rising detection). **Tag v1.0.0 here** - daily-usable.
- **M5 - X + transcripts** (twscrape ingestion; YT transcripts feeding extraction).
- **M6 - Hardening + migration** (backfill tooling, near-dup grouping, GitLab cutover, docs, runbook).
- **v1.5 - Slack push** (E7): digest to Slack DM/channel post-generation.
- **v2 - remote-labs** (§15): Postgres Repository impl, serving layer, Okta, subscription model, Slack app fan-out.
- Parking lot: per-keyword RSS out, newsletter, admin UI, personal LLM digests (capped), more categories.

Order rationale: value lands at M2/M3 (usable trend surface) before the riskier integrations (X sessions, transcripts) - those can lag without blocking daily use.

## 12. Issue plan (agent-ready)

Conventions:

- IDs `GRP-xx`. One epic ≈ one agent context window. Issues inside an epic are sequential unless marked `[P]` (parallel-safe).
- Every issue body must carry: scope, non-scope, files touched, acceptance criteria (AC), test list. Template in `docs/issue-template.md` (GRP-02).
- Agent workflow per issue: read epic brief (`docs/epics/E<n>.md`, ~1 page, written once per epic) + the issue only. **No cross-epic context required by design** - epic briefs restate needed interfaces.
- Model routing: design/ambiguous issues → Opus-class; well-specified implementation issues → Sonnet-class. Marked below.

### E0 - Foundation (M0) - brief: repo shape, tooling, contracts

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-01 | Repo scaffold: uv, pyproject, ruff+mypy, pytest, makefile (`check`, `test`, targets stubbed); template-repo hygiene (settings.example.yml, docs/setup.md, no personal paths) | Sonnet | `make check` green in CI; fork+secrets+cron = running clone |
| GRP-02 | Docs: architecture.md (condensed §5-§7 of this PRD), issue-template.md, epic brief template | Sonnet | agents can onboard from docs alone |
| GRP-03 | Storage layer: `Repository` interface + v1 impl (JSONL truth writer/reader + SQLite cache rebuild, schema §6 as DDL); Actions concurrency group + rebase-retry on data commits | Opus | rebuild deterministic; double-run idempotent; interface has no sqlite types in signatures (Postgres-swappable) |
| GRP-04 | Config layer: `ConfigProvider` interface + filesystem-YAML impl; pydantic schemas for groups/keywords/settings + `grepify validate` | Sonnet | rejects dup source ids/url_hashes, bad kinds, missing category; fixture tests |
| GRP-05 | CLI skeleton (`typer`): all subcommands stubbed, run_id + run manifest writer | Sonnet | `grepify health` prints manifest |
| GRP-06 | GH Actions: `validate` on MR, `pipeline` cron calling make targets, Pages deploy of placeholder site | Sonnet | cron runs green end to end |
| GRP-07 | Seed curated groups: scrub trendcloud AI source list (118, 12 paginated pages, expect ~10% dead) into **categorized** group files (ai-research / ai-business / ai-tooling-dev / youtube-ai / reddit-ai), verify feeds alive, mark dead ones disabled | Opus | categorization judgment involved; validate passes; list in issue body |

### E1 - Ingestion core (M1) - brief: fetcher interface, item contract, dedup rules

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-10 | Fetcher interface + registry (`Fetcher.fetch(source) -> list[RawItem]`), fake fetcher for tests | Opus | design issue; defines contract all of E1/E5 uses |
| GRP-11 | RSS fetcher: conditional GET, timeout, malformed-feed tolerance | Sonnet | 6 fixture feeds incl. broken ones |
| GRP-12 | [P] YouTube metadata fetcher (channel RSS) | Sonnet | video_id as external_id |
| GRP-13 | [P] Reddit fetcher (`new.json`, UA, backoff) | Sonnet | respects 50-item cap |
| GRP-14 | Normalizer + dedup: item_id hashing, unique index handling, content_hash (simhash) for near-dups | Opus | idempotency test: double-run zero new rows |
| GRP-15 | Ingest orchestrator: per-source isolation, fetch_log, caps, summary to manifest | Sonnet | one failing source doesn't fail run (test) |
| GRP-16 | Health snapshot: consecutive-failure computation, `data/health.json` | Sonnet | fixture-driven |

### E2 - Extraction (M2) - brief: LLM provider contract, budget rules, fallback

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-20 | LLM provider module: OpenAI-compat client, retries (bounded, jittered), budget circuit breaker, llm_log | Opus | breaker test: 41st call refused at cap 40 |
| GRP-21 | Extract batcher + prompt v1: strict-JSON schema, item echo validation, per-batch fallback on 2nd failure | Opus | malformed-JSON fixtures |
| GRP-22 | [P] Fallback extractor (YAKE), method flag, re-extraction backfill command | Sonnet | fallback rows re-extractable |
| GRP-23 | [P] Keyword normalization + alias/mute application module | Sonnet | pure functions, table-driven tests |
| GRP-24 | Eval harness: 30 labeled items, jaccard score, `make eval` report | Sonnet | Kyle labels the 30 (small manual task) |
| GRP-25 | Wire extract into pipeline cron; data-quality assertions (§10.7) | Sonnet | e2e integration test |

### E3 - Site v1 (M3) - brief: build contract (DB+config → public/), page inventory, style tokens

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-30 | Jinja SSG skeleton (decision locked - §5): base layout, dark mobile-first, style tokens, inline-SVG sparkline helper, ADR recorded | Sonnet | snapshot of base pages |
| GRP-31 | Trend queries module: cloud dataset, deltas, top sources, stats (pure SQL, window-parameterized) | Opus | unit tests on canned DB |
| GRP-32 | Home page: cloud + stats + latest lists | Sonnet | snapshot test |
| GRP-33 | [P] Items browser: pagination, emitted JSON filters, near-dup collapse | Sonnet | snapshot + JS filter test |
| GRP-34 | [P] Sources + health pages | Sonnet | renders from config+health.json |
| GRP-35 | Build command + Pages deploy of real site; trailing-90d emission rule | Sonnet | build < 2 min on fixture of 50k items |
| GRP-36 | `.gitlab-ci.yml` mirroring GH workflows (kept green from here on) | Sonnet | dry-run via `gitlab-ci-local` or lint |

### E4 - Digests + keyword pages (M4) - brief: digest data contract, url schemes

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-40 | Rising detection + digest input assembler, **per category** (deterministic, tested) | Opus | ratio/threshold config-driven |
| GRP-41 | Daily digest generation per category: prompt v1, storage, skip-threshold, provenance | Opus | offline fixture test with fake LLM |
| GRP-42 | [P] Weekly digest (ISO week) | Sonnet | reuses GRP-41 machinery |
| GRP-43 | Digest pages + index + filters | Sonnet | snapshot tests |
| GRP-44 | Keyword detail pages: timeline sparkline, related keywords, tabbed content | Sonnet | co-occurrence query unit-tested |
| GRP-45 | Cron gating: digest steps run in correct time-of-day windows, America/Edmonton-pinned | Sonnet | pure-function time-gate tests incl. DST edges |

### E5 - X + transcripts (M5) - brief: twscrape ops runbook (from xfilter), transcript caps

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-50 | twscrape integration: account/session management in CI, X fetcher behind GRP-10 interface, since_id tracking | Opus | failure modes documented: challenge, ratelimit, suspended |
| GRP-51 | X items in site (kind tab, keyword extraction on tweet text) | Sonnet | fixtures from xfilter |
| GRP-52 | Transcript fetcher: youtube-transcript-api, compression, caps, absence-is-ok | Sonnet | idempotent, size-cap test |
| GRP-53 | Transcript excerpting into extraction batches (first 1500 chars + smart cut) | Sonnet | eval delta reported |

### E6 - Hardening + migration (M6)

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-60 | Backfill + maintenance commands: re-extract, reindex, vacuum, prune transcripts | Sonnet | |
| GRP-61 | Runbook: docs/runbook.md - every failure mode → diagnosis → fix, phone-operable | Opus | reviewed against real incidents so far |
| GRP-62 | GitLab cutover: repo migrate, schedules, Pages, secrets; GH archived | Sonnet | one pipeline green on GitLab before switch |
| GRP-63 | Data size guardrail check in CI (JSONL dir + transcripts) + documented parquet escape hatch | Sonnet | warn at 100 MB, fail at 200 MB |
| GRP-64 | Naming finalization + favicon/wordmark, README polish | Sonnet | |

### E7 - Slack push (v1.5) - brief: digest payload contract, Slack limits

| ID | Title | Model | Notes / AC |
|---|---|---|---|
| GRP-70 | Slack notifier module: webhook impl + bot-token `chat.postMessage` impl behind one interface, Block Kit formatting, truncation, failure-isolated (F-SLK-03) | Sonnet | fixture tests; no pipeline failure on Slack 500 |
| GRP-71 | Wire into pipeline post-digest; secrets docs; health-page surfacing | Sonnet | e2e with fake Slack |

Estimated total: ~37 issues, 8 epics, each epic sized for 1-3 agent sessions.

## 13. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| twscrape breakage / X countermeasures (recurring) | High | Isolated behind fetcher interface; site fully functional without X; runbook; treat as best-effort source class |
| Gemini free-tier limits/model availability change | Med | Named-profile abstraction + budget gate + local fallback extractor; digest degrades to template ("top keywords: ...") |
| YouTube transcript API blocked from CI IPs | Med | Absence-tolerant by design; optional local fetch path from phone/Termux later |
| Reddit JSON blocked from CI IPs | Med | `.rss` endpoint fallback at reduced cadence (F-ING-04) |
| Data-in-git growth | Low (was Med) | JSONL metadata-only rows, caps, GRP-63 guardrail, parquet escape hatch; SQLite never committed |
| Keyword noise/fragmentation degrades cloud | High | Alias map + mutes + eval harness + rank cap 8/item |
| CI free minutes exhausted | Low | <15 min/day budget; cadence reducible to 1x/day with zero code change |
| Scope creep (multi-user before v2, more categories) | High | Non-goals section is the contract; parking lot exists; v2 boundary in §15 |
| v1→v2 rework risk | Low by design | Repository/ConfigProvider interfaces + JSONL-as-migration + category-keyed digests mean v2 swaps impls, not pipeline |

## 14. Open questions - status

1. ~~Name~~: **grepify** (working; final call at GRP-64, runners-up newsgrep/rollup).
2. ~~Categories at launch~~: **ai + data-eng**. data-engineering.yml seeds: benn.substack.com/feed, roundup.getdbt.com/feed (Analytics Eng Roundup), Locally Optimistic, dbt + Snowflake engineering blogs (verify feeds at GRP-07).
3. Reddit seeds (proposed, confirm at GRP-07): r/LocalLLaMA, r/MachineLearning, r/dataengineering.
4. X watchlist seed (confirmed + candidates): karpathy; candidates simonw, emollick, natolambert, swyx, fchollet; data-eng lane bennstancil, jthandy. Pick 4-6 at GRP-50.
5. Digest tone: prompt v1 = narrative + TL;DR bullets (bullets double as Slack payload); adjust after week 1.
6. **OPEN** - Slack v1.5 mechanism default: webhook-to-private-channel (one secret, no app approval, ships today - recommended) vs bot-token DM (v2-correct shape). Both built behind one interface (GRP-70) regardless.

## 15. v2 - remote-labs (design boundary, not v1 scope)

North-star architecture is what trendcloud actually runs (live app + real DB); v1 deliberately trades that for zero-cost CI + static. The swap is contained by design:

- **Storage**: Postgres implements the `Repository` interface. Migration = `COPY FROM` the JSONL truth files; full history preserved, no export tooling.
- **Serving**: FastAPI (or Phoenix if adopted by hosting team) replaces SSG for dynamic views; Okta in front of app + admin, never in the pipeline.
- **Multi-user model**: shared corpus, per-user views. Ingestion/extraction/trends stay global - a user "adding" an existing source (matched on `url_hash`) creates a subscription row, never a re-fetch or re-extraction. Personalization = filters over shared data (free at any user count).
- **Digests stay category-keyed**: users subscribe to category digests (site + Slack app DM fan-out via F-SLK-04). Per-user LLM digests remain the one linear-cost feature - capped opt-in v2.x at most.
- **Config**: DB-backed `ConfigProvider` + checkbox UI over the same curated group files (groups become seed rows).
- **Cost posture**: infra cost not a constraint inside remote-labs; governance = LLM/agent token budgets (existing budget-gate machinery, retuned) per Work-plan policy - no uncontrolled background calls (CSR incident rule stands).
- **Gate**: v2 starts only after v1 has run reliably for Kyle locally/remotely per the remote-labs entry bar (built + tested outside first).
