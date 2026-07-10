# Pre-v1 hardening work order

The fixes to land **before any new features and before tagging v1.0.0**, in
priority order, plus the execution + handoff protocol a single agent session
(Opus) runs from. Grounded in a diagnosis of the live `data` branch on
2026-07-10 (run `20260710T180418Z`).

## 0. Priority + the digest gate (Kyle's call)

**Fix the HTML contamination before generating any more digests.** Dirty
keywords (from data ingested before the #19 HTML-strip fix) are feeding digests;
every new digest inherits the noise. So Phase A below runs first, and the digest
generation is *paused* until the re-extract is verified clean.

Root-cause note (so a resuming session does not re-litigate it): the normalizer
is **correct today**. Running the current `_strip_html` over every dirty stored
summary on the data branch leaves zero markup. The `div` / `div class` / `span`
keywords are all YAKE-fallback rows extracted from items ingested *before* #19;
ingest is idempotent and extraction is cached, so those old summaries and their
keyword rows were never rewritten. The fix is a one-time data remediation, not a
normalizer rewrite.

## 1. Execution + handoff protocol

### Model + honesty about context
- Run on **Opus**. One session can loop through all tasks.
- **There is no reliable live "context %" readout available to the agent.** Do
  not claim a precise gauge. Instead use the checkpoint discipline below so no
  work is ever lost and any session can resume: that is the real safety net, and
  it makes the exact percentage irrelevant.

### Stacked PRs
- One task = one branch = one PR = one `make check`-green, subagent-reviewed unit.
- Branches **stack**: `t1` off `main`, `t2` off `t1`, `t3` off `t2`, ... Each PR
  targets the previous task's branch as its base, so they review and merge in
  order. When an earlier PR merges to `main`, rebase the rest onto `main`
  (`git rebase --onto main <old-base> <branch>`).
- Simpler fallback if stacking gets painful: cut each task off `main` and merge
  strictly one at a time. Note which you chose in `HANDOFF.md`.

### Checkpoint cadence (the handoff mechanism)
- After **every** task's PR is pushed, rewrite `HANDOFF.md` (format below) and
  commit it on that task's branch. This is the resume point.
- Hand off **at task boundaries**, not mid-edit. If a single task is large,
  sub-checkpoint: commit WIP with a clear message and record the exact next step
  in `HANDOFF.md` before stopping.
- When you judge the session is getting long (many tasks done, large diffs read,
  or the harness has summarized context once already), **stop after the current
  task**, finalize `HANDOFF.md`, and tell Kyle: "handing off, start a fresh
  session and point it at `docs/prev1-hardening.md` + `HANDOFF.md`." A fresh
  session resumes with full budget.

### Per-task definition of done (unchanged from CLAUDE.md)
`make check` green; tests + fixtures for everything testable; module failure
modes documented; fresh-context subagent review passed; branch swept clean of
em/en dashes and attribution; PR body has a per-AC checklist + test evidence.

### `HANDOFF.md` format (living file at repo root, git-ignored or on-branch)
```
# HANDOFF - pre-v1 hardening
Updated: <iso ts, from a tool, not guessed>
Branch stack base: main @ <sha>
Tasks:
  T1 digest-pause      [merged #NN | pushed, PR #NN open | in-progress | todo]
  T2 renormalize       [...]
  ...
Current branch: <name>  (base: <name>)
Next concrete step: <one or two sentences - the very next action>
Operational steps run: O1 remediation [done <run id> | not yet]
Open decisions: Reddit strategy (i/ii/iii) - <pending Kyle | chosen: ...>
Gotchas: <anything a resuming session must know>
```

## 2. Tasks

Ordered. Phase A first (Kyle's gate). T-numbers are the stack order.

### Phase A - stop dirty digests, then clean the data

**T1 - Digest pause switch.**
Add `digest.enabled: bool = true` to `SettingsConfig.digest`
(`grepify/config/schemas.py`); the `digest` command skips + logs (writes a
manifest note, no LLM calls, no files) when false. Lets us freeze digest
generation during remediation and unfreeze after. Tests: gate honored both ways.
Files: `config/schemas.py`, `digest/pipeline.py` (or `generate.py`), `cli.py`,
tests. Small.

**T2 - `renormalize` maintenance command (GRP-60).**
`grepify maintain renormalize` (or extend `backfill`): for every stored item,
re-apply the current `_strip_html` to `summary`; if it changed, rewrite that
item to truth. Then force re-extraction of the changed items (drop their
existing keyword rows and re-run `run_extract_pipeline(force=True)` over just
them), so keyword rows regenerate from the cleaned summary. Idempotent: a second
run rewrites nothing. Deterministic + tested with a dirty-summary fixture.
Depends on: nothing (normalizer already correct). Files: `extract/backfill.py`
or a new `grepify/maintenance.py`, `repository/*` (a truth-rewrite path for
items + keyword-row deletion by item_id), `cli.py`, tests. **Largest task -
sub-checkpoint if needed.**

**O1 - Operational remediation run (not a code PR).**
After T1+T2 merge: set `digest.enabled: false`, run
`grepify maintain renormalize` + `grepify extract` against the data branch
(a manual `workflow_dispatch`, or locally against the `data` worktree), verify
zero HTML keywords remain (`grep` the keywords JSONL), regenerate the affected
digests, then set `digest.enabled: true`. Record the run id in `HANDOFF.md`.

### Phase B - reliability + connection errors

**T3 - Daily-digest reliability (v1.0 blocker).**
Bug: only `daily-ai-2026-07-08` exists, yet the LLM log shows successful digest
calls on 07-10 with no committed digest file for 07-09/07-10. Investigate
generation-vs-persistence: does `add_digest` run, does `commit-data` include new
digest files, does the period/skip logic silently no-op? Fix so a daily digest
is produced + committed every day the gate fires. Add a regression test.
Files: `digest/pipeline.py`, `digest/generate.py`, `scripts/commit_pipeline_data.py`,
`cli.py`, tests.

**T4 - Next-digest time on the site (v1.0 gate).**
Surface on the health (and/or home) page: the next scheduled digest time
(America/Edmonton, from the GRP-45 gate) and the last generated digest per
category. Pure render from the clock + stored digests; snapshot-tested.
Files: `site/trends.py` or `site/pages.py`, a template, `site/build.py`, tests.

**T5 - Feed-health audit + `doctor` report.**
Triage the ~25 dead/blocked RSS feeds from the fetch log:
- HTTP 4xx (14: ai-techpark, aimodels, clarifai-blog, ...) - fix moved URLs
  where trivially found, else `enabled: false`.
- Unparseable (8: aim-ai, shaip-blog, theodo, ...) - serving HTML/challenge
  pages; mostly `enabled: false`.
- TLS/conn (3: insideainews, knowtechie - stuck on SSLv3; bdan-ai refused) -
  `enabled: false`.
Add `grepify doctor` (or extend `health`) that reports per-source last status +
error class, so this triage is repeatable. Feed liveness is checked by
`grepify validate` on the PR. Files: `sources/groups/*.yml`, a small report
command, tests. Also fold in two cheap hardening items: bounded retry on YouTube
transient 5xx, and the entity-encoded-tag edge in `_strip_html`
(strip -> unescape -> conservative second strip), each with a test.

**T6 - Reddit strategy. DECIDED: option (ii) - best-effort + quiet (Kyle, 2026-07-10).**
26 Reddit sources fail: `403` on `new.json`, `429` on the `.rss` fallback -
datacenter-IP blocking, no code fix makes scraping reliable. Kyle chose the
zero-secret interim: **mark Reddit a best-effort source class** - reduce its
fetch cadence relative to other kinds and stop flagging Reddit `error` rows on
`/health` (so it is not 26 red rows of expected noise), while still attempting
each run and logging the outcome. Do NOT build the OAuth API (option i) or drop
Reddit (option iii). Implementation sketch: a per-kind "best-effort" flag (or a
Reddit-specific health-suppression) so consecutive Reddit failures do not set
the `flagged` bit, plus a cadence knob; keep the isolation contract (a Reddit
failure never fails the run). Tests for the suppression + cadence logic.

### Phase C - polish + audit

**T7 - Doc fix (trivial).**
`grepify/extract/eval.py` docstring + `tests/fixtures/eval/README.md` say
`expected_keywords` "starts empty" - the set is 30/30 labeled. Correct the docs.

**T8 - Full code-review + simplify + audit pass.**
Run `/code-review` and `/simplify` (or a fresh-context reviewer) over the whole
shipped package, not just a diff: correctness, dead code, duplication, interface
hygiene, test gaps. Land fixes as their own PR(s). This is the pre-v1 audit Kyle
asked for. Hold GitLab cutover (GRP-62) - explicitly out of scope until Kyle is
ready.

## 3. What is explicitly NOT in this work order
- GitLab cutover (GRP-62) - deferred by Kyle.
- New features (Slack push E7, etc.) - after v1.
- The X fetcher - retired (see `docs/epics/E5.md`).

## 4. v1.0.0 readiness gate (Kyle's definition)
Tag v1.0.0 only when: HTML remediation done (Phase A + O1), daily digests
generate + persist automatically (T3), and the site shows the next digest run
(T4). Feed audit (T5) and the audit pass (T8) are strongly recommended but the
three above are the hard gate.

## 5. Follow-ups surfaced during hardening (post-v1, Kyle-greenlit for a later set)

**F1 - Digest keyword drill-down should be digest-scoped (Kyle, 2026-07-10).**
Today a digest keyword chip links to the global keyword page
(`keyword/<slug>/`), which is built over a 30-day trailing window across **all**
categories (`grepify/site/trends.py` `keyword_details`, `windows.keyword_days`).
So the articles/sources shown there do not match the chip's number, which is that
digest's own scope: distinct articles for the keyword within the digest's period
(daily or weekly) and category. Desired behavior: clicking a keyword **from a
digest** opens the articles used in **that** digest for the keyword
(period- and category-scoped), with a quick link out to the all-time / 30-day
keyword page for the same term. Fold in the trivial wording fix at the same time:
the chip tooltip in `grepify/site/templates/digest_detail.html` says "mentions"
but the count is **distinct articles** - relabel it (e.g. "N articles"). Scope:
a new or parameterized period+category-scoped keyword view + template + snapshot
tests; a PRD-diff candidate (design to be confirmed before building). Not part of
T1-T8; do **not** slip it into the current stack.
