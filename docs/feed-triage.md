# Feed triage runbook (#39)

How grepify captures per-source fetch errors and how we resolve them case by
case. Follow-up to T5 (#30, `grepify doctor` + disabled dead feeds) and T6
(#31, Reddit best-effort/quiet).

## Where errors show up

- **Health page** (`/health/`): the source-health table has an **Error** column
  showing each source's `error_class` (`http_4xx` / `http_5xx` / `tls` /
  `connection` / `unparseable` / `other`, or `-` when it last succeeded). Tap or
  long-press an Error cell to read the full `last_error` message in the tooltip.
  A flagged source (>=5 consecutive errors, non-Reddit) turns its row red.
- **`grepify doctor`** report: a flat, deterministic table joining config
  (`enabled`) with fetch-log truth (`status`, `error_class`, `streak`,
  `last_error`). Leads with a `N sources, X last-run error, Y flagged` summary.
  Run it locally against the data branch:

  ```
  git fetch origin data
  git worktree add --detach ./_data origin/data
  uv run grepify --config-root sources --data-root ./_data doctor
  git worktree remove ./_data
  ```

- **Scheduled pipeline**: the `Feed triage report (doctor)` step in
  `.github/workflows/pipeline.yml` runs `make doctor` after ingest every run and
  appends the report to the Actions job summary. This is the automatic capture
  going forward - new breakage shows up in the run summary without anyone
  re-running doctor by hand. The step is read-only, makes no network/LLM calls,
  and references no secret.

## The repeatable process

1. Read the doctor report (or the pipeline job summary). Look at `status`,
   `error_class`, and `streak` per source.
2. For a **non-Reddit** source with a real persistent streak (flagged,
   streak >=5):
   - If the URL is recoverable, fix it in `sources/groups/*.yml`.
   - If it is dead, set `enabled: false` on that source with an inline
     evidence-note comment (error class + streak + date), exactly as T5 did.
3. **Reddit** sources are quiet by design (T6): they never flag and stay
   enabled as best-effort. Leave them alone.
4. **Intermittent** flappers (low streak, succeed on most fetches) stay
   enabled - disabling them would discard a working source. Note them here
   instead.
5. Live-verification of feed URLs is **not possible** in the build/CI
   environment (outbound feed hosts are blocked, even healthy ones), so triage
   works from `fetch_log` evidence only. Do not add live feed-pinging.

## Current resolution (evidence, as of #39)

Doctor over the data branch: 128 sources.

- **10 persistently-dead non-Reddit feeds** (streak 16; classes `http_4xx` /
  `tls` / `unparseable`): already **disabled** in `sources/groups/*.yml` with
  inline T5 evidence-note comments. No further action.
- **3 intermittent HTTP 415 flappers** - `artificial-lawyer`, `bdan-ai`,
  `la-biblia-de-la-ia`: **stay enabled**. Their 4-day `fetch_log` shows they
  succeed on most fetches each day and return 415 (or occasionally a timeout)
  only intermittently - a server-side WAF/rate hiccup, not a dead URL. Doctor
  streak is 1 and they are correctly not flagged (threshold is >=5). Disabling
  them would throw away working sources, so we keep them and watch.
- **`ai-time-journal`**: known **empty-feed watch item**, not an error. It
  fetches OK (HTTP 200) but returns 0 items every run (doctor status `empty`,
  4 days straight). This is out of the error-streak scope; it is not disabled.
  Revisit if it stays empty long-term or starts erroring.
- **Reddit** (~26 sources, streak ~17, `http_4xx` 429): quiet by design (T6).
  Not fixed, not disabled.

## Current resolution (evidence, as of #45)

#45 works the remaining non-Reddit source-fetch errors one error class per PR.

**Class 1 - `http_4xx` 403 + `unparseable` (this PR).**

- **Root cause.** The RSS fetcher sent a bot User-Agent
  (`grepify-ingest/0.1 ...`) and no `Accept` header. Cloudflare / Substack WAFs
  answer that with an HTTP 403, or serve an HTML challenge page - which is not
  feed XML, so feed parsing fails and the source surfaces as `unparseable`. Same
  root cause, two error classes.
- **Fix.** `grepify/ingest/rss.py` now sends a realistic browser User-Agent plus
  a feed `Accept` header (`application/rss+xml` / `application/atom+xml` / xml)
  on every request, so WAF-fronted hosts return the feed XML. Conditional-GET
  headers are unchanged.
- **Re-enabled to retry (7).** 403: `aimodels`, `copyleaks-blog`, `ai-techpark`,
  `benn-substack`. unparseable: `aim-ai`, `shaip-blog`,
  `theodo-data-and-ai-blog`. Each carries an inline `#45` evidence note in
  `sources/groups/*.yml`.
- **Kept disabled.** `clarifai-blog` was an HTTP 404 (moved/dead URL), not a WAF
  block, so a fetch-header change cannot recover it - it needs a corrected URL.
- **Verification is pending.** Egress to feed hosts is blocked in CI/build, so
  this fix cannot be verified live in the PR's validate run. It lands blind and
  is verified on the next scheduled pipeline run's doctor job summary: the 7
  re-enabled sources should fetch/parse instead of 403/unparseable.

**Remaining classes (follow-up PRs).** Class 2 - `http_4xx` 415 flappers
(`artificial-lawyer`, `bdan-ai`, `la-biblia-de-la-ia`); the class 1 Accept
header may already stop the intermittent 415s, so re-check on the next doctor
summary first. Class 3 - `tls` handshake failures (`inside-ai-news`,
`knowtechie-ai`).
</content>
</invoke>
