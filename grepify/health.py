"""Health snapshot: per-source consecutive-failure computation (GRP-16, PRD §8 F-ING-08).

Reads ``fetch_log`` history (via :meth:`Repository.iter_fetch_log
<grepify.repository.base.Repository.iter_fetch_log>`) and computes, per
source, its most recent status and how many attempts in a row have ended in
``error``. Five or more consecutive errors flags the source — v1 does **not**
auto-disable it (PRD §2 Non-Goals): a flagged source is still retried every
run, the flag is purely informational for the health page / phone debugging
(PRD §4 flow 4).

This is a pure computation over whatever fetch-log history it is given, so it
is entirely fixture-driven in tests — no repository or filesystem needed to
exercise :func:`compute_health`. :func:`write_health_snapshot` is the thin I/O
wrapper the ``ingest`` CLI command calls, per the PRD §5 architecture diagram
(health snapshot branches directly off ingest, not a separate pipeline stage).

Failure modes
-------------
:func:`compute_health` is pure and never raises (an empty history yields an
empty snapshot). :func:`write_health_snapshot` does file I/O — a filesystem
error (e.g. a read-only data root) propagates uncaught, same as every other
data-root write in this package (:mod:`grepify.run`).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from grepify.models import FetchLogEntry, FetchStatus
from grepify.paths import DataLayout

CONSECUTIVE_FAILURE_THRESHOLD = 5  # F-ING-08


class SourceHealth(BaseModel):
    """One source's rollup from fetch_log history (PRD §8 F-ING-08)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    attempts: int
    last_status: FetchStatus
    last_started_at: str
    last_error: str | None = None
    consecutive_failures: int
    flagged: bool


class HealthSnapshot(BaseModel):
    """``data/health.json`` contents — every source seen in fetch_log history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    generated_at: str
    sources: list[SourceHealth] = Field(default_factory=list)


def compute_health(
    entries: Iterable[FetchLogEntry], *, run_id: str, generated_at: str
) -> HealthSnapshot:
    """Compute per-source health from fetch_log ``entries``.

    ``entries`` should already be in chronological order per source (the
    order :meth:`Repository.iter_fetch_log
    <grepify.repository.base.Repository.iter_fetch_log>` guarantees) — this
    groups by ``source_id`` while preserving that order, so the last entry
    seen for a source is its most recent attempt and consecutive failures are
    a trailing count of ``error`` statuses back from there. Any non-error
    status (``ok``, ``empty``, ``skipped``) resets the count.
    """
    by_source: dict[str, list[FetchLogEntry]] = {}
    for entry in entries:
        by_source.setdefault(entry.source_id, []).append(entry)

    sources = [
        _source_health(source_id, history) for source_id, history in sorted(by_source.items())
    ]
    return HealthSnapshot(run_id=run_id, generated_at=generated_at, sources=sources)


def _source_health(source_id: str, history: list[FetchLogEntry]) -> SourceHealth:
    last = history[-1]
    consecutive = 0
    for entry in reversed(history):
        if entry.status is not FetchStatus.ERROR:
            break
        consecutive += 1
    return SourceHealth(
        source_id=source_id,
        attempts=len(history),
        last_status=last.status,
        last_started_at=last.started_at,
        last_error=last.error,
        consecutive_failures=consecutive,
        flagged=consecutive >= CONSECUTIVE_FAILURE_THRESHOLD,
    )


def write_health_snapshot(
    entries: Iterable[FetchLogEntry], layout: DataLayout, *, run_id: str, generated_at: str
) -> HealthSnapshot:
    """Compute and persist ``data/health.json`` (PRD §5 diagram: ingest -> health snapshot)."""
    snapshot = compute_health(entries, run_id=run_id, generated_at=generated_at)
    layout.health_file.parent.mkdir(parents=True, exist_ok=True)
    layout.health_file.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return snapshot
