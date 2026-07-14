# Operations runbook (GRP-61)

Symptom-first operations guide for grepify. Start from **what you see**, follow
the diagnosis steps, apply the fix. Written to be worked from a phone: the four
surfaces you operate through are the **Actions run summaries**, the **health
page** (`/health/` on the deployed site), the **GitHub mobile app** (issues,
PRs, file edits, `workflow_dispatch`), and a **Claude Code session** (paste the
session prompts under each fix).

This runbook consolidates and links, it does not duplicate. The per-error-class
feed deep dive stays in [`docs/feed-triage.md`](feed-triage.md); the source
lifecycle direction is [`docs/adr/0002-source-acquisition-ladder.md`](adr/0002-source-acquisition-ladder.md);
the O1 remediation procedure is [`docs/prev1-hardening.md`](prev1-hardening.md).

## Environment facts you must know first

These five facts explain most confusing symptoms. Read them before triaging.

- **Data branch truth lives at the repo ROOT of the `data` branch**, not under
  `data/`. On the `data` branch, `logs/`, `items/`, `keywords/`, `digests/`,
  `runs/`, and `health.json` sit at the top level. (The README/layout still
  says `data/` until GRP-60 lands; trust this.) The pipeline checks that branch
  out as a worktree at `./data` in CI, so inside CI the same files appear under
  `./data/...`.
- **CI egress to feed hosts is BLOCKED.** PR-triggered `validate` and the
  build environment cannot reach feed hosts at all, even healthy ones. Only the
  scheduled `pipeline` workflow (`schedule` / `workflow_dispatch`) runs where
  fetches can happen. So you can never "test a feed URL" from CI or from a PR;
  triage works from `fetch_log` evidence only. Do not add live feed-pinging.
- **The doctor report is emitted only to the Actions job summary.** The
  `Feed triage report (doctor)` step writes `make doctor` into
  `$GITHUB_STEP_SUMMARY`. It is not committed and cannot be fetched after the
  fact by tooling. To verify after a run, read the `data` branch fetch log
  directly (recipe below).
- **Auto-merge is OFF.** Kyle merges every PR manually. A green PR does not land
  itself.
- **The pipeline token has limits.** The default `GITHUB_TOKEN` cannot delete
  remote branches (403) and cannot push tags. Cleanup and tagging are manual.

## Which surface shows what

- **Actions run summary** (GitHub app -> Actions -> the run): per-step
  pass/fail, and the embedded **Feed triage (doctor)** table. This is where a
  red run and per-source fetch status both show up.
- **Health page** (`/health/`): next scheduled digest, last digest per
  category, and the source-health table (Last status, Error class, attempts,
  consecutive failures). A flagged source (>= 5 consecutive non-Reddit
  failures) renders its row red with a ⚑.
- **The deployed site**: staleness (old dates, missing today's digest) is the
  reader-visible symptom of a pipeline that stopped committing or deploying.
- **`grepify health`** (CLI/session): prints the latest run manifest.
- **`grepify doctor`** (CLI/session): the full per-source triage table.

## Symptom index

| What you see | Go to |
|---|---|
| Pipeline run is red (a step failed) | [Red pipeline run](#red-pipeline-run) |
| A source row is red / flagged on the health page | [Source fetch failures](#source-fetch-failures-by-error-class) |
| An Error-column code (`http_4xx`, `tls`, ...) on a source | [Source fetch failures](#source-fetch-failures-by-error-class) |
| Keyword extraction all fell back / digest prose is templated | [LLM budget or provider](#llm-budget-exhaustion-or-provider-down) |
| Today's digest is missing / no new digest on the site | [Digest skipped or missed](#digest-skipped-or-missed) |
| Build step failed / site did not regenerate | [Build failure](#build-failure) |
| `Commit pipeline data` step failed | [Data-branch commit conflicts](#data-branch-commit-conflicts) |
| Site did not update though the run was green | [Pages deploy failure](#pages-deploy-failure) |
| Site is stale and there are NO recent pipeline runs at all | [Pages deploy failure](#pages-deploy-failure) (cron auto-disabled) |
| Site shows old HTML-junk keywords (`div`, `span`) | [O1: HTML-contaminated keywords](#o1-html-contaminated-keywords) |

## Error taxonomy: what stops the run vs what degrades

From `grepify/errors.py`. This tells you whether a red run is a real stop or
just noise in the logs.

**Stops the run (systemic faults, fix required):**

- `ConfigError` - config could not be loaded/parsed (missing dir, bad YAML,
  schema violation). Surfaced by `validate`. Fix the config.
- `RepositoryError` - storage could not satisfy a request (unreadable JSONL
  truth, cache rebuild failure). Fix the data/state.
- `DataQualityError` - a post-extract data-quality assertion (PRD §10.7) was
  violated. Fails loudly on purpose - it signals a bug in
  extraction/normalization, not an unreachable dependency. Do not paper over it.
- `CommitError` - the data commit lost the push race past its retry budget.
  See [Data-branch commit conflicts](#data-branch-commit-conflicts).

**Degrades, does NOT stop the run (isolated, expected):**

- `FetchError` - a single source failed (timeout, HTTP error, malformed feed,
  auth challenge, rate limit). The ingest orchestrator logs an `error`
  `fetch_log` row and continues; one dead feed never fails the run (PRD §9).
- `LlmError` - an LLM call could not be completed. The extract batcher degrades
  that batch to the deterministic fallback extractor.
- `BudgetExceededError` (subclass of `LlmError`) - the per-run budget circuit
  breaker refused a call before any network I/O. Stops further LLM calls for the
  run; the run itself continues on fallback.

So: a run full of `FetchError`/`LlmError` log lines is **not** a red run. A red
run means a `ConfigError`, `RepositoryError`, `DataQualityError`, `CommitError`,
or an infrastructure step (checkout, install, deploy) actually failed.

## Common recipe: read the data branch from a session

Most diagnosis below needs the `data` branch truth. From a Claude Code session
(egress to GitHub is fine; egress to feed hosts is not):

```
git fetch origin data
git worktree add --detach ./_data origin/data
# truth is at the ROOT of that worktree:
#   ./_data/health.json, ./_data/runs/, ./_data/items/,
#   ./_data/keywords/, ./_data/logs/fetch/YYYY/MM/DD.jsonl, ./_data/digests/
git worktree remove ./_data
```

To run doctor against it locally (doctor makes no network/LLM calls):

```
uv run grepify --config-root sources --data-root ./_data doctor
```

---

## Red pipeline run

**What you see:** Actions -> the `pipeline` run has a red X on one step.

**GRP-64: a red `Ingest`/`Extract`/`Daily digest`/`Weekly digest` no longer
skips Build/Deploy.** Those four steps run with `continue-on-error: true` -
`Commit pipeline data` is skipped for that run (no partial truth committed),
the `./data` worktree is rolled back to its last committed state, and `Build
site`/`Deploy to Pages` still run from that truth, so the site redeploys. A
`Check upstream pipeline health` step then fails the job on purpose so the run
still shows red and `Notify on failure` still fires - the deploy succeeding
does not hide the underlying failure. `.gitlab-ci.yml` mirrors this with
`allow_failure: exit_codes: 64` on the single `pages` job (GitLab's classic
Pages deploy is tied to that job's own success, so there is no separate
deploy step to keep independent the way GitHub Actions has one).

**Diagnosis (phone):**
1. Open the run in the GitHub app. Identify **which step** is red - the fix
   differs completely by step:
   - `Ingest` red -> almost never; ingest tolerates per-source failures. A red
     ingest is a `ConfigError`/`RepositoryError`, not a dead feed. Build/Deploy
     still ran (see above); fix the root cause on its own timeline.
   - `Extract` red -> a `DataQualityError` (assertion) or a config/storage
     fault, not an LLM outage (LLM outages degrade, see below). Build/Deploy
     still ran.
   - `Daily/Weekly digest` red -> config/storage; LLM problems degrade.
     Build/Deploy still ran.
   - `Check upstream pipeline health` red -> not a bug by itself; it is the
     step that turns one of the three rows above into a red run. Go fix
     whichever of `Ingest`/`Extract`/`Daily digest`/`Weekly digest` actually
     failed.
   - `Commit pipeline data` red -> [commit conflicts](#data-branch-commit-conflicts).
   - `Build site` red -> [build failure](#build-failure); this one still
     blocks `Deploy to Pages`.
   - `Deploy to Pages` red -> [Pages deploy](#pages-deploy-failure).
   - `Install` / `Checkout` red -> transient infra; re-run first.
2. Expand the step log and read the exception type. Match it to the taxonomy
   above to know whether it is systemic (fix) or a mislabeled degrade.

**Fix:**
- Transient infra (install/checkout/network blip): re-run the job. GitHub app
  -> the run -> Re-run failed jobs.
- Systemic (`ConfigError`/`RepositoryError`/`DataQualityError`): reproduce and
  fix in a session. Paste:

  > Read docs/runbook.md and grepify/errors.py. The `pipeline` run <RUN URL>
  > failed at the `<STEP>` step with `<EXCEPTION + MESSAGE>`. Reproduce it
  > locally against the data branch (worktree recipe in the runbook), find the
  > root cause, and open a fix PR on a feature branch. `validate` cannot reach
  > feed hosts, so keep the repro offline.

---

## Source fetch failures by error class

**What you see:** a red/flagged row on `/health/`, or an Error-column code in
the health table or the doctor summary: `http_4xx` / `http_5xx` / `tls` /
`connection` / `unparseable` / `other`. A source flags at **>= 5 consecutive
non-Reddit failures**.

This is the single richest failure area; the deep dive with per-class root
causes and the #45 case study is [`docs/feed-triage.md`](feed-triage.md). The
lifecycle direction (active / degraded / paywalled / gone / dead, and the
acquisition ladder) is [ADR 0002](adr/0002-source-acquisition-ladder.md). This
section is the phone-operable summary.

**Diagnosis (phone):**
1. On the Actions run summary, read the **Feed triage (doctor)** table: per
   source `status`, `error_class`, `streak`, `last_error`. The one-line header
   ("N sources, X last-run error, Y flagged") is the at-a-glance tally.
2. Or open `/health/` and read the source-health table; tap a red row's Error
   cell for the full `last_error`.
3. Classify by `error_class` and `streak`:
   - **Reddit (`http_4xx` 429):** quiet by design (best-effort, `cadence.py`).
     Never flags. Leave it alone.
   - **Low streak, succeeds most runs (intermittent flapper, e.g. HTTP 415):**
     leave enabled and watch. Disabling throws away a working source.
   - **Persistent streak >= 5, non-Reddit:** real breakage. Continue.
4. Verify against the fetch log (never live-ping the feed - egress is blocked).
   In a session, use the worktree recipe, then:

   ```
   grep <source-id> ./_data/logs/fetch/2026/07/13.jsonl
   ```

**Fix (by class, from feed-triage.md):**
- **`http_4xx` 403 or `unparseable` (WAF / HTML challenge):** the shipped
  transport already sends a browser User-Agent + feed `Accept` header
  (`grepify/ingest/rss.py`, `grepify/ingest/http.py`). If it still fails, the
  host is server-side WAF/IP blocking us; a UA is not enough. Disable with an
  evidence note, or (per ADR 0002) classify `dead` and consider the opt-in
  recovery rungs. Do not treat a WAF 403 as a paywall.
- **`tls` sslv3 handshake failure:** the transport already runs at
  `DEFAULT@SECLEVEL=1` with cert verification on. If it still fails, the
  server's legacy TLS is incompatible even at seclevel 1; classify `dead`,
  recheck later.
- **`http_4xx` 404/410 on both feed and site root, or DNS NXDOMAIN:** the
  target is `gone`. Per ADR 0002, `gone` sources are **removed** from the group
  file (reasoning in the commit message), not left disabled forever.
- **Recoverable URL (moved feed, wrong path):** fix the `url` in
  `sources/groups/*.yml`.

Config edits are single-line YAML changes you can make in the GitHub app. To
disable a dead source, set `enabled: false` with an inline evidence note (error
class + streak + date + run id), exactly as prior sweeps did. Open a PR; Kyle
merges (auto-merge is off).

**Session prompt for a full sweep:**

> Read docs/runbook.md, docs/feed-triage.md, and docs/adr/0002. Do a source
> triage sweep: fetch the data branch, run doctor against it, and for every
> non-Reddit source flagged (streak >= 5) classify it by error class and
> propose the config edit (fix URL, disable with evidence note, or remove if
> gone) on a feature branch. Do not live-ping feeds. Keep feed-triage.md as the
> deep dive and add a closeout note there.

---

## LLM budget exhaustion or provider down

**What you see:** extraction rows all show `method=fallback` (deterministic YAKE
instead of the LLM), or digest prose looks templated rather than written. The
run is still **green** - this is a degrade, not a failure (PRD §9/§13).

**Diagnosis (phone / session):**
1. This is expected behavior, not a broken run. Confirm which happened:
   - **Budget exhausted:** `BudgetExceededError` - the per-run circuit breaker
     (`max_calls_per_run`, PRD §5) refused calls after the cap. Look for it in
     the Extract step log.
   - **Provider down / transport error:** `LlmError` - the batcher degraded that
     batch to fallback.
2. In a session, inspect the LLM log on the data branch
   (`./_data/logs/llm/...`) to see call counts and where it stopped.

**Fix:**
- **Transient provider outage:** no action; the next scheduled run retries
  automatically and re-extracts fresh items through the LLM. If you want the
  affected items reprocessed sooner, run `make backfill` (re-extracts
  `method=fallback` rows through the real LLM) via a `workflow_dispatch`.
- **Budget too low for the corpus:** if fallback is happening every run because
  the cap is too small, that is a config decision (`max_calls_per_run` in
  `sources/settings.yml`). Propose the change in a PR; do not raise it silently -
  it affects cost.
- **Provider config wrong (base URL / key):** the secrets (`LLM_BASE_URL`,
  `LLM_API_KEY`) are only consumed by the `pipeline` workflow, never by
  `validate`/PR paths (public repo). Check the repo Actions secrets in the
  GitHub app -> Settings. Never echo or log them.

---

## Digest skipped or missed

**What you see:** today's daily digest (or Monday's weekly) is missing on
`/health/` "Last digest per category", or the site shows an old digest date.

**Diagnosis (phone):**
1. First check whether the digest was even **due**. Digests are gated
   (`grepify/digest/gating.py`, GRP-63): a run fires once local time is at or
   past the **05:00 America/Edmonton** morning opening *and* the period's
   digest does not exist yet - there is no closing hour, so any later run that
   day (13:00/19:00 MDT etc.) is a natural retry, not a skip, if the morning
   run missed the opening or never landed. Weekly follows the same rule, gated
   to Monday. On `/health/`, the "Next scheduled digest" line tells you when
   the next one is due (and shows today's already-past opening if today's is
   still missing).
2. If it was due, open that morning run in the Actions app and look at the
   `Daily digest` / `Weekly digest` steps:
   - **Step was skipped (grey):** the gate did not fire for that run. Check the
     `Digest gate` step output (`daily=` / `weekly=`).
   - **Step ran but produced nothing:** a category may be below the item
     threshold (recorded as skipped, not failed - expected on a thin day), or
     the digest for that (category, period) already exists and was idempotently
     skipped with no LLM call.
   - **Step ran but the file was not committed:** see
     [commit conflicts](#data-branch-commit-conflicts) - generation succeeded
     but persistence did not.

**Fix:**
- **Was not due yet:** no action. Wait for the morning opening, or force it.
- **A cron run missed the morning opening or slipped late:** no action needed -
  the next cron run that day retries automatically (GRP-63), and the daily
  digest's own catch-up window backfills any recent day still missing.
- **Force a digest on demand:** GitHub app -> Actions -> `pipeline` -> Run
  workflow -> set `force_digest: true`. This runs both daily and weekly
  regardless of the gate (idempotent: existing digests are skipped, so it only
  fills gaps).
- **Genuinely broken generation/persistence:** paste:

  > Read docs/runbook.md and grepify/digest/pipeline.py. The daily digest for
  > <DATE> is missing on the site though the gate fired (run <URL>). Fetch the
  > data branch and check: was the digest generated (LLM log), does it exist in
  > digests/ truth, and did commit-data include it? Find where the chain broke
  > and open a fix PR with a regression test.

---

## Build failure

**What you see:** the `Build site` step is red, or the site did not regenerate
though earlier steps were green.

**Diagnosis (phone / session):**
1. Read the `Build site` step log. `grepify build` renders JSONL truth (from the
   `./data` worktree) into `public/`. A build failure is a `RepositoryError`
   (unreadable/malformed truth or a cache rebuild failure) or a template/render
   bug, never a network problem (build makes no fetches).
2. Reproduce locally in a session against the data branch:

   ```
   git fetch origin data
   git worktree add --detach ./_data origin/data
   uv run grepify --config-root sources --data-root ./_data build
   git worktree remove ./_data
   ```

   The same exception the CI step hit reproduces here (build is deterministic
   and offline).

**Fix:**
- **Malformed/partial JSONL truth on the data branch** (e.g. a half-written
  commit): identify the bad row from the traceback and correct the truth via a
  data-branch commit; `make check` cannot catch data-branch corruption because
  the data lives on another branch.
- **Template / render bug:** fix in `grepify/site/`, add a snapshot test, PR it.
- Note `GREPIFY_BASE_PATH` sets the Pages sub-path (`/<repo>/`); a wrong base
  path shows as broken links/assets on the deployed site, not a red build.

**Session prompt:**

> Read docs/runbook.md. `grepify build` failed at the Build site step of run
> <URL> with `<EXCEPTION>`. Reproduce it offline against the data branch
> (worktree recipe in the runbook), fix the root cause (data correction or a
> render fix with a snapshot test) on a feature branch, and confirm the build
> is green against the data worktree.

---

## Data-branch commit conflicts

**What you see:** the `Commit pipeline data` step is red with a `CommitError`,
or "push ... failed after N attempts".

**Background:** `main` requires PRs, so the pipeline commits JSONL truth to a
dedicated `data` branch (worktree at `./data`), not `main`. `commit_data`
(`grepify/repository/commit.py`) stages truth, commits with a `[skip ci]`
message (so the write cannot re-trigger the cron), and pushes with **rebase-retry
up to 5 attempts**. Concurrent runs are also serialized by the Actions
`concurrency: pipeline` group. A `CommitError` means the push kept losing the
race past the retry budget, or a non-race git failure (auth, corrupt state)
occurred.

**Diagnosis (phone / session):**
1. Read the step log. Distinguish:
   - **Lost the race repeatedly** ("push failed after 5 attempts"): rare, since
     the concurrency group serializes runs. Usually means two runs raced despite
     it, or the remote moved fast.
   - **Non-race git failure** (auth 403, rebase conflict, corrupt worktree):
     the underlying `git` error is in the log.
2. Check the `data` branch state in a session (worktree recipe) - is the last
   commit present, is the branch healthy?

**Fix:**
- **Transient race:** re-run the job (GitHub app -> Re-run failed jobs). The
  next run rebases onto the current `data` head and commits cleanly. Data is not
  lost - truth is append-only JSONL and the next ingest re-derives cleanly.
- **Rebase conflict on the data branch** (should be rare with append-only
  JSONL): resolve in a session against the `data` branch and push the fix. The
  token can push to `data`; remember it **cannot delete branches or push tags**,
  so do not script those.
- If the branch is wedged, paste:

  > Read docs/runbook.md and grepify/repository/commit.py. The `Commit pipeline
  > data` step of run <URL> failed with `<ERROR>`. Fetch the data branch,
  > diagnose whether it is a lost push race or a real git conflict, and get the
  > data branch back to a clean pushable state. Remember: truth is at the repo
  > root of the data branch, and the token cannot delete branches or push tags.

---

## Pages deploy failure

**What you see:** the run is green through `Build site` but `Deploy to Pages`
is red, or the run is fully green yet the live site did not change.

**Not this symptom (GRP-64):** a run that is red only at `Check upstream
pipeline health` (with `Build site`/`Deploy to Pages` green above it) is
expected - the site deployed from existing committed truth while an
ingest/extract/digest step failed. See [Red pipeline run](#red-pipeline-run).

**Diagnosis (phone):**
1. Open the `Configure Pages` / `Upload Pages artifact` / `Deploy to Pages`
   steps. These are GitHub-managed actions (`configure-pages@v5`,
   `upload-pages-artifact@v3`, `deploy-pages@v4`). A failure here is almost
   always infrastructure or a repo Pages setting, not grepify code.
2. Check repo Settings -> Pages in the GitHub app: source must be **GitHub
   Actions** (not a branch). The workflow needs `pages: write` and
   `id-token: write` permissions (already set) and the `github-pages`
   environment.
3. If deploy was green but the site is stale, the artifact built empty or the
   build ran on stale data - check the `Build site` step actually produced
   `public/` content and that `Commit pipeline data` committed this run's data
   before the build read it.

**Fix:**
- **Transient deploy failure:** re-run the job. Pages deploys are commonly
  flaky; a re-run usually succeeds.
- **Pages source misconfigured:** set Settings -> Pages -> Source to "GitHub
  Actions". This is a one-time repo setting.
- **Green run, stale site:** confirm the build read the just-committed data.
  Order in the workflow is commit-data -> build -> deploy, so a stale site with
  a green run points at build reading the wrong data root or an empty artifact;
  reproduce the build offline (see [Build failure](#build-failure)).
- **Stale site and NO recent runs at all (silent death):** GitHub auto-disables
  cron workflows after about 60 days without repo activity, and a disabled cron
  produces no failing runs, so the pipeline's failure-notification issue never
  fires. Check the Actions tab for a "scheduled workflows disabled" banner on
  the pipeline workflow and re-enable with one tap. This is the one failure
  mode no in-repo notification can catch; if it ever bites, a free external
  uptime monitor pinging the health page closes it for good.

---

## O1: HTML-contaminated keywords

**What you see:** the site's trend/cloud/keyword views surface junk keywords
like `div`, `div class`, `span` (HTML fragments, not real terms).

**Background:** these are YAKE-fallback rows extracted from items ingested
**before** the #19 HTML-strip fix. The normalizer is correct today; ingest is
idempotent and extraction is cached, so old dirty summaries were never
rewritten. The fix is a one-time data remediation (O1), not a code change. Full
procedure: [`docs/prev1-hardening.md`](prev1-hardening.md).

**Status:** O1 already ran and verified clean (2026-07-11, run
`20260711T024728Z-e1938d`; see HANDOFF for the evidence). If dirty keywords
reappear, it means new contamination, which would be a real bug in the
normalizer - investigate that, do not just re-run O1.

**Fix (if a fresh remediation is genuinely needed):**
1. GitHub app -> Actions -> `pipeline` -> Run workflow -> set
   `run_remediation: true`. This runs `make maintain-renormalize` against the
   data branch before ingest: re-cleans stored summaries and re-extracts the
   changed items. It is idempotent - a clean corpus rewrites nothing.
2. Verify zero HTML keywords remain by scanning the keyword JSONL on the data
   branch (worktree recipe), grepping for `div`, `span`, and markup characters
   (`<`, `>`, `=`).

**Session prompt:**

> Read docs/prev1-hardening.md and docs/runbook.md. Dirty HTML keywords (`div`,
> `span`) are showing on the site again. Fetch the data branch and check: are
> these old pre-#19 rows that O1 missed, or new rows created after O1's run
> `20260711T024728Z-e1938d`? If new, find the normalizer regression and fix it.
> If old, propose the remediation run.

---

## Escalation and scope notes

- **Never commit to `main`.** Every fix is a feature branch + PR; Kyle merges
  (auto-merge is off).
- **`docs/prd.md` is the source of truth.** If a fix implies a scope, schema, or
  architecture change, propose the diff in the PR - do not change it silently.
- **Secrets stay out of PR-triggered paths.** Only the `pipeline` workflow
  touches `LLM_API_KEY` / `LLM_BASE_URL`; never reference a secret from
  `validate` or any `pull_request`-triggered step, and never log a credential.
</content>
</invoke>
