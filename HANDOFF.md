# HANDOFF - pre-v1 hardening

Updated: 2026-07-10T19:44:24Z
Plan: docs/prev1-hardening.md (read this first)
Branch stack base: main @ 8b3ea1c

Strategy: STACKED PRs (t1 off main, t2 off t1, t3 off t2, ...). Each PR targets
the prior task's branch as base. When an earlier PR merges to main, rebase the
rest onto main (`git rebase --onto main <old-base> <branch>`) and repoint the
next PR's base.

Standing rules for this tranche (Kyle, 2026-07-10) - apply to ALL future PRs:
  - Every PR uses the template at .github/pull_request_template.md (PR #25, off main).
    It MUST include a "How to test" section that is PHONE-FIRST: default to steps
    doable on a phone and say exactly what Kyle should SEE; mark any step that needs
    a computer "(computer)". If a change has no user-visible surface (backend/CLI/
    data), say so and point to the automated evidence. Existing PRs #22/#23/#24 were
    backfilled with this section.
  - Ready-to-merge sweep before opening OR updating any PR, and again after commits
    land: (a) no em/en dashes in the diff; (b) NO AI-authorship attribution in commit
    messages, PR bodies, OR the commit author identity (no Co-Authored-By /
    Claude-Session / "Generated with/by" / claude.ai/code).
  - GIT IDENTITY (decided, done): commits use
    `Datarogie <42312814+Datarogie@users.noreply.github.com>` (Kyle's GitHub
    no-reply identity). The 4 branches were history-rewritten to this author +
    committer and force-pushed. EVERY session MUST run, at start:
      git config user.name "Datarogie"
      git config user.email "42312814+Datarogie@users.noreply.github.com"
    so no commit is ever authored as Claude again. (At GitLab cutover this moves to
    Kyle's work email - GRP-62.)

Tasks:
  T1 digest-pause switch        [pushed, PR #22 (base main), CI green, review APPROVE]
  T2 renormalize command (GRP-60)[pushed, PR #23 (base t1), CI green, review fixed]
  T3 daily-digest reliability   [pushed, PR #24 (base t2), review fixed; CI re-running after de-flake]
  O1 remediation run (operational)[todo] <- after T1+T2 merge
  T4 next-digest time on site   [todo]  (v1.0 gate) <- NEXT
  T5 feed-health audit + doctor [todo]
  T6 reddit strategy            [decided: option ii - best-effort + quiet; ready to build]
  T7 eval doc fix               [todo]
  T8 code-review + simplify pass[todo]

Current branch: claude/prev1-t3-daily-digest (base: claude/prev1-t2-renormalize).
Next concrete step: START A FRESH SESSION for T4. Branch claude/prev1-t4-next-digest
  off claude/prev1-t3-daily-digest. Surface on the health (and/or home) page: the
  next scheduled digest time (America/Edmonton, from the GRP-45 gate in
  grepify/digest/gating.py) and the last generated digest per category (from stored
  digests). Pure render from clock + stored digests; snapshot-tested. Files:
  site/trends.py or site/pages.py, a template, site/build.py, tests.

--- Per-task summaries (done this session) ---

T1 (PR #22): DigestSettings.enabled: bool = true. `digest` no-ops (manifest note,
  exit 0, no LLM/files) when false; check runs BEFORE the LLM_BASE_URL guard.
  Files: config/schemas.py, cli.py, settings.example.yml, tests/test_cli_digest.py.

T2 (PR #23): `grepify maintain renormalize` (GRP-60). Public clean_summary in
  ingest/normalize.py is the single summary-cleaner (rstrip-after-truncate = true
  fixed point - a review-caught idempotency bug). New repo truth-maintenance:
  rewrite_items + delete_item_keywords (atomic temp+replace writes, emptied
  partitions removed). maintenance.py renormalize_summaries = pure/idempotent core;
  CLI wires a force re-extract over just the changed items. Files: ingest/normalize.py
  + __init__.py, repository/base.py + jsonl_sqlite.py, maintenance.py, cli.py, tests.

T3 (PR #24): self-healing daily digests. ROOT CAUSE (verified on the data branch):
  generation + persistence WORK; the daily digest is missed because the pipeline
  runs the digest step only inside the narrow GRP-45 gate window (05:00-08:59
  Edmonton) and GitHub cron jitter pushes the "morning" run past it (13:00 UTC
  landed 15:31 UTC = 09:31 MDT), with no recovery. Fix (command/pipeline layer,
  gate/gating.py + pipeline.yml untouched so T4 can still surface the gate):
  periods.recent_days(); run_digest_pipeline walks a catch-up window
  (digest.daily_lookback_days, default 7) and skips (category, period) pairs whose
  REAL (non-template) digest already exists - idempotent + self-healing; template
  digests are re-attempted so a recovered LLM upgrades them. DigestRunResult gains
  already_present; manifest reports digests_already_present + category_periods_considered.
  Files: digest/periods.py, digest/pipeline.py, digest/__init__.py,
  config/schemas.py, cli.py, settings.example.yml, tests (periods/generate/cli_digest).

Operational steps run: O1 not yet (do after T1+T2 merge to main; see plan §O1:
  set digest.enabled false, run `grepify maintain renormalize` + `grepify extract`
  on the data branch, grep keywords JSONL for zero HTML keywords, regenerate the
  affected digests, set digest.enabled true; record the run id here).

Documented follow-ups (post-v1, NOT in T1-T8 - see plan section 5):
  - F1 digest keyword drill-down should be digest-scoped: clicking a keyword on a
    digest should show the articles used in THAT digest (period + category, daily or
    weekly), with a quick link to the all-time/30-day keyword page. Fold in the
    trivial fix that the digest chip tooltip says "mentions" but the count is
    distinct articles (relabel). PRD-diff candidate; Kyle greenlit documenting it,
    build later. Do NOT slip into the current stack.

Open decisions:
  - COMMIT AUTHOR IDENTITY: RESOLVED - rewritten to Datarogie GitHub no-reply
    identity across all 4 branches (see Standing rules above). Nothing pending.
  - Reddit = option ii (best-effort: reduce cadence, stop flagging so /health is not
    26 red rows; NOT the OAuth API, NOT dropped). T6 unblocked.
  - COMMIT AUTHOR: commits are authored as `Claude <noreply@anthropic.com>` (the
    env default). Message bodies are clean (no Co-Authored-By / Claude-Session /
    "Generated with Claude Code"). The author identity is borderline vs CLAUDE.md's
    no-agent-attribution rule. Left as-is (rewriting author across 3 pushed,
    CI-green, stacked branches is invasive). Kyle to decide whether to amend author
    to his identity before merge, or set git user.name/email for future sessions.
  - T3 residual (flagged in PR #24): catch-up recovers missed days only if SOME
    gated run lands in the window within daily_lookback_days. Full belt-and-suspenders
    = widen the gate window OR run digest on every pipeline run (relies on the new
    idempotency); both touch CI/gating.py and affect T4, so left for Kyle.

Gotchas:
  - Always `git fetch origin main` before branching; local origin/main was stale earlier.
  - HTML contamination is STALE DATA, not a live normalizer bug (plan section 0).
  - The `data` branch holds truth at the REPO ROOT (digests/, items/, logs/, runs/),
    NOT under data/. Inspect with `git show origin/data:runs/<id>.json` etc.
  - PRs run the `validate` workflow (validate.yml, on: pull_request). Its checks
    report as GitHub *check runs*, so `pull_request_read get_status` shows
    total_count 0; use actions_list(list_workflow_runs, validate.yml) to see real
    status. That tool's output is huge - pipe the saved file through jq.
  - Dash sweep: `git grep -P \x{2014}` fails on this box's PCRE build. Use
    `git diff | python3 -c "import sys; print('CLEAN' if not any(c in '<emdash><endash>' for c in sys.stdin.read()) else 'FOUND')"` with the literal chars.
  - latest_manifest() is a coin flip between two same-second runs (run_id =
    second-precision ts + random suffix). In multi-run tests assert on stdout +
    stored truth, not latest_manifest. (Bit both T2 and T3 CLI tests.)
  - Fresh-context subagent review per task is valuable - it caught the T2
    truncation-idempotency bug and the T3 flaky test before/at CI.
