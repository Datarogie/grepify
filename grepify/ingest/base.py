"""Fetcher contract: ``RawItem`` + the ``Fetcher`` interface (GRP-10).

This is the boundary between *fetching* (kind-specific parsing of feed XML,
channel RSS, reddit JSON, tweet objects - GRP-11/12/13, GRP-50) and
*normalizing* (identity + hashing - :mod:`grepify.ingest.normalize`, GRP-14).

A :class:`RawItem` is what a fetcher emits for one feed entry, *before* identity
is computed. Fetchers do the messy per-kind parsing and hand back plain records;
they never compute ``item_id`` / ``content_hash`` / ``canonical_url``, so those
identity rules live in exactly one place (the normalizer) regardless of source
kind. This keeps a new source kind cheap: parse into ``RawItem`` and stop.

Failure modes
-------------
- ``Fetcher.fetch`` MUST raise :class:`~grepify.errors.FetchError` on any
  per-source failure (timeout, HTTP error, malformed feed, auth challenge, rate
  limit). It is non-fatal by contract: the orchestrator (GRP-15) catches it,
  logs an ``error`` ``fetch_log`` row, and continues (PRD §9). An **empty** feed
  is a normal ``return []`` - not an error.
- Constructing a ``RawItem`` with a wrong-typed field raises
  ``pydantic.ValidationError`` at the boundary rather than propagating junk into
  normalization.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from grepify.errors import FetchError
from grepify.models import Rung, Source, SourceKind


class AcquisitionError(FetchError):
    """Fetch failure that carries a sanitized structured acquisition trace."""

    def __init__(self, message: str, *, acquisition_trace: str | None) -> None:
        self.acquisition_trace = acquisition_trace
        super().__init__(message)


class RawItem(BaseModel):
    """One fetched feed entry, pre-normalization (see module docstring).

    ``extra="forbid"`` catches a fetcher emitting an unexpected field (typo /
    format drift) instead of silently dropping it; ``frozen`` keeps a fetched
    record immutable through normalization.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    title: str
    external_id: str | None = None  # guid / video_id / reddit id / tweet id
    summary: str | None = None  # raw description/selftext; normalizer strips html + truncates to 2k
    author: str | None = None
    published_at: str | None = None  # ISO-8601 if provided; None -> normalizer uses fetched_at
    lang: str | None = None
    transcript_ref: str | None = None  # youtube only; typically None until E5


@dataclass(frozen=True)
class FetchOutcome:
    """A fetch result plus which acquisition rung served it (ADR 0002 §1).

    ``rung`` is :attr:`~grepify.models.Rung.DIRECT` for the primary feed and a
    fallback rung when the ladder recovered a source from an alternate path;
    ``resolved_url`` records the URL a fallback rung served from (``None`` for
    ``DIRECT``, which is just ``source.url``) as degraded evidence.
    """

    items: list[RawItem]
    rung: Rung = Rung.DIRECT
    resolved_url: str | None = None
    acquisition_trace: str | None = None


class Fetcher(ABC):
    """One source kind's fetcher. All kinds implement this identical contract."""

    @property
    @abstractmethod
    def kind(self) -> SourceKind:
        """The single source kind this fetcher handles (its registry key)."""

    @abstractmethod
    def fetch(self, source: Source) -> list[RawItem]:
        """Return ``source``'s current entries as :class:`RawItem`s.

        Contract (see module docstring): an empty feed returns ``[]``; any
        per-source failure raises :class:`~grepify.errors.FetchError` so the run
        can isolate and continue. Implementations must not compute identity or
        hashes - that is :mod:`grepify.ingest.normalize`'s job.
        """

    def acquire(self, source: Source) -> FetchOutcome:
        """Fetch ``source`` and report which acquisition rung served it.

        The default is a single direct fetch (rung 0), so a fetcher with no
        fallback ladder inherits it unchanged. Ladder-aware fetchers
        (:mod:`grepify.ingest.rss`, :mod:`grepify.ingest.reddit`) override this
        to walk their ordered rungs; the orchestrator records the rung on the
        ``fetch_log`` row.
        """
        return FetchOutcome(items=self.fetch(source))
