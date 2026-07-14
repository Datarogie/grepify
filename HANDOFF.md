# HANDOFF - review backlog executed end to end (2026-07-14)

Updated: 2026-07-14T04:00:00Z
Session: the 2026-07-13 review backlog (#55-#75, #94) was built, reviewed, and
merged across four agent waves in a single orchestrated session. v1.0.0 is
tagged and released.

## State of the project

- **v1.0.0 released** (tag on main, GitHub Release published by Kyle;
  O1 verified done first - see the O1 section below).
- **All review-backlog code issues shipped and merged**: #55 release
  hygiene, #56 x-kind validate gap, #57 pins, #58 supply-chain guard
  (dependabot + make audit + ruff S), #59 ingest single-scan, #60 docs truth
  sweep, #61 runbook, #62 data-size guardrail, #63 existence-based digest
  gate, #64 deploy-from-committed-truth, #65 ADR 0002 + #66 acquisition
  ladder and lifecycle classes, #68 rising strip, #69 last-visit delta,
  #70 coverage view, #71 Edmonton-aligned trend windows, #72 digest/site
  cycle broken (grepify/windows.py), #73 selectolax HTML parser (+O2
  renormalize ran: run id 20260713T222930Z-919057, 17 items rewritten),
  #74 pipeline failure notification + PRD 10.6 struck, #94 comment pruning.
- **Test suite**: 547 -> 710 tests, make check green throughout.
- **Remaining open**:
  - #67 personal-source promotion design doc (PR in flight; Kyle reviews
    the design, it is v2-facing only).
  - #75 firstmate trial (parked: needs a machine Kyle controls; options
    recorded on the issue).
  - Dependabot PRs #82-#86 if any remain unmerged.
  - PRD diff proposals awaiting Kyle: X-retirement text (#88 PR body),
    v2 sources-table status columns (#100 PR body / ADR 0002 open q4).

## Operating decisions made this session (Kyle)

- WIP cap retired (CLAUDE.md Parallel work: no-file-overlap rule instead).
- Comment discipline added to CLAUDE.md (minimal self-documenting code).
- Orchestrator review-and-merge mode: agent PRs are adversarially reviewed
  and merged by the orchestrating session; Kyle is consulted only for
  design-level or ambiguous calls.
- Sources policy: gone targets are REMOVED; dead ones shelved with evidence
  and a 30-day recheck; paywalled labelled honestly, never worked around
  (ADR 0002, shipped in #66). clarifai-blog removed as gone (Kyle had veto,
  did not exercise it).
- PRD 10.6 nightly canary struck (option A) with the runbook covering the
  silent-cron-death case.

## O1 verification (GRP-55, retained for the record)

O1 ran 2026-07-11, run id 20260711T024728Z-e1938d (994 items rewritten,
5503 dirty keyword rows deleted); second pass proved convergence. Full
evidence in the git history of this file (PR #76).

## Gotchas carried forward

- Egress to feed hosts is BLOCKED in CI/build; verify feeds via the data
  branch fetch log (logs/fetch/YYYY/MM/DD.jsonl) in a detached worktree.
- Data branch truth is at the REPO ROOT (logs/, items/, keywords/,
  digests/, runs/), not under data/ (README now says so correctly).
- Auto-merge is OFF; the session git token cannot delete remote branches or
  push tags (tags go through GitHub Releases from Kyle's account).
- No em/en dashes and no AI-authorship attribution anywhere (hooks enforce);
  the PR-creation tool auto-appends an attribution footer - strip it via an
  update immediately after creating any PR.
- Stale scratch branches exist; merged claude/grp-* branches cannot be
  deleted by the token and accumulate - ignore or clean manually.
- Background agents can be stopped silently mid-task; a watchdog checking
  worktree mtimes every 30 minutes catches it, and WIP can be salvaged by
  committing the worktree state with --no-verify and relaunching a
  finishing agent on the branch.

## Next

- Kyle: review the #67 design PR when it lands; decide the two parked PRD
  diffs; merge remaining dependabot PRs.
- Next session candidates (from the review's product list, not yet
  issues): co-occurrence neighborhood graphic on keyword pages, template
  polish as product surface, data-eng category seeding, E7 Slack push
  (v1.5), GitLab cutover (GRP-62, deferred by Kyle).
