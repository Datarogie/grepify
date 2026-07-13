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
