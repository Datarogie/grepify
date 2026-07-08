"""Data-directory layout.

Single source of the on-disk layout so the repository impl and any tooling agree
on where truth lives (PRD §5). Truth is append-only JSONL partitioned by date;
the SQLite cache is a single file that is never committed.

Layout::

    data/
      items/YYYY/MM/DD.jsonl        # by published_at date
      keywords/YYYY/MM/DD.jsonl     # by extracted_at date
      digests/<digest_id>.json      # one file per digest
      logs/fetch/YYYY/MM/DD.jsonl   # by started_at date
      logs/llm/YYYY/MM/DD.jsonl     # by created_at date
      transcripts/                  # compressed blobs (E5)
      runs/<run_id>.json            # run manifests
      health.json                   # per-source health snapshot (GRP-16)
      grepify.db                    # derived cache (gitignored)

Failure modes
-------------
Pure path arithmetic — no I/O, so nothing here raises for missing directories.
A malformed ISO date string raises ``ValueError`` from :func:`date_parts`.
"""

from __future__ import annotations

from pathlib import Path


class DataLayout:
    """Resolves data paths relative to a data root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def items_dir(self) -> Path:
        return self.root / "items"

    @property
    def keywords_dir(self) -> Path:
        return self.root / "keywords"

    @property
    def digests_dir(self) -> Path:
        return self.root / "digests"

    @property
    def fetch_log_dir(self) -> Path:
        return self.root / "logs" / "fetch"

    @property
    def llm_log_dir(self) -> Path:
        return self.root / "logs" / "llm"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "transcripts"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def cache_db(self) -> Path:
        return self.root / "grepify.db"

    @property
    def health_file(self) -> Path:
        return self.root / "health.json"

    def dated_file(self, base: Path, iso_ts: str) -> Path:
        """Return ``base/YYYY/MM/DD.jsonl`` for an ISO-8601 timestamp string."""
        year, month, day = date_parts(iso_ts)
        return base / year / month / f"{day}.jsonl"

    def run_manifest(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def digest_file(self, digest_id: str) -> Path:
        return self.digests_dir / f"{digest_id}.json"


def date_parts(iso_ts: str) -> tuple[str, str, str]:
    """Extract (YYYY, MM, DD) from an ISO-8601 timestamp string.

    Uses the leading date component directly so partitioning matches the stored
    text without timezone re-interpretation.
    """
    date_part = iso_ts[:10]
    pieces = date_part.split("-")
    if len(pieces) != 3:
        raise ValueError(f"not an ISO-8601 date: {iso_ts!r}")
    year, month, day = pieces
    if not (
        len(year) == 4
        and len(month) == 2
        and len(day) == 2
        and year.isdigit()
        and month.isdigit()
        and day.isdigit()
    ):
        raise ValueError(f"not an ISO-8601 date: {iso_ts!r}")
    return year, month, day
