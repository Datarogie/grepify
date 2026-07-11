# Design proposal: "Your digest" view (#38)

Status: DRAFT for Kyle review, before any implementation. Static SSG (Jinja) +
small vanilla JS, no server, no per-user backend (PRD §5 locked).

## Problem / decision recap
Kyle chose option (c): a dedicated **"your digest"** view built from the topics
you follow, not an ephemeral filter. "Topics" == digest **category**. Today
there are exactly two categories in the data: `ai` and `data-eng` (one weekly
`ai` digest exists too). The set can grow later, so the design must generalize
past two.

## The four design questions

### 1. How is a follow-set chosen and persisted (no server)?
Options: committed config / URL-encoded state / localStorage.
- Committed config is the same for everyone - it is not "yours". Reject.
- localStorage: sticky per device/browser, zero server, the standard static-site
  personalization primitive. Not cross-device by itself.
- URL-encoded (`?topics=ai,data-eng`): stateless, shareable, bookmarkable,
  carries the set across devices via a link. Not sticky on its own.

**Recommendation: localStorage as the primary store, with a URL param as an
override + share mechanism.** Reader toggles topic chips -> saved to
localStorage (`grepify.followed_topics`). A `?topics=...` in the URL, if
present, wins for that visit and can be re-saved. Default when nothing is
followed: show ALL topics (identical to today's behavior, no regression).
Cross-device expectation stated plainly in the UI: "follows are saved on this
device; use Share to carry them elsewhere."

### 2. What does "your digest" render?
The digest index list, filtered to the followed categories, merged across them,
ordered newest -> oldest by **period** (consistent with #37), reusing the exact
row markup of the index (title, `kind · category · period`, keyword chips). The
existing daily/weekly kind filter still applies within it.
- Empty state A (follows chosen, but no digests in them yet): friendly "No
  digests in your topics yet" with a link to browse all.
- Empty state B (nothing followed): treat as "all" (or a one-time nudge to pick
  topics). No dead end.

### 3. Navigation / IA - new page, default landing, or a mode?
Two viable shapes; both are genuinely a dedicated, persisted view (satisfying
option c, since what makes it "c" not "a" is the persisted follow-set + being a
first-class destination, not a bespoke server render):

- **IA-1 (recommended, leanest):** a first-class **"Your digest" nav item** ->
  its own page `digest/yours/` that is SERVER-rendered with ALL digests
  (newest-first by period) and progressively enhanced by JS to (a) hide rows
  whose category is not followed and (b) show topic-follow chips + a Share link.
  With JS off it gracefully degrades to "all digests, newest first". Reuses the
  index row partial; one new thin page; snapshot test covers the server-rendered
  (all-topics) baseline. Digests page stays the full archive; Home unchanged.

- **IA-2 (heavier):** a fully separate template + render path + its own data
  shaping and larger snapshot surface. More code/tests for little gain given the
  row markup is identical to the index.

**Recommendation: IA-1.** Optionally also add the topic chips to the existing
Digests index (so the archive is filterable by topic too, closing the "category
present but not filterable" gap the issue notes) - low marginal cost since the
JS + chip markup already exist. Keep it "nice, not bloaty": no framework, one
small JS file extending `digests.js`.

### 4. Consistency with #37
"Your digest" and the index share the same newest-first-by-period ordering from
`all_digests()` (#37). The filter only hides rows client-side; it never
reorders, so the two stay consistent by construction. Land #37 first (in
flight), build #38 on top.

## Rough build shape (if approved)
- `grepify/site/templates/digest_yours.html` (extends base; reuses index row
  markup, ideally via a shared `{% macro %}` or `{% include %}` so the row is
  defined once).
- Extend `grepify/site/static/digests.js` (or a small `your-digest.js`): read/
  write `localStorage` follow-set, honor `?topics=`, render topic chips, filter
  rows by `data-category`, Share-link button. Add `data-category="{{ d.category }}"`
  to the index rows (needed for filtering; also enables topic filter on the index).
- Nav link "Your digest" in `grepify/site/render.py` nav list.
- `build.py`: write `digest/yours/index.html` (server-rendered, all topics).
- Tests: byte-stable snapshot of the server-rendered page (all-topics baseline);
  determinism; the JS filter behavior is client-only (documented, mirrors how the
  existing kind filter is handled - server render is the tested surface).
- Docstring failure modes; `make check` green.

## KYLE DECISIONS (2026-07-11, review answered)
- Persistence: **localStorage for now.** Longer-term Kyle wants profiles that save
  settings/setup and manage notifications etc. -> design the follow-set store so a
  future "profile" layer can supersede localStorage without a rewrite (keep the
  read/write behind one small accessor; do NOT build profiles/notifications now -
  out of scope for #38, likely a future epic).
- IA: **dedicated "Your digest" page + add the topic filter to the main Digests
  index too** (IA-1, recommended). Build both surfaces.

## Open questions for Kyle (RESOLVED above)
Q1. Persistence: localStorage-primary + URL-share override (recommended), or a
    different split (e.g. URL-only, or also remember on the index)?
Q2. IA: dedicated "Your digest" page as a new nav destination (IA-1,
    recommended), and do you also want the topic filter added to the main
    Digests index? Or keep topic-following ONLY on the dedicated page?
Everything else (ordering, empty states, no-framework, graceful degradation)
follows the recommendation unless you say otherwise.
