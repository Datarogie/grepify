"""``FakeFetcher`` - the shipped, fixture-based fetcher double (GRP-10).

Shipped in the package (not ``tests/``) because §9 makes fixture-backed fakes
the way *every* E1/E2/E5 test drives ingestion without network. A ``FakeFetcher``
returns canned :class:`~grepify.ingest.base.RawItem`s per source, or raises a
canned :class:`~grepify.errors.FetchError` to exercise the failure-isolation path
(GRP-15) - the same contract a real fetcher obeys.

Failure modes
-------------
None of its own beyond the contract it emulates: it raises the
:class:`~grepify.errors.FetchError` it was configured with for a given source,
and returns ``[]`` for a source with no canned result (an empty feed is normal,
not an error).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from grepify.errors import FetchError
from grepify.ingest.base import Fetcher, RawItem
from grepify.models import Source, SourceKind


class FakeFetcher(Fetcher):
    """A :class:`~grepify.ingest.base.Fetcher` driven by canned results.

    :param kind: the source kind this fake claims (its registry key).
    :param results: maps ``source_id`` to either the ``RawItem``s to return or a
        :class:`~grepify.errors.FetchError` to raise for that source.
    :param default: result for a ``source_id`` absent from ``results``
        (default: an empty feed).
    """

    def __init__(
        self,
        kind: SourceKind = SourceKind.RSS,
        *,
        results: Mapping[str, Sequence[RawItem] | FetchError] | None = None,
        default: Sequence[RawItem] | None = None,
    ) -> None:
        self._kind = kind
        self._results: dict[str, Sequence[RawItem] | FetchError] = dict(results or {})
        self._default: Sequence[RawItem] = tuple(default or ())
        self.calls: list[str] = []  # source_ids fetched, in call order (for assertions)

    @property
    def kind(self) -> SourceKind:
        return self._kind

    def fetch(self, source: Source) -> list[RawItem]:
        self.calls.append(source.source_id)
        outcome = self._results.get(source.source_id, self._default)
        if isinstance(outcome, FetchError):
            raise outcome
        return list(outcome)
