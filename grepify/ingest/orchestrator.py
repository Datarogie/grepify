"""Ingest orchestrator: per-source isolation, caps, fetch_log, run summary (GRP-15).

Wires the pieces built earlier in this epic (GRP-10..14) into one entrypoint
the CLI's ``ingest`` command drives: load enabled sources from a
:class:`~grepify.config.provider.ConfigProvider`, dispatch each through a
:class:`~grepify.ingest.registry.FetcherRegistry`, normalize+dedup its raw
items, and append them via the :class:`~grepify.repository.base.Repository`.

Per-source isolation (PRD §9)
------------------------------
A source's whole attempt (fetch, normalize, dedup, write) is wrapped so that a
:class:`~grepify.errors.FetchError` *or any other exception* is caught, logged
as an ``error`` :class:`~grepify.models.FetchLogEntry`, and the run continues
with the next source. One dead feed never fails the run. This includes a
source whose ``kind`` has no fetcher registered (:class:`KeyError` from
:meth:`~grepify.ingest.registry.FetcherRegistry.get`, GRP-56): ``grepify
validate`` is the primary defense against that ever reaching ingest, but a
config edited after the last validate run (or run out-of-band of CI) can
still slip one through, so the orchestrator isolates it exactly like any
other per-source failure rather than letting it take down every other source.
Config/repository construction failures (bad YAML, unreadable truth) are *not*
caught here - those are systemic and are expected to fail the whole run
(PRD §5).

Caps (F-ING-06)
---------------
Every source's raw items are truncated to ``item_cap`` (default 50) right
after fetch, regardless of whether the fetcher already applies its own
client-side cap (Reddit requests ``limit=50`` and slices again). Slicing an
already-capped list to the same bound is a no-op, so this never
double-truncates incorrectly - the cap is a property of every source kind,
not something each fetcher has to get right alone.

Fetchable sources (ADR 0002 §2)
--------------------------------
A source is ingested only if its parent group is enabled (PRD §7 group
semantics) and it is itself either enabled (``active``/``degraded``) or ``dead``.
``dead`` sources are dispatched for the slow re-check (a long per-source cadence
interval, :data:`DEAD_RECHECK_HOURS`) so a server that recovers is noticed
without a human re-investigation; a ``paywalled`` source is terminal and never
dispatched (no ladder walk, ToS-respecting), and a ``gone`` source no longer
exists in config at all.

Acquisition ladder (ADR 0002 §1)
---------------------------------
Each source is dispatched through :meth:`FetcherRegistry.acquire
<grepify.ingest.registry.FetcherRegistry.acquire>`, which walks the fetcher's
ordered rungs and reports which one served. That rung is recorded on the
``fetch_log`` row, so a fallback-served (``degraded``) source is visibly
degraded rather than silently fine.

Cadence (T6, GRP-31)
--------------------
Enabled sources are further split into "due" and "cadence-skipped" by
:mod:`grepify.ingest.cadence`, using ``settings.ingest.min_interval_hours``
per :class:`~grepify.models.SourceKind` (Reddit's default reduces it to
roughly once a day; every other kind is unaffected and stays due every run).
A cadence-skipped source is never dispatched to its fetcher - it gets a
``skipped`` :class:`~grepify.models.FetchLogEntry` directly, so the health
page shows it was deliberately not attempted rather than looking stale, and
:attr:`IngestSummary.sources_attempted` excludes it (it was never actually
attempted this run - see :attr:`IngestSummary.sources_skipped`).

Failure modes
-------------
- A single source's :class:`~grepify.errors.FetchError`, an unregistered
  ``source.kind`` (``KeyError``, GRP-56), or any other exception -> logged
  ``error``, run continues (see above). Cadence never changes this: a
  cadence-skipped source is not dispatched at all, so it cannot fail the run
  either.
- An empty (post-cap) fetch result -> logged ``empty``, not an error.
- A successful fetch (even with zero *new* items on a re-run) -> logged
  ``ok``; ``items_new`` is whatever :meth:`Repository.add_items
  <grepify.repository.base.Repository.add_items>` actually wrote (F-ING-07:
  reruns yield zero new rows, not an error).
- Loading config (:class:`~grepify.errors.ConfigError`) is systemic and
  propagates, failing the whole run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from grepify.clock import Clock, to_iso
from grepify.config.provider import ConfigProvider
from grepify.errors import FetchError
from grepify.ingest.cadence import last_real_attempt_at, split_by_cadence
from grepify.ingest.normalize import dedup_within_batch, normalize_batch
from grepify.ingest.reddit import RedditFetcher
from grepify.ingest.registry import FetcherRegistry
from grepify.ingest.rss import RssFetcher
from grepify.ingest.transcript import TranscriptStore
from grepify.ingest.youtube import YouTubeFetcher
from grepify.models import FetchLogEntry, FetchStatus, Rung, Source, SourceStatus
from grepify.repository.base import Repository

ITEM_CAP_DEFAULT = 50  # F-ING-06
DEAD_RECHECK_HOURS = 30 * 24  # ADR 0002 §2: slow re-check cadence for `dead` sources


@dataclass(frozen=True)
class IngestServices:
    """The collaborators one ingest run needs, bundled so call sites (and the
    per-source helpers below) don't thread four separate parameters."""

    config: ConfigProvider
    repository: Repository
    registry: FetcherRegistry
    clock: Clock


@dataclass(frozen=True)
class SourceResult:
    """One source's outcome for a run (mirrors the ``fetch_log`` row written for it)."""

    source_id: str
    status: FetchStatus
    items_new: int
    duration_ms: int
    error: str | None = None
    rung: Rung | None = None


@dataclass(frozen=True)
class IngestSummary:
    """Run-level rollup of every :class:`SourceResult` (feeds the run manifest)."""

    results: list[SourceResult] = field(default_factory=list)

    @property
    def sources_ok(self) -> int:
        return sum(1 for r in self.results if r.status is FetchStatus.OK)

    @property
    def sources_empty(self) -> int:
        return sum(1 for r in self.results if r.status is FetchStatus.EMPTY)

    @property
    def sources_error(self) -> int:
        return sum(1 for r in self.results if r.status is FetchStatus.ERROR)

    @property
    def sources_skipped(self) -> int:
        """Sources not dispatched this run for cadence (T6) - see the module
        docstring. Excluded from :attr:`sources_attempted`."""
        return sum(1 for r in self.results if r.status is FetchStatus.SKIPPED)

    @property
    def sources_attempted(self) -> int:
        return sum(1 for r in self.results if r.status is not FetchStatus.SKIPPED)

    @property
    def items_new(self) -> int:
        return sum(r.items_new for r in self.results)

    @property
    def duration_ms(self) -> int:
        return sum(r.duration_ms for r in self.results)


def build_registry(*, transcript_store: TranscriptStore | None = None) -> FetcherRegistry:
    """The production registry: one real fetcher per source kind (rss/youtube/
    reddit).

    ``transcript_store``, when given, lets the YouTube fetcher attach a
    transcript to each video (GRP-52); absent, YouTube behavior is unchanged.
    """
    registry = FetcherRegistry()
    registry.register(RssFetcher())
    registry.register(YouTubeFetcher(transcript_store=transcript_store))
    registry.register(RedditFetcher())
    return registry


def run_ingest(
    services: IngestServices, *, run_id: str, item_cap: int = ITEM_CAP_DEFAULT
) -> IngestSummary:
    """Fetch every enabled, cadence-due source, normalize+dedup, and append to
    the repository.

    Returns the run's :class:`IngestSummary`; see the module docstring for the
    per-source isolation, cap, and cadence rules.
    """
    now = services.clock.now()
    started_at = to_iso(now)
    last_real_attempt = last_real_attempt_at(services.repository.iter_fetch_log())
    fetchable = _fetchable_sources(services.config)
    decision = split_by_cadence(
        fetchable,
        now=now,
        last_real_attempt=last_real_attempt,
        min_interval_hours=services.config.settings().ingest.min_interval_hours,
        per_source_min_interval_hours={
            s.source_id: DEAD_RECHECK_HOURS for s in fetchable if _is_dead_recheck(s)
        },
    )
    results = [
        _skip_for_cadence(source, services, run_id=run_id, started_at=started_at)
        for source in decision.skipped
    ] + [_run_source(source, services, run_id=run_id, item_cap=item_cap) for source in decision.due]
    return IngestSummary(results=results)


def _is_dead_recheck(source: Source) -> bool:
    """Whether a ``dead`` source is re-probed on the slow cadence (ADR 0002 §2).

    Only an explicitly-classified ``dead`` source is: validate requires such a
    source to carry ``evidence``, so its presence distinguishes a triaged
    ``status: dead`` from a legacy bare ``enabled: false`` (which maps to
    ``dead`` for display but is a plain off switch, never re-probed)."""
    return source.status is SourceStatus.DEAD and source.evidence is not None


def _fetchable_sources(config: ConfigProvider) -> list[Source]:
    """Sources the run may dispatch: every enabled (``active``/``degraded``)
    source, plus explicitly-classified ``dead`` sources (for the slow re-check,
    ADR 0002 §2, gated to the 30-day interval by cadence). ``paywalled`` is
    terminal and never dispatched (no ladder walk, ToS-respecting); a legacy
    bare ``enabled: false`` source and any source in a disabled group are
    excluded entirely."""
    enabled_groups = {g.group_id for g in config.groups() if g.enabled}
    return [
        s
        for s in config.sources()
        if s.group_id in enabled_groups and (s.enabled or _is_dead_recheck(s))
    ]


def _record(repository: Repository, entry: FetchLogEntry) -> SourceResult:
    """Persist one ``fetch_log`` row and return the :class:`SourceResult` that
    mirrors it.

    The single place a fetch_log row and its run-summary mirror are paired
    (a real attempt via :func:`_finish`, a cadence skip via
    :func:`_skip_for_cadence`). The result is *derived from the entry that was
    written*, so the two can never disagree. ``entry.duration_ms`` is always set
    by both callers (``0`` for a skip), so the ``or 0`` only guards the
    optional-typed column.
    """
    repository.log_fetch(entry)
    return SourceResult(
        source_id=entry.source_id,
        status=entry.status,
        items_new=entry.items_new,
        duration_ms=entry.duration_ms or 0,
        error=entry.error,
        rung=entry.rung,
    )


def _skip_for_cadence(
    source: Source, services: IngestServices, *, run_id: str, started_at: str
) -> SourceResult:
    """Record a cadence skip (T6): logged as ``skipped``, never dispatched to
    the fetcher, so it cannot affect the per-source isolation guarantee. A skip
    has no attempt to time, so its ``duration_ms`` is zero and it carries no
    error - built and recorded through :func:`_record` like every other
    outcome."""
    return _record(
        services.repository,
        FetchLogEntry(
            source_id=source.source_id,
            run_id=run_id,
            started_at=started_at,
            status=FetchStatus.SKIPPED,
            items_new=0,
            duration_ms=0,
        ),
    )


@dataclass(frozen=True)
class _Attempt:
    """Per-source timing/identity context threaded through the try/except below."""

    repository: Repository
    source: Source
    run_id: str
    started_at: str
    t0: float


def _run_source(
    source: Source, services: IngestServices, *, run_id: str, item_cap: int
) -> SourceResult:
    attempt = _Attempt(
        repository=services.repository,
        source=source,
        run_id=run_id,
        started_at=to_iso(services.clock.now()),
        t0=time.monotonic(),
    )
    try:
        outcome = services.registry.acquire(source)
        raw_items = outcome.items[:item_cap]
        if not raw_items:
            return _finish(
                attempt,
                FetchStatus.EMPTY,
                rung=outcome.rung,
                acquisition_trace=outcome.acquisition_trace,
            )
        fetched_at = to_iso(services.clock.now())
        items = dedup_within_batch(normalize_batch(raw_items, source, fetched_at=fetched_at))
        items_new = services.repository.add_items(items)
        return _finish(
            attempt,
            FetchStatus.OK,
            items_new=items_new,
            rung=outcome.rung,
            acquisition_trace=outcome.acquisition_trace,
        )
    except FetchError as exc:
        return _finish(attempt, FetchStatus.ERROR, error=str(exc))
    except KeyError as exc:
        # Unregistered source.kind (FetcherRegistry.get): isolated per-source like
        # any other failure, defense in depth behind `validate` (see docstring).
        message = str(exc.args[0]) if exc.args else str(exc)
        return _finish(attempt, FetchStatus.ERROR, error=message)
    except Exception as exc:
        return _finish(attempt, FetchStatus.ERROR, error=f"{type(exc).__name__}: {exc}")


def _finish(  # noqa: PLR0913
    attempt: _Attempt,
    status: FetchStatus,
    *,
    items_new: int = 0,
    error: str | None = None,
    rung: Rung | None = None,
    acquisition_trace: str | None = None,
) -> SourceResult:
    return _record(
        attempt.repository,
        FetchLogEntry(
            source_id=attempt.source.source_id,
            run_id=attempt.run_id,
            started_at=attempt.started_at,
            status=status,
            items_new=items_new,
            error=error,
            duration_ms=int((time.monotonic() - attempt.t0) * 1000),
            rung=rung,
            acquisition_trace=acquisition_trace,
        ),
    )
