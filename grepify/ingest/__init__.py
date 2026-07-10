"""Ingestion core (E1): the fetcher contract (GRP-10), concrete fetchers
(GRP-11/12/13), and normalizer/dedup (GRP-14).

Public surface every E1/E2/E5 issue builds on:

- Fetch contract: :class:`RawItem`, :class:`Fetcher`, :class:`FetcherRegistry`,
  and the shipped :class:`FakeFetcher` test double.
- Concrete fetchers: :class:`RssFetcher`, :class:`YouTubeFetcher`,
  :class:`RedditFetcher` - each independently unit-testable against fixtures
  via the injectable :class:`~grepify.ingest.http.Transport` protocol, no
  network required. Registering them into a :class:`FetcherRegistry` for a
  live run is the ingest orchestrator's job (GRP-15, not yet built).
- Normalize + identity: :func:`normalize`, :func:`normalize_batch`,
  :func:`compute_item_id`, :func:`canonicalize_url`, :func:`dedup_within_batch`,
  :func:`clean_summary` (the shared summary cleaner, re-used by GRP-60 remediation).
- Near-dup layer: :func:`compute_content_hash`, :func:`hamming_distance`,
  :func:`group_near_duplicates`.
- Orchestration (GRP-15): :func:`run_ingest`, :class:`IngestServices`,
  :class:`IngestSummary`, :class:`SourceResult`, :func:`build_registry` - the
  per-source-isolated run loop the ``ingest`` CLI command drives.
- Transcripts (E5, GRP-52/53): :class:`TranscriptStore` + :class:`TranscriptClient`
  fetch/cache a YouTube transcript per video (absence-tolerant), and
  :func:`excerpt_transcript` cuts the <=1500-char excerpt the extract prompt
  uses.

Failure modes
-------------
None of its own - this is a pure re-export aggregator. See each submodule's
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
    clean_summary,
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
from grepify.ingest.transcript import (
    TranscriptClient,
    TranscriptStore,
    YouTubeTranscriptApiClient,
    excerpt_transcript,
)
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
    "TranscriptClient",
    "TranscriptStore",
    "YouTubeFetcher",
    "YouTubeTranscriptApiClient",
    "build_registry",
    "canonicalize_url",
    "clean_summary",
    "compute_content_hash",
    "compute_item_id",
    "dedup_within_batch",
    "excerpt_transcript",
    "group_near_duplicates",
    "hamming_distance",
    "normalize",
    "normalize_batch",
    "run_ingest",
]
