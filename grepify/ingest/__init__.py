"""Ingestion core (E1): the fetcher contract (GRP-10), concrete fetchers
(GRP-11/12/13), and normalizer/dedup (GRP-14).

Public surface every E1/E2/E5 issue builds on:

- Fetch contract: :class:`RawItem`, :class:`Fetcher`, :class:`FetcherRegistry`,
  and the shipped :class:`FakeFetcher` test double.
- Concrete fetchers: :class:`RssFetcher`, :class:`YouTubeFetcher`,
  :class:`RedditFetcher` — each independently unit-testable against fixtures
  via the injectable :class:`~grepify.ingest.http.Transport` protocol, no
  network required. Registering them into a :class:`FetcherRegistry` for a
  live run is the ingest orchestrator's job (GRP-15, not yet built).
- Normalize + identity: :func:`normalize`, :func:`normalize_batch`,
  :func:`compute_item_id`, :func:`canonicalize_url`, :func:`dedup_within_batch`.
- Near-dup layer: :func:`compute_content_hash`, :func:`hamming_distance`,
  :func:`group_near_duplicates`.
- Orchestration (GRP-15): :func:`run_ingest`, :class:`IngestServices`,
  :class:`IngestSummary`, :class:`SourceResult`, :func:`build_registry` — the
  per-source-isolated run loop the ``ingest`` CLI command drives.

The X fetcher (E5) plugs into the same :class:`Fetcher` contract without
changing it.

Failure modes
-------------
None of its own — this is a pure re-export aggregator. See each submodule's
docstring for its failure modes (``base``/``registry``/``fake`` for the fetch
contract, ``rss``/``youtube``/``reddit``/``http``/``feedutil`` for the concrete
fetchers, ``normalize``/``dedup`` for identity + near-dup, ``orchestrator`` for
per-source isolation).
"""

from __future__ import annotations

from grepify.ingest.base import Fetcher, RawItem
from grepify.ingest.dedup import compute_content_hash, group_near_duplicates, hamming_distance
from grepify.ingest.fake import FakeFetcher
from grepify.ingest.normalize import (
    canonicalize_url,
    compute_item_id,
    dedup_within_batch,
    normalize,
    normalize_batch,
)
from grepify.ingest.orchestrator import (
    IngestServices,
    IngestSummary,
    SourceResult,
    build_registry,
    run_ingest,
)
from grepify.ingest.reddit import RedditFetcher
from grepify.ingest.registry import FetcherRegistry
from grepify.ingest.rss import RssFetcher
from grepify.ingest.youtube import YouTubeFetcher

__all__ = [
    "FakeFetcher",
    "Fetcher",
    "FetcherRegistry",
    "IngestServices",
    "IngestSummary",
    "RawItem",
    "RedditFetcher",
    "RssFetcher",
    "SourceResult",
    "YouTubeFetcher",
    "build_registry",
    "canonicalize_url",
    "compute_content_hash",
    "compute_item_id",
    "dedup_within_batch",
    "group_near_duplicates",
    "hamming_distance",
    "normalize",
    "normalize_batch",
    "run_ingest",
]
