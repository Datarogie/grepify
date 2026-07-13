# Full project review - 2026-07-13

Scope: whole repo at main `942121f` - product idea, PRD alignment, package
code, CI/ops/security, docs and tracker. `make check` is green at this
commit (ruff, `mypy --strict`, 547 tests). Findings are ranked within each
section; a consolidated action plan is at the end.

## 1. Executive summary

Grepify has quietly shipped its entire v1 plan. M0-M5 are done, most of M6 is
done, and the codebase is unusually disciplined: failure-mode docstrings
everywhere, byte-stable builds, bounded LLM budgets, per-source isolation,
clean secret hygiene in a public repo. The biggest problems are not in the
code - they are around it:

1. **The release never happened.** PRD §11 says "tag v1.0.0 at M4"; there are
   no git tags at all. The project is at-or-past its own release gate
   (docs/prev1-hardening.md §4) but was never tagged.
2. **The issue tracker is inverted from reality.** All 10 open issues
   (#29-#33, #37, #38, #39, #47, #50) have merged PRs; the genuinely open
   work (runbook GRP-61, GitLab cutover GRP-62, size guardrail GRP-63,
   naming GRP-64, Slack E7, the F1 chip-link fix) has no open issues.
3. **One real latent outage** in an otherwise robust pipeline: a validated
   `kind: x` source crashes the whole ingest run (§3.1).
4. **Docs drift is concentrated on the X retirement**: README, setup,
   architecture, and PRD §5/§6/§10 still describe twscrape/X as live.

## 2. Where the project stands

| Milestone | Status |
|---|---|
| M0 skeleton, M1 ingest, M2 extraction, M3 site, M4 digests + keywords, M5 transcripts + ai-voices | Shipped |
| M6 hardening | Partial: backfill/maintenance and near-dup grouping done; GRP-61/62/63/64 open |
| v1.0.0 tag | Missing (no tags exist) |
| v1.5 Slack (E7) | Not started |

The prev1-hardening gates: T3 (self-healing digests, #24) and T4 (next-digest
time, #34) are merged. O1 (the operational data-remediation run) has no
recorded run id anywhere - HANDOFF.md moved on to #45 without it. Either O1
ran and was not recorded, or it is still pending; that is the only thing
standing between the repo and its own definition of releasable.

## 3. Correctness findings (package)

### 3.1 A `kind: x` source fails the entire ingest run (P1)

Config accepts `kind: x` with a `handle` locator
(`grepify/config/schemas.py:35`, validated clean by `grepify validate`), but
`build_registry` registers only rss/youtube/reddit fetchers
(`grepify/ingest/orchestrator.py:141-152`). At ingest time the registry
lookup raises `KeyError`, which `_run_source` re-raises as systemic
(`orchestrator.py:260-265`) - one misconfigured source kills every other
source. This contradicts the "one dead source never fails the run"
principle. Fix: `validate` should require every configured kind to have a
registered fetcher (`registry.registered_kinds()` already exists and is only
used in tests), or treat an unregistered kind as a per-source error row.

### 3.2 Ingest rescans all truth once per source (P1 performance)

The orchestrator calls `repository.add_items()` per source
(`orchestrator.py:256`), and each call runs `existing_item_ids()`, which
rglobs and reads every items JSONL file
(`grepify/repository/jsonl_sqlite.py:60`, `_existing_keys` around line 400).
That is O(sources x total_items) per run - the dominant cost as history
grows. Fix: load the existing-id set once per run and add all sources'
items in one batch.

### 3.3 The `pin` list is accepted but never applied (P2)

PRD §7 promises pinned keywords are "always shown in cloud if any mentions".
`pin` exists in the schema (`grepify/config/schemas.py:118`) and in
`sources/keywords.yml`, but nothing in `grepify/keywords.py` or
`grepify/site/trends.py` applies it - `cloud()` ranks purely by count and
truncates. Silent config no-op: either implement pin injection or remove the
field until it works.

### 3.4 Smaller items (P3)

- Sparkline edge buckets: trend windows end at `clock.now()`, so a
  `days`-long window spans days+1 calendar dates and the last bucket
  conflates two partial days (`grepify/site/trends.py:652-656`).
  Deterministic but subtly wrong; aligning windows to Edmonton midnight
  would also make cloud counts stable within a day.
- Digest gate comments disagree: `gating.py:11` says 05:00-08:00,
  `next_digest.py:3` says 05:00-08:59; the code (`hour <= 8`) matches the
  latter. Align the comments.
- Cross-source guid dedup is intentional but silent: two sources sharing a
  guid collapse to whichever ingested first (`ingest/normalize.py:164-167`).
  Worth one docstring line.

## 4. Security / CI / ops findings

The hard rules hold: `validate.yml` references no secrets and is
`contents: read`; secrets appear only in `pipeline.yml` (schedule +
dispatch, never PR); no `shell=True` anywhere; the data-commit message is a
static constant so feed content cannot reach a shell. Loop prevention on the
data branch is triple-guarded. Ranked improvements:

1. **No dependency automation or advisory scanning** (P1). pyproject pins
   are floor-only, `uv.lock` carries reproducibility, but nothing watches
   for CVEs in a secret-bearing pipeline on a public repo. Add dependabot
   or renovate plus a pip-audit step in validate. Low effort, highest
   security ROI.
2. **Enable ruff `S` (bandit) on the core package** - cheap standing guard
   for the subprocess-heavy `repository/commit.py` path.
3. **Deploy is coupled to ingest success** (`pipeline.yml:149-165`): a
   transient feed/LLM failure also skips an otherwise-safe redeploy from
   existing truth. Consider letting build+deploy proceed when only
   ingest/extract failed.
4. **Digest gate vs cron drift**: only the 13:00 UTC run reaches the
   05-08 Edmonton window; GitHub cron commonly fires late, and a slip
   skips the daily digest (softened by `daily_lookback_days: 7`, so it
   self-heals next day). Widening the window or gating on "no digest yet
   today" would remove the miss entirely.
5. **Shallow clone + data-worktree rebase** (`pipeline.yml:59`,
   `scripts/ensure-data-branch.sh`, `commit.py:69`) is the most brittle
   mechanism in the design; fetch the data ref with explicit history so
   `pull --rebase` cannot hit a grafted-history edge.
6. **GitLab CI `eval "$(make digest-gate)"`** (`.gitlab-ci.yml:60`): safe
   today because `format_gate` emits only literals, but source a written
   key=value file instead to kill the latent eval-injection pattern.

## 5. Architecture findings

- **Repository swappability is narrower than advertised.** No SQLite types
  leak into signatures (the stated rule holds), but the ABC encodes the v1
  lifecycle: `rebuild_cache()`, `count_*()`, `load_config()`
  (`repository/base.py:120-136`) are meaningless for a Postgres backend
  where the DB is truth. And the entire read path (`site/trends.py`) talks
  `sqlite3` directly by documented design - so "Postgres-swappable" covers
  the pipeline write path only. Before v2: split a narrow read/write
  `Repository` from a `CacheProjector`, and decide whether the query path
  gets a backend-neutral interface or stays explicitly v1-only. No action
  needed now beyond stating it plainly in architecture.md.
- **digest and site packages import each other**, patched with a deferred
  import at `site/trends.py:312`. `Window` / `previous_window` /
  `is_rising` are shared primitives in the wrong layer; hoist them to a
  neutral module and the cycle disappears.
- **Dead or vestigial code**: `digest/pipeline.py period_for()` (superseded
  by `periods_for()`), `KeywordAlias` model + `keyword_aliases` table
  (reserved for v2, never read or written), `SourceKind.X` (see 3.1).
  Remove or annotate as reserved.
- **The regex HTML stripper** (`ingest/normalize.py:178-195`) is the most
  complex code in the package with honestly-documented failure modes a real
  tolerant parser (selectolax or lxml) would not have. Replacing it removes
  about five documented edge-case bugs and simplifies the module; verify
  idempotency for the `renormalize` fixed point.

## 6. Docs and tracker hygiene

- **Close the 10 merged-but-open issues** (#29-#33, #37, #38, #39, #47,
  #50) and open issues for the real remainder (GRP-61/62/63/64, E7, F1,
  and O1 if it never ran). Right now the tracker misleads anyone asking
  "what is left".
- **Propagate the X retirement**: README ("RSS / YouTube / Reddit / X"),
  docs/setup.md (twscrape secrets row), docs/architecture.md (x via
  twscrape in the diagram), PRD §10.2 (twscrape fixtures),
  docs/design/ui-redesign-brief.md §1. PRD edits go through the
  propose-a-diff rule.
- **README data layout is wrong**: says truth is committed under `data/`;
  the data branch keeps `logs/ items/ digests/ runs/` at the repo root
  (HANDOFF.md gotcha).
- **Write docs/runbook.md (GRP-61)**: the content already exists scattered
  across feed-triage.md, prev1-hardening.md, and HANDOFF.md; it is mostly
  consolidation, and it is the doc a phone-only operator needs most.
- **Missing from §10**: the nightly live smoke test (three canary sources +
  one real LLM call) was never built; either build it or strike it from
  the PRD via a proposed diff.

## 7. Product-level assessment

The concept is coherent and the constraints (single user, mobile-first,
zero-server, under 1 EUR/mo) are strengths. Weak spot: with batch cadence,
category-level digests, and a read-only surface, the app competes with a
plain RSS reader; the analysis layer is what a reader cannot do, so lead
with it. Ideas that compound without new infra or meaningful LLM spend:

1. **"Since you last opened" delta.** One localStorage timestamp (the
   your-digest pattern already exists) turns the fixed 24h digest into a
   personal changelog - the highest-leverage cheap win.
2. **Surface rising first.** `is_rising` exists but hides as a cloud badge;
   a "Rising this week" strip at the top of Home puts the differentiator in
   the first thumb-scroll (the UI brief already flags the cloud pushing
   digests below the fold).
3. **Make source rot visible as coverage, not errors.** Feeds keep dying
   (about 13 disabled so far) and Reddit is largely blocked from CI IPs;
   the digest narrows silently. A "sources you are no longer hearing from"
   surface framed as coverage keeps the input honest.
4. **Co-occurrence neighborhood graphic** on keyword pages - the data is
   already computed; a small inline SVG (same byte-stable discipline as
   sparklines) shows trend structure a list cannot.
5. **Treat the cloneable-template angle as product surface.** Template
   polish (one-command bootstrap, sample data branch) is near-free and is
   the only growth path that does not touch the v2 cost problem.
6. **data-eng category is thin** - one barely-populated topic undermines
   the "follow the topics you want" feature; either seed it properly or
   fold it until it earns a tab.

## 8. Recommended order of work

1. Release hygiene: confirm or run O1, tag v1.0.0, close the 10 stale
   issues, open issues for the real remainder. (Session-sized, no code.)
2. Fix 3.1 (x-kind validation gap) and 3.3 (pin no-op) - small, contract
   honesty.
3. CI security pass: dependabot + pip-audit, ruff `S`. (Small.)
4. Fix 3.2 (ingest rescan) before history grows further.
5. Docs sweep: X retirement propagation, README layout fix, runbook
   (GRP-61), PRD diffs proposed for the smoke test and §10.2.
6. Product wins in order: rising strip, since-last-visit delta, coverage
   surface.
7. Defer: HTML-parser swap, digest/site cycle hoist, deploy decoupling,
   GitLab cutover (already deferred by Kyle), Slack E7.

Items 2-6 are each one issue; the wayfinder skill is overkill for them.
Wayfinder fits if v2 (remote-labs, Postgres, multi-user) starts moving -
that is the fog-of-war-sized effort it was written for.
