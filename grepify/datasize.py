"""Data-branch size guardrail (GRP-63): warn at 100 MB, fail at 200 MB.

The `data` branch is append-only by design (PRD §5 - JSONL truth, readable
diffs, no pruning in v1) so nothing about the pipeline itself stops it growing
forever. This module sums the three directories the issue scopes the guardrail
to - `items/`, `keywords/` (the JSONL truth partitions,
see :class:`grepify.paths.DataLayout`) and `transcripts/` (E5 compressed
blobs) - and classifies the total against two thresholds. `digests/`,
`logs/`, `runs/`, and `health.json` are excluded: they are small and not the
source of unbounded growth (PRD §7 - metadata-only rows, capped transcripts).
`grepify.db` is excluded too - it is the gitignored SQLite cache, never
committed, so it is not part of the branch's on-disk (in-git) footprint.

"MB" throughout means 2**20 bytes (mebibytes) - the informal but common
convention for file-size limits (e.g. GitHub's own "100 MiB" push warning),
not the SI decimal megabyte. The literal default thresholds are
``100 * 1024 * 1024`` and ``200 * 1024 * 1024`` bytes.

Escape hatch: parquet compaction
---------------------------------
This module only measures and classifies; it does not compact or prune
anything (issue #62 non-scope). If a run ever reports ``fail`` (>= 200 MB),
the documented path out - not yet implemented - is:

1. Pick a cutoff date old enough that its digests/trend windows have already
   rolled past it (the trailing-90d items browser and the digest lookback
   windows are the only readers of raw JSONL rows; anything older is only
   ever read in bulk, never row-by-row).
2. Compact the `items/YYYY/MM/DD.jsonl` and `keywords/YYYY/MM/DD.jsonl`
   partitions older than the cutoff into columnar, compressed Parquet files
   (e.g. one file per month) written alongside the JSONL, then remove the
   compacted JSONL partitions from the `data` branch in the same commit.
3. Point `JsonlSqliteRepository`'s cache rebuild at "recent JSONL + archived
   Parquet" so trend/digest queries keep seeing full history - the `Repository`
   interface (PRD §5 v2-proofing) already hides the storage shape from every
   caller, so this is an implementation change behind that interface, not a
   pipeline or schema change.
4. Transcripts compact the same way: older blobs move to a Parquet (or plain
   compressed archive) sidecar, keyed by `item_id`, instead of one file per
   video under `transcripts/`.

This keeps the escape hatch a size-triggered, deliberate one-off migration
(same posture as the v1->v2 Postgres migration) rather than automatic silent
pruning, which PRD §2 rules out as a v1 non-goal. No parquet code exists yet -
this docstring and docs/data-size.md are the plan to execute if/when a real
`fail` is ever hit; docs/data-size.md notes it should fold into
docs/runbook.md (GRP-61) once that exists.

Failure modes
-------------
:func:`compute_data_size` never raises for an absent directory (e.g. a fresh
`data` branch with no `transcripts/` yet) - it treats a missing directory as
zero bytes, same posture as the rest of the pipeline treating "not yet
populated" as a normal state rather than an error. It also never raises for a
file that vanishes mid-walk (e.g. a concurrent process) - a
``FileNotFoundError`` on ``stat()`` is treated as zero bytes for that file
rather than failing the whole guardrail over a benign race. :func:`classify_size`
is pure arithmetic over its three integer arguments and cannot raise. Neither
function does any git or network I/O - the guardrail is a plain filesystem
read over whatever `--data-root` already points at (the pipeline's `data`
worktree in production), so it fails loudly only via its exit code (wired in
:mod:`grepify.cli`), never a raised exception the caller has to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from grepify.paths import DataLayout

BYTES_PER_MB = 1024 * 1024
DEFAULT_WARN_BYTES = 100 * BYTES_PER_MB
DEFAULT_FAIL_BYTES = 200 * BYTES_PER_MB


class SizeLevel(StrEnum):
    """Guardrail verdict for a total byte count against the warn/fail thresholds."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DataSizeReport:
    """Per-directory byte counts plus the thresholds they were classified against."""

    items_bytes: int
    keywords_bytes: int
    transcripts_bytes: int
    warn_bytes: int = DEFAULT_WARN_BYTES
    fail_bytes: int = DEFAULT_FAIL_BYTES

    @property
    def total_bytes(self) -> int:
        return self.items_bytes + self.keywords_bytes + self.transcripts_bytes

    @property
    def level(self) -> SizeLevel:
        return classify_size(self.total_bytes, self.warn_bytes, self.fail_bytes)


def classify_size(
    total_bytes: int,
    warn_bytes: int = DEFAULT_WARN_BYTES,
    fail_bytes: int = DEFAULT_FAIL_BYTES,
) -> SizeLevel:
    """Classify ``total_bytes`` against the warn/fail thresholds.

    ``ok`` below ``warn_bytes``, ``warn`` in ``[warn_bytes, fail_bytes)``,
    ``fail`` at/over ``fail_bytes``. Kept separate from any directory-walking
    I/O so the threshold math is trivially unit-testable with plain integers.
    """
    if total_bytes >= fail_bytes:
        return SizeLevel.FAIL
    if total_bytes >= warn_bytes:
        return SizeLevel.WARN
    return SizeLevel.OK


def _dir_size_bytes(path: Path) -> int:
    """Recursively sum file sizes under ``path``; a missing directory is 0 bytes."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except FileNotFoundError:
            # Benign race (e.g. a concurrent writer) - skip rather than fail
            # the whole guardrail over one vanished file.
            continue
    return total


def compute_data_size(
    layout: DataLayout,
    *,
    warn_bytes: int = DEFAULT_WARN_BYTES,
    fail_bytes: int = DEFAULT_FAIL_BYTES,
) -> DataSizeReport:
    """Sum ``items/`` + ``keywords/`` JSONL plus ``transcripts/`` under ``layout``.

    Scoped to exactly those three directories per issue #62 - see the module
    docstring for why the rest of the data root (cache, logs, digests, runs)
    is excluded.
    """
    return DataSizeReport(
        items_bytes=_dir_size_bytes(layout.items_dir),
        keywords_bytes=_dir_size_bytes(layout.keywords_dir),
        transcripts_bytes=_dir_size_bytes(layout.transcripts_dir),
        warn_bytes=warn_bytes,
        fail_bytes=fail_bytes,
    )


def format_report(report: DataSizeReport) -> str:
    """One deterministic summary line - safe to print to a terminal or append to
    ``$GITHUB_STEP_SUMMARY``. Carries a ``WARN``/``FAIL`` prefix at those levels
    so the line reads as an annotation even outside a CI UI that highlights it."""
    prefix = {
        SizeLevel.OK: "data size",
        SizeLevel.WARN: "WARN: data size",
        SizeLevel.FAIL: "FAIL: data size",
    }[report.level]
    total_mb = report.total_bytes / BYTES_PER_MB
    items_mb = report.items_bytes / BYTES_PER_MB
    keywords_mb = report.keywords_bytes / BYTES_PER_MB
    transcripts_mb = report.transcripts_bytes / BYTES_PER_MB
    warn_mb = report.warn_bytes / BYTES_PER_MB
    fail_mb = report.fail_bytes / BYTES_PER_MB
    return (
        f"{prefix}: {total_mb:.1f} MB "
        f"(items {items_mb:.1f} MB, keywords {keywords_mb:.1f} MB, "
        f"transcripts {transcripts_mb:.1f} MB) - warn>={warn_mb:.0f} MB fail>={fail_mb:.0f} MB"
    )
