# HANDOFF - pre-v1 hardening

Updated: 2026-07-10T18:37:54Z
Plan: docs/prev1-hardening.md (read this first)
Branch stack base: main @ b29a319

Tasks:
  T1 digest-pause switch        [todo]
  T2 renormalize command (GRP-60)[todo]  <- largest; sub-checkpoint if needed
  O1 remediation run (operational)[todo] <- after T1+T2 merge
  T3 daily-digest reliability   [todo]  (v1.0 blocker)
  T4 next-digest time on site   [todo]  (v1.0 gate)
  T5 feed-health audit + doctor [todo]
  T6 reddit strategy            [decided: option ii - best-effort + quiet; ready to build]
  T7 eval doc fix               [todo]
  T8 code-review + simplify pass[todo]

Current branch: claude/prev1-hardening (the PLAN pr #21, merged to main) - no code yet.
Next concrete step: start T1 (branch claude/prev1-t1-digest-pause off latest main).
Operational steps run: O1 not yet.
Open decisions: none. Reddit = option ii (best-effort: reduce cadence, stop flagging
  so /health is not 26 red rows; NOT the OAuth API, NOT dropped). T6 unblocked.
Gotchas:
  - Local origin/main was stale earlier; always 'git fetch origin main' before branching.
  - HTML issue is STALE DATA, not a live normalizer bug (see plan section 0).
  - The 'data' branch holds truth; remediation (O1) runs against it, not main.
