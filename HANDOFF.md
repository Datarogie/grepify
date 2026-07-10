# HANDOFF - pre-v1 hardening

Updated: 2026-07-10T19:04:39Z
Plan: docs/prev1-hardening.md (read this first)
Branch stack base: main @ 8b3ea1c

Strategy: STACKED PRs (t1 off main, t2 off t1, ...). Each PR targets the prior
task's branch as base. When an earlier PR merges to main, rebase the rest onto
main (`git rebase --onto main <old-base> <branch>`).

Tasks:
  T1 digest-pause switch        [pushed, PR #22 open (base main)]
  T2 renormalize command (GRP-60)[todo]  <- largest; sub-checkpoint if needed
  O1 remediation run (operational)[todo] <- after T1+T2 merge
  T3 daily-digest reliability   [todo]  (v1.0 blocker)
  T4 next-digest time on site   [todo]  (v1.0 gate)
  T5 feed-health audit + doctor [todo]
  T6 reddit strategy            [decided: option ii - best-effort + quiet; ready to build]
  T7 eval doc fix               [todo]
  T8 code-review + simplify pass[todo]

Current branch: claude/prev1-t1-digest-pause (base: main). PR #22 open, CI pending.
Next concrete step: start T2 (branch claude/prev1-t2-renormalize off claude/prev1-t1-digest-pause).

T1 summary (done): DigestSettings.enabled: bool = true. `digest` command no-ops
  (manifest note, exit 0, no LLM calls, no files) when false; check runs before the
  LLM_BASE_URL guard. Files: config/schemas.py, cli.py, settings.example.yml,
  tests/test_cli_digest.py. Subagent review: APPROVE. make check green (440 tests).

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
