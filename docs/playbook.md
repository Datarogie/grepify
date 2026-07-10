# Grepify - Agent Build Playbook

From empty repo to v1.5, session by session. Companion to `grepify-prd-and-issue-plan.md` (the PRD). Designed for Claude Code sessions driven from mobile.

## Ground rules (every session)

- One session = one group below. Fresh context each time. The agent reads only: `docs/prd.md`, the epic brief, and the issues in scope.
- Session ends with: `make check` green, MR opened with issue IDs in title, snapshot/eval deltas stated in MR description.
- Kyle's per-session effort: paste the prompt, answer any blocking question, review MR from phone, merge.
- Model routing: **Opus-class** (opus 4.8) for sessions marked O - interface design, prompts, judgment. **Sonnet-class** (latest sonnet) for sessions marked S - well-specified implementation. If a Sonnet session stalls on ambiguity twice, restart it on Opus rather than pushing.
- WIP discipline: max 2 sessions in flight (matches your CLAUDE.md cap). [P]-marked sessions are the only safe parallels.

## Session 0 - manual, 10 min, no agent

1. Create GH repo `grepify` (private), default branch `main`.
2. Commit `docs/prd.md` (the PRD file) as the first commit.
3. Add CI secrets: `LLM_API_KEY` (Gemini). Slack/X secrets come later (S9/S10).
4. Start S1.

## Sessions

| # | Model | Scope (issues) | Gate to next |
|---|---|---|---|
| S1 | O | **E0 brief + GRP-01..05** - scaffold, docs, Repository+ConfigProvider interfaces w/ v1 impls, CLI skeleton. Brief written first, then implementation. | `make check` green locally in CI-less repo |
| S2 | S | **GRP-06** - Actions: validate-on-MR, pipeline cron, Pages placeholder. MUST implement: `[skip ci]` on data commits, `paths-ignore: [data/**]`, concurrency group, `fetch-depth: 1`. | cron run green, no self-trigger loop observed |
| S3 | O | **GRP-07** - scrape trendcloud /sources (12 pages), categorize into group yamls (ai-research/business/tooling/youtube/reddit + data-engineering seeds from PRD §14), liveness-check feeds, disable dead. | Kyle skims categorization in MR (phone task, ~10 min) |
| S4 | O | **E1 brief + GRP-10, GRP-14** - fetcher interface, normalizer+dedup. The two design-critical E1 issues together so the contract is coherent. | idempotency test passes |
| S5 | S | **GRP-11..13, 15, 16** - RSS/YT/Reddit fetchers, orchestrator, health. [P]-safe to run alongside S6 prep. | real cron fills `data/` for 2+ days clean |
| S6 | O | **E2 brief + GRP-20, 21** - LLM provider w/ budget breaker, extract batcher + prompt v1. | breaker test green; sample extraction eyeballed |
| S7 | S | **GRP-22, 23, 25** - YAKE fallback, normalization/alias module, pipeline wiring + DQ assertions. Decide backfill mode here (PRD weak-spot #2): recommend one-time `--backfill` with cap 200 calls. | full pipeline green on cron |
| S7k | - | **Kyle manual: GRP-24 labels** - label 30 items on phone (agent pre-generates candidate file in S7). | eval baseline recorded |
| S8 | S | **E3 brief (incl. determinism rules: sorted iteration, injected clock) + GRP-30..36** - Jinja site, trend queries (GRP-31 is the one Opus-ish item; acceptable on Sonnet with the brief, escalate if math gets hand-wavy), GitLab CI file. Big session - split 30/31 vs 32-36 if context strains. | site live on Pages; snapshots green |
| S9 | O | **E4 brief + GRP-40..45** - digests per category, keyword pages, tz gating. Prompt work = Opus. | first real daily digest reads well (Kyle judgment) → **tag v1.0.0** |
| S10 | O | **E5 brief + GRP-50..53** - twscrape (burner account, secrets added now), X in site, transcripts. Isolated + best-effort; failure here never blocks. | X items flowing or explicitly parked with runbook note |
| S11 | S | **E6: GRP-60, 62, 63** - maintenance cmds, GitLab cutover, size guardrails. | one green pipeline on GitLab before DNS/bookmark switch |
| S12 | O | **GRP-61 runbook + GRP-64 name final** - runbook reviewed against every incident hit in S1-S11. | phone-operable runbook |
| S13 | S | **E7: GRP-70, 71** - Slack webhook notifier (secret added), wired post-digest. | digest lands in #grepify-digest |

Rough shape: 13 agent sessions + 2 small manual tasks. S5/S7/S8 are the only ones likely to need a second sitting.

## Kickoff prompt - S1 (template for all sessions)

```
Read docs/prd.md fully. You are implementing sessions from its issue plan.

This session: write docs/epics/E0.md (1 page: repo shape, Repository and
ConfigProvider interface signatures, tooling standards), then implement
GRP-01 through GRP-05 exactly as scoped in the PRD.

Hard rules:
- Follow PRD §5 decisions verbatim (JSONL truth + SQLite cache, Jinja later,
  budget gates). No architecture changes without asking.
- No features beyond the issues in scope. No silent behavior changes.
- Python 3.12, uv, ruff+mypy strict on core, pytest. lowercase sql, no select *.
- Every module documents its failure modes in the docstring.
- Interfaces must be Postgres-swappable: no sqlite types in signatures.
- Done = make check green + tests for everything testable + MR-ready diff.

Work in a plan-then-execute style: numbered plan first (short), then build.
State risky assumptions and proceed. Ask only if truly blocked.
```

Per-session variants: swap the session line ("This session: read docs/epics/E1.md if it exists, else write it first, then implement GRP-10 and GRP-14...") - everything else stays identical. Sessions S2+ add: "Read the epic briefs of any interface you consume; do not read other epics' code beyond its public interface."

## Prompt appendix - session-specific injections

- **S2**: "Prove no self-trigger loop: include the workflow-level guards ([skip ci], paths-ignore, concurrency group) and describe in the MR how you verified them."
- **S3**: "Categorization rubric: research = papers/labs/academic; business = funding/market/enterprise; tooling-dev = frameworks/infra/how-to. When ambiguous, tooling-dev. Output a table in the MR: source → category → alive?"
- **S6**: "The budget breaker is the most important code in this repo (see PRD: CSR incident). Bounded retries, jittered, circuit breaker, llm_log rows for every call including failures. Test the 41st call is refused."
- **S8**: "Determinism: no datetime.now() in render path - clock is injected; all dict/set iteration sorted; snapshot tests must pass twice in a row in CI."
- **S9**: "Digest prompt: narrative 2-4 paragraphs + TL;DR bullets. Generate against the last 3 real days of data and paste all outputs in the MR for review."
- **S10**: "X is best-effort: every failure mode (challenge, ratelimit, suspension) degrades to skip+log, never raise past the orchestrator."

## Failure/retry protocol

- Agent goes sideways (rewrites architecture, scope-creeps): kill session, restart with same prompt + "Previous attempt violated: <rule>. Do not."
- Two sideways attempts on a Sonnet session → rerun on Opus.
- Merge conflicts between [P] sessions: the later MR rebases; never merge-commit data files by hand.
- Anything discovered mid-session that changes the PRD: agent proposes a PRD diff in the MR, never edits silently.

## Standing ops tasks

- **Source-liveness recheck (periodic).** The Health page flags a source after
  5 consecutive fetch errors (F-ING-08); flagged is not the same as dead. Triage
  splits two ways, and only one ends in removal:
  - **Blocked-but-alive** (e.g. reddit `new.json` returning 403/429 to CI egress
    IPs): the feed is fine, CI just can't reach it. Do NOT prune. Robustness
    options, in order of effort: (1) confirm the F-ING-04 `.rss` fallback fires
    (reddit's `/r/<sub>/.rss` is blocked far less often than the JSON endpoint)
    and lower its cadence; (2) an authenticated reddit API app (OAuth token as a
    CI secret) for a stable quota; (3) a non-CI fetch path (self-hosted / Termux)
    for sources CI IPs can't reach, mirroring the YouTube-transcript fallback
    idea (PRD §13). YouTube channel RSS (`feeds/videos.xml`) is usually NOT
    blocked - verify before grouping it with reddit.
  - **Genuinely dead** (feed 404s or moved; the GRP-07 seed had ~10% expected
    dead): first look for the moved feed URL; only if it is truly gone, disable
    it. v1 never auto-disables (PRD §2 Non-Goals) - pruning is always a reviewed
    MR.
  Action (fits GRP-60): a maintenance command that re-pings flagged feeds and
  emits a triaged report (blocked / dead / recovered) for a human to act on -
  it never deletes sources on its own.
