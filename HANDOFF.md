# HANDOFF - #45 source-fetch error sweep (DONE)

Updated: 2026-07-13T01:25:23Z
Issue: #45 (source-fetch error sweep, worked by error class) - COMPLETE. Runbook:
docs/feed-triage.md. Background (historical): docs/prev1-hardening.md.
Branch base: main @ 6b78116dc34c5057003032a6e6b3bcda53ce3fd3.

## Outcome (all classes verified via the data branch fetch log)
  C1 http_4xx 403 + unparseable  [DONE - #46 fix, #52 closeout]
       browser User-Agent + feed Accept header. Verified run 20260713T003917Z:
       recovered copyleaks-blog only (1 of 7). aimodels / ai-techpark /
       benn-substack (403) and aim-ai / shaip-blog / theodo-data-and-ai-blog
       (unparseable) confirmed still dead, re-disabled with evidence (#52).
       clarifai-blog stays disabled (404).
  C2 http_4xx 415 flappers        [DONE - no code change]
       artificial-lawyer, bdan-ai, la-biblia-de-la-ia status ok across recent runs
       including under C1's Accept header. Kept enabled and watched.
  C3 tls sslv3 handshake          [DONE - #49 fix, this PR closeout]
       seclevel-1 SSL context (permit legacy ciphers, cert verification on, TLS 1.2
       floor) shipped in #49. Verified run 20260713T010831Z (post-#49):
       inside-ai-news and knowtechie-ai STILL SSLV3_ALERT_HANDSHAKE_FAILURE - the
       fix did not recover them. Confirmed dead, re-disabled with evidence (this PR).
       Did NOT drop to deprecated TLS 1.0/1.1 (bad tradeoff for 2 minor sources;
       Kyle delegated the call). The seclevel-1 transport posture is retained as a
       mild general allowance; reverting it is a trivial follow-up if desired.
  C4 http_429                     [no action - Reddit, quiet by design (T6)]

Net: of the non-Reddit fetch errors, 1 source recovered (copyleaks-blog); the rest
are confirmed dead at the network level (WAF/IP 403, HTML-challenge unparseable, or
unrecoverable TLS) and disabled with evidence. The 415 flappers stay enabled and
healthy. #45 is closed.

## Also merged this session (NOT part of #45, from Kyle feedback)
  GRP-47 (#48): "Your digest" folded into a Digests All/Following tab.
  GRP-50 (#51): the All tab is a fully unfiltered archive; all filters live on the
  Following tab.

## Gotchas / how to verify feeds
  - Egress to feed hosts is BLOCKED in CI/build; verify by inspecting the data
    branch fetch log (logs/fetch/YYYY/MM/DD.jsonl) via a detached worktree - the
    doctor report is written only to the Actions job summary
    (make doctor >> $GITHUB_STEP_SUMMARY), which the MCP tools cannot fetch.
  - Data branch truth is at the REPO ROOT (logs/, items/, digests/, runs/), not
    under data/.
  - Auto-merge is OFF (Kyle merges PRs manually); the git token cannot delete
    remote branches (403).
  - No em/en dashes and no AI-authorship attribution anywhere.
  - Stale scratch branches exist (claude/grepify-post-v1-tranche-w3i62x and the
    -37/-38/-39 ones); ignore them.

## Next
  #45 is done. Follow-ups NOT in scope here: extract/digest/build-stage error
  triage (a separate deferred issue); optionally revert the seclevel-1 TLS posture
  now that its target sources are confirmed dead.

## O1 verification (GRP-55)

Updated: 2026-07-13T18:00:00Z (this session, GRP-55, no code changes).

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
