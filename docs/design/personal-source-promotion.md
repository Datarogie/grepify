# Design proposal: personal sources to global promotion (#67)

Status: PROPOSED (v2-facing, design-only). Awaiting Kyle's review; nothing here
changes v1 behavior, and the PRD §15 rows are proposed in the PR description, not
applied. Depends on the classification vocabulary shipped in ADR 0002 (source
acquisition ladder + `SourceStatus`).

## Problem / decision recap

Kyle's direction (2026-07-13): once grepify opens to multiple users (v2,
remote-labs, PRD §15), a personal source that enough distinct users add on their
own should be **silently promoted** to the global/shared registry so the whole
community benefits without a curator touching it. Two things have to be true for
that to be safe:

1. The add-source flow must set honest expectations at add time - a paywalled or
   dead feed is named as such up front, never silently accepted and left to fail
   (Kyle's standing rule: paywalled sources get an honest message, never a
   workaround).
2. A shared registry needs guards so one person (or a bot) cannot inject junk
   into everyone's corpus by adding it under several accounts.

The architecture is already shaped for this. `url_hash` is the canonical feed
identity (PRD §6, `sources.url_hash` unique; `SourceSpec.url_hash` in
`grepify/config/schemas.py`), PRD §15 already says a user adding an existing
source "creates a subscription row, never a re-fetch or re-extraction", and ADR
0002 shipped the five-state lifecycle (`active` / `degraded` / `paywalled` /
`gone` / `dead`) plus the doctor-proposes/human-commits recheck machinery. This
design extends those, it does not invent a parallel system.

**Scope guard.** This is v2 territory. Digests stay category-keyed (PRD non-goal
stands); no per-user auth model beyond what promotion needs; no implementation.
The v1-now vs v2-later split (§4) is explicit that nothing in v1 changes now.

## 1. Promotion mechanics

### 1.1 Identity and subscriber count

`url_hash` is the join key. Two users adding the same feed resolve to one
`sources` row (the §6 unique constraint guarantees this); each add is a
**subscription** row `(user_id, url_hash, added_at)`, never a second source and
never a re-fetch. The per-source subscriber count is therefore just
`count(distinct user_id) where url_hash = ?` - no denormalized counter needed at
the scale v2 starts at, though a cached count column is a trivial later
optimization.

"Distinct" means distinct **verified** user identity (§3), not distinct add
events: adding, unsubscribing, and re-adding does not inflate the count.

### 1.2 Promotion threshold X (config)

A personal source is promoted to global when **`count(distinct subscribers) >=
X`** and the source clears the abuse/quality gates in §3.

**Recommended X = 3.** Reasons: (a) it matches the repo's existing "3 is the
floor for a thing to be real" convention (`keyword_min_mentions = 3`,
`rising_min_count = 3` in `grepify/config/schemas.py`), so it reads as
consistent; (b) 2 is too easy to reach as one person plus a friend, or via two
throwaway accounts; (c) at the single-digit user counts v2 starts with, 3 is
reachable for genuinely shared interest without being a wall. X lives in config
(proposed `promotion.min_subscribers`, v2-only settings block, §4) so Kyle can
retune it as the user base grows - it should scale up, not down, with headcount.

Promotion is evaluated in the pipeline (the same place ingest/classification
already run with egress), not synchronously on the add - it is a periodic sweep
over subscriber counts, so a source crosses the line on the next run after it
qualifies, not mid-request.

### 1.3 What "silently move to global" means, concretely

Three things change and one thing deliberately does not.

- **Visibility.** The source leaves the adder's personal namespace and joins the
  shared `sources` registry: it appears on the global sources page, is ingested
  **once globally** (not per-subscriber - ingestion/extraction/trends stay
  global per PRD §15), and its items enter the shared corpus and are searchable
  by everyone. "Silent" = no curator action was required and no user is
  notified; it is not a hidden state.
- **Curation ownership transfers to the registry.** Before promotion the source
  is the adder's - they can remove it from their own list freely. After
  promotion it is a shared/curated source like a seeded group source: the
  original adder no longer solely owns it and cannot unilaterally delete it for
  everyone. Its lifecycle is now driven by the registry's doctor/recheck
  machinery (§1.4), and unsubscribing only drops that user's subscription row,
  it does not remove the global source.
- **Category assignment is NOT automatic - it is curator-gated.** This is the
  one deliberate brake. A newly promoted source enters an **uncategorized
  (pending)** holding state: its items are in the corpus and searchable, but it
  belongs to no `category`, so **it contributes to no category digest** until a
  curator assigns one. This is what protects the category-keyed digest non-goal:
  promotion cannot inject a source into the `ai` or `data-eng` digests without a
  human deciding it belongs there. Silent promotion buys shared *ingestion and
  visibility*; it does not buy a seat in a digest.

The thing that does not change: **digests stay category-keyed and per-category,
never per-user** (PRD §15). Promotion feeds the corpus, category assignment
feeds the digests, and the two stay separate.

### 1.4 Demotion / removal when a promoted source dies

A promoted source is an ordinary source in the shared registry, so it dies the
ordinary way - there is no promotion-specific death path. It rides ADR 0002
exactly:

- `active -> degraded` if it starts serving from a fallback rung; it stays in
  the corpus and (if categorized) its digest, flagged as running on a crutch.
- `active`/`degraded -> dead` when the full ladder fails for the dead threshold
  (ADR 0002 proposed 16 consecutive runs); disabled, slow re-check every ~30
  days in case the operator fixes it.
- `any -> gone` on a strong non-existence signal (404/410 + NXDOMAIN); removed
  from the registry, reasoning in the removing commit (git is the audit trail).
  `url_hash` identity means a future re-add is recognized as the same source and
  can re-accumulate subscribers.
- Every transition is **proposed by doctor from `fetch_log` evidence and applied
  by a human** (the `--propose` YAML-patch flow shipped in ADR 0002 §3); v2's
  DB-backed curator UI is the same propose-then-confirm shape, just rendered
  instead of a YAML diff. No auto-disable (PRD §2).

So promotion is silent and symmetric: a source that the crowd promoted, and then
that dies, is demoted/removed by the same machinery that handles every other
source. The paywall judgment stays human-only, as ADR 0002 requires.

**Demotion on subscriber drop-off is deliberately NOT wired.** Once a source is
global its items are already in the shared corpus and may anchor trends and
digests; yanking it because subscribers fell back under X would orphan corpus
data and thrash the registry. Promotion is **sticky**: only lifecycle death
(`dead`/`gone`) removes a promoted source, never a headcount dip. (Open question
Q3 revisits this.)

## 2. Add-source UX contract

The contract is the same in both eras: **the user learns the source's
acquisition class at add time, in the ADR 0002 vocabulary, before committing to
it.** Only the surface differs.

### 2.1 Classification feedback (shared vocabulary)

| Class | Message shown at add time | Added? |
|-------|---------------------------|--------|
| `active` | "Reachable. Serving directly." | yes |
| `degraded` | "Reachable via fallback (rung N: alt endpoint / autodiscovery / mirror). Kept, running on a fallback." | yes, flagged |
| `paywalled` | The honest message: "Subscriber-only feed. There is no free acquisition path and none will be attempted." No add, no workaround offered. | no |
| `gone` | "This feed/page no longer exists (404/410 or DNS). Nothing to subscribe to." | no |
| `dead` | "Reachable target but unfetchable after every path we try (WAF/TLS/challenge). Not added; you can retry later." | no (retryable) |

The `paywalled` and `dead` rows are the point of the whole contract: instead of
accepting a source and letting the user watch it silently produce nothing, the
add flow tells them why up front. Paywalled gets Kyle's honest message and stops
there - no third-party-generator workaround is surfaced to the user (rung 4 stays
a maintainer-only opt-in per ADR 0002, never presented as a "we can get around
the wall" button).

### 2.2 v1 surface: `grepify validate` (already the shape)

In v1 the "add flow" is editing a group file and running `grepify validate` /
`grepify doctor`. `validate` is offline (CI egress to feed hosts is blocked, ADR
0002), so it confirms schema, locator, and status/evidence coherence but cannot
reach the host; the live classification lands from the scheduled pipeline's
ladder run and shows up as the source's `status` on the sources page (which
already renders `degraded (via fallback)`, `paywalled` messages, and `dead`
evidence - see `grepify/site/templates/sources.html`). `doctor --propose` then
proposes any transition. This is the v1 realization of the contract and it needs
no change.

### 2.3 v2 surface: the checkbox UI add flow

In v2 the same classification runs server-side when a user pastes a URL in the
checkbox UI (PRD §15 "checkbox UI over the same curated group files"). The UI
resolves `url_hash`, and:

- If the source already exists globally: one-click subscribe, show its current
  class (and note if it is `degraded`).
- If it is new: run the ladder, then show the §2.1 message inline. `active`/
  `degraded` -> "Added to your sources." `paywalled`/`gone`/`dead` -> the honest
  message and no subscription created. The user is never left with a silently
  failing source in their list.

Because the classification is a server call that can reach hosts, v2 can give the
verdict synchronously at add time - the one capability v1's blocked-egress
`validate` cannot, and the reason this half of the contract waits for v2.

## 3. Abuse / quality guards for a shared registry

A source must clear **all** of these before the promotion sweep will move it
global. They compose; none alone is sufficient.

| Guard | Recommended value | Why |
|-------|-------------------|-----|
| Minimum distinct subscribers | **X = 3 verified users** (§1.2) | Real shared interest, not one person; distinct verified identity, not distinct add events. |
| Minimum source age | **14 days since first subscription** | A brand-new feed cannot promote same-day; gives a spam feed time to be reported and a real feed time to prove it keeps producing. |
| Proven acquirability | **must be `active` or `degraded`, with >= 1 successful fetch on record** | A source that has never actually served items cannot be promoted; `paywalled`/`dead`/`gone` are ineligible by construction (they were never added, §2). Reuses the shipped lifecycle - no new signal. |
| Curator approval for category | **required before it enters any digest** (§1.3) | The category-keyed digest non-goal's guard: the crowd can promote a source into the corpus, only a curator can promote it into a digest. |
| Per-user promotion influence cap | **one account = one subscriber toward X** | Blunts sockpuppets: the count is `distinct verified user_id`, so N adds from one identity count once. Verification strength is the real lever (Q4). |

Spam/junk resistance, layered:

- **Age + acquirability + subscriber count together** mean a junk feed has to be
  reachable, stay reachable for two weeks, and be independently added by three
  verified people before it can reach the corpus - a high bar for a spammer to
  fake without real accounts.
- **Curator category gate** means even a promoted junk source produces nothing
  in a digest; the blast radius of a bad promotion is "it is searchable in the
  corpus", not "it is in everyone's morning digest".
- **Doctor lifecycle** cleans up after the fact: a promoted source that turns out
  to be dead/gone is removed by the existing recheck machinery.
- A lightweight **report/flag affordance** (a user marks a global source as
  junk) can feed a curator's review queue; specified as a v2 nicety, not a
  blocker, so it is not over-built now.

## 4. v1-now vs v2-later

**Explicitly: nothing in v1 needs to change now.** The cheap future-proofing is
already in the tree; everything else is server-side and waits for the v2 gate
(PRD §15: v2 starts only after v1 runs reliably for Kyle).

### Already done in v1 (the cheap future-proofing, no change needed)

- **`url_hash` canonical identity + unique constraint** (PRD §6;
  `SourceSpec.canonical_url` / `url_hash` in `grepify/config/schemas.py`). This
  is the entire foundation of the subscription/promotion model: "same feed = one
  source" is already true, so v2's subscription rows and subscriber count join on
  a key that exists today. Keeping this discipline (every kind resolves to one
  canonical URL that hashes stably) is the single most important v1 habit to
  preserve.
- **`added_at`** on the source (PRD §6, `SourceSpec.added_at`) - the timestamp
  the minimum-age guard needs.
- **The shipped lifecycle fields** (`SourceStatus`, `status`/`evidence`/
  `message`/`ladder`/`active_url`, `Rung`) - the add-source UX vocabulary and the
  promoted-source death path are both just this, reused. The classification the
  add flow reports is the classification the pipeline already computes.
- **`config_json` passthrough** (PRD §6) - room for per-source promotion metadata
  later without a schema change.

### Waits for v2 (server-side, gated)

- The **users / subscriptions tables** and any auth/identity (Okta per §15) -
  there is no user model in v1, so there is nothing to count.
- The **subscriber count + promotion sweep** (periodic job comparing counts to
  X, applying the §3 gates).
- The **`promotion` settings block** (`min_subscribers`, `min_age_days`) - a v2
  addition to `SettingsConfig`; deliberately **not** added to
  `grepify/config/schemas.py` now, since v1 has no promotion to configure.
- The **curator category-assignment queue** and the **report/flag** affordance.
- The **synchronous add-time classification in the checkbox UI** - needs the
  server that can reach hosts on request (v1's `validate` is blocked-egress and
  offline by design).

## Open questions for Kyle

1. **Promotion threshold X.** Recommended **3** (matches the repo's `min = 3`
   convention, resists two-account promotion, reachable at low user counts).
   Right number, and should it be a fixed count or a fraction of active users as
   the base grows? *Recommendation: fixed count now, revisit as a fraction only
   if the user base gets large.*
2. **Minimum source age.** Recommended **14 days since first subscription**.
   Right window, or tie it to "N successful fetches on record" instead of / in
   addition to wall-clock age? *Recommendation: keep wall-clock 14d AND the
   >= 1-successful-fetch acquirability gate; they guard different failure modes.*
3. **Sticky vs demotable promotion.** Recommended **sticky**: only lifecycle
   death (`dead`/`gone`) removes a promoted source, never a subscriber drop-off,
   to avoid orphaning corpus/trend data. Agree, or do you want demotion back to
   personal if subscribers fall under X for a sustained period? *Recommendation:
   sticky.*
4. **Subscriber verification strength.** The whole abuse model rests on "distinct
   verified user". How strong is verification in v2 - Okta-backed accounts only,
   or is a lighter sign-in acceptable (which weakens the sockpuppet guard and
   argues for a higher X)? *Recommendation: gate promotion counting on
   Okta-backed identity; lighter tiers can browse/subscribe but do not count
   toward X.*
5. **Curator for category assignment.** Who is the curator in v2 - just Kyle, or
   a small trusted set? This sets how fast a promoted source can earn a category
   (and thus a digest seat). *Recommendation: Kyle-only at v2 launch; widen only
   if the review queue becomes a bottleneck.*
