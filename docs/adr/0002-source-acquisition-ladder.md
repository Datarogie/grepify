# ADR 0002 - Source acquisition ladder and lifecycle classification

- **Status**: Proposed (pending Kyle sign-off on the rungs; gates #66/#67)
- **Date**: 2026-07-13 (recorded at GRP-65)
- **Deciders**: Kyle
- **Context PRD**: §2 (Non-Goals: no auto-disable in v1), §5 (architecture, locked),
  §6 (`sources` / `fetch_log` schema), §7 (source config), §9 (per-source
  isolation, egress posture)
- **Prior art**: `docs/feed-triage.md` (#39/#45 sweeps), `grepify/ingest/http.py`
  (browser UA + Accept + seclevel-1 TLS transport), `grepify/ingest/cadence.py`
  (Reddit best-effort), `grepify/doctor.py`, `grepify/config/schemas.py`

## Context

Today a source is a binary: `enabled: true` or `enabled: false`. Triage
(#39, #45) has repeatedly shown that this one bit hides at least five very
different situations, and that collapsing them loses the reasoning each time a
sweep runs:

- feeds that work (`active`);
- feeds whose direct URL is blocked or moved but that are still reachable by
  another path (should be `degraded`, today they are just off);
- feeds behind a subscription wall, where any acquisition attempt is both
  futile and a ToS problem (should be `paywalled`, today indistinguishable from
  a transient outage);
- feeds whose underlying page/subreddit/channel no longer exists (should be
  `gone` and removed, today they linger disabled forever);
- feeds that still exist but stay unfetchable after every reasonable path
  (`dead`, worth a slow re-check, not worth a human re-investigation each sweep).

Two hard constraints from the environment shape everything below:

1. **CI egress to feed hosts is blocked.** PR-triggered `validate` cannot reach
   feed hosts at all (even healthy ones). Only the scheduled `pipeline` workflow
   (`schedule` / `workflow_dispatch`) runs in a network context that can fetch
   feeds. So any ladder rung that needs a live request runs **only in the
   pipeline**; `validate` is restricted to offline/static checks. This matches
   the existing triage rule (`docs/feed-triage.md`: "do not add live
   feed-pinging").
2. **Everything stays within publisher ToS.** No auth bypass, no paywall
   circumvention, no credential replay. A realistic browser User-Agent and a
   feed `Accept` header (already shipped in `grepify/ingest/http.py`) are a
   legitimate reader posture, not a bypass. A hard subscription wall is exactly
   what the `paywalled` class is for: we label it and stop, we do not work
   around it.

This ADR proposes (1) an ordered acquisition ladder with an adopt/reject
verdict per rung, (2) a terminal classification replacing `enabled`, (3) doctor
integration so triage becomes review-a-diff, and (4) a config shape that does
not break `ConfigProvider` or the v2 DB-backed impl. No behavior changes here;
implementation is the follow-up issue.

## Decision

### 1. The acquisition ladder

A source has an ordered list of acquisition strategies ("rungs"). Ingest tries
them in order and stops at the first that yields parseable feed XML with items.
The rung that served is recorded, so the classification layer (below) can tell
"served from the primary" from "served from a fallback". The default ladder for
every source is implicit (rungs 0 to 2); a source only carries an explicit
`ladder:` when it opts into a higher, non-default rung (3 or 4).

Verdicts:

| # | Rung | Verdict | Runs in |
|---|------|---------|---------|
| 0 | Direct feed, current transport (browser UA + feed `Accept` + seclevel-1 TLS) | **Adopt** | pipeline (fetch); already shipped |
| 1 | Alternative endpoints for the same source (per kind) | **Adopt** | pipeline |
| 2 | Feed autodiscovery via HTML `<link rel="alternate">` | **Adopt** | pipeline |
| 3 | Publisher-own mirrors/alternates (Substack/Medium canonical feeds) | **Adopt** (curated, per-source, not auto-probed) | pipeline |
| 4 | Third-party feed generation (openrss.org, RSSHub) | **Adopt only as explicit per-source opt-in; reject as a default** | pipeline |
| 5 | Archive-based last resort (Wayback / archive.org) | **Reject** | n/a |

**Rung 0 - direct feed, current transport. Adopt.** This is what ships today.
`grepify/ingest/http.py` already fetches through an `ssl.SSLContext` pinned to
security level 1 (legacy ciphers permitted, certificate verification kept fully
on, TLS 1.2 floor), and the RSS fetcher sends a browser User-Agent plus a feed
`Accept` header (#45/#46, #49). Cost: zero beyond a normal fetch. Reliability:
recovers WAF-403 and sslv3 hosts that a bot UA / default OpenSSL rejected.
ToS-clean: a reader UA is not a bypass. It is rung 0 for every source. The fetch
itself only happens in the pipeline (egress).

**Rung 1 - alternative endpoints for the same source. Adopt.** Same publisher,
same content, a different well-known URL for it. No third party, no ToS concern,
cheap (one extra GET only when rung 0 failed). Per kind:

- **reddit**: `.../r/<sub>/new.json` (current canonical) vs `.../r/<sub>/.rss`.
  (Reddit stays best-effort/quiet per `cadence.py`; the ladder does not change
  that - see the classification rules.)
- **youtube**: `feeds/videos.xml?channel_id=` vs `?user=` vs `?playlist_id=` for
  the uploads playlist.
- **rss (WordPress-shaped, the bulk of the registry)**: `/feed/` vs
  `/feed/atom/` vs `/?feed=rss2`, and the trailing-slash variant. Most of the
  disabled feeds below are WordPress `/feed/` paths.

**Rung 2 - feed autodiscovery. Adopt.** Fetch the source's HTML home/blog page
and read `<link rel="alternate" type="application/rss+xml|atom+xml">`. This is
the standard, publisher-sanctioned way to find a feed and is the correct
response to a moved feed (a 404 on the old feed URL where the site still
publishes one). Cost: one HTML GET plus a follow-up feed GET, only on rung-0/1
failure. Pipeline-only (egress). Guardrails: follow at most one discovered
alternate, same registrable domain only (no off-site redirect chasing), and
record the discovered URL as evidence so the human sees what the machine chose.

**Rung 3 - publisher-own mirrors/alternates. Adopt, but curated.** Some
publishers run more than one canonical feed surface: Substack always exposes
`<pub>.substack.com/feed`; Medium exposes `medium.com/feed/@user` and
`/feed/<publication>`. These are the publisher's own infrastructure, so they are
ToS-clean. We adopt them, but as a **per-source, human-recorded** rung, not an
auto-probed one: mirror discovery is heuristic and guessing wrong wastes fetches
and pollutes evidence. When a maintainer knows a source's alternate, they pin it
in `ladder:`; doctor may *suggest* one (e.g. "host is substack.com, try /feed")
but never invents it silently.

**Rung 4 - third-party feed generation (openrss.org, RSSHub). Adopt only as an
explicit per-source opt-in; reject as a default rung.** These regenerate a feed
for a site that blocks or lacks one. The recovery value is real (they are the
standard answer to Cloudflare-fronted Substack/Medium blocks), but each carries
availability and ToS risk that must be stated per the acceptance criteria:

- **openrss.org.** Its Terms of Use (retrieved 2026-07-13 via search; the terms
  page returns 403 to automated fetch, consistent with its own anti-abuse
  posture) restrict use to **non-commercial** purposes, forbid **redistributing**
  its content/service, and **rate-limit unverified** readers, asking clients to
  cap request frequency. Grepify is a personal, non-commercial aggregator, which
  fits the non-commercial clause, but re-publishing openrss-sourced items on a
  public site sits close to the redistribution line, and an unverified server IP
  is subject to strict rate limits. Verdict: permissible for personal use, not
  safe to lean on as a default.
- **RSSHub.** MIT-licensed and self-hostable (Docker/npm). The public
  `rsshub.app` instance is community-run and rate-limited (about 200 req/IP/hr,
  requests logged), so depending on it inherits a third party's uptime and
  policy. A **self-hosted** RSSHub removes the ToS/availability risk but adds an
  always-on service, which contradicts v1's zero-server posture (ADR 0001, PRD
  §5). Verdict: the only clean form is self-hosted, which is out of v1 scope.

So rung 4 is **opt-in per source**, off by default, and self-hosted RSSHub is
the preferred form when a maintainer turns it on. Reliance on public
third-party instances as the standing acquisition path is rejected: it makes the
site's freshness hostage to an external server's rate limiter and blurs the
redistribution line. A source only reaches rung 4 if a human wrote it into that
source's `ladder:`.

**Rung 5 - archive-based last resort (Wayback / archive.org). Reject.**
Archives serve *stale* snapshots, but grepify is a trend-and-digest tool whose
entire value is *fresh* items in a trailing window; resurrecting old items from
an archive would inject dated content into the cloud and digests, which is worse
than the source being absent. It also has its own rate limits and ToS. Most
importantly, a source that exists *only* in an archive is precisely the signal
that the live source is `gone` or `dead` - the right response is to classify it,
not to paper over it. Rejected: not worth having at all.

### 2. Terminal classification (replaces binary enabled/disabled)

Five terminal states. `enabled` becomes a derived convenience, not the source of
truth (see config shape for back-compat).

| Class | Meaning | Fetched? | Stored where |
|-------|---------|----------|--------------|
| `active` | Serving from the primary rung (0) | yes | group file, `status: active` (or default) |
| `degraded` | Serving, but from a fallback rung (1 to 4) | yes | group file, `status: degraded` + resolved rung |
| `paywalled` | Behind a subscription wall; no free path exists and none will be attempted | no | group file, `status: paywalled` + `message` |
| `gone` | Target (page/subreddit/channel) no longer exists | no (removed) | **removed from group**; evidence in git history |
| `dead` | Still exists but unfetchable after the full ladder | no | group file, `status: dead` + `evidence`; slow re-check |

Key distinctions this encodes that the binary could not:

- `degraded` keeps a recovered feed **in the digest** instead of silently
  dropping it, and flags that it is running on a crutch.
- `paywalled` is a deliberate, ToS-respecting terminal: it is not an error to be
  retried, and the config carries a human-readable `message` that also renders
  on the sources page (`grepify/site/templates/sources.html`) so a reader
  understands why the source is silent and that no workaround is coming.
- `gone` is a **removal**, not a disable: the source leaves the group file and
  the reasoning lives in the commit message (git is the audit trail), so the
  registry does not accrete dead weight over time. `url_hash` identity (PRD §6)
  means a future re-add is still recognized as the same source.
- `dead` is the honest "we tried everything, it still will not load" bucket:
  disabled, but re-checked on a slow cadence in case the server is fixed
  (matching the sslv3 hosts, which could recover if the operator upgrades TLS).

**Transition rules.** Transitions are proposed from `fetch_log` evidence
(doctor, below) and applied by a human editing config (v1 non-goal: no
auto-disable, PRD §2). Thresholds reuse the existing streak machinery
(`grepify/health.py`: flag at >= 5 consecutive non-Reddit failures).

- `active -> degraded`: rung 0 fails but a fallback rung serves this run.
- `degraded -> active`: rung 0 serves again.
- `active`/`degraded -> dead`: the **full ladder** fails for >= a dead threshold
  of consecutive runs. Proposed threshold: 16 consecutive failed runs (the
  observed streak of the confirmed-dead #45 feeds), i.e. stricter than the
  flag-at-5 alert, so "flagged" (investigate) and "dead" (give up) stay
  distinct.
- `any -> gone`: a strong non-existence signal across the whole ladder - HTTP
  404/410 on both the feed and the site root, or DNS NXDOMAIN, sustained for >=
  the dead threshold. Distinguished from `dead` by *what* the error is: 404/410
  on the target itself (nothing to fetch) vs a WAF block / TLS failure / 5xx
  (something is there but refuses us).
- `-> paywalled`: **human-only.** Doctor may *hint* a paywalled candidate on
  signals like HTTP 402, or a 200 whose body is a subscribe/login interstitial,
  but classification requires a human because it is a ToS judgment, and getting
  it wrong risks either attempting a wall (bad) or discarding a recoverable feed.
- `dead -> active/degraded`: a slow-cadence re-check (proposed: every 30 days,
  reusing the `cadence.py` "due since last real attempt" mechanism) succeeds.
- **Reddit is exempt from `-> dead`/`-> gone` proposals.** Reddit stays
  best-effort/quiet (`cadence.py`, `IngestSettings.quiet_kinds`); its 429s are
  expected from CI IPs and are not evidence the subreddit is gone. Doctor never
  proposes a terminal-down transition for a `quiet_kinds` source.

### 3. Doctor integration (triage becomes review-a-diff)

`grepify/doctor.py` stays read-only and never writes config (PRD §2). It gains a
proposal layer on top of the existing `DoctorRow` join:

- Each row gains a `proposed_status` and a `reason` (evidence string) computed by
  applying the transition rules above to the source's current class and its
  `fetch_log` streak/error-class/last-status. A row with no crossing proposes
  nothing (`proposed_status = None`).
- A new `--propose` output mode emits a **minimal YAML patch** per group file:
  the exact `status:`/`evidence:` edits (and, for `gone`, the source removals)
  that the evidence justifies. The maintainer reviews that diff, edits config,
  and commits - triage is now "read a proposed diff and accept/adjust" instead
  of "re-derive from raw fetch_log rows". The patch is a suggestion artifact,
  never auto-applied.
- The proposal computation is a pure function of `(current class, HealthSnapshot
  row)`, so it stays deterministic and diffable like the rest of doctor, and it
  runs in the scheduled pipeline's existing `Feed triage report (doctor)` step
  (read-only, no network, no secret - unchanged posture from #39).

This keeps the human in the loop (the ToS/paywall judgment especially) while
removing the from-scratch investigation that #39/#45 had to redo each sweep.

### 4. Config shape (no break to ConfigProvider or the v2 DB impl)

The classification and ladder live in `sources/groups/*.yml` as **additive,
optional** fields on a source. Existing files stay valid.

```yaml
  # active (default): no new fields needed
  - id: simon-willison
    kind: rss
    url: https://simonwillison.net/atom/everything/

  # degraded: serving from a fallback rung
  - id: some-blog
    kind: rss
    url: https://example.com/feed/            # rung 0 (failing)
    status: degraded
    ladder: [direct, alt_endpoint, autodiscovery]
    active_url: https://example.com/blog/atom.xml   # the rung that served
    evidence: "#66 2026-08-01 run <id>: rung 0 HTTP 403; autodiscovery found atom.xml. Recovered."

  # paywalled: labelled, never worked around; message renders on the sources page
  - id: some-wall
    kind: rss
    url: https://example.com/feed
    status: paywalled
    message: "Subscriber-only feed. No free acquisition path; not attempted."

  # dead: unfetchable after the full ladder; slow re-check
  - id: aim-ai
    kind: rss
    url: https://analyticsindiamag.com/ai-news-updates/feed/
    status: dead
    evidence: "#45 2026-07-13 run 20260713T003917Z: unparseable Cloudflare challenge page after browser UA + Accept; full ladder failed. Recheck 30d."

  # gone: NOT represented here - the source is removed from the file entirely;
  # the reason lives in the removing commit's message.
```

Why this does not break either backend:

- **`ConfigProvider` is unchanged at the signature level.** Its abstract methods
  (`grepify/config/provider.py`) return domain models (`Source`, `SourceGroup`);
  no filesystem type is added. We add an optional `status` (+ `evidence`,
  `message`, `ladder`, `active_url`) to `SourceSpec` (`config/schemas.py`) and a
  `status` field to the `Source` domain model. `extra="forbid"` is preserved -
  these are declared fields, not passthrough keys.
- **`enabled` stays, derived.** `status` is authoritative; `enabled` is computed
  (`active`/`degraded` -> enabled; `paywalled`/`dead` -> disabled) so every
  existing reader (ingest, health, site) keeps working untouched. For back-compat
  a file may still carry `enabled:` alone (absent `status`): `enabled: true` maps
  to `active`, `enabled: false` maps to `dead`. `validate` errors only if an
  explicit `status` and an explicit `enabled` disagree, and (offline check) if a
  `dead` source lacks `evidence` or a `paywalled` source lacks a `message`.
- **v2 DB-backed impl.** The `sources` table (PRD §6) already carries `enabled`
  and an opaque `config_json`. The zero-migration option is to fold `status` and
  its evidence into `config_json`, which flows through `ConfigProvider`
  unchanged. But `status` deserves to be queryable (the sources page filters on
  it), so the cleaner path is a nullable `status` column plus `evidence`/`message`
  columns, defaulting from `enabled` for old rows - additive and
  Postgres-swappable, no SQLite-specific types. Because that touches the locked
  §6 schema, it is **proposed as a PRD diff for Kyle in the PR description, not
  applied here.**

## Worked example: the 11 currently disabled feeds

There are 11 source-level `enabled: false` entries today (the "~13" of the issue;
the count is 11 after the #45 closeout). Classified under the new scheme from the
evidence notes already in the YAML and `docs/feed-triage.md`:

| Source | Group | Evidence | New class | Ladder note |
|--------|-------|----------|-----------|-------------|
| `ai-techpark` | ai-business | HTTP 403 persistent after browser UA; server-side WAF/IP block | **dead** | rungs 0-2 cannot beat a WAF; rung 4 (self-host RSSHub) opt-in candidate |
| `aim-ai` | ai-business | Unparseable Cloudflare HTML challenge, not feed XML | **dead** | JS challenge defeats rungs 0-2; rung 4 opt-in candidate |
| `inside-ai-news` | ai-business | sslv3 handshake failure, dead even at seclevel-1 | **dead** | network-level; recheck 30d in case operator fixes TLS |
| `knowtechie-ai` | ai-business | sslv3 handshake failure, dead even at seclevel-1 | **dead** | as above |
| `aimodels` | ai-tooling-dev | Substack HTTP 403 after browser UA | **dead** | Substack -> strong rung 4 (openrss/self-host RSSHub) opt-in candidate |
| `shaip-blog` | ai-tooling-dev | Unparseable HTML challenge page | **dead** | rung 4 opt-in candidate |
| `theodo-data-and-ai-blog` | ai-tooling-dev | Unparseable HTML challenge page | **dead** | rung 4 opt-in candidate |
| `benn-substack` | data-engineering | Substack HTTP 403 after browser UA | **dead** | Substack -> strong rung 4 opt-in candidate |
| `clarifai-blog` | ai-tooling-dev | HTTP 404 (16/16); moved/dead URL, not a WAF | **gone candidate** | a pipeline rung-2 autodiscovery probe on clarifai.com/blog decides: found a live alternate -> `degraded`/`active`; blog root also 404 -> `gone` (remove) |
| `dbt-developer-blog` | data-engineering | UNVERIFIED path; never confirmed live, never fetched | **provisional active (re-probe)** | not dead - enable so the pipeline ladder (rung 2 autodiscovery on docs.getdbt.com) resolves the real feed; classify from first result |
| `snowflake-engineering` | data-engineering | UNVERIFIED path; never confirmed live, never fetched | **provisional active (re-probe)** | as above, autodiscovery on snowflake.com/engineering-blog |

Summary: **8 dead** (WAF-403, Cloudflare-challenge, or sslv3 - all "exists but
refuses us", none are paywalls, so none get worked around), **1 gone candidate**
(`clarifai-blog`, a 404 that one autodiscovery probe will confirm as gone or
recover), **2 provisional-active re-probes** (`dbt-developer-blog`,
`snowflake-engineering` - never actually fetched, so the honest move is to let
the ladder resolve them in the pipeline, not leave them dark). **Zero paywalled**
among the 11: the 403s are bot/WAF blocks, not subscription walls, and we
deliberately do not relabel a WAF as a paywall.

For orientation (not part of the 11): the 3 HTTP-415 flappers
(`artificial-lawyer`, `bdan-ai`, `la-biblia-de-la-ia`) are `active` (they serve
most runs); `ai-time-journal` is `active` with an empty-feed watch note; the ~26
Reddit sources are `active` best-effort and exempt from down-transition
proposals.

## Consequences

- **Triage stops repeating itself.** The reasoning that #39/#45 rebuilt by hand
  each sweep is now encoded (`status` + `evidence`) and proposed as a diff by
  doctor. A sweep becomes: read the pipeline's proposed patch, accept/adjust,
  commit.
- **Recoverable feeds come back without dropping them.** `degraded` keeps a
  fallback-served feed in the digest and marks the crutch; the WAF-blocked
  Substack feeds have a named (opt-in) recovery path instead of a dead end.
- **The registry stops accreting corpses.** `gone` sources are removed, with the
  reasoning in git history, so the file list reflects reality as the registry
  scales (the issue's "foundation for scaling").
- **Readers get an honest sources page.** `paywalled` renders a message; a silent
  source is explained, not mysterious.
- **CI posture is preserved.** All live rungs run only in the scheduled pipeline;
  `validate` stays offline (schema + status/enabled agreement + evidence/message
  presence). No secret is referenced by any PR-triggered path.
- **v2 boundary stays clean.** The change is additive to the config schema and
  domain model; the DB column is proposed, not imposed, and is
  Postgres-swappable.

## Alternatives considered

- **Keep binary `enabled` + richer comments.** This is the status quo and is what
  keeps forcing full re-investigation; comments are not queryable and cannot
  drive the sources page or a doctor diff. Rejected.
- **Auto-disable / auto-classify in the pipeline.** Rejected by PRD §2 (v1 has no
  auto-disable) and because `paywalled`/`gone` are judgment calls with ToS and
  removal consequences. Doctor proposes; a human commits.
- **A separate lifecycle file instead of inline fields.** A second file keyed by
  `source_id` would drift from the group files and duplicate identity. Inline
  keeps one source of truth per source and rides the existing `ConfigProvider`.
- **Public third-party generators as a default rung.** Rejected: openrss.org's
  non-commercial/no-redistribution terms and unverified-reader rate limits, and
  `rsshub.app`'s community-run rate limits, make site freshness hostage to an
  external policy. Self-hosted RSSHub is the only clean form and is opt-in.
- **Archive resurrection.** Rejected wholesale: stale content defeats a
  freshness tool, and archive-only existence is itself the `gone`/`dead` signal.

## Follow-up (implementation issues, scoped from this design)

Proposed split (gated on Kyle's sign-off here):

- **#66 - ladder + classification core.** `status` on `SourceSpec`/`Source` with
  `enabled` derivation and back-compat mapping; ladder execution (rungs 0-2 by
  default, 3-4 opt-in) in the pipeline ingest step; `validate` offline checks;
  sources page renders `paywalled` messages and `degraded` markers. Migrate the
  11 disabled feeds to their classes above; run the two re-probes and the
  `clarifai-blog` autodiscovery probe in one pipeline run and record results.
- **#67 - doctor proposal mode.** `proposed_status`/`reason` on `DoctorRow`, the
  `--propose` YAML-patch output, and the 30-day dead re-check cadence. Reddit
  exemption honored.

## Open questions for Kyle

1. **Dead threshold.** 16 consecutive failed runs (the observed #45 streak) to
   distinguish `dead` from merely flagged-at-5. Right number?
2. **Dead re-check cadence.** 30 days via the existing `cadence.py` mechanism -
   acceptable, or slower/faster?
3. **Rung 4 (third-party generators).** OK to keep it opt-in and self-host-only
   given the openrss.org non-commercial/no-redistribution terms, or reject rung 4
   entirely for v1? This decides whether the 5 WAF/Substack `dead` feeds have any
   sanctioned recovery path.
4. **v2 `status` column.** Adopt the additive nullable `status`/`evidence`/
   `message` columns on `sources` (PRD §6 diff below), or fold status into the
   existing `config_json` to avoid touching the locked schema?
5. **`gone` audit trail.** Git commit message as the sole record of a removal,
   or also keep a short `docs/` ledger of removed sources?

<!-- PRD diff is proposed in the PR description, not applied here (CLAUDE.md: docs/prd.md is source of truth). -->

### #119 provider-aware refinement

The generic RSS ladder is now provider-aware. WordPress-shaped alternates remain
available for generic RSS/WordPress-like feeds, but Substack hosts skip those
rung-1 variants because they are unsupported guesses for that provider. A
Substack source can still recover through the direct feed, same-host homepage
feed autodiscovery, or a human-pinned `active_url` that is passed through the
central outbound URL/redirect policy before any request.

The lifecycle implication is important: runner-specific 403s do not prove that
a publication is dead. Such sources should be `degraded` when they are publicly
live but blocked or served only via fallback. `dead` remains reserved for
full-ladder failures where the source is not recoverable after bounded,
reviewable acquisition attempts.

Reddit stays JSON-first and best-effort. JSON 403 falls through to `.rss`
without retry bursts; 429 is treated as rate limiting, records sanitized
`Retry-After` evidence, and uses bounded provider backoff before trying RSS.
