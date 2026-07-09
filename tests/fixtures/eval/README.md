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

- `item_id` / `title` / `summary` are copied verbatim from real ingested
  items — this is what `title`+`summary` extraction actually sees (PRD
  §8 F-EXT-01).
- `expected_keywords` is **empty on purpose**. That's the manual labeling
  task (playbook S7k): fill each item's list with the keywords a human would
  expect the extractor to find, e.g. `["genai", "anthropic", "policy"]`.
  Keep them lowercase, 2-8 per item, no punctuation — matching
  `grepify.keywords.normalize_keyword`'s output shape (the scorer normalizes
  both sides before comparing, so exact casing/whitespace doesn't matter, but
  matching the convention keeps diffs clean).
- Items can be left with `expected_keywords: []` indefinitely — `make eval`
  skips unlabeled items when computing the mean score (but still shows their
  predicted keywords, so you can see what the model currently guesses while
  you decide on labels).

## Labeling from a phone

Open this file in the GitHub app's editor (or any plain-text editor) on a
branch, tap into a line, and replace the `[]` after `"expected_keywords":`
with a real list, e.g.:

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
