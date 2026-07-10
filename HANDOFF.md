# HANDOFF - pre-v1 hardening

Updated: 2026-07-10T19:23:24Z
Plan: docs/prev1-hardening.md (read this first)
Branch stack base: main @ 8b3ea1c

Strategy: STACKED PRs (t1 off main, t2 off t1, ...). Each PR targets the prior
task's branch as base. When an earlier PR merges to main, rebase the rest onto
main (`git rebase --onto main <old-base> <branch>`).

Tasks:
  T1 digest-pause switch        [pushed, PR #22 open (base main), CI green]
  T2 renormalize command (GRP-60)[pushed, PR #23 open (base t1)]  <- review done + fixed
  O1 remediation run (operational)[todo] <- after T1+T2 merge
  T3 daily-digest reliability   [in-progress]  (v1.0 blocker)
  T4 next-digest time on site   [todo]  (v1.0 gate)
  T5 feed-health audit + doctor [todo]
  T6 reddit strategy            [decided: option ii - best-effort + quiet; ready to build]
  T7 eval doc fix               [todo]
  T8 code-review + simplify pass[todo]

Current branch: claude/prev1-t3-* (starting). T3 branches off claude/prev1-t2-renormalize.
Next concrete step: T3 - investigate why daily digests generate (LLM log shows calls
  07-10) but no digest file is committed for 07-09/07-10; fix generation-vs-persistence;
  add regression test. Files: digest/pipeline.py, digest/generate.py,
  scripts/commit_pipeline_data.py, cli.py, tests.

T1 summary (done): DigestSettings.enabled: bool = true. `digest` no-ops (manifest
  note, exit 0, no LLM/files) when false; check before LLM_BASE_URL guard.
  Files: config/schemas.py, cli.py, settings.example.yml, tests/test_cli_digest.py.
  Subagent review APPROVE. make check green.

T2 summary (done): `grepify maintain renormalize` (GRP-60). Public clean_summary in
  normalize.py (single source of truth; rstrip-after-truncate = true fixed point).
  New repo truth-maintenance: rewrite_items + delete_item_keywords (atomic temp+replace
  writes, empties removed). maintenance.py renormalize_summaries (pure/idempotent core).
  CLI wires force re-extract over just the changed items. Files: ingest/normalize.py,
  ingest/__init__.py, repository/base.py + jsonl_sqlite.py, maintenance.py, cli.py,
  tests (repository/maintenance/cli_maintain/normalize). Subagent review: REQUEST CHANGES
  -> found a blocking truncation-idempotency bug (trailing space on word-boundary cut
  caused perpetual rewrite/re-extract); FIXED (rstrip) + crash-safe atomic writes +
  boundary test. make check green (455 tests).

Operational steps run: O1 not yet.
Open decisions: none. Reddit = option ii (best-effort: reduce cadence, stop flagging
  so /health is not 26 red rows; NOT the OAuth API, NOT dropped). T6 unblocked.
Gotchas:
  - Local origin/main was stale earlier; always 'git fetch origin main' before branching.
  - HTML issue is STALE DATA, not a live normalizer bug (see plan section 0).
  - The 'data' branch holds truth; remediation (O1) runs against it, not main.
  - PRs on this repo run the `validate` workflow (validate.yml, on: pull_request).
  - git grep -P with \x{2014} fails on this box's PCRE build; sweep dashes via
    `git diff | python3 -c "..."` checking for the literal chars instead.
