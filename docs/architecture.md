# Architecture

Condensed from PRD §5-§7 (the source of truth - read it for rationale). This is
the onboarding reference; when the two disagree, the PRD wins.

## Shape

```
sources/*.yml → ingest → normalize+dedup → JSONL truth → rebuild → SQLite cache
                  │                                          │
                  │ (yt transcripts, ai-voices rss)          ├─ extract (LLM keywords, batched, cached)
                  │                                          ├─ trends (pure SQL/python, deterministic)
                  │                                          ├─ digest (daily/weekly, LLM, per category)
                  └─ fetch_log / health                      └─ build (Jinja SSG → public/) → Pages
```

Run 2-4×/day on CI cron. All steps are `make` targets that also run locally; CI
is just a scheduler.

## Storage: JSONL truth + SQLite cache

- **Truth** = append-only JSONL committed to a dedicated **`data` branch**
  (not `main`, which requires PRs - GRP-06):
  `data/items/YYYY/MM/DD.jsonl`, `data/keywords/YYYY/MM/DD.jsonl`,
  `data/digests/*.json`, plus `data/logs/{fetch,llm}/…`. Readable diffs, no binary
  blobs in history, retroactive reprocessing = rerun over files. This IS the v2
  migration (`COPY FROM` into Postgres).
- **Cache** = `data/grepify.db`, rebuilt from JSONL each run (seconds at this
  scale), **never committed**. All trend/timeline/related-keyword queries hit it.
- **Idempotency**: writing the same record twice is a no-op (dedup on primary
  key). Rebuild is deterministic: same JSONL → same DB.

## Interfaces (v2-proofing)

All storage behind one **`Repository`** interface (v1 = JSONL+SQLite; v2 =
Postgres). All config behind one **`ConfigProvider`** interface (v1 =
filesystem-YAML; v2 = DB-backed + checkbox UI). Signatures use domain models and
builtins only - **no SQLite-specific types leak**, so the ingest/extract/digest
pipeline's write path never knows which backend is active.

This swappability covers the **write path only**. `site/trends.py` reads the
rebuilt cache with `sqlite3` directly, by deliberate v1 design (PRD §5's static
SSG is thrown away wholesale at the v2 boundary, PRD §15) - it is not behind
`Repository` and is not part of the v2-proofing guarantee above.

## LLM

Named profiles selected by config (`llm.active_profile`); each profile carries a
**hard budget gate** (`max_calls_per_run`) - bounded retries, circuit breaker, no
unbounded loops (CSR incident rule). Model + prompt version recorded per row.
If the LLM is unavailable or over budget → deterministic **YAKE fallback**, rows
flagged `method='fallback'` for later re-extraction; the site still builds.

## Frontend

Jinja SSG (locked). Dark, mobile-first. Sparklines = inline SVG generated in
Python at build. Build is a pure function of DB + config → byte-stable output
(except timestamps) for snapshot tests. No LLM in the serving path.

## Config layout (PRD §7)

```
sources/
  groups/*.yml   # curated category bundles; each group carries a `category`
  keywords.yml   # aliases / mutes / pins
  settings.yml   # cadence, windows, llm profiles, budgets
```

Digests are generated **per category**, never per user - the one feature whose
cost would scale with users, so it stays category-level even in v2.

## Data model

Logical schema in PRD §6 (the column set is the contract either backend
implements). Trends are **not** materialized - cloud/timeline/related-keyword are
computed at build time by SQL over `item_keywords ⋈ items` for the window.

## Timezone

Digest day boundary and cron gating are pinned to **America/Edmonton**, not UTC.
