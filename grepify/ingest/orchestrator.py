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
with the next source. One dead feed never fails the run. Config/repository
construction failures (bad YAML, unreadable truth) are *not* caught here -
those are systemic and are expected to fail the whole run (PRD §5).

Caps (F-ING-06)
---------------
Every source's raw items are truncated to ``item_cap`` (default 50) right
after fetch, regardless of whether the fetcher already applies its own
client-side cap (Reddit requests ``limit=50`` and slices again). Slicing an
already-capped list to the same bound is a no-op, so this never
double-truncates incorrectly - the cap is a property of every source kind,
not something each fetcher has to get right alone.

Enabled sources
----------------
A source is ingested only if both its own ``enabled`` flag and its parent
group's ``enabled`` flag are true (PRD §7 group semantics: disabling a group
disables everything in it, not just its own flag).

Failure modes
-------------
- A single source's :class:`~grepify.errors.FetchError` or any other
  exception -> logged ``error``, run continues (see above).
- An empty (post-cap) fetch result -> logged ``empty``, not an error.
- A successful fetch (even with zero *new* items on a re-run) -> logged
  ``ok``; ``items_new`` is whatever :meth:`Repository.add_items
  <grepify.repository.base.Repository.add_items>` actually wrote (F-ING-07:
  reruns yield zero new rows, not an error).
- Loading config (:class:`~grepify.errors.ConfigError`) or dispatching an
  unregistered ``source.kind`` (``KeyError``) are systemic and propagate,
  failing the whole run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from grepify.clock import Clock, to_iso
from grepify.config.provider import ConfigProvider
from grepify.errors import FetchError
from grepify.ingest.normalize import dedup_within_batch, normalize_batch
from grepify.ingest.reddit import RedditFetcher
from grepify.ingest.registry import FetcherRegistry
from grepify.ingest.rss import RssFetcher
from grepify.ingest.youtube import YouTubeFetcher
from grepify.models import FetchLogEntry, FetchStatus, Source
from grepify.repository.base import Repository

ITEM_CAP_DEFAULT = 50  # F-ING-06


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


@dataclass(frozen=True)
class IngestSummary:
    """Run-level rollup of every :class:`SourceResult` (feeds the run manifest)."""

    results: list[SourceResult] = field(default_factory=list)

    @property
    def sources_attempted(self) -> int:
        return len(self.results)

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
    def items_new(self) -> int:
        return sum(r.items_new for r in self.results)

    @property
    def duration_ms(self) -> int:
        return sum(r.duration_ms for r in self.results)


def build_registry() -> FetcherRegistry:
    """The production registry: one real fetcher per source kind (E1 scope)."""
    registry = FetcherRegistry()
    registry.register(RssFetcher())
    registry.register(YouTubeFetcher())
    registry.register(RedditFetcher())
    return registry


def run_ingest(
    services: IngestServices, *, run_id: str, item_cap: int = ITEM_CAP_DEFAULT
) -> IngestSummary:
    """Fetch every enabled source, normalize+dedup, and append to the repository.

    Returns the run's :class:`IngestSummary`; see the module docstring for the
    per-source isolation and cap rules.
    """
    results = [
        _run_source(source, services, run_id=run_id, item_cap=item_cap)
        for source in _enabled_sources(services.config)
    ]
    return IngestSummary(results=results)


def _enabled_sources(config: ConfigProvider) -> list[Source]:
    enabled_groups = {g.group_id for g in config.groups() if g.enabled}
    return [s for s in config.sources() if s.enabled and s.group_id in enabled_groups]


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
        raw_items = services.registry.fetch(source)[:item_cap]
        if not raw_items:
            return _finish(attempt, FetchStatus.EMPTY)
        fetched_at = to_iso(services.clock.now())
        items = dedup_within_batch(normalize_batch(raw_items, source, fetched_at=fetched_at))
        items_new = services.repository.add_items(items)
        return _finish(attempt, FetchStatus.OK, items_new=items_new)
    except FetchError as exc:
        return _finish(attempt, FetchStatus.ERROR, error=str(exc))
    except KeyError:
        # An unregistered source.kind is a systemic config/wiring bug (see
        # FetcherRegistry.fetch), not a per-source hiccup - it must propagate
        # and fail the run, so it is deliberately not isolated like the
        # broader `except Exception` below.
        raise
    except Exception as exc:
        return _finish(attempt, FetchStatus.ERROR, error=f"{type(exc).__name__}: {exc}")


def _finish(
    attempt: _Attempt, status: FetchStatus, *, items_new: int = 0, error: str | None = None
) -> SourceResult:
    duration_ms = int((time.monotonic() - attempt.t0) * 1000)
    attempt.repository.log_fetch(
        FetchLogEntry(
            source_id=attempt.source.source_id,
            run_id=attempt.run_id,
            started_at=attempt.started_at,
            status=status,
            items_new=items_new,
            error=error,
            duration_ms=duration_ms,
        )
    )
    return SourceResult(
        source_id=attempt.source.source_id,
        status=status,
        items_new=items_new,
        duration_ms=duration_ms,
        error=error,
    )
