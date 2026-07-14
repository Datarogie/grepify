"""Fetcher registry: one :class:`~grepify.ingest.base.Fetcher` per source kind.

The registry is the single dispatch point the ingest orchestrator (GRP-15) uses:
it registers one fetcher per :class:`~grepify.models.SourceKind` and routes a
:class:`~grepify.models.Source` to the fetcher matching ``source.kind``. Keeping
dispatch here (not a ``match`` in the orchestrator) means adding a new kind's
fetcher is one ``register`` call, no orchestrator change.

Failure modes
-------------
Registration is a **systemic programming error** when it collides, so it
raises loudly rather than being swallowed like
:class:`~grepify.errors.FetchError`:

- Registering two fetchers for the same kind → :class:`ValueError`.

Fetching / getting a kind with no registered fetcher also raises
:class:`KeyError` here, but it is no longer treated as systemic by every
caller (GRP-56): ``grepify validate`` uses :meth:`registered_kinds` to reject
an enabled source with an uncovered kind before ingest ever runs, and the
orchestrator additionally isolates a :class:`KeyError` that reaches
:meth:`fetch` per-source (defense in depth), the same as a
:class:`~grepify.errors.FetchError` from inside a fetcher's ``fetch``, which
propagates unchanged for the orchestrator to isolate.
"""

from __future__ import annotations

from grepify.ingest.base import Fetcher, FetchOutcome, RawItem
from grepify.models import Source, SourceKind


class FetcherRegistry:
    """Maps each :class:`~grepify.models.SourceKind` to its fetcher."""

    def __init__(self) -> None:
        self._by_kind: dict[SourceKind, Fetcher] = {}

    def register(self, fetcher: Fetcher) -> None:
        """Register ``fetcher`` under its own ``kind``. Raises :class:`ValueError`
        if that kind is already registered (double-registration is a bug)."""
        kind = fetcher.kind
        if kind in self._by_kind:
            raise ValueError(f"a fetcher is already registered for kind {kind!r}")
        self._by_kind[kind] = fetcher

    def get(self, kind: SourceKind) -> Fetcher:
        """Return the fetcher for ``kind``. Raises :class:`KeyError` if none."""
        try:
            return self._by_kind[kind]
        except KeyError:
            raise KeyError(f"no fetcher registered for kind {kind!r}") from None

    def fetch(self, source: Source) -> list[RawItem]:
        """Dispatch ``source`` to the fetcher for ``source.kind`` and fetch.

        A :class:`~grepify.errors.FetchError` from the fetcher propagates
        unchanged (the orchestrator isolates it); a missing fetcher raises
        :class:`KeyError`.
        """
        return self.get(source.kind).fetch(source)

    def acquire(self, source: Source) -> FetchOutcome:
        """Dispatch ``source`` and return its :class:`~grepify.ingest.base.FetchOutcome`.

        Same dispatch/isolation contract as :meth:`fetch`, but the fetcher walks
        its acquisition ladder and reports which rung served (ADR 0002 §1).
        """
        return self.get(source.kind).acquire(source)

    def registered_kinds(self) -> frozenset[SourceKind]:
        """The kinds with a registered fetcher (for coverage checks / diagnostics)."""
        return frozenset(self._by_kind)
