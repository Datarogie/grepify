# Eval candidate set (GRP-24, PRD §10.5)

`keyword_eval_candidates.jsonl` is the 30-item labeled set the eval harness
(`make eval`) scores the current extraction output against. It is a **fixed
test fixture, not pipeline truth** — pulled once from real ingested items on
the `data` branch, committed here, and never regenerated automatically.

## Format

One JSON object per line:

```json
{"item_id": "...", "title": "...", "summary": "...", "expected_keywords": []}
```

- `item_id` and `title` are copied verbatim from real ingested items on the
  `data` branch. `summary` is the real ingested summary with HTML tags
  stripped, whitespace collapsed, and **platform/publisher boilerplate
  removed** (WordPress "The post ... appeared first on ..." footers, "Read
  More" stubs, Reddit's "submitted by /u/user [link] [comments]" footer,
  YouTube channel-promo blocks — social links, sponsor plugs, "Subscribe..."
  CTAs, trailing hashtag spam) — none of that is part of what the item is
  actually *about*, and leaving it in would let it leak into labels (an
  earlier draft of item 23 below picked up "sqlite" as a keyword purely
  because a sponsor plug mentioned it). Two items (18, 23) turned out to
  have *nothing but* boilerplate in their real summary; those have
  `summary: null` and are labeled from the title alone. Production
  extraction (`grepify/extract/prompt.py`) sends the raw, uncleaned summary
  (capped at 500 chars) to the LLM, so this fixture is a close proxy for
  real extraction input on the substance, not a byte-exact copy — a heavily
  boilerplate-laden item could score slightly differently here than in a
  real pipeline run for that reason.
- `expected_keywords` currently holds a **first-pass draft**, written by the
  agent (not the LLM under test) reading each title/summary — a starting
  point for the manual labeling task (playbook S7k), not a finished label
  set. Review and edit every item; anything left as drafted should be a
  deliberate agreement, not an unreviewed default. Keep entries lowercase,
  2-8 per item, no punctuation — matching
  `grepify.keywords.normalize_keyword`'s output shape (the scorer normalizes
  both sides before comparing, so exact casing/whitespace doesn't matter, but
  matching the convention keeps diffs clean).
- An item's `expected_keywords` can be reset to `[]` if you'd rather label it
  from scratch — `make eval` skips `[]` items when computing the mean score
  (but still shows their predicted keywords). Known limitation: `[]` is the
  *only* sentinel for "not labeled," so there's no way to positively label
  "this item should legitimately have zero keywords" — give it at least one
  keyword even if extraction should find little.
- Bare years (`"2025"`, `"2026"`) are deliberately **not** used as keywords,
  even when a year appears in the title/summary — `published_at` and the
  trend timeline (E3/E4) already carry when something happened, so a year
  isn't a topical keyword in the way a keyword-cloud entry needs to be; it'd
  just be redundant noise. If a year is genuinely the subject (e.g. an
  item's whole point is a 2025-vs-2026 comparison), prefer a keyword that
  names *what's* being compared over the years themselves.

## Labeling from a phone

Open this file in the GitHub app's editor (or any plain-text editor) on a
branch, tap into a line, and edit the list after `"expected_keywords":`,
e.g.:

```json
{"item_id": "abc123...", "title": "...", "summary": "...", "expected_keywords": ["chatgpt", "openai", "policy"]}
```

Each line must stay valid JSON (double quotes, comma-separated strings, no
trailing comma). Commit + push; no other file needs to change.

## Running the harness

```
LLM_BASE_URL=... LLM_API_KEY=... make eval
```

Prints a Markdown report (mean jaccard + per-item predicted/expected) to
paste into the MR description whenever the extract prompt or LLM profile
changes (PRD §10.5). Not part of `make check` or any CI workflow — manual
and offline by design.
