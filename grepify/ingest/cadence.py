"""Per-source-kind fetch cadence (T6, GRP-31): Reddit best-effort scheduling.

Reddit is treated as a best-effort source class (the already-decided
strategy, option ii - see ``docs/prev1-hardening.md`` / ``HANDOFF.md``): every
Reddit source keeps being fetched, but on a reduced cadence rather than every
run - it is skipped on the runs it is not yet due, and each skip is still
logged - so it does not spend the pipeline's request budget on a source class
that is disproportionately likely to be blocked from CI IPs. This module is the
pure scheduling decision behind that; :mod:`grepify.ingest.orchestrator` is the
only caller.

Cadence is derived from history, not a persisted counter
----------------------------------------------------------
There is no new per-source "next due" state to persist. Instead, a source of
kind ``k`` is due once ``settings.ingest.min_interval_hours[k]`` hours have
elapsed since its last **real** fetch attempt (``ok``/``empty``/``error``) in
``fetch_log`` - a source with no such history yet is always due. A kind absent
from ``min_interval_hours`` (or mapped to ``<= 0``, the default for every kind
but Reddit) is due every run, exactly like pre-T6 behavior.

A cadence-skipped source gets a ``skipped`` :class:`~grepify.models.FetchLogEntry`
(the ``skipped`` status already existed in the PRD §6 schema for exactly this
kind of case) so the health/doctor view shows it was deliberately not
attempted this run rather than looking stale. Skip entries are deliberately
excluded from :func:`last_real_attempt_at` - if they were not, a skip logged
on one run would itself become the new "last attempt", pushing the reference
point forward by only one run's worth of cadence each time and the source
would never accumulate enough elapsed time to become due again.

Failure modes
-------------
Pure computation over its inputs; :func:`last_real_attempt_at` and
:func:`split_by_cadence` never raise. ``fetch_log.started_at`` is always
written via :func:`grepify.clock.to_iso`, so :func:`grepify.clock.from_iso`
always parses it; a hand-edited or corrupted truth file is out of scope, same
as every other reader of that column.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from grepify.clock import from_iso
from grepify.models import FetchLogEntry, Source, SourceKind


def last_real_attempt_at(entries: Iterable[FetchLogEntry]) -> dict[str, datetime]:
    """Most recent real-attempt (not ``skipped``) instant per ``source_id``.

    A "real attempt" is any status for which
    :attr:`~grepify.models.FetchStatus.is_real_attempt` is true - the same
    predicate the health rollup uses, so the two never disagree on what counts
    as an attempt. ``entries`` need only be chronological per source (the order
    :meth:`~grepify.repository.base.Repository.iter_fetch_log` guarantees) - a
    later entry for a source simply overwrites the running value, so the
    result holds each source's latest real attempt regardless of how sources
    interleave in the iterable.
    """
    last: dict[str, datetime] = {}
    for entry in entries:
        if entry.status.is_real_attempt:
            last[entry.source_id] = from_iso(entry.started_at)
    return last


@dataclass(frozen=True)
class CadenceDecision:
    """One run's due/skip split over a set of sources (T6)."""

    due: list[Source] = field(default_factory=list)
    skipped: list[Source] = field(default_factory=list)


def split_by_cadence(
    sources: Iterable[Source],
    *,
    now: datetime,
    last_real_attempt: Mapping[str, datetime],
    min_interval_hours: Mapping[SourceKind, int],
) -> CadenceDecision:
    """Split ``sources`` into those due for a fetch attempt this run and those
    to skip for cadence (T6).

    A source is due when its kind's configured interval is ``<= 0`` (or
    unconfigured), when it has no recorded real attempt yet, or when at least
    that many hours have elapsed since its last real attempt. Everything else
    is skipped this run.
    """
    due: list[Source] = []
    skipped: list[Source] = []
    for source in sources:
        interval = min_interval_hours.get(source.kind, 0)
        last = last_real_attempt.get(source.source_id)
        if interval <= 0 or last is None or now - last >= timedelta(hours=interval):
            due.append(source)
        else:
            skipped.append(source)
    return CadenceDecision(due=due, skipped=skipped)
