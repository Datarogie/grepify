# Data-branch size guardrail (#62, GRP-63)

The `data` branch is append-only JSONL truth by design (PRD §5) - nothing
prunes it. This is the guardrail that measures it every pipeline run so
unbounded growth is visible before it becomes an incident, plus the documented
escape hatch for what to do if the fail threshold is ever actually hit.

> This is a standalone doc because docs/architecture.md is being edited by
> another in-flight stream and docs/runbook.md (GRP-61) does not exist yet on
> this branch. Fold this file into docs/runbook.md once GRP-61 lands - see
> that issue.

## What is measured

`grepify datasize` (`grepify/datasize.py`) sums three directories under the
data root:

- `items/` - JSONL item truth, partitioned `YYYY/MM/DD.jsonl`
- `keywords/` - JSONL keyword truth, same partitioning
- `transcripts/` - E5 compressed transcript blobs

`digests/`, `logs/`, `runs/`, `health.json`, and `grepify.db` (the gitignored,
never-committed SQLite cache) are excluded - see the module docstring for why.
"MB" means 2**20 bytes (mebibytes) throughout, not the SI decimal megabyte.

## Thresholds

| Total size | Exit code | Behavior |
|---|---|---|
| < 100 MB | 0 | one info line, no annotation |
| 100 MB - 200 MB | 0 | one `WARN:` line - visible, does not fail the run |
| >= 200 MB | non-zero | fails the guardrail step (and the pipeline run) |

`make datasize` wraps `grepify datasize` (default thresholds); both
`.github/workflows/pipeline.yml` and `.gitlab-ci.yml` call the same target
right after checking out the `data` branch worktree, before ingest/extract
spend any network or LLM budget on a run that is about to fail anyway.

- **GitHub**: the guardrail step tees its output line into
  `$GITHUB_STEP_SUMMARY` (with `set -o pipefail`, so a `tee` success does not
  mask a failing exit code) - the current size is visible on every run's
  summary page regardless of outcome.
- **GitLab**: same `make datasize` call in the `pages` job. GitLab has no
  per-step job-summary UI equivalent to GitHub's, so the line lands in the
  plain job log instead - the logic lives in the make target either way
  (F-OPS-03 portability).

## If a run actually fails (>= 200 MB)

This has not happened yet. When it does, the pipeline stops before
ingest/extract/digest for that run (data keeps accumulating from the last
successful run, nothing is lost) and the fix is a deliberate, one-off
compaction migration, not automatic pruning (PRD §2 rules out silent
retention changes in v1):

1. Pick a cutoff date old enough that nothing reads that far back row-by-row -
   the trailing-90d items browser and the digest daily/weekly lookback windows
   are the only per-row readers; anything older is safe to compact.
2. Compact `items/YYYY/MM/DD.jsonl` and `keywords/YYYY/MM/DD.jsonl` partitions
   older than the cutoff into columnar, compressed Parquet files (e.g. one per
   month), and remove the compacted JSONL partitions from the `data` branch in
   the same commit.
3. Point the cache rebuild (`JsonlSqliteRepository`) at "recent JSONL +
   archived Parquet" so trend/digest queries keep seeing full history - the
   `Repository` interface (PRD §5 v2-proofing) already hides the storage shape
   from every caller, so this is an implementation change behind that
   interface, not a pipeline or schema change.
4. Compact `transcripts/` the same way: older blobs move to a Parquet (or
   plain compressed archive) sidecar keyed by `item_id`.

No parquet code exists yet - this is the plan to execute if/when a real `fail`
is hit, tracked as a future issue against this doc (and, per PRD §5, the same
compaction step doubles as prep for the eventual v1 -> v2 Postgres migration:
`COPY FROM` still works over the recent-JSONL + archived-Parquet split).

## Running it locally

```
git fetch origin data
git worktree add --detach ./_data origin/data
uv run grepify --data-root ./_data datasize
git worktree remove ./_data
```

## Tests

Threshold math (`classify_size`) and directory summation
(`compute_data_size`) are unit-tested against fixture directory trees in
`tests/test_datasize.py` - under/warn-band/over, plus the missing-directory
and per-directory-breakdown cases. `tests/test_cli.py` covers the CLI wiring
(exit codes, output format) end to end via `CliRunner`.
