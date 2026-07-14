"""Health snapshot: per-source consecutive-failure computation (GRP-16, PRD §8 F-ING-08).

Reads ``fetch_log`` history (via :meth:`Repository.iter_fetch_log
<grepify.repository.base.Repository.iter_fetch_log>`) and computes, per
source, its most recent status and how many attempts in a row have ended in
``error``. Five or more consecutive errors flags the source - v1 does **not**
auto-disable it (PRD §2 Non-Goals): a flagged source is still retried every
run, the flag is purely informational for the health page / phone debugging
(PRD §4 flow 4).

This is a pure computation over whatever fetch-log history it is given, so it
is entirely fixture-driven in tests - no repository or filesystem needed to
exercise :func:`compute_health`. :func:`write_health_snapshot` is the thin I/O
wrapper the ``ingest`` CLI command calls, per the PRD §5 architecture diagram
(health snapshot branches directly off ingest, not a separate pipeline stage).

Error classes (T5 audit, GRP-30)
---------------------------------
:func:`classify_error` buckets the free-text ``fetch_log.error`` string into a
coarse :class:`ErrorClass` (``http_4xx`` / ``http_5xx`` / ``tls`` /
``connection`` / ``unparseable`` / ``other``), so a triage pass (the
``doctor`` CLI report, :mod:`grepify.doctor`) can group the roughly two dozen
recurring dead sources by *kind* of failure instead of re-reading every raw
message by hand. It is a best-effort text classifier over the fixed set of
messages the fetchers actually raise (see ``grep -rn 'raise FetchError' -
grepify/ingest``) - an HTTP status is matched first (most specific), then
known substrings; anything else falls back to ``other`` rather than raising or
guessing.

Best-effort / quiet sources (T6, GRP-31)
-----------------------------------------
``quiet_source_ids`` (``compute_health`` / ``write_health_snapshot``) is the
set of sources - Reddit's, by convention of the caller
(:mod:`grepify.cli`, driven by ``settings.ingest.quiet_kinds`) - whose
``flagged`` bit is always ``False``, regardless of ``consecutive_failures``.
This is scoping, not hiding: ``consecutive_failures`` (and ``attempts``,
``last_status``, ``last_error``, ``error_class``) are computed and shown
exactly as for any other source, so the behavior stays fully auditable; only
the boolean that turns a row red on the health page is suppressed for these
sources, per the decided best-effort/quiet Reddit strategy.

Cadence skips are transparent (T6, GRP-31)
-------------------------------------------
A reduced-cadence source (Reddit) is logged ``skipped`` on the runs it is not
due (:mod:`grepify.ingest.cadence`). Those ``skipped`` rows are dropped before
the per-source rollup, so a skip is a *non-event* here: it never resets the
consecutive-failure streak and never overwrites ``last_status`` / ``last_error``
/ ``error_class`` with blank values. Without this, a chronically-failing Reddit
source - skipped on most runs - would read as ``skipped`` / streak 0 on the
health page and drop out of the ``doctor`` error tally the moment a skip landed,
losing the failure history the ``skipped`` rows in ``fetch_log`` (and this
rollup) are meant to keep auditable.

Failure modes
-------------
:func:`compute_health` and :func:`classify_error` are pure and never raise (an
empty history yields an empty snapshot; an unrecognized error string yields
``ErrorClass.OTHER``, not an exception). :func:`write_health_snapshot` does
file I/O - a filesystem error (e.g. a read-only data root) propagates
uncaught, same as every other data-root write in this package (:mod:`grepify.run`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from grepify.models import FetchLogEntry, FetchStatus, Rung
from grepify.paths import DataLayout

CONSECUTIVE_FAILURE_THRESHOLD = 5  # F-ING-08

_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")
_UNPARSEABLE_MARKERS = ("unparseable feed", "malformed")
_TLS_MARKERS = ("ssl", "tls", "certificate")
_CONNECTION_MARKERS = (
    "connection refused",
    "connection reset",
    "network is unreachable",
    "timed out",
    "timeout",
    "name or service not known",
    "no route to host",
)


class ErrorClass(StrEnum):
    """Coarse bucket a ``fetch_log`` error message falls into (T5 audit)."""

    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    TLS = "tls"
    CONNECTION = "connection"
    UNPARSEABLE = "unparseable"
    OTHER = "other"


def classify_error(error: str | None) -> ErrorClass | None:
    """Classify a ``fetch_log.error`` string into an :class:`ErrorClass`.

    Returns ``None`` for ``None`` (a non-error status carries no error text).
    An HTTP status embedded anywhere in the message (``"HTTP 403"``,
    ``"...returned HTTP 429"``) is checked first since it is the most
    specific signal; substring markers for TLS/connection/unparseable
    failures come next; anything unrecognized is :attr:`ErrorClass.OTHER`.
    """
    if error is None:
        return None

    by_status = _classify_by_http_status(error)
    if by_status is not None:
        return by_status

    lowered = error.lower()
    if any(marker in lowered for marker in _UNPARSEABLE_MARKERS):
        return ErrorClass.UNPARSEABLE
    if any(marker in lowered for marker in _TLS_MARKERS):
        return ErrorClass.TLS
    if any(marker in lowered for marker in _CONNECTION_MARKERS):
        return ErrorClass.CONNECTION
    return ErrorClass.OTHER


def _classify_by_http_status(error: str) -> ErrorClass | None:
    """Return the class for an embedded ``HTTP <code>`` status, if present."""
    status_match = _HTTP_STATUS_RE.search(error)
    if status_match is None:
        return None
    code = int(status_match.group(1))
    if 400 <= code < 500:
        return ErrorClass.HTTP_4XX
    if 500 <= code < 600:
        return ErrorClass.HTTP_5XX
    return None


class SourceHealth(BaseModel):
    """One source's rollup from fetch_log history (PRD §8 F-ING-08)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    attempts: int
    last_status: FetchStatus
    last_started_at: str
    last_error: str | None = None
    error_class: ErrorClass | None = None
    consecutive_failures: int
    flagged: bool
    last_rung: Rung | None = None


class HealthSnapshot(BaseModel):
    """``data/health.json`` contents - every source seen in fetch_log history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    generated_at: str
    sources: list[SourceHealth] = Field(default_factory=list)


def compute_health(
    entries: Iterable[FetchLogEntry],
    *,
    run_id: str,
    generated_at: str,
    quiet_source_ids: Iterable[str] = (),
) -> HealthSnapshot:
    """Compute per-source health from fetch_log ``entries``.

    ``entries`` should already be in chronological order per source (the
    order :meth:`Repository.iter_fetch_log
    <grepify.repository.base.Repository.iter_fetch_log>` guarantees) - this
    groups by ``source_id`` while preserving that order. ``skipped`` entries
    (a cadence non-attempt, T6) are transparent to the rollup: they are
    dropped before the per-source computation, so the last entry that counts
    for a source is its most recent *real* attempt and consecutive failures
    are a trailing count of ``error`` statuses back from there. Any non-error
    real status (``ok``, ``empty``) resets the count.

    ``quiet_source_ids`` (T6) never get ``flagged=True`` no matter how many
    consecutive failures they accumulate - see the module docstring.
    """
    by_source: dict[str, list[FetchLogEntry]] = {}
    for entry in entries:
        if not entry.status.is_real_attempt:
            continue  # T6: a cadence non-attempt (SKIPPED), transparent to the health rollup
        by_source.setdefault(entry.source_id, []).append(entry)

    quiet = frozenset(quiet_source_ids)
    sources = [
        _source_health(source_id, history, quiet=source_id in quiet)
        for source_id, history in sorted(by_source.items())
    ]
    return HealthSnapshot(run_id=run_id, generated_at=generated_at, sources=sources)


def _source_health(source_id: str, history: list[FetchLogEntry], *, quiet: bool) -> SourceHealth:
    # ``history`` here is already ``skipped``-free (see compute_health), so
    # every entry is a real fetch attempt: ``last`` is the last real attempt,
    # ``attempts`` counts only real attempts, and the streak below ignores the
    # cadence skips entirely (T6 auditability - a chronic Reddit outage keeps
    # its error status/streak even on the runs it was skipped).
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
        error_class=classify_error(last.error),
        consecutive_failures=consecutive,
        flagged=consecutive >= CONSECUTIVE_FAILURE_THRESHOLD and not quiet,
        last_rung=last.rung,
    )


def write_health_snapshot(
    entries: Iterable[FetchLogEntry],
    layout: DataLayout,
    *,
    run_id: str,
    generated_at: str,
    quiet_source_ids: Iterable[str] = (),
) -> HealthSnapshot:
    """Compute and persist ``data/health.json`` (PRD §5 diagram: ingest -> health snapshot)."""
    snapshot = compute_health(
        entries, run_id=run_id, generated_at=generated_at, quiet_source_ids=quiet_source_ids
    )
    layout.health_file.parent.mkdir(parents=True, exist_ok=True)
    layout.health_file.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return snapshot
