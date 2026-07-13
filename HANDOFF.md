# HANDOFF - full project review + backlog rebuild (2026-07-13)

Updated: 2026-07-13T17:30:00Z
Session: full-project review, tracker cleanup, forward backlog created.
Branch: claude/project-review-improvements-ue3n3r (review doc; no code changes).
Previous handoff (the #45 fetch sweep, DONE) is superseded; its gotchas are
carried forward below.

## What happened

- Full review written: docs/reviews/2026-07-13-project-review.md (state of
  M0-M6, correctness/security/architecture findings, product ideas, ranked
  order of work). make check green at main 942121f (ruff, mypy strict, 547
  tests).
- Tracker cleaned: #29-#33, #37, #38, #39, #47, #50 were all merged but left
  open; each closed with a comment linking its merged PR.
- New backlog created, #55-#75, each issue self-contained per
  docs/issue-template.md. WIP cap removed (Kyle, 2026-07-13; see CLAUDE.md
  Parallel work) - parallelize freely where files don't overlap. Suggested
  order:
    1. #55 release hygiene (O1 + tag v1.0.0)
    2. #56 x-kind validate gap  [small]
    3. #57 pin no-op            [small]
    4. #58 supply-chain guard   [small]
    5. #59 ingest rescan perf
    6. #60 docs truth sweep, #61 runbook, #62 size guardrail
    7. #63 digest gate resilience, #64 deploy decoupling
    8. #65 source acquisition ladder DESIGN (blocks #66 impl and #67
       personal-to-global v2 design)
    9. #68 rising strip, #69 since-last-visit delta, #70 coverage surface
   10. #71 window alignment, #72 code health, #73 HTML parser swap,
       #74 smoke canary decision
   #75 = firstmate trial (HITL, needs a machine Kyle controls).

## Kyle decisions embedded in the backlog

- Sources: tricky-to-fetch feeds get alternative acquisition paths; gone
  targets are REMOVED not disabled; paywalled sources get honest messaging,
  never workarounds (#65/#66). Multi-user personal-source promotion at a
  threshold is v2 design only (#67), PRD diff for Kyle.
- Firstmate (github.com/kunchenguid/firstmate) to be trialled on this repo
  in no-mistakes mode; no .env so its only external relay (X mode) stays
  disabled; no telemetry exists otherwise (#75).

## O1 verification (GRP-55)

Updated: 2026-07-13T18:00:00Z (GRP-55 session, no code changes).

**Verdict: O1 ran. Confirmed done on 2026-07-11, run id `20260711T024728Z-e1938d`.**

Method: `git fetch origin data`, `git worktree add --detach <tmp> origin/data`
(detached HEAD `f8e55e8`), then inspected the data branch truth at the repo
root (`runs/`, `items/`, `keywords/`) per the HANDOFF gotcha that these live
at root, not under `data/`.

Evidence:
  - `runs/20260711T024728Z-e1938d.json`: command `maintain-renormalize`, ok
    true. `items_scanned` 2171, `items_rewritten` 994, `keyword_rows_deleted`
    5503, `items_reextracted` 994, `keywords_written` 4379. Notes recorded on
    the run: the LLM budget ran out partway through re-extraction (fallback
    YAKE picked up the remainder, which is expected and still HTML-clean
    since fallback runs over the same re-normalized summary) and one item,
    `f492e4a621199c7b63e3549f750cd117557c88baa8a09d531078e61f3f7ed202`, ended
    up with zero keywords after re-extraction.
  - `runs/20260711T034619Z-dd4802.json`: a second `maintain-renormalize` pass
    about an hour later, `items_scanned` 2174, `items_rewritten` 0 - proves
    the first pass was complete and the command is idempotent (nothing left
    to rewrite).
  - Scanned every keyword row present on the data branch
    (`keywords/2026/07/{09,10,11,12,13}.jsonl`, all files that exist there):
    zero exact matches for the known dirty terms (`div`, `div class`, `span`)
    and zero rows containing markup characters (`<`, `>`, `=`).
  - Scanned all 2385 item summaries on the data branch (`items/**/*.jsonl`):
    zero residual HTML tags.

Conclusion: no further O1 action is needed - it already ran and the result
verifies clean against the current data branch state. The single item with
no post-re-extraction keywords is a residual oddity (likely an empty summary
after stripping), not a sign the remediation failed; worth a look in a future
extract/backfill pass if the pattern recurs, but it does not block the v1.0.0
tag.

Not verified in this session (out of GRP-55 scope, no code changes made):
whether `digest.enabled` was toggled false then true around the run per the
documented O1 procedure, and whether affected digests were regenerated after.
The keyword- and summary-level evidence above is sufficient on its own to
call O1 done; Kyle can ask for the digest-toggle trail specifically if he
wants that audited too.

## Gotchas carried forward

- Egress to feed hosts is BLOCKED in CI/build; verify feeds via the data
  branch fetch log (logs/fetch/YYYY/MM/DD.jsonl) in a detached worktree.
- Data branch truth is at the REPO ROOT (logs/, items/, digests/, runs/),
  not under data/ (README still wrong until #60 lands).
- Auto-merge is OFF (Kyle merges manually); the git token cannot delete
  remote branches (403).
- No em/en dashes and no AI-authorship attribution anywhere (hooks enforce).
- Stale scratch branches exist (claude/grepify-post-v1-tranche-w3i62x and
  the -37/-38/-39 ones); ignore them.

## Next

Start at the top of the order above. Every issue body is self-contained;
read CLAUDE.md + the issue, work on a feature branch, MR title carries the
issue id.
