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
  T6 reddit strategy            [blocked: needs Kyle i/ii/iii]
  T7 eval doc fix               [todo]
  T8 code-review + simplify pass[todo]

Current branch: claude/prev1-hardening (base: main) - the PLAN pr only, no code yet.
Next concrete step: on Kyle's go-ahead, start T1 (branch claude/prev1-t1-digest-pause off this).
Operational steps run: O1 not yet.
Open decisions: Reddit strategy (i auth-API / ii best-effort-quiet / iii drop) - PENDING Kyle. T6 blocked on it.
Gotchas:
  - Local origin/main was stale earlier; always 'git fetch origin main' before branching.
  - HTML issue is STALE DATA, not a live normalizer bug (see plan section 0).
  - The 'data' branch holds truth; remediation (O1) runs against it, not main.
